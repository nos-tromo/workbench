"""Calls on the MCP door as the tests send them: one JSON-RPC message each, and its answer read.

The tool tests of several modules call through here, so none of them imports another's fixtures to reach the wire.
"""

import json
from collections.abc import Mapping
from typing import Any, Final

import httpx2
from fastapi.testclient import TestClient

from tests.api.conftest import HD, HG
from workbench.api.auth import MCP_PATH
from workbench.api.mcp_wire import MODERN

META_PREFIX: Final = "io.modelcontextprotocol/"

#: An empty object: no params, no capabilities.
NONE: Final[dict[str, object]] = {}

#: The id a notification leaves out.
NOTIFICATION: Final = object()

Headers = Mapping[str, str | bytes | None]


def send(client: TestClient, body: bytes, headers: Headers, path: str = MCP_PATH) -> httpx2.Response:
    """POST raw bytes; a header given as ``None`` is left out, even one the client would add by itself."""
    present = [
        (name.encode(), value if isinstance(value, bytes) else value.encode("latin-1"))
        for name, value in headers.items()
        if value is not None
    ]
    request = client.build_request("POST", path, content=body, headers=present)
    for name, value in headers.items():
        if value is None and name in request.headers:
            del request.headers[name]
    return client.send(request, follow_redirects=False)


def rpc(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    id: object = 1,
    version: str | None = MODERN,
    headers: Headers = HD,
    meta: bool | Mapping[str, object] = True,
    name: str | None = None,
) -> httpx2.Response:
    """Send one JSON-RPC message as docint does: the body and headers :func:`message` builds."""
    body, sent = message(method, params, id=id, version=version, headers=headers, meta=meta, name=name)
    return send(client, body, sent)


def message(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    id: object = 1,
    version: str | None = MODERN,
    headers: Headers = HD,
    meta: bool | Mapping[str, object] = True,
    name: str | None = None,
) -> tuple[bytes, Headers]:
    """Build one JSON-RPC message and its headers as docint sends them.

    ``Content-Type``, ``Accept`` and ``MCP-Protocol-Version`` go on every message; on the modern revision also
    ``Mcp-Method``, ``Mcp-Name`` on ``tools/call`` (``name``, else the tool's) and ``params._meta`` with the version
    and ``clientCapabilities``. ``meta`` may give another ``_meta``; ``headers`` come last, ``None`` leaving one out.
    """
    sent = dict(params or {})
    modern = version == MODERN
    if isinstance(meta, Mapping):
        sent["_meta"] = dict(meta)
    elif meta and modern:
        sent["_meta"] = {f"{META_PREFIX}protocolVersion": version, f"{META_PREFIX}clientCapabilities": NONE}
    protocol: dict[str, str | bytes | None] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if version is not None:
        protocol["MCP-Protocol-Version"] = version
    if modern:
        protocol["Mcp-Method"] = method
        if method == "tools/call":
            protocol["Mcp-Name"] = name if name is not None else str(sent.get("name"))
    body: dict[str, object] = {"jsonrpc": "2.0", "method": method, "params": sent}
    if id is not NOTIFICATION:
        body["id"] = id
    return json.dumps(body).encode(), {**protocol, **headers}


def acall(
    client: TestClient, tool: str, arguments: dict[str, Any] | None = None, *, headers: Headers = HG, **options: Any
) -> httpx2.Response:
    """Call a tool on the MCP door as Ghostwriter does, on the modern revision."""
    return rpc(client, "tools/call", {"name": tool, "arguments": arguments or {}}, headers=headers, **options)


def raw_call(client: TestClient, tool: str, arguments: str, *, headers: Headers = HG) -> httpx2.Response:
    """Call a tool on the legacy revision with its arguments given as JSON text, as a client may write them.

    A test reaches here what a Python value cannot carry through :func:`json.dumps`: ``NaN``, an escape of half a
    surrogate pair, a nesting past the parser's own depth.
    """
    params = f'{{"name": "{tool}", "arguments": {arguments}}}'
    message = f'{{"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {params}}}'
    protocol = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    return send(client, message.encode(), {**protocol, **headers})


def error(r: httpx2.Response, status: int, code: int) -> dict[str, Any]:
    """Check a JSON-RPC error answer's status and code, and give its body."""
    assert r.status_code == status, r.text
    assert r.headers["content-type"] == "application/json"
    body = r.json()
    assert body["jsonrpc"] == "2.0" and "result" not in body
    assert body["error"]["code"] == code, body
    return body


def result(r: httpx2.Response) -> dict[str, Any]:
    """Check a JSON-RPC result answer and give its ``result``."""
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/json"
    body = r.json()
    assert body["jsonrpc"] == "2.0" and "error" not in body
    return body["result"]


def refused(r: httpx2.Response) -> dict[str, Any]:
    """Check an ``isError`` result and give its ``structuredContent``."""
    answer = result(r)
    assert answer["isError"] is True
    (block,) = answer["content"]
    assert block["type"] == "text" and block["text"]
    return answer["structuredContent"]


def answered(r: httpx2.Response) -> dict[str, Any]:
    """Check the result of a tool that ran, and give its ``structuredContent``: the tool's answer and ``outputs``."""
    answer = result(r)
    assert answer["isError"] is False, answer
    return answer["structuredContent"]


def refusal(r: httpx2.Response) -> tuple[str, str]:
    """Check an ``isError`` result and give its reason token and its text."""
    reason = refused(r)["reason"]
    return reason, result(r)["content"][0]["text"]


def unavailable(r: httpx2.Response) -> None:
    """Check the JSON-RPC error of a call whose audit record could not be written."""
    body = error(r, 500, -32603)
    assert body["error"] == {"code": -32603, "message": "audit unavailable", "data": {"reason": "audit_unavailable"}}


def _no_nulls(value: object) -> object:
    """A JSON value without the ``null`` fields of its objects, at any depth, as the tool's own measure leaves them."""
    if isinstance(value, dict):
        return {key: _no_nulls(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_no_nulls(item) for item in value]
    return value


def sent_size(structured: Mapping[str, Any]) -> int:
    """Measure a tool's answer as it cut itself to the result limit: compact UTF-8 JSON, without nulls and outputs."""
    answer = _no_nulls({key: value for key, value in structured.items() if key != "outputs"})
    return len(json.dumps(answer, ensure_ascii=False, separators=(",", ":")).encode())
