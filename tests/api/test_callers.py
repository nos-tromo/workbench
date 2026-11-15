"""Each caller is a row: its key, its door, its file form, its tools and its limits; the gate keeps it to its door."""

import asyncio
import dataclasses
import hmac

import pytest
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from tests.api.conftest import DOCINT_KEY, HD, KEY
from tests.api.test_files import put
from workbench.api import auth
from workbench.api.auth import (
    ALL_TOOLS,
    Caller,
    CallerRegistry,
    InvalidPrincipal,
    KeyGate,
    door_allows,
    principal_from_headers,
)
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
        (Caller("open-webui", KEY, ALL_TOOLS), "/mcp", False),
        (Caller("open-webui", KEY, ALL_TOOLS), "/files/list", True),
        (Caller("open-webui", KEY, ALL_TOOLS), "/api/v1/terminals/t/mcp", True),
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
    assert row(registry.get("open-webui")) == ("terminal", "workspace", ALL_TOOLS, 120, None)
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
