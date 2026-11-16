"""The MCP door: the wire, the dispatch by era, the results and each refusal of ``tools/call`` (spec §3.3).

The executor is the scripted one of ``tests/api/test_tools_python.py``. The legacy sequences are written here by hand;
the official client speaks the modern era in ``tests/api/test_mcp_sdk_client.py``.
"""

import asyncio
import base64
import dataclasses
import json
import logging
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from tests.api import test_tools_python as python_tests
from tests.api.conftest import DOCINT_KEY, HD, KEY
from tests.api.test_files import AuditLines
from tests.api.test_tools_documents import Scripted
from tests.api.test_tools_python import ran
from workbench import __version__
from workbench.api import mcp
from workbench.api.audit import AuditLog
from workbench.api.auth import MCP_PATH, KeyGate
from workbench.api.jobs import Busy, ExecutorClient, JobQueue
from workbench.api.main import create_app
from workbench.api.mcp_wire import INSTRUCTIONS, MODERN
from workbench.api.metrics import Metrics
from workbench.api.routing import MAX_DEPTH, nested_deeper_than
from workbench.api.store import Store
from workbench.config import Settings

#: The fixtures of ``run_python``'s tests: the real app over the scripted executor.
app = python_tests.app
fake_executor = python_tests.fake_executor

#: Open WebUI's headers, its key on the terminal door.
OW: Final = {"Authorization": f"Bearer {KEY}", "X-User-Id": "user-a", "X-Session-Id": "chat-1"}
META_PREFIX: Final = "io.modelcontextprotocol/"
SERVER_INFO: Final = {"name": "workbench", "version": __version__}
PASSAGES: Final = {
    "name": "passages.json",
    "content": '[{"n": 1, "text": "synthetic harbour notes", "author": "user-a"}]',
}
ARGS: Final = {"code": "print(2, 'authors')", "data": [PASSAGES]}
PNG: Final = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
COUNTS: Final = b"author,posts\nuser-a,12\nuser-b,7\n"
INLINE_OPERATION: Final = "tool.run_python.inline"

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
    """Send one JSON-RPC message as docint does.

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
    message: dict[str, object] = {"jsonrpc": "2.0", "method": method, "params": sent}
    if id is not NOTIFICATION:
        message["id"] = id
    return send(client, json.dumps(message).encode(), {**protocol, **headers})


def call(client: TestClient, arguments: object = ARGS, **options: Any) -> httpx2.Response:
    """Call inline ``run_python`` on the modern revision."""
    return rpc(client, "tools/call", {"name": "run_python", "arguments": arguments}, **options)


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


def inline_events(audit_lines: AuditLines) -> list[dict[str, Any]]:
    """The audit events of inline ``run_python``."""
    return [e for e in audit_lines() if e["operation"] == INLINE_OPERATION]


def deep(levels: int) -> object:
    """A JSON value that nests ``levels`` arrays."""
    value: object = []
    for _ in range(levels - 1):
        value = [value]
    return value


def asgi(app: FastAPI, headers: list[tuple[bytes, bytes]]) -> tuple[list[str], list[Message]]:
    """POST to ``/mcp`` through the app's whole stack, declaring a 40 MiB body; record whether a chunk was asked for."""
    asked: list[str] = []
    sent: list[Message] = []

    async def receive() -> Message:
        asked.append("body")
        return {"type": "http.request", "body": b"x" * 1024, "more_body": True}

    async def collect(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": MCP_PATH,
        "raw_path": MCP_PATH.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [*headers, (b"content-type", b"application/json"), (b"content-length", b"41943040")],
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 8000),
    }
    asyncio.run(app(scope, receive, collect))
    return asked, sent


# --- the wire ------------------------------------------------------------------------------------------------------


def test_a_batch_is_an_invalid_request(client: TestClient) -> None:
    """One message per POST: an array is refused as a whole, with no id."""
    batch = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": NONE}]
    body = error(send(client, json.dumps(batch).encode(), {**HD, "Content-Type": "application/json"}), 400, -32600)
    assert "id" not in body


def test_invalid_json_is_a_parse_error(client: TestClient) -> None:
    """A body that is not JSON, or not UTF-8, is -32700."""
    for raw in (b"{not json", b"", b'{"jsonrpc": "2.0", "id": 1, "method": "ping\xff"}'):
        body = error(send(client, raw, {**HD, "Content-Type": "application/json"}), 400, -32700)
        assert "id" not in body


def test_a_body_nested_past_the_depth_limit_is_an_invalid_request(client: TestClient) -> None:
    """``MAX_DEPTH`` levels pass; one more is -32600, as the route class refuses it."""
    within = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"x": deep(MAX_DEPTH - 2)}}
    past = {**within, "params": {"x": deep(MAX_DEPTH - 1)}}
    assert not nested_deeper_than(within, MAX_DEPTH) and nested_deeper_than(past, MAX_DEPTH)
    headers = {"Content-Type": "application/json", "MCP-Protocol-Version": "2025-11-25", **HD}
    assert send(client, json.dumps(within).encode(), headers).status_code == 200
    body = error(send(client, json.dumps(past).encode(), headers), 400, -32600)
    assert "id" not in body


def test_a_declared_body_over_2_mib_is_a_jsonrpc_413_before_it_is_read(app: FastAPI) -> None:
    """Through the whole stack: the declared length is refused, as JSON-RPC, before a chunk is asked for."""
    asked, sent = asgi(app, [(b"authorization", f"Bearer {DOCINT_KEY}".encode())])
    assert asked == []
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"]) == {
        "jsonrpc": "2.0",
        "error": {"code": -32600, "message": "request too large"},
    }


def test_the_mcp_door_needs_the_key(app: FastAPI) -> None:
    """Without a key, the gate answers 401 before any body is read."""
    asked, sent = asgi(app, [])
    assert asked == []
    assert sent[0]["status"] == 401


def test_a_streamed_body_over_2_mib_is_a_jsonrpc_413(client: TestClient) -> None:
    """A chunked body is counted as it arrives and stopped past 2 MiB, answered as JSON-RPC."""

    def chunks() -> Iterator[bytes]:
        yield b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"x": "'
        for _ in range(3):
            yield b"x" * (1024 * 1024)
        yield b'"}}'

    request = client.build_request(
        "POST", MCP_PATH, content=chunks(), headers={**HD, "Content-Type": "application/json"}
    )
    assert "content-length" not in request.headers
    body = error(client.send(request), 413, -32600)
    assert body == {"jsonrpc": "2.0", "error": {"code": -32600, "message": "request too large"}}


@pytest.mark.parametrize(
    ("content_type", "accept", "status"),
    [
        ("text/plain", None, 415),
        (None, None, 415),
        ("application/json-seq", None, 415),
        ("Application/JSON; charset=UTF-8", None, 200),
        ("application/json", "text/event-stream", 406),
        ("application/json", "text/html", 406),
        ("application/json", "application/json;q=0, text/event-stream", 406),
        ("application/json", "*/*", 200),
        ("application/json", "application/*", 200),
        ("application/json", "text/html, Application/JSON;q=0.5", 200),
    ],
)
def test_media_types_are_matched_as_http_says(
    client: TestClient, content_type: str | None, accept: str | None, status: int
) -> None:
    """Case-insensitively, parameters allowed; a missing ``Accept`` is ``*/*``, and ``q=0`` refuses a type."""
    r = rpc(client, "tools/list", headers={**HD, "Content-Type": content_type, "Accept": accept})
    if status == 200:
        assert [tool["name"] for tool in result(r)["tools"]] == ["run_python"]
    else:
        assert "id" not in error(r, status, -32600)


def test_an_origin_header_is_403(client: TestClient) -> None:
    """No browser is a caller."""
    body = error(rpc(client, "tools/list", headers={**HD, "Origin": "https://edge-host.invalid"}), 403, -32600)
    assert "id" not in body


@pytest.mark.parametrize("method", ["GET", "DELETE", "PUT", "PATCH", "OPTIONS"])
def test_get_and_delete_are_405_with_allow_post(client: TestClient, method: str) -> None:
    """Any method but POST: the house JSON-RPC error with no id, and ``Allow`` names POST alone."""
    r = client.request(method, MCP_PATH, headers=HD)
    assert r.headers["allow"] == "POST"
    assert "id" not in error(r, 405, -32600)
    head = client.request("HEAD", MCP_PATH, headers=HD)
    assert (head.status_code, head.headers["allow"]) == (405, "POST")


def test_no_session_is_minted_and_one_sent_is_ignored(client: TestClient) -> None:
    """A session id and a ``Last-Event-ID`` change nothing; no answer carries ``Mcp-Session-Id``."""
    stale = {**HD, "Mcp-Session-Id": "session-1", "Last-Event-ID": "event-1"}
    answers = [
        rpc(client, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}}, version=None, headers=stale),
        rpc(client, "tools/list", headers=stale),
        rpc(client, "tools/list", version="2025-11-25", headers=stale),
        call(client, headers=stale),
    ]
    assert [r.status_code for r in answers] == [200, 200, 200, 200]
    assert [r for r in answers if "mcp-session-id" in r.headers] == []


def test_every_answer_is_json_and_a_notification_is_202_without_a_body(client: TestClient) -> None:
    """Results and errors are ``application/json``; a notification is 202 with nothing in it, whatever its method."""
    answers = [
        rpc(client, "tools/list"),
        rpc(client, "nowhere/at-all"),
        rpc(client, "tools/list", version=None),
        send(client, b"{not json", {**HD, "Content-Type": "application/json"}),
        send(client, b"{}", {**HD, "Content-Type": "text/plain"}),
        client.get(MCP_PATH, headers=HD),
    ]
    assert {r.headers["content-type"] for r in answers} == {"application/json"}
    for method in ("notifications/initialized", "notifications/cancelled", "nowhere/at-all"):
        r = rpc(client, method, id=NOTIFICATION, version=None, meta=False)
        assert (r.status_code, r.content) == (202, b"")
        assert "mcp-session-id" not in r.headers


@pytest.mark.parametrize("request_id", [1, 0, "req-1", -7, ""])
def test_the_id_comes_back_as_sent(client: TestClient, request_id: str | int) -> None:
    """A string or an integer, zero and empty included, comes back as it was sent, with its type."""
    body = rpc(client, "tools/list", id=request_id).json()
    assert body["id"] == request_id and type(body["id"]) is type(request_id)
    failed = rpc(client, "nowhere/at-all", id=request_id).json()
    assert failed["id"] == request_id and type(failed["id"]) is type(request_id)


@pytest.mark.parametrize("request_id", [None, True, 1.5, {}, []])
def test_an_id_of_another_type_is_an_invalid_request(client: TestClient, request_id: object) -> None:
    """``null``, a boolean, a float or a container is not an id: -32600, and no id in the answer."""
    assert "id" not in error(rpc(client, "tools/list", id=request_id), 400, -32600)


@pytest.mark.parametrize(
    "message",
    [
        {"id": 1, "method": "tools/list"},
        {"jsonrpc": "1.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 1, "method": 7},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": []},
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        "tools/list",
    ],
)
def test_a_message_of_another_shape_is_an_invalid_request(client: TestClient, message: object) -> None:
    """No ``jsonrpc: "2.0"``, a method that is not a string, params that are not an object, a response, a string."""
    raw = json.dumps(message).encode()
    assert "id" not in error(send(client, raw, {**HD, "Content-Type": "application/json"}), 400, -32600)


# --- the door's own caller -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "GET", "DELETE"])
def test_open_webuis_key_on_mcp_is_not_found(client: TestClient, method: str) -> None:
    """The terminal caller finds no MCP door: the gate's 404, never a 405 or an answer of the door."""
    r = client.request(method, MCP_PATH, headers={**OW, "Content-Type": "application/json"}, content=b"{}")
    assert (r.status_code, r.json()) == (404, {"detail": "not found"})


@pytest.mark.parametrize("headers", [HD, OW], ids=["docint", "open-webui"])
@pytest.mark.parametrize("method", ["POST", "GET"])
def test_mcp_with_a_trailing_slash_is_not_found_never_a_redirect(
    client: TestClient, headers: dict[str, str], method: str
) -> None:
    """``/mcp/`` is a path no route serves, for either key: no 307 that would point at the door."""
    r = client.request(method, MCP_PATH + "/", headers=headers, follow_redirects=False)
    assert (r.status_code, r.json()) == (404, {"detail": "not found"})
    assert "location" not in r.headers


def test_the_door_refuses_to_run_under_a_root_path(app: FastAPI) -> None:
    """Behind a proxy that sets a root path, the door is not there: the gate judged a path routing does not use."""
    with TestClient(app, root_path="/prefix") as prefixed:
        for path in (MCP_PATH, "/prefix" + MCP_PATH):
            for headers in (HD, OW):
                r = prefixed.post(path, headers={**headers, "Content-Type": "application/json"}, content=b"{}")
                assert (r.status_code, r.json()) == (404, {"detail": "not found"}), (path, headers is HD)
            r = prefixed.get(path, headers=HD)
            assert (r.status_code, r.json()) == (404, {"detail": "not found"})


def test_without_the_gate_the_door_still_refuses_open_webuis_key(app: FastAPI) -> None:
    """Were the gate bypassed, every method of ``/mcp`` still answers Open WebUI's key with the gate's 404."""
    app.user_middleware = [middleware for middleware in app.user_middleware if middleware.cls is not KeyGate]
    methods = sorted(
        method for route in iter_route_contexts(app.routes) if route.path == MCP_PATH for method in route.methods or ()
    )
    assert {"POST", "GET", "DELETE"} <= set(methods)
    with TestClient(app) as bypassed:
        assert bypassed.get("/nowhere").status_code == 404  # the gate would answer 401: it is really out
        for method in methods:
            r = bypassed.request(method, MCP_PATH, headers={**OW, "Content-Type": "application/json"}, content=b"{}")
            assert r.status_code == 404, method
            assert method == "HEAD" or r.json() == {"detail": "not found"}, method
        assert bypassed.post(MCP_PATH, content=b"{}").status_code == 401


# --- the dispatch --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"MCP-Protocol-Version": "2026-07-28"}])
def test_initialize_is_legacy_whatever_its_headers(client: TestClient, headers: dict[str, str]) -> None:
    """``initialize`` negotiates a legacy revision and opens no session."""
    params = {"protocolVersion": "2025-06-18", "capabilities": NONE, "clientInfo": {"name": "client-a", "version": "1"}}
    r = rpc(client, "initialize", params, version=None, meta=False, headers={**HD, **headers})
    assert result(r) == {
        "protocolVersion": "2025-06-18",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
    }
    assert "mcp-session-id" not in r.headers


@pytest.mark.parametrize("requested", ["2025-03-26", "2024-11-05", "2099-01-01", "2026-07-28", 7, None])
def test_initialize_naming_neither_legacy_revision_gets_2025_11_25(client: TestClient, requested: object) -> None:
    """An older, a newer, the modern or no revision at all: the answer is ``2025-11-25``, as the legacy rules ask."""
    params = {"capabilities": NONE} if requested is None else {"protocolVersion": requested, "capabilities": NONE}
    assert result(rpc(client, "initialize", params, version=None, meta=False))["protocolVersion"] == "2025-11-25"


@pytest.mark.parametrize("version", ["2025-11-25", "2025-06-18"])
def test_a_legacy_session_works_with_the_version_header_alone(
    client: TestClient, fake_executor: Scripted, version: str
) -> None:
    """initialize, initialized, ping, tools/list, tools/call: no ``Mcp-Method``, no ``_meta``, no modern fields."""
    fake_executor.payload = ran("2 authors")
    legacy: dict[str, Any] = {"version": version, "meta": False}
    initialized = result(rpc(client, "initialize", {"protocolVersion": version, "capabilities": {}}, version=None))
    assert initialized["protocolVersion"] == version
    notified = rpc(client, "notifications/initialized", id=NOTIFICATION, **legacy)
    assert notified.status_code == 202
    assert result(rpc(client, "ping", id="ping-1", **legacy)) == {}
    listed = result(rpc(client, "tools/list", **legacy))
    assert [tool["name"] for tool in listed["tools"]] == ["run_python"]
    called = result(call(client, **legacy))
    assert called["isError"] is False and called["structuredContent"]["stdout"] == "2 authors"
    for answer in (initialized, listed, called):
        assert {"resultType", "ttlMs", "cacheScope"}.isdisjoint(answer)
        assert "_meta" not in answer


def test_a_request_without_the_version_header_is_a_header_mismatch(client: TestClient) -> None:
    """After ``initialize`` every request states its revision in ``MCP-Protocol-Version``."""
    body = error(rpc(client, "tools/list", version=None, meta=False), 400, -32020)
    assert body["id"] == 1


@pytest.mark.parametrize("sent", ["2026-07-28\x7f", "2026-07-28 x", b"2026-07-28\xff"])
def test_a_version_header_with_invalid_characters_is_a_header_mismatch(client: TestClient, sent: str | bytes) -> None:
    """A control character, a space inside or a byte past ASCII is no revision: -32020."""
    error(rpc(client, "tools/list", headers={**HD, "MCP-Protocol-Version": sent}), 400, -32020)


@pytest.mark.parametrize("requested", ["2025-03-26", "2099-01-01", "latest"])
def test_an_unserved_revision_is_32022_naming_both(client: TestClient, requested: str) -> None:
    """The three revisions the door serves, and the one asked for."""
    body = error(rpc(client, "tools/list", headers={**HD, "MCP-Protocol-Version": requested}), 400, -32022)
    assert body["error"]["data"] == {"supported": ["2026-07-28", "2025-11-25", "2025-06-18"], "requested": requested}


@pytest.mark.parametrize("sent", [None, "tools/call", "Tools/List", b"tools/list\xff"])
def test_a_modern_request_needs_mcp_method_matching_the_body(client: TestClient, sent: str | bytes | None) -> None:
    """Missing, another method, another case or another byte: -32020."""
    error(rpc(client, "tools/list", headers={**HD, "Mcp-Method": sent}), 400, -32020)


@pytest.mark.parametrize(
    ("sent", "status"),
    [
        (None, 400),
        ("list_files", 400),
        ("=?base64?cnVuX3B5dGhvbg==?=", 200),
        ("=?base64?%%%?=", 400),
        ("=?base64?cnVuX3B5dGhvbg?=", 400),
        ("=?base64?/w==?=", 400),
    ],
)
def test_a_modern_tools_call_needs_mcp_name_matching_the_body(
    client: TestClient, sent: str | None, status: int
) -> None:
    """Missing, another tool, or a base64 value that does not decode to the body's name as UTF-8: -32020."""
    r = call(client, headers={**HD, "Mcp-Name": sent})
    if status == 200:
        assert result(r)["isError"] is False
    else:
        error(r, status, -32020)


def test_a_stray_mcp_name_on_another_method_is_ignored(client: TestClient) -> None:
    """Only ``tools/call`` names a tool in its headers; the spec is silent on any other method."""
    r = rpc(client, "tools/list", headers={**HD, "Mcp-Name": "list_files"})
    assert [tool["name"] for tool in result(r)["tools"]] == ["run_python"]


@pytest.mark.parametrize(
    "meta",
    [
        {},
        {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},
        {"io.modelcontextprotocol/clientCapabilities": {}},
        {"io.modelcontextprotocol/protocolVersion": 7, "io.modelcontextprotocol/clientCapabilities": {}},
        {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": "x"},
        {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": NONE,
            "io.modelcontextprotocol/clientInfo": "x",
        },
        "x",
    ],
)
def test_modern_meta_needs_the_version_and_client_capabilities(client: TestClient, meta: object) -> None:
    """Missing or malformed: -32602."""
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}}
    headers = {**HD, "Content-Type": "application/json", "MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/list"}
    body = error(send(client, json.dumps(message).encode(), headers), 400, -32602)
    assert body["id"] == 1
    no_params = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    error(send(client, json.dumps(no_params).encode(), headers), 400, -32602)


def test_client_info_may_come_with_the_meta(client: TestClient) -> None:
    """``clientInfo`` is optional; an object is taken."""
    meta = {
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientCapabilities": NONE,
        "io.modelcontextprotocol/clientInfo": {"name": "client-a", "version": "1"},
    }
    assert [tool["name"] for tool in result(rpc(client, "tools/list", meta=meta))["tools"]] == ["run_python"]


def test_a_meta_version_that_disagrees_with_the_header_is_32020(client: TestClient) -> None:
    """The body and the header state one revision."""
    meta = {"io.modelcontextprotocol/protocolVersion": "2025-11-25", "io.modelcontextprotocol/clientCapabilities": NONE}
    error(rpc(client, "tools/list", meta=meta), 400, -32020)


@pytest.mark.parametrize(
    ("method", "version"),
    [
        ("resources/list", MODERN),
        ("ping", MODERN),
        ("initialize/again", MODERN),
        ("server/discover", "2025-11-25"),
        ("resources/list", "2025-06-18"),
    ],
)
def test_a_method_the_era_does_not_serve_is_404_32601(client: TestClient, method: str, version: str) -> None:
    """Modern: discover, list, call. Legacy: ping, list, call. Anything else is not found."""
    body = error(rpc(client, method, version=version), 404, -32601)
    assert body["id"] == 1


# --- results -------------------------------------------------------------------------------------------------------


def test_discover_names_the_revisions_the_caps_and_the_cache(client: TestClient) -> None:
    """The revisions, the capability, two sentences for the model, an hour's private cache, and the server's name."""
    found = result(rpc(client, "server/discover"))
    assert found == {
        "supportedVersions": ["2026-07-28", "2025-11-25", "2025-06-18"],
        "capabilities": {"tools": {"listChanged": False}},
        "instructions": INSTRUCTIONS,
        "ttlMs": 3_600_000,
        "cacheScope": "private",
        "resultType": "complete",
        "_meta": {"io.modelcontextprotocol/serverInfo": SERVER_INFO},
    }
    assert INSTRUCTIONS.count(". ") == 1 and INSTRUCTIONS.endswith(".")


def test_tools_list_is_the_callers_inline_tools_in_name_order(client: TestClient) -> None:
    """``run_python`` alone, its range from 5 to docint's cap, its output described, five minutes' private cache."""
    listed = result(rpc(client, "tools/list"))
    assert [tool["name"] for tool in listed["tools"]] == ["run_python"]
    assert (listed["ttlMs"], listed["cacheScope"], listed["resultType"]) == (300_000, "private", "complete")
    assert "nextCursor" not in listed
    (tool,) = listed["tools"]
    assert set(tool) == {"name", "title", "description", "inputSchema", "outputSchema"}
    limit = tool["inputSchema"]["properties"]["time_limit_s"]
    assert (limit["minimum"], limit["maximum"]) == (5, 60)
    assert tool["inputSchema"]["type"] == "object" and tool["outputSchema"]["type"] == "object"
    assert set(tool["outputSchema"]["properties"]) >= {"stdout", "files", "job_id", "reason"}
    assert "x-mcp-header" not in json.dumps(listed)


def test_a_tool_result_is_the_structured_answer_and_its_blocks(client: TestClient, fake_executor: Scripted) -> None:
    """The structured answer as compact JSON text, then the chart for the user and the table as a resource."""
    fake_executor.payload = ran("2 authors")
    fake_executor.files = {"chart.png": PNG, "counts.csv": COUNTS}
    answer = result(call(client))
    structured = answer["structuredContent"]
    text, image, resource = answer["content"]
    assert text == {"type": "text", "text": json.dumps(structured, ensure_ascii=False, separators=(",", ":"))}
    assert (image["type"], image["mimeType"], base64.b64decode(image["data"])) == ("image", "image/png", PNG)
    assert resource["type"] == "resource" and resource["resource"]["text"] == COUNTS.decode()
    assert answer["isError"] is False
    assert (structured["stdout"], structured["reason"], structured["hint"]) == ("2 authors", None, None)


def test_the_text_block_stays_within_the_result_limit(
    client: TestClient, fake_executor: Scripted, settings: Settings
) -> None:
    """The text block is what ``fit_python`` measured: compact UTF-8 JSON with its Nones, at most the result limit."""
    fake_executor.payload = ran("ü" * 100_000, "x" * 50_000)
    answer = result(call(client))
    (text,) = answer["content"]
    assert answer["structuredContent"]["truncated"] is True
    assert len(text["text"].encode("utf-8")) <= settings.result_max_kb * 1024
    assert json.loads(text["text"]) == answer["structuredContent"]


def test_every_modern_result_carries_result_type_and_server_info(client: TestClient, fake_executor: Scripted) -> None:
    """Discover, list, a call that ran and one that did not."""
    answers = [result(rpc(client, "server/discover")), result(rpc(client, "tools/list")), result(call(client))]
    answers.append(result(call(client, {"code": ""})))
    assert answers[-1]["isError"] is True
    for answer in answers:
        assert answer["resultType"] == "complete"
        assert answer["_meta"] == {"io.modelcontextprotocol/serverInfo": SERVER_INFO}


# --- tools/call, one per row of §3.3 -------------------------------------------------------------------------------


@pytest.mark.parametrize("version", [MODERN, "2025-11-25"])
@pytest.mark.parametrize("tool", ["list_files", "read_document", "nowhere"])
def test_docints_call_of_a_workspace_tool_is_an_unknown_tool(client: TestClient, tool: str, version: str) -> None:
    """A workspace tool, or a name no tool has: -32602 "unknown tool", on either era."""
    body = error(rpc(client, "tools/call", {"name": tool, "arguments": {}}, version=version), 400, -32602)
    assert body["error"]["message"] == "unknown tool"


@pytest.mark.parametrize("params", [{"name": 7, "arguments": {}}, {"arguments": {}}])
def test_a_call_that_names_no_tool_is_an_unknown_tool(client: TestClient, params: dict[str, object]) -> None:
    """A name that is not a string, or none: -32602 "unknown tool"."""
    body = error(rpc(client, "tools/call", params, version="2025-11-25"), 400, -32602)
    assert body["error"]["message"] == "unknown tool"


@pytest.mark.parametrize("headers", [{"X-User-Id": None}, {"X-User-Id": "../x"}, {"X-Session-Id": "a/b"}])
def test_an_invalid_user_or_session_is_an_invalid_request(
    client: TestClient, fake_executor: Scripted, headers: dict[str, str | None]
) -> None:
    """The user and the session are the headers' and must be plain ids: -32600, and nothing runs."""
    body = error(call(client, headers={**HD, **headers}), 400, -32600)
    assert body["error"]["message"] in {"missing or invalid user", "invalid session"}
    assert fake_executor.requests == []


def test_without_a_session_the_call_runs_in_the_default_one(client: TestClient, audit_lines: AuditLines) -> None:
    """``X-Session-Id`` is optional."""
    assert result(call(client, headers={**HD, "X-Session-Id": None}))["isError"] is False
    (event,) = inline_events(audit_lines)
    assert event["target"]["container"] == "chat:default"


@pytest.mark.parametrize("purpose", ["reason=%C3", "reason=Prüfung".encode(), "reason=a&reason=b"])
def test_a_malformed_purpose_is_invalid_params(
    client: TestClient, fake_executor: Scripted, purpose: str | bytes
) -> None:
    """Not form-encoded UTF-8, not ASCII, or a field twice: -32602 "invalid purpose", and nothing runs."""
    body = error(call(client, headers={**HD, "X-Audit-Purpose": purpose}), 400, -32602)
    assert body["error"]["message"] == "invalid purpose"
    assert fake_executor.requests == []


def test_a_stated_purpose_and_request_id_reach_the_event(client: TestClient, audit_lines: AuditLines) -> None:
    """The purpose as the vendored parser reads it; a request id only when it looks like one."""
    stated = {**HD, "X-Audit-Purpose": "case_ref=case-0001&reason=docint+chat", "X-Request-ID": "req-1"}
    assert result(call(client, headers=stated))["isError"] is False
    assert result(call(client, headers={**HD, "X-Request-ID": "not one/of ours"}))["isError"] is False
    first, second = inline_events(audit_lines)
    assert first["purpose"] == {"case_ref": "case-0001", "reason": "docint chat"}
    assert first["request_id"] == "req-1"
    assert "purpose" not in second and "request_id" not in second


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"code": "print(1)", "time_limit_s": 4},
        {"code": "print(1)", "time_limit_s": 61},
        {"code": "print(1)", "x": 1},
        {"code": "print(1)", "data": [{"name": "../passages.json", "content": "[]"}]},
        [],
        None,
    ],
)
def test_arguments_that_do_not_validate_are_an_error_result(
    client: TestClient, fake_executor: Scripted, audit_lines: AuditLines, arguments: object
) -> None:
    """A result the model can read: ``invalid_arguments``, where and why, never the value; nothing runs or is kept."""
    structured = refused(call(client, arguments))
    assert structured["reason"] == "invalid_arguments"
    assert structured["detail"]
    for entry in structured["detail"]:
        assert set(entry) == {"type", "loc", "msg"}
    assert fake_executor.requests == [] and inline_events(audit_lines) == []


def test_an_invalid_argument_is_never_quoted(client: TestClient) -> None:
    """The text and the detail name where and why, never the value sent."""
    marker = "zz-synthetic-marker"
    r = call(client, {"code": "print(1)", "data": [{"name": f"{marker}.xlsx", "content": marker}]})
    assert marker not in r.text


@pytest.mark.parametrize("reason", ["queue_full", "wait_timeout", "executor_busy"])
def test_a_queue_refusal_is_an_error_result_with_its_token(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch, reason: Any
) -> None:
    """The queue's own token, and the one short text for every refusal of the queue."""

    async def refuse(*_: object) -> Any:
        raise Busy(reason)

    monkeypatch.setattr(app.state.jobs, "submit_inline", refuse)
    r = call(client)
    assert refused(r) == {"reason": reason}
    assert result(r)["content"][0]["text"] == "busy, retry later"


def test_an_app_without_a_queue_is_executor_busy(client: TestClient, app: FastAPI) -> None:
    """No queue runs nothing."""
    app.state.jobs = None
    assert refused(call(client)) == {"reason": "executor_busy"}


def test_a_line_too_large_for_the_executor_is_too_large(
    settings: Settings, store: Store, audit: AuditLog, audit_lines: AuditLines, tmp_path: Path
) -> None:
    """512 KiB of quotes fits the body and the arguments, and doubles on the executor's line: ``too_large``."""
    queue = JobQueue(settings, ExecutorClient(tmp_path / "none.sock"), store, Metrics(settings.data_dir))
    data = [{"name": f"p{n}.json", "content": '"' * (64 * 1024)} for n in range(8)]
    with TestClient(create_app(settings, store=store, audit=audit, jobs=queue)) as client:
        r = call(client, {"code": "print(1)", "data": data})
    assert refused(r) == {"reason": "too_large"}
    assert result(r)["content"][0]["text"] == "request too large"
    assert inline_events(audit_lines) == []


def test_code_that_could_not_complete_is_not_completed(
    client: TestClient, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """Out of time: ``not_completed`` with the executor's reason, and the run is recorded."""
    fake_executor.status = "timeout"
    fake_executor.message = "the job ran past its wall time of 60 s"
    r = call(client)
    assert refused(r) == {"reason": "not_completed"}
    assert result(r)["content"][0]["text"] == "the code could not be completed: the job ran past its wall time of 60 s"
    (event,) = inline_events(audit_lines)
    assert event["criteria"]["refused"] == "not_completed"


@pytest.mark.parametrize(
    ("payload", "text"),
    [
        ({"error": "storage_full: storage is full"}, "storage_full: storage is full"),
        ({"error": "KeyError: '/data/jobs/0123/spec.json'"}, "the tool failed"),
    ],
)
def test_another_refusal_is_refused(
    client: TestClient, fake_executor: Scripted, audit_lines: AuditLines, payload: dict[str, str], text: str
) -> None:
    """The runner's known refusal, or its fixed text for any other: ``refused``, never a JSON-RPC error."""
    fake_executor.status = "error"
    fake_executor.payload = payload
    r = call(client)
    assert refused(r) == {"reason": "refused"}
    assert result(r)["content"][0]["text"] == text
    (event,) = inline_events(audit_lines)
    assert event["criteria"]["refused"] == "refused"


def test_an_answer_that_cannot_be_read_is_refused(client: TestClient, fake_executor: Scripted) -> None:
    """The runner's answer is not of its shape: ``refused``, as Task 5 tokenises it."""
    fake_executor.payload = {"stdout": 1}
    assert refused(call(client)) == {"reason": "refused"}


def test_a_missing_required_purpose_is_purpose_required_and_recorded_denied(
    settings: Settings, store: Store, audit: AuditLog, audit_lines: AuditLines, fake_executor: Scripted
) -> None:
    """With ``combine`` requiring a purpose, a call that states none runs nothing and is recorded as denied."""
    required = dataclasses.replace(settings, require_purpose=frozenset({"combine"}))
    queue = JobQueue(required, fake_executor, store, Metrics(settings.data_dir))
    with TestClient(create_app(required, store=store, audit=audit, jobs=queue)) as strict:
        r = call(strict, headers={**HD, "X-Request-ID": "req-1"})
        assert refused(r) == {"reason": "purpose_required"}
        assert result(r)["content"][0]["text"] == "a stated purpose is required"
        assert result(call(strict, headers={**HD, "X-Audit-Purpose": "reason=docint+chat"}))["isError"] is False
    denied, ran_once = inline_events(audit_lines)
    assert (denied["outcome"], denied["request_id"]) == ("denied", "req-1")
    assert ran_once["outcome"] == "ok"
    assert len(fake_executor.requests) == 1


def test_an_audit_failure_is_32603_audit_unavailable(
    broken_audit: None, client: TestClient, fake_executor: Scripted
) -> None:
    """Fail closed: no result, no content, the reason in the error's data."""
    fake_executor.files = {"counts.csv": COUNTS}
    body = error(call(client), 500, -32603)
    assert body["error"] == {"code": -32603, "message": "audit unavailable", "data": {"reason": "audit_unavailable"}}
    assert body["id"] == 1
    assert "user-a" not in json.dumps(body)


def test_anything_else_is_32603_internal_logged_by_method_and_type(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An error no rule expected: one log line by the method and the type, never its message."""

    async def fail(*_: object) -> Any:
        raise RuntimeError("/data/secret-path")

    monkeypatch.setattr(app.state.jobs, "submit_inline", fail)
    caplog.set_level(logging.DEBUG)
    body = error(call(client), 500, -32603)
    assert body["error"] == {"code": -32603, "message": "internal error", "data": {"reason": "internal"}}
    (line,) = [record.getMessage() for record in caplog.records if "unexpected" in record.getMessage()]
    assert "tools/call" in line and "RuntimeError" in line
    assert "secret-path" not in caplog.text


def test_a_method_name_is_never_logged_unless_served(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A method the door does not serve, a header's value or a tool's name a client chose reaches no log line."""
    marker = "zz-synthetic-marker"
    caplog.set_level(logging.DEBUG)
    rpc(client, f"{marker}/method")
    rpc(client, "tools/list", headers={**HD, "MCP-Protocol-Version": marker})
    call(client, headers={**HD, "X-User-Id": marker + "/x", "X-Audit-Purpose": marker})
    rpc(client, "tools/call", {"name": marker, "arguments": {}}, name=marker)

    def fail(*_: object) -> Any:
        raise RuntimeError(marker)

    monkeypatch.setattr(mcp, "select_era", fail)
    assert error(rpc(client, f"{marker}/method"), 500, -32603)["error"]["data"] == {"reason": "internal"}
    assert marker not in caplog.text
    assert "an unexpected error ended a request on /mcp (RuntimeError)" in caplog.text


def test_identity_never_comes_from_meta(client: TestClient, audit_lines: AuditLines) -> None:
    """A user or session in ``_meta``, which a model may shape, changes nothing: the headers name them."""
    meta = {
        "io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientCapabilities": NONE,
        "user": "user-b",
        "io.modelcontextprotocol/user": "user-b",
        "session": "chat-2",
    }
    assert result(call(client, meta=meta))["isError"] is False
    (event,) = inline_events(audit_lines)
    assert event["actor"]["user"] == "user-a"
    assert event["target"]["container"] == "chat:chat-1"
