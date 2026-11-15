"""Each caller is a row: its key, its door, its file form, its tools and its limits; the gate keeps it to its door."""

import asyncio
import dataclasses
import hmac
from urllib.parse import unquote

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from tests.api.conftest import DOCINT_KEY, HD, KEY
from tests.api.test_files import put
from workbench.api import auth
from workbench.api.audit import AuditLog
from workbench.api.auth import (
    OPEN_PATHS,
    Caller,
    CallerRegistry,
    InvalidPrincipal,
    KeyGate,
    door_allows,
    principal_from_headers,
    require_caller,
)
from workbench.api.main import create_app
from workbench.api.openapi import tool_openapi
from workbench.api.registry import WORKSPACE_TOOLS
from workbench.api.store import Store
from workbench.config import ConfigError, Settings
from workbench.models import Principal

H = {"Authorization": f"Bearer {KEY}", "X-User-Id": "user-a", "X-Session-Id": "chat-1"}
CHAT = "/workspace/chat-1"
DOCINT = Caller("docint", DOCINT_KEY, frozenset({"run_python"}), door="mcp", files="inline", wall_s=60, slots=2)
NOT_FOUND = (404, {"detail": "not found"})


# --- the door ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/files/list"),
        ("POST", "/files/upload"),
        ("POST", "/tools/run_python"),
        ("GET", "/openapi.json"),
        ("GET", "/api/config"),
        ("GET", "/api/v1/terminals/t/mcp"),
        ("GET", "/nowhere"),
    ],
)
def test_an_mcp_caller_finds_nothing_but_its_door(client: TestClient, method: str, path: str) -> None:
    """With docint's key, no terminal route, no handshake route and no path Open WebUI's proxy forwards is found."""
    r = client.request(method, path, headers=HD)
    assert (r.status_code, r.json()) == NOT_FOUND


@pytest.mark.parametrize("path", ["/healthz", "/metrics"])
def test_the_service_routes_stay_open_to_every_caller(client: TestClient, path: str) -> None:
    """The healthcheck and the scraper's route answer whatever key comes with the request."""
    assert client.get(path, headers=HD).status_code == 200


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_a_terminal_caller_finds_no_mcp(client: TestClient, method: str) -> None:
    """Open WebUI's key does not open the MCP door, whatever the method."""
    r = client.request(method, "/mcp", headers=H)
    assert (r.status_code, r.json()) == NOT_FOUND


@pytest.mark.parametrize(
    ("key", "path", "passes"),
    [
        (DOCINT_KEY, "/files/upload", False),
        (DOCINT_KEY, "/mcp", True),
        (KEY, "/mcp", False),
        (KEY, "/files/upload", True),
    ],
)
def test_the_door_answers_before_any_body_is_read(settings: Settings, key: str, path: str, passes: bool) -> None:
    """A path the caller's door does not allow is refused before the first body chunk is asked for."""
    asked: list[str] = []

    async def inner(_scope: Scope, _receive: Receive, _send: Send) -> None:
        asked.append("app")

    async def receive() -> Message:
        asked.append("body")
        return {"type": "http.request", "body": b"x" * 1024, "more_body": True}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [
            (b"authorization", f"Bearer {key}".encode()),
            (b"content-length", str(40 * 1024 * 1024).encode()),
        ],
    }
    asyncio.run(KeyGate(inner, callers=CallerRegistry.from_settings(settings))(scope, receive, send))
    assert asked == (["app"] if passes else [])
    if not passes:
        assert sent[0]["status"] == 404


def _passes_the_gate(callers: CallerRegistry, method: str, target: str, key: str) -> bool:
    """Send one request through :class:`KeyGate` and say whether it reached the app behind it.

    The scope is built from the request target as uvicorn builds it: ``path`` percent-decoded, the query apart.
    """
    reached: list[str] = []

    async def inner(scope: Scope, _receive: Receive, _send: Send) -> None:
        reached.append(scope["path"])

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: Message) -> None:
        return None

    raw_path, _, query = target.partition("?")
    scope: Scope = {
        "type": "http",
        "method": method,
        "path": unquote(raw_path),
        "raw_path": raw_path.encode(),
        "query_string": query.encode(),
        "headers": [(b"authorization", f"Bearer {key}".encode())],
    }
    asyncio.run(KeyGate(inner, callers=callers)(scope, receive, send))
    return bool(reached)


@pytest.mark.parametrize(
    ("method", "target", "docint", "open_webui"),
    [
        ("POST", "/MCP", False, True),
        ("POST", "//mcp", False, True),
        ("POST", "/%6Dcp", True, False),
        ("POST", "/mcp?x=1", True, False),
        ("POST", "/mcp/", False, True),
        ("HEAD", "/mcp", True, False),
        ("OPTIONS", "/mcp", True, False),
        ("HEAD", "/files/list", False, True),
        ("OPTIONS", "/files/list", False, True),
    ],
)
def test_the_gate_judges_the_path_routing_will_see(
    settings: Settings, method: str, target: str, docint: bool, open_webui: bool
) -> None:
    """Each form of a path reaches the app for the caller whose door it is, whatever the method, and for no other."""
    callers = CallerRegistry.from_settings(settings)
    assert _passes_the_gate(callers, method, target, DOCINT_KEY) is docint
    assert _passes_the_gate(callers, method, target, KEY) is open_webui


def test_the_routes_own_key_check_refuses_a_caller_off_the_terminal_door(app: FastAPI) -> None:
    """The check every terminal route depends on answers docint's key with the gate's 404 and lets Open WebUI's by."""

    def request(key: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/files/list",
                "headers": [(b"authorization", f"Bearer {key}".encode())],
                "app": app,
            }
        )

    with pytest.raises(HTTPException) as refused:
        asyncio.run(require_caller(request(DOCINT_KEY)))
    assert (refused.value.status_code, refused.value.detail) == (404, "not found")
    assert asyncio.run(require_caller(request(KEY))).name == "open-webui"


def test_without_the_gate_every_keyed_route_refuses_docints_key(app: FastAPI) -> None:
    """Were :class:`KeyGate` bypassed, every keyed route still refuses docint's key: file browser, handshake, tools."""
    app.user_middleware = [middleware for middleware in app.user_middleware if middleware.cls is not KeyGate]
    routes = sorted(
        (method, path)
        for route in iter_route_contexts(app.routes)
        if (path := route.path) is not None and path not in OPEN_PATHS
        for method in route.methods or ()
    )
    assert ("POST", "/tools/run_python") in routes and ("GET", "/openapi.json") in routes
    with TestClient(app) as client:
        assert client.get("/nowhere").status_code == 404  # the gate would answer 401: it is really out
        for method, path in routes:
            r = client.request(method, path, headers=HD)
            assert (r.status_code, r.json()) == NOT_FOUND, (method, path)


@pytest.mark.parametrize("headers", [H, HD], ids=["open-webui", "docint"])
@pytest.mark.parametrize(
    ("method", "path"), [("GET", "/nowhere"), ("POST", "/tools/nothing"), ("GET", "/api/v1/policies")]
)
def test_an_unrouted_path_answers_one_404_body_with_either_key(
    client: TestClient, headers: dict[str, str], method: str, path: str
) -> None:
    """A path no route serves is the door's 404 too, so the text does not tell ``/mcp`` apart from the others."""
    r = client.request(method, path, headers=headers)
    assert (r.status_code, r.json()) == NOT_FOUND


def test_an_open_webui_user_id_with_docints_key_reaches_nothing(client: TestClient, store: Store) -> None:
    """Sending Open WebUI's user and chat ids, docint still sees none of that user's files."""
    put(store, "q.csv")
    path = f"{CHAT}/q.csv"
    for route, params in (
        ("/files/list", {"directory": CHAT}),
        ("/files/view", {"path": path}),
        ("/files/display", {"path": path}),
    ):
        r = client.get(route, params=params, headers=HD)
        assert (r.status_code, r.json()) == NOT_FOUND, route
    listing = client.get("/files/list", params={"directory": CHAT}, headers=H).json()
    assert [entry["name"] for entry in listing["entries"]] == ["q.csv"]


@pytest.mark.parametrize(
    ("caller", "path", "allowed"),
    [
        (DOCINT, "/mcp", True),
        (DOCINT, "/mcp/", False),
        (DOCINT, "/files/list", False),
        (DOCINT, "/", False),
        (Caller("open-webui", KEY, WORKSPACE_TOOLS), "/mcp", False),
        (Caller("open-webui", KEY, WORKSPACE_TOOLS), "/files/list", True),
        (Caller("open-webui", KEY, WORKSPACE_TOOLS), "/api/v1/terminals/t/mcp", True),
    ],
)
def test_door_allows_one_path_to_mcp_and_every_other_to_terminal(caller: Caller, path: str, allowed: bool) -> None:
    """An MCP caller has ``/mcp`` alone; a terminal caller every path but that one."""
    assert door_allows(caller, path) is allowed


# --- the registry --------------------------------------------------------------------------------------------------


def test_the_registry_holds_a_row_per_key(settings: Settings) -> None:
    """Open WebUI on the terminal door with every tool; docint on the MCP door, inline, with its own limits."""
    registry = CallerRegistry.from_settings(settings)

    def row(caller: Caller) -> tuple[object, ...]:
        return (caller.door, caller.files, caller.tools, caller.wall_s, caller.slots)

    assert [caller.name for caller in registry.callers] == ["open-webui", "docint"]
    assert row(registry.get("open-webui")) == ("terminal", "workspace", WORKSPACE_TOOLS, 120, None)
    assert row(registry.get("docint")) == ("mcp", "inline", frozenset({"run_python"}), 60, 2)
    assert registry.get("open-webui").key == KEY
    assert registry.get("docint").key == DOCINT_KEY
    assert KEY not in repr(registry.callers)
    assert DOCINT_KEY not in repr(registry.callers)
    with pytest.raises(KeyError):
        registry.get("nobody")


def test_each_key_alone_makes_its_one_row(settings: Settings) -> None:
    """A host with one caller's key has that caller alone; with neither the registry is not built."""
    only_docint = CallerRegistry.from_settings(dataclasses.replace(settings, open_webui_key=None))
    assert [caller.name for caller in only_docint.callers] == ["docint"]
    only_open_webui = CallerRegistry.from_settings(dataclasses.replace(settings, docint_key=None))
    assert [caller.name for caller in only_open_webui.callers] == ["open-webui"]
    with pytest.raises(ConfigError, match="OPEN_WEBUI_WORKBENCH_KEY or DOCINT_WORKBENCH_KEY"):
        CallerRegistry.from_settings(dataclasses.replace(settings, open_webui_key=None, docint_key=None))


def test_two_callers_with_one_key_stop_the_start(settings: Settings) -> None:
    """A key held twice would make the caller depend on the order of the rows: the start stops, naming no key."""
    with pytest.raises(ConfigError) as refused:
        CallerRegistry.from_settings(dataclasses.replace(settings, docint_key=KEY))
    assert str(refused.value) == "two callers hold the same key"
    assert KEY not in str(refused.value)


@pytest.mark.parametrize(("key", "name"), [(KEY, "open-webui"), (DOCINT_KEY, "docint"), ("w" * 32, None)])
def test_authentication_compares_every_caller(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, key: str, name: str | None
) -> None:
    """Every caller's key is compared, whichever matches or none, so the time taken does not show which one did."""
    calls: list[None] = []
    compare_digest = hmac.compare_digest

    def counted(a: bytes, b: bytes) -> bool:
        calls.append(None)
        return compare_digest(a, b)

    registry = CallerRegistry.from_settings(settings)
    monkeypatch.setattr(auth.hmac, "compare_digest", counted)
    found = registry.authenticate(key)
    assert len(calls) == 2
    assert (found.name if found is not None else None) == name


# --- the user and the chat -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ({}, "missing or invalid user"),
        ({"X-User-Id": "../x"}, "missing or invalid user"),
        ({"X-User-Id": "user-a", "X-Session-Id": "a/b"}, "invalid session"),
    ],
)
def test_principal_from_headers_refuses_with_the_doors_texts(headers: dict[str, str], message: str) -> None:
    """The terminal door's two texts, for both doors."""
    with pytest.raises(InvalidPrincipal) as refused:
        principal_from_headers(DOCINT, headers)
    assert str(refused.value) == message


def test_principal_from_headers_names_the_caller_and_defaults_the_chat() -> None:
    """The caller comes from the key, never from a header; a request without a chat is in ``default``."""
    assert principal_from_headers(DOCINT, {"X-User-Id": "user-a"}) == Principal("docint", "user-a", "default")
    assert principal_from_headers(DOCINT, {"x-user-id": "user-a", "x-session-id": "chat-1"}) == Principal(
        "docint", "user-a", "chat-1"
    )


# --- the tool list -------------------------------------------------------------------------------------------------


def test_a_reduced_list_is_refused_by_require_tool(
    settings: Settings, store: Store, audit: AuditLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool off the caller's list is the door's 404 and is not in its document; the rest of the list still works."""

    def reduced(_settings: Settings) -> CallerRegistry:
        return CallerRegistry([Caller("open-webui", KEY, WORKSPACE_TOOLS - {"read_document"})])

    monkeypatch.setattr(CallerRegistry, "from_settings", reduced)
    with TestClient(create_app(settings, store=store, audit=audit)) as client:
        r = client.post("/tools/read_document", json={"path": f"{CHAT}/q.csv"}, headers=H)
        assert (r.status_code, r.json()) == NOT_FOUND
        doc = client.get("/openapi.json", headers=H).json()
        assert {op["operationId"] for item in doc["paths"].values() for op in item.values()} == WORKSPACE_TOOLS - {
            "read_document"
        }
        assert client.post("/tools/list_files", headers=H).status_code == 200


def test_each_caller_gets_its_own_document(app: FastAPI) -> None:
    """Open WebUI's holds the nine tools and no header parameter; docint's holds no path; each is built once."""
    callers: CallerRegistry = app.state.callers
    open_webui, docint = callers.get("open-webui"), callers.get("docint")
    doc = tool_openapi(app, open_webui)
    operations = [op for item in doc["paths"].values() for op in item.values()]
    assert len(operations) == 9 and {op["operationId"] for op in operations} == WORKSPACE_TOOLS
    assert [param for op in operations for param in op.get("parameters", []) if param["in"] == "header"] == []
    assert tool_openapi(app, docint)["paths"] == {}
    assert tool_openapi(app, open_webui) is doc
    assert tool_openapi(app, docint) is tool_openapi(app, docint)
