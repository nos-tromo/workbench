"""The MCP door's wire rules: the revisions, the header checks and the JSON-RPC shapes. Nothing here does I/O.

The door is stateless and answers JSON only (spec §3.3). ``2026-07-28`` is the modern revision: every request states
it in the ``MCP-Protocol-Version`` header and in ``params._meta``, and names its method in ``Mcp-Method`` (its tool in
``Mcp-Name`` on ``tools/call``). ``2025-11-25`` and ``2025-06-18`` are the legacy ones: ``initialize`` negotiates
one, and later requests send the version header alone. Any other revision is refused.

A refusal is a :class:`WireError`: its HTTP status, its JSON-RPC code, a short fixed text and, for an unserved
revision, the revision asked for. ``Caller``, ``ToolOutcome`` and ``ToolSpec`` are imported for type checking only.
"""

import base64
import binascii
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from fastapi.responses import JSONResponse

from workbench import __version__

if TYPE_CHECKING:
    from workbench.api.auth import Caller
    from workbench.api.registry import ToolOutcome, ToolSpec

#: The modern revision: every request carries its own version, no session.
MODERN: Final = "2026-07-28"
#: The legacy revisions, newest first: ``initialize`` negotiates one.
LEGACY: Final = ("2025-11-25", "2025-06-18")
#: Every revision the door serves.
SUPPORTED: Final = (MODERN, *LEGACY)
#: What an ``initialize`` that names neither legacy revision gets.
LEGACY_DEFAULT: Final = "2025-11-25"

#: The prefix of the reserved ``_meta`` keys.
META: Final = "io.modelcontextprotocol/"
PROTOCOL_VERSION: Final = META + "protocolVersion"
CLIENT_CAPABILITIES: Final = META + "clientCapabilities"
CLIENT_INFO: Final = META + "clientInfo"
SERVER_INFO: Final = META + "serverInfo"

PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603
HEADER_MISMATCH: Final = -32020
UNSUPPORTED_VERSION: Final = -32022

#: What ``server/discover`` tells the model about the server, in two sentences.
INSTRUCTIONS: Final = (
    "workbench runs Python on the data each call carries, in a sandbox with no network, and deletes that data with "
    "the job. Call run_python with the code and its data files; tables come back as CSV and charts as PNG."
)

#: How long a client may keep ``server/discover``'s and ``tools/list``'s answers, in milliseconds.
DISCOVER_TTL_MS: Final = 3_600_000
TOOLS_TTL_MS: Final = 300_000

#: A JSON-RPC id: a string or an integer.
RequestId = str | int

_VERSION_HEADER: Final = "mcp-protocol-version"
_METHOD_HEADER: Final = "mcp-method"
_NAME_HEADER: Final = "mcp-name"
#: A header value that is one token of visible ASCII.
_TOKEN: Final = re.compile(r"[\x21-\x7e]+")
_BASE64: Final = re.compile(r"=\?base64\?(.*)\?=", re.DOTALL)
#: The media ranges that cover a JSON answer, by how specific each is (RFC 9110 section 12.5.1).
_JSON_RANGES: Final = {"*/*": 0, "application/*": 1, "application/json": 2}


@dataclass(frozen=True)
class WireError(Exception):
    """A request the door refuses before any tool runs, or one that ended without a result.

    Attributes:
        status: The HTTP status.
        code: The JSON-RPC error code.
        message: A short fixed text.
        data: What the error's ``data`` holds, if anything.
        id: The request's id, once it is known.
    """

    status: int
    code: int
    message: str
    data: Mapping[str, object] | None = None
    id: RequestId | None = None


@dataclass(frozen=True)
class Message:
    """One JSON-RPC message, its shape checked.

    Attributes:
        id: The request's id; ``None`` for a notification.
        method: The method.
        params: The params, ``{}`` when it has none.
    """

    id: RequestId | None
    method: str
    params: dict[str, Any]


@dataclass(frozen=True)
class Era:
    """The rules a request is answered by.

    Attributes:
        version: The revision it states.
        modern: Whether it is :data:`MODERN`.
    """

    version: str
    modern: bool


def _invalid(message: str) -> WireError:
    """A ``400`` invalid request, with no id: the message's shape is wrong, so its id is not known."""
    return WireError(400, INVALID_REQUEST, message)


def parse_message(body: Any) -> Message:
    """Check the shape of one JSON-RPC message.

    Args:
        body: The request's JSON body.

    Returns:
        The message.

    Raises:
        WireError: ``400``, ``-32600``, for a batch, a body that is not an object, a ``jsonrpc`` other than ``"2.0"``,
            a method that is not a string, params that are not an object, or an id that is present and not a string
            or an integer (``null``, a boolean, a float, a container).
    """
    if isinstance(body, list):
        raise _invalid("batches are not supported")
    if not isinstance(body, dict):
        raise _invalid("invalid request")
    method = body.get("method")
    params = body.get("params", {})
    if body.get("jsonrpc") != "2.0" or not isinstance(method, str) or not isinstance(params, dict):
        raise _invalid("invalid request")
    if "id" not in body:
        return Message(None, method, params)
    request_id = body["id"]
    if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
        raise _invalid("invalid request id")
    return Message(request_id, method, params)


def _media_type(value: str) -> str:
    """The media type of a header item, lower-cased, without its parameters."""
    return value.split(";", 1)[0].strip().lower()


def _refused_by_q(value: str) -> bool:
    """Whether a media range's ``q`` parameter is zero, or not a weight at all."""
    for parameter in value.split(";")[1:]:
        name, _, weight = parameter.partition("=")
        if name.strip().lower() == "q":
            try:
                return float(weight.strip()) <= 0
            except ValueError:
                return True
    return False


def check_media(content_type: str | None, accept: str | None) -> None:
    """Check that the request is JSON and that the client takes a JSON answer.

    Media types match case-insensitively and may carry parameters. A missing ``Accept`` counts as ``*/*``.

    Args:
        content_type: The ``Content-Type`` header, or ``None``.
        accept: The ``Accept`` header, or ``None``.

    Raises:
        WireError: ``415`` if the body is not ``application/json``; ``406`` if the most specific range of ``Accept``
            that covers ``application/json`` (itself, ``application/*`` or ``*/*``) is weighted ``q=0``, or none does.
    """
    if content_type is None or _media_type(content_type) != "application/json":
        raise WireError(415, INVALID_REQUEST, "the body must be application/json")
    if accept is None:
        return
    covering = [
        (_JSON_RANGES[_media_type(item)], not _refused_by_q(item))
        for item in accept.split(",")
        if _media_type(item) in _JSON_RANGES
    ]
    top = max((rank for rank, _ in covering), default=None)
    if top is None or not any(allowed for rank, allowed in covering if rank == top):
        raise WireError(406, INVALID_REQUEST, "the answer is application/json")


def decode_name(value: str) -> str | None:
    """Decode an ``Mcp-Name`` header.

    Args:
        value: The header's value.

    Returns:
        The text of a ``=?base64?<b64>?=`` value, decoded as UTF-8; ``None`` if its base64 or its UTF-8 is malformed;
        any other value as it is.
    """
    found = _BASE64.fullmatch(value)
    if found is None:
        return value
    try:
        return base64.b64decode(found.group(1), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None


def _mismatch(message: str) -> WireError:
    """A ``400`` header mismatch."""
    return WireError(400, HEADER_MISMATCH, message)


def _check_meta(params: Mapping[str, Any], version: str) -> None:
    """Check a modern request's ``_meta``: the revision, equal to the header's, and the client's capabilities.

    Raises:
        WireError: ``400``, ``-32602``, if ``_meta`` is not an object, the revision is missing or not a string,
            ``clientCapabilities`` is missing or not an object, or ``clientInfo`` is there and not an object;
            ``400``, ``-32020``, if the revision is not the header's.
    """
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        raise WireError(400, INVALID_PARAMS, "params._meta must state the revision and the client's capabilities")
    stated = meta.get(PROTOCOL_VERSION)
    if (
        not isinstance(stated, str)
        or not isinstance(meta.get(CLIENT_CAPABILITIES), dict)
        or not isinstance(meta.get(CLIENT_INFO, {}), dict)
    ):
        raise WireError(400, INVALID_PARAMS, "params._meta must state the revision and the client's capabilities")
    if stated != version:
        raise _mismatch("the revision in params._meta is not the header's")


def select_era(message: Message, headers: Mapping[str, str]) -> Era:
    """Choose the rules a request is answered by, from its headers, and check what those rules ask of it.

    For every method but ``initialize``.

    Args:
        message: The request.
        headers: Its headers, by lower-case name.

    Returns:
        The legacy era for a legacy revision; the modern era for :data:`MODERN`, once ``Mcp-Method``, ``Mcp-Name`` on
        ``tools/call`` and ``params._meta`` agree with the body. A stray ``Mcp-Name`` on another method is ignored.

    Raises:
        WireError: ``400``, ``-32020``, if ``MCP-Protocol-Version`` is missing or holds invalid characters, or a
            modern header disagrees with the body; ``400``, ``-32022``, for a revision the door does not serve, with
            the revisions it serves and the one asked for; ``400``, ``-32602``, for a modern ``_meta`` that is missing
            or malformed.
    """
    version = headers.get(_VERSION_HEADER)
    if version is None or _TOKEN.fullmatch(version) is None:
        raise _mismatch("MCP-Protocol-Version is missing or malformed")
    if version in LEGACY:
        return Era(version, modern=False)
    if version != MODERN:
        data = {"supported": list(SUPPORTED), "requested": version}
        raise WireError(400, UNSUPPORTED_VERSION, "unsupported protocol version", data)
    if headers.get(_METHOD_HEADER) != message.method:
        raise _mismatch("Mcp-Method does not match the body's method")
    if message.method == "tools/call":
        name = headers.get(_NAME_HEADER)
        if name is None or decode_name(name) != message.params.get("name"):
            raise _mismatch("Mcp-Name does not match the body's tool")
    _check_meta(message.params, version)
    return Era(version, modern=True)


def jsonrpc_result(request_id: RequestId, result: Mapping[str, Any]) -> dict[str, Any]:
    """A JSON-RPC result.

    Args:
        request_id: The request's id.
        result: The result.

    Returns:
        The message.
    """
    return {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}


def jsonrpc_error(err: WireError) -> dict[str, Any]:
    """A JSON-RPC error.

    Args:
        err: The refusal.

    Returns:
        The message: its ``id`` only when the request's is known, its ``data`` only when there is any.
    """
    error: dict[str, Any] = {"code": err.code, "message": err.message}
    if err.data is not None:
        error["data"] = dict(err.data)
    message: dict[str, Any] = {"jsonrpc": "2.0", "error": error}
    if err.id is not None:
        message["id"] = err.id
    return message


def _server() -> dict[str, str]:
    """The server's ``Implementation``: its name and version."""
    return {"name": "workbench", "version": __version__}


def modern(result: Mapping[str, Any]) -> dict[str, Any]:
    """Mark a result as the modern revision asks: complete, and stamped with the server's name and version.

    Args:
        result: The result.

    Returns:
        The result with ``resultType: "complete"`` and ``_meta[SERVER_INFO]``.
    """
    meta = dict(result.get("_meta") or {})
    meta[SERVER_INFO] = _server()
    return {**result, "resultType": "complete", "_meta": meta}


def _capabilities() -> dict[str, Any]:
    """What the server can do: list and call tools, a list that does not change while it runs."""
    return {"tools": {"listChanged": False}}


def initialize_result(requested: object) -> dict[str, Any]:
    """Answer a legacy ``initialize``.

    Args:
        requested: The ``protocolVersion`` the client asked for, whatever it is.

    Returns:
        The requested revision if it is legacy, else :data:`LEGACY_DEFAULT`; the capabilities and the server.
    """
    version = requested if isinstance(requested, str) and requested in LEGACY else LEGACY_DEFAULT
    return {"protocolVersion": version, "capabilities": _capabilities(), "serverInfo": _server()}


def discover_result() -> dict[str, Any]:
    """Answer ``server/discover``.

    Returns:
        The revisions served, the capabilities, the instructions and how long a client may keep the answer.
    """
    return {
        "supportedVersions": list(SUPPORTED),
        "capabilities": _capabilities(),
        "instructions": INSTRUCTIONS,
        "ttlMs": DISCOVER_TTL_MS,
        "cacheScope": "private",
    }


def _tool(spec: "ToolSpec", caller: "Caller") -> dict[str, Any]:
    """One tool as ``tools/list`` names it.

    Raises:
        ValueError: The spec has no ``input_schema`` or no ``output_model``; every inline spec has both.
    """
    if spec.input_schema is None or spec.output_model is None:
        raise ValueError(f"{spec.name} cannot be listed: it has no input schema or no output model")
    return {
        "name": spec.name,
        "title": spec.title,
        "description": spec.description,
        "inputSchema": spec.input_schema(caller),
        "outputSchema": spec.output_model.model_json_schema(),
    }


def tools_list_result(specs: Sequence["ToolSpec"], caller: "Caller", era: Era) -> dict[str, Any]:
    """Answer ``tools/list``.

    Args:
        specs: The caller's tools, in name order.
        caller: The caller; a tool's arguments may depend on it.
        era: The rules the request is answered by.

    Returns:
        Every tool by its name, title, description and schemas; on the modern era with how long a client may keep the
        list. There is never a ``nextCursor``.
    """
    result: dict[str, Any] = {"tools": [_tool(spec, caller) for spec in specs]}
    if era.modern:
        result["ttlMs"] = TOOLS_TTL_MS
        result["cacheScope"] = "private"
    return result


def tool_result(outcome: "ToolOutcome") -> dict[str, Any]:
    """Answer a tool call that ran.

    Args:
        outcome: The tool's answer.

    Returns:
        The structured answer, also as compact UTF-8 JSON text (with its ``None`` fields, as the tool measured it)
        ahead of the tool's own blocks, and ``isError: false``.
    """
    text = {"type": "text", "text": _compact_json(outcome.structured)}
    return {"content": [text, *outcome.blocks], "structuredContent": outcome.structured, "isError": False}


def tool_error(reason: str, text: str, extra: Mapping[str, object] | None = None) -> dict[str, Any]:
    """Answer a tool call that did not run or did not complete, as a result the model can read.

    Args:
        reason: The token callers map; the text may change, the token does not.
        text: A short text for the model.
        extra: More of the structured answer, such as where the arguments failed.

    Returns:
        The text, ``structuredContent`` with the token as ``reason``, and ``isError: true``.
    """
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {"reason": reason, **(extra or {})},
        "isError": True,
    }


def _compact_json(value: object) -> str:
    """Write a value as compact JSON, its text unescaped."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def too_large() -> JSONResponse:
    """The answer to a request body over the bound: ``413``, ``-32600``, with no id.

    Returns:
        The response.
    """
    return JSONResponse(jsonrpc_error(WireError(413, INVALID_REQUEST, "request too large")), status_code=413)
