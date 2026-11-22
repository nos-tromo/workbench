"""Each caller is a row: its key, its file form, its tools and its limits; the gate keeps it to its paths."""

import asyncio
import dataclasses
import hmac
import json
from urllib.parse import unquote

import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from tests.api.conftest import CHAT, DOCINT_KEY, GHOSTWRITER_KEY, HD, HG, PG, AuditLines, put
from tests.api.mcp_calls import acall, error, result, rpc
from tests.callers import GHOSTWRITER_TOOLS
from workbench.api import auth
from workbench.api.audit import AuditLog
from workbench.api.auth import (
    MCP_PATH,
    OPEN_PATHS,
    Caller,
    CallerRegistry,
    InvalidPrincipal,
    KeyGate,
    door_allows,
    principal_from_headers,
)
from workbench.api.main import create_app
from workbench.api.registry import WORKSPACE_TOOLS
from workbench.api.store import Store
from workbench.config import NO_CALLER_KEY, ConfigError, Settings
from workbench.models import Principal

DOCINT = Caller("docint", DOCINT_KEY, frozenset({"run_python"}), files="inline", wall_s=60, slots=2)
GHOSTWRITER = Caller("ghostwriter", GHOSTWRITER_KEY, GHOSTWRITER_TOOLS, files="workspace", wall_s=120, slots=2)
NOT_FOUND = (404, {"detail": "not found"})


# --- the door ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/files/list"),
        ("POST", "/files/upload"),
        ("GET", "/nowhere"),
    ],
)
def test_an_mcp_caller_finds_nothing_but_its_door(client: TestClient, method: str, path: str) -> None:
    """With docint's key, no file route and no other path is found."""
    r = client.request(method, path, headers=HD)
    assert (r.status_code, r.json()) == NOT_FOUND


@pytest.mark.parametrize("path", ["/healthz", "/metrics"])
def test_the_service_routes_stay_open_to_every_caller(client: TestClient, path: str) -> None:
    """The healthcheck and the scraper's route answer whatever key comes with the request, a wrong one too."""
    assert client.get(path, headers=HD).status_code == 200
    assert client.get(path, headers={"Authorization": "Bearer " + "w" * 43}).status_code == 200


def _through_the_gate(settings: Settings, key: str, path: str) -> tuple[list[str], list[Message]]:
    """POST to ``path`` through :class:`KeyGate` with ``key`` and a 40 MiB ``Content-Length``.

    Returns:
        What the gate asked for, the app or the body, in order, and the messages it sent.
    """
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
    return asked, sent


@pytest.mark.parametrize(
    ("key", "path", "passes"),
    [
        (DOCINT_KEY, "/files/upload", False),
        (DOCINT_KEY, "/mcp", True),
        (GHOSTWRITER_KEY, "/mcp", True),
    ],
)
def test_the_door_answers_before_any_body_is_read(settings: Settings, key: str, path: str, passes: bool) -> None:
    """A path the caller's door does not allow is refused before the first body chunk is asked for."""
    asked, sent = _through_the_gate(settings, key, path)
    assert asked == (["app"] if passes else [])
    if not passes:
        assert sent[0]["status"] == 404


@pytest.mark.parametrize(
    ("key", "path", "passes"),
    [
        (GHOSTWRITER_KEY, "/files/upload", True),
        (GHOSTWRITER_KEY, "/files/prepare", True),
        (GHOSTWRITER_KEY, "/files/other", False),
        (GHOSTWRITER_KEY, "/nowhere", False),
        (DOCINT_KEY, "/files/chat", False),
        (DOCINT_KEY, "/files/prepare", False),
    ],
)
def test_ghostwriters_door_answers_before_any_body_is_read(
    settings: Settings, key: str, path: str, passes: bool
) -> None:
    """Ghostwriter's file routes pass the gate; its other paths, and the chat delete for anyone else, do not."""
    asked, sent = _through_the_gate(settings, key, path)
    assert asked == (["app"] if passes else [])
    if not passes:
        assert sent[0]["status"] == 404
        assert json.loads(sent[1]["body"]) == {"detail": "not found"}


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
    ("method", "target", "docint", "ghostwriter"),
    [
        ("POST", "/MCP", False, False),
        ("POST", "//mcp", False, False),
        ("POST", "/%6Dcp", True, True),
        ("POST", "/mcp?x=1", True, True),
        ("POST", "/mcp/", False, False),
        ("HEAD", "/mcp", True, True),
        ("OPTIONS", "/mcp", True, True),
        ("HEAD", "/files/list", False, True),
        ("OPTIONS", "/files/list", False, True),
        ("POST", "/Files/upload", False, False),
        ("POST", "//files/upload", False, False),
        ("POST", "/files/upload/", False, False),
        ("POST", "/files/./upload", False, False),
        ("POST", "/files/upload%2F", False, False),
        ("POST", "/files/%75pload", False, True),
        ("DELETE", "/files/%63hat", False, True),
    ],
)
def test_the_gate_judges_the_path_routing_will_see(
    settings: Settings, method: str, target: str, docint: bool, ghostwriter: bool
) -> None:
    """Each form of a path reaches the app for the caller who may reach it, whatever the method, and for no other."""
    callers = CallerRegistry.from_settings(settings)
    assert _passes_the_gate(callers, method, target, DOCINT_KEY) is docint
    assert _passes_the_gate(callers, method, target, GHOSTWRITER_KEY) is ghostwriter


def test_without_the_gate_every_keyed_route_refuses_docints_key(app: FastAPI) -> None:
    """Were :class:`KeyGate` bypassed, every file route still refuses docint's key, the chat delete included.

    The MCP door's own route refuses a request under a root path the same way (``tests/api/test_mcp.py``).
    """
    app.user_middleware = [middleware for middleware in app.user_middleware if middleware.cls is not KeyGate]
    routes = sorted(
        (method, path)
        for route in iter_route_contexts(app.routes)
        if (path := route.path) is not None and path not in OPEN_PATHS | {MCP_PATH}
        for method in route.methods or ()
    )
    assert ("GET", "/files/list") in routes and ("DELETE", "/files/chat") in routes
    with TestClient(app) as client:
        assert client.get("/nowhere").status_code == 404  # the gate would answer 401: it is really out
        for method, path in routes:
            r = client.request(method, path, headers=HD)
            assert (r.status_code, r.json()) == NOT_FOUND, (method, path)


@pytest.mark.parametrize("headers", [HG, HD], ids=["ghostwriter", "docint"])
@pytest.mark.parametrize(("method", "path"), [("GET", "/nowhere"), ("POST", "/no/such/route"), ("GET", "/files/other")])
def test_an_unrouted_path_answers_one_404_body_with_either_key(
    client: TestClient, audit_lines: AuditLines, headers: dict[str, str], method: str, path: str
) -> None:
    """A path no route serves is the door's 404 too, so the text does not tell ``/mcp`` apart from the others.

    Nothing is recorded.
    """
    started = audit_lines()
    r = client.request(method, path, headers=headers)
    assert (r.status_code, r.json()) == NOT_FOUND
    assert audit_lines() == started


def test_ghostwriters_user_id_with_docints_key_reaches_nothing(client: TestClient, store: Store) -> None:
    """Sending Ghostwriter's user and chat ids, docint still sees none of that user's files."""
    put(store, "q.csv", p=PG)
    path = f"{CHAT}/q.csv"
    for route, params in (("/files/list", {"directory": CHAT}), ("/files/view", {"path": path})):
        r = client.get(route, params=params, headers=HD)
        assert (r.status_code, r.json()) == NOT_FOUND, route
    listing = client.get("/files/list", params={"directory": CHAT}, headers=HG).json()
    assert [entry["name"] for entry in listing["entries"]] == ["q.csv"]


@pytest.mark.parametrize(
    ("caller", "path", "allowed"),
    [
        (DOCINT, "/mcp", True),
        (DOCINT, "/mcp/", False),
        (DOCINT, "/files/list", False),
        (DOCINT, "/other/mcp", False),
        (DOCINT, "/", False),
    ],
)
def test_door_allows_a_caller_with_inline_data_mcp_alone(caller: Caller, path: str, allowed: bool) -> None:
    """A caller with inline data has ``/mcp`` alone."""
    assert door_allows(caller, path) is allowed


@pytest.mark.parametrize(
    ("caller", "path", "allowed"),
    [
        *[
            (GHOSTWRITER, path, True)
            for path in ["/mcp", "/files/upload", "/files/prepare", "/files/view", "/files/list", "/files/chat"]
        ],
        *[
            (GHOSTWRITER, path, False)
            for path in ["/files", "/files/", "/files/other", "/files/listing", "/mcp/", "/nowhere"]
        ],
        (DOCINT, "/files/chat", False),
        (DOCINT, "/files/upload", False),
        (DOCINT, "/files/prepare", False),
    ],
)
def test_door_allows_the_ghostwriter_its_door_and_five_file_routes(caller: Caller, path: str, allowed: bool) -> None:
    """Ghostwriter has ``/mcp`` and the five file routes; the chat delete is no other caller's."""
    assert door_allows(caller, path) is allowed


# --- the registry --------------------------------------------------------------------------------------------------


def test_the_registry_holds_a_row_per_key(settings: Settings) -> None:
    """Two rows, both on the MCP door: docint with inline data, Ghostwriter with files; each with its own caps."""
    registry = CallerRegistry.from_settings(settings)

    def row(caller: Caller) -> tuple[object, ...]:
        return (caller.files, caller.tools, caller.wall_s, caller.slots)

    assert [caller.name for caller in registry.callers] == ["docint", "ghostwriter"]
    assert row(registry.get("docint")) == ("inline", frozenset({"run_python"}), 60, 2)
    assert row(registry.get("ghostwriter")) == ("workspace", GHOSTWRITER_TOOLS, 120, 3)
    assert registry.get("docint").key == DOCINT_KEY
    assert registry.get("ghostwriter").key == GHOSTWRITER_KEY
    assert registry.authenticate("wrong") is None
    assert registry.authenticate("") is None
    assert DOCINT_KEY not in repr(registry.callers)
    assert GHOSTWRITER_KEY not in repr(registry.callers)
    with pytest.raises(KeyError):
        registry.get("nobody")


def test_only_the_ghostwriter_is_confined(settings: Settings) -> None:
    """The one caller with workspace files is held to its chat; docint, with inline data, is not."""
    assert [c.name for c in CallerRegistry.from_settings(settings).callers if c.confined] == ["ghostwriter"]


def test_each_key_alone_makes_its_one_row(settings: Settings) -> None:
    """A host with one caller's key has that caller alone; with none the registry is not built, naming both keys.

    The executor's settings carry no key, so the api refuses to build a registry from them.
    """
    only_docint = CallerRegistry.from_settings(dataclasses.replace(settings, ghostwriter_key=None))
    assert [caller.name for caller in only_docint.callers] == ["docint"]
    only_ghostwriter = CallerRegistry.from_settings(dataclasses.replace(settings, docint_key=None))
    assert [caller.name for caller in only_ghostwriter.callers] == ["ghostwriter"]
    with pytest.raises(ConfigError) as refused:
        CallerRegistry.from_settings(dataclasses.replace(settings, docint_key=None, ghostwriter_key=None))
    assert str(refused.value) == NO_CALLER_KEY
    assert "DOCINT_WORKBENCH_KEY or GHOSTWRITER_WORKBENCH_KEY" in NO_CALLER_KEY


@pytest.mark.parametrize("taken", ["docint", "ghostwriter"])
def test_two_callers_with_one_key_stop_the_start(settings: Settings, taken: str) -> None:
    """A key held twice would make the caller depend on the order of the rows: the start stops, naming no key.

    Either caller may be the one whose key the other's variable was set to.
    """
    if taken == "docint":
        twice, key = dataclasses.replace(settings, ghostwriter_key=DOCINT_KEY), DOCINT_KEY
    else:
        twice, key = dataclasses.replace(settings, docint_key=GHOSTWRITER_KEY), GHOSTWRITER_KEY
    with pytest.raises(ConfigError) as refused:
        CallerRegistry.from_settings(twice)
    assert str(refused.value) == "two callers hold the same key"
    assert key not in str(refused.value)


@pytest.mark.parametrize("tools", [frozenset({"nowhere"}), frozenset({"list_files", "nowhere"})])
def test_a_tool_ghostwriters_door_cannot_serve_stops_the_start(settings: Settings, tools: frozenset[str]) -> None:
    """A name no tool has on Ghostwriter's list stops the start.

    The message names the variable and the tools it may list, never a name the variable was set to.
    """
    with pytest.raises(ConfigError, match="GHOSTWRITER_WORKBENCH_TOOLS") as refused:
        CallerRegistry.from_settings(dataclasses.replace(settings, ghostwriter_tools=tools))
    assert [name for name in tools - WORKSPACE_TOOLS if name in str(refused.value)] == []


@pytest.mark.parametrize(
    ("key", "name"),
    [(DOCINT_KEY, "docint"), (GHOSTWRITER_KEY, "ghostwriter"), ("w" * 32, None)],
)
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
    """The two texts, whichever caller sent the headers."""
    with pytest.raises(InvalidPrincipal) as refused:
        principal_from_headers(DOCINT, headers)
    assert str(refused.value) == message


def test_principal_from_headers_names_the_caller_and_defaults_the_chat() -> None:
    """The caller comes from the key, never from a header; a request without a chat is in ``default``."""
    assert principal_from_headers(DOCINT, {"X-User-Id": "user-a"}) == Principal("docint", "user-a", "default")
    assert principal_from_headers(DOCINT, {"x-user-id": "user-a", "x-session-id": "chat-1"}) == Principal(
        "docint", "user-a", "chat-1"
    )


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ({"X-User-Id": "user-a"}, "invalid session"),
        ({"X-User-Id": "user-a", "X-Session-Id": "default"}, "invalid session"),
        ({"X-Session-Id": "chat-1"}, "missing or invalid user"),
    ],
)
def test_the_ghostwriter_needs_a_chat_of_its_own(headers: dict[str, str], message: str) -> None:
    """Ghostwriter's chat is never ``default``, so none of its files can land in a folder its chats share."""
    with pytest.raises(InvalidPrincipal) as refused:
        principal_from_headers(GHOSTWRITER, headers)
    assert str(refused.value) == message


def test_ghostwriters_principal_is_confined() -> None:
    """Its principal carries the confinement to the chat it names."""
    assert principal_from_headers(GHOSTWRITER, {"X-User-Id": "user-a", "X-Session-Id": "chat-1"}) == PG


def _refused_on_ghostwriters_routes(
    client: TestClient, audit_lines: AuditLines, headers: dict[str, str], message: str
) -> None:
    """The file routes answer 400 with ``message``, the MCP door ``-32600`` with it, and nothing is recorded."""
    started = audit_lines()
    r = client.get("/files/list", params={"directory": CHAT}, headers=headers)
    assert (r.status_code, r.json()) == (400, {"detail": message})
    r = client.get("/files/view", params={"path": f"{CHAT}/q.csv"}, headers=headers)
    assert (r.status_code, r.json()) == (400, {"detail": message})
    assert error(acall(client, "list_files", headers=headers), 400, -32600)["error"]["message"] == message
    assert audit_lines() == started


@pytest.mark.parametrize("user", ["../x", "", " ", "a b", "x/y", ".hidden", "a" * 129])
def test_a_user_header_that_is_not_a_plain_id_is_refused(
    client: TestClient, audit_lines: AuditLines, user: str
) -> None:
    """The user id becomes a folder name, so anything that is not a plain id is refused, on Ghostwriter's paths."""
    _refused_on_ghostwriters_routes(client, audit_lines, {**HG, "X-User-Id": user}, "missing or invalid user")


def test_the_user_header_is_required(client: TestClient, audit_lines: AuditLines) -> None:
    """Every route but ``/healthz`` and ``/metrics`` needs a user."""
    headers = {name: value for name, value in HG.items() if name != "X-User-Id"}
    _refused_on_ghostwriters_routes(client, audit_lines, headers, "missing or invalid user")


@pytest.mark.parametrize("session", ["../x", "", "a b", "a/b", "a" * 129])
def test_a_session_header_that_is_not_a_plain_id_is_refused(
    client: TestClient, audit_lines: AuditLines, session: str
) -> None:
    """The chat id becomes a folder name too."""
    _refused_on_ghostwriters_routes(client, audit_lines, {**HG, "X-Session-Id": session}, "invalid session")


# --- the tool list -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("full", [False, True], ids=["reduced", "full"])
@pytest.mark.parametrize("tool", sorted(GHOSTWRITER_TOOLS))
def test_a_reduced_list_is_refused_on_the_mcp_door(
    settings: Settings, store: Store, audit: AuditLog, tool: str, full: bool
) -> None:
    """Each tool is an unknown tool to a list without it, never to one with it; the tool list follows the list."""
    listed = GHOSTWRITER_TOOLS if full else GHOSTWRITER_TOOLS - {tool}
    app = create_app(dataclasses.replace(settings, ghostwriter_tools=listed), store=store, audit=audit)
    with TestClient(app) as client:
        r = acall(client, tool, {})
        if full:
            assert "error" not in r.json(), r.json()
        else:
            assert error(r, 400, -32602)["error"]["message"] == "unknown tool"
        assert {t["name"] for t in result(rpc(client, "tools/list", headers=HG))["tools"]} == listed
