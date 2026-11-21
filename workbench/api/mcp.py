"""The MCP door: ``POST /mcp``, stateless and JSON only, for the callers that come in through it (spec §3.3).

The wire rules are :mod:`workbench.api.mcp_wire`'s; this module reads the request and dispatches it, in this order:
an ``Origin`` header (``403``), the media types, the body, its shape, a notification (``202``, no body),
``initialize`` (legacy, whatever its headers), the era, and then the method the era serves. ``tools/call`` finds the
tool on the caller's list, reads who it is for and why from the headers (never from ``_meta``), validates the
arguments with the caller's wall cap and runs the tool. A tool that refuses answers a result with ``isError`` and its
reason token (:func:`call_failure`); only a failed audit write and an error no rule expected are JSON-RPC errors.

``X-Accept-Images: 1`` says a call takes images (:func:`~workbench.api.auth.accepts_images`), and is read on every
method as the other headers are: ``server/discover``, ``tools/list`` and ``tools/call`` without it tell and show what
workbench 0.6 did, with no ``view_file`` and no image block. It only chooses what the model is told and shown.

Every answer is JSON and none opens a session: no ``Mcp-Session-Id`` is minted, and one that is sent is ignored, as
is ``Last-Event-ID``. Nothing a client chose reaches a log line: the one line written names a method only if the
door serves it.
"""

import dataclasses
import json
import logging
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from starlette.exceptions import HTTPException

from workbench.api.auth import (
    MCP_PATH,
    Caller,
    InvalidPrincipal,
    InvalidPurpose,
    accepts_images,
    principal_from_headers,
    purpose_from_headers,
    require_mcp_caller,
)
from workbench.api.errors import BUSY, INVALID_PURPOSE, PURPOSE_REQUIRED, TOO_LARGE, compact, describe
from workbench.api.jobs import Busy, RequestTooLarge
from workbench.api.mcp_wire import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    Message,
    RequestId,
    WireError,
    check_media,
    discover_result,
    initialize_result,
    jsonrpc_error,
    jsonrpc_result,
    modern,
    parse_message,
    select_era,
    too_large,
    tool_error,
    tool_result,
    tools_list_result,
)
from workbench.api.registry import PurposeRequired, Registry, ToolEnv
from workbench.api.routing import EncodableRoute
from workbench.api.store import StoreError
from workbench.api.tools import NotCompleted
from workbench.paths import PathError
from workbench.utils.audit_event import AuditWriteError
from workbench.utils.audit_runtime import clean_request_id

_log = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False, route_class=EncodableRoute)

#: The methods each era serves.
_MODERN_METHODS: Final = frozenset({"server/discover", "tools/list", "tools/call"})
_LEGACY_METHODS: Final = frozenset({"ping", "tools/list", "tools/call"})
#: The method names a log line may hold: those the door serves.
SERVED: Final = frozenset({"initialize", *_MODERN_METHODS, *_LEGACY_METHODS})
#: Every method the door answers with ``405``: all but POST.
_NOT_ALLOWED: Final = ("GET", "HEAD", "DELETE", "PUT", "PATCH", "OPTIONS")

McpCaller = Annotated[Caller, Depends(require_mcp_caller)]


def _answer(err: WireError, headers: dict[str, str] | None = None) -> JSONResponse:
    """The JSON-RPC error of a refusal, with its status."""
    return JSONResponse(jsonrpc_error(err), status_code=err.status, headers=headers)


def _id(message: Message | None) -> RequestId | None:
    """The request's id, once its shape is known to be right."""
    return None if message is None else message.id


def call_failure(exc: Exception) -> tuple[str, str] | None:
    """Say how a tool's refusal is answered as a result: its reason token and its short text.

    Args:
        exc: What the tool raised.

    Returns:
        The token and the text of an ``isError`` result, or ``None`` for an error that is no tool's refusal.
    """
    if isinstance(exc, PurposeRequired):
        return "purpose_required", PURPOSE_REQUIRED
    if isinstance(exc, Busy):
        return exc.reason, BUSY
    if isinstance(exc, RequestTooLarge):
        return "too_large", TOO_LARGE
    if isinstance(exc, NotCompleted):
        return "not_completed", str(exc.detail)
    if isinstance(exc, (HTTPException, PathError, StoreError)):
        return "refused", describe(exc)[1]
    return None


async def _call(request: Request, caller: Caller, params: dict[str, Any]) -> dict[str, Any]:
    """Run a ``tools/call``: the tool, who it is for, why, its arguments, then the tool itself.

    Args:
        request: The request; its headers name the user, the session, the purpose, the request id and whether the call
            takes images.
        caller: The authenticated caller.
        params: The call's params.

    Returns:
        The tool's result, or an ``isError`` result for arguments that do not validate or a tool's refusal.

    Raises:
        WireError: ``-32602`` for a tool not on the caller's list (or one that only shows images, on a call that takes
            none), ``-32600`` for a user or session header that is missing or not a plain id, ``-32602`` for a
            malformed purpose.
        Exception: What the tool raised that is no refusal of its, such as a failed audit write.
    """
    registry: Registry = request.app.state.registry
    images = accepts_images(request.headers)
    name = params.get("name")
    spec = registry.find(caller, name, images=images) if isinstance(name, str) else None
    if spec is None or spec.input_model is None:
        raise WireError(400, INVALID_PARAMS, "unknown tool")
    try:
        p = principal_from_headers(caller, request.headers)
    except InvalidPrincipal as refused:
        raise WireError(400, INVALID_REQUEST, str(refused)) from None
    try:
        purpose = purpose_from_headers(request.headers)
    except InvalidPurpose:
        raise WireError(400, INVALID_PARAMS, INVALID_PURPOSE) from None
    request_id = clean_request_id(request.headers.get("x-request-id"))
    try:
        body = spec.input_model.model_validate(params.get("arguments", {}), context={"wall_cap": caller.wall_s})
    except ValidationError as exc:
        detail = compact(exc.errors(include_url=False, include_context=False, include_input=False))
        return tool_error("invalid_arguments", json.dumps(detail, ensure_ascii=False, separators=(",", ":")), detail)
    state = request.app.state
    env = ToolEnv(
        p=p,
        caller=caller,
        settings=state.settings,
        store=state.store,
        audit=state.audit,
        jobs=state.jobs,
        purpose=purpose,
        request_id=request_id,
        images=images,
    )
    try:
        outcome = await spec.run(body, env)
    except Exception as exc:
        failure = call_failure(exc)
        if failure is None:
            raise
        return tool_error(*failure)
    return tool_result(outcome)


async def _dispatch(request: Request, caller: Caller, message: Message) -> dict[str, Any]:
    """Answer a request by the era its headers choose.

    Args:
        request: The request.
        caller: The authenticated caller.
        message: The request's message, its shape checked.

    Returns:
        The result; a modern one marked as :func:`~workbench.api.mcp_wire.modern` asks.

    Raises:
        WireError: The headers, the ``_meta`` or the method are refused, or ``tools/call`` is.
    """
    if message.method == "initialize":
        return initialize_result(message.params.get("protocolVersion"))
    era = select_era(message, request.headers)
    if message.method not in (_MODERN_METHODS if era.modern else _LEGACY_METHODS):
        raise WireError(404, METHOD_NOT_FOUND, "method not found")
    if message.method == "ping":
        result: dict[str, Any] = {}
    elif message.method == "server/discover":
        result = discover_result(caller, images=accepts_images(request.headers))
    elif message.method == "tools/list":
        registry: Registry = request.app.state.registry
        result = tools_list_result(registry.for_caller(caller, images=accepts_images(request.headers)), caller, era)
    else:
        result = await _call(request, caller, message.params)
    return modern(result) if era.modern else result


@router.post(MCP_PATH)
async def mcp(request: Request, caller: McpCaller) -> Response:
    """Answer one JSON-RPC message of an MCP caller.

    Args:
        request: The request.
        caller: The authenticated caller of the MCP door.

    Returns:
        The JSON-RPC result or error; ``202`` with no body for a notification.
    """
    message: Message | None = None
    try:
        if "origin" in request.headers:
            raise WireError(403, INVALID_REQUEST, "a browser is not a caller")
        check_media(request.headers.get("content-type"), request.headers.get("accept"))
        try:
            body = await request.json()
        except HTTPException as exc:
            if exc.status_code == 413:
                return too_large()
            if exc.status_code != 422:
                raise
            raise WireError(400, INVALID_REQUEST, "request nested too deeply") from None
        except ValueError:
            raise WireError(400, PARSE_ERROR, "parse error") from None
        message = parse_message(body)
        if message.id is None:
            return Response(status_code=202)
        return JSONResponse(jsonrpc_result(message.id, await _dispatch(request, caller, message)))
    except WireError as err:
        if err.id is None and message is not None:
            err = dataclasses.replace(err, id=message.id)
        return _answer(err)
    except AuditWriteError:
        return _answer(
            WireError(500, INTERNAL_ERROR, "audit unavailable", {"reason": "audit_unavailable"}, _id(message))
        )
    except Exception as exc:
        method = message.method if message is not None and message.method in SERVED else "a request"
        _log.error("an unexpected error ended %s on /mcp (%s)", method, type(exc).__name__)
        return _answer(WireError(500, INTERNAL_ERROR, "internal error", {"reason": "internal"}, _id(message)))


@router.api_route(MCP_PATH, methods=list(_NOT_ALLOWED))
async def not_allowed(_caller: McpCaller) -> JSONResponse:
    """Answer any method but POST on the MCP door: ``405``, ``Allow: POST``.

    Args:
        _caller: The authenticated caller of the MCP door; another caller finds no door here.

    Returns:
        The JSON-RPC error, with no id.
    """
    return _answer(WireError(405, INVALID_REQUEST, "only POST is served"), headers={"Allow": "POST"})
