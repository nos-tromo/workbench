"""The app's shared machinery: the key gate, the request bound, service routes, store errors, start and factory."""

import asyncio
import hmac
import io
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from tests.api.conftest import CHAT, DOCINT_KEY, GHOSTWRITER_KEY, HG
from tests.api.mcp_calls import send
from tests.callers import EIGHT
from workbench.api import auth
from workbench.api.audit import AuditLog
from workbench.api.auth import CallerRegistry, KeyGate
from workbench.api.main import app_factory, create_app
from workbench.api.store import Conflict, NotFound, QuotaExceeded, StorageFull, Store, TooLarge
from workbench.config import ConfigError, Settings
from workbench.paths import PathError

KEYED_ROUTES = [
    ("POST", "/mcp"),
    ("GET", "/files/list?directory=/workspace/chat-1"),
    ("POST", "/files/upload?directory=/workspace/chat-1"),
    ("GET", "/files/view?path=/workspace/chat-1/a.txt"),
    ("DELETE", "/files/chat"),
]


BAD_AUTHORIZATION: list[dict[str, str]] = [
    {},
    {"Authorization": "Bearer wrong"},
    {"Authorization": f"Bearer {GHOSTWRITER_KEY}x"},
    {"Authorization": f"Bearer {GHOSTWRITER_KEY[:-1]}"},
    {"Authorization": GHOSTWRITER_KEY},
    {"Authorization": f"Basic {GHOSTWRITER_KEY}"},
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer "},
]


@pytest.mark.parametrize(("method", "url"), KEYED_ROUTES)
@pytest.mark.parametrize("headers", BAD_AUTHORIZATION)
def test_wrong_or_missing_key_is_refused(client: TestClient, method: str, url: str, headers: dict[str, str]) -> None:
    """Anything but ``Bearer <key>`` gets the same 401, on every keyed route."""
    r = client.request(method, url, headers={**headers, "X-User-Id": "user-a"}, json={"path": "/workspace"})
    assert r.status_code == 401
    assert r.json() == {"detail": "missing or invalid key"}
    assert r.headers["www-authenticate"] == "Bearer"


def test_the_scheme_is_matched_without_regard_to_case(client: TestClient) -> None:
    """RFC 9110: the authentication scheme is case-insensitive."""
    lower = {**HG, "Authorization": f"bearer {GHOSTWRITER_KEY}"}
    assert client.get("/files/list", params={"directory": CHAT}, headers=lower).status_code == 200


def test_the_key_is_compared_in_constant_time(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The presented key goes through ``hmac.compare_digest``, never ``==``: at the gate, then at the route."""
    seen: list[tuple[bytes, bytes]] = []
    compare_digest = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        seen.append((a, b))
        return compare_digest(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    assert client.get("/files/list", params={"directory": CHAT}, headers=HG).status_code == 200
    compared = [(GHOSTWRITER_KEY, DOCINT_KEY), (GHOSTWRITER_KEY, GHOSTWRITER_KEY)]
    assert seen == [(a.encode(), b.encode()) for a, b in compared] * 2


def test_the_key_is_checked_before_the_user(client: TestClient) -> None:
    """A caller without the key learns nothing about the other headers."""
    for path in ("/files/list", "/files/view", "/files/chat"):
        r = client.get(path, headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401, path


def test_headers_that_are_not_ascii_are_refused_not_crashed(client: TestClient) -> None:
    """A byte outside ASCII in a key or an id is an ordinary refusal."""
    listing = {"directory": CHAT}
    key = client.get("/files/list", params=listing, headers={b"authorization": "Bearer k\u00e9y".encode("latin-1")})
    assert key.status_code == 401
    session = {b"x-session-id": b"chat-1"}
    for name in (b"x-user-id", b"x-session-id"):
        headers = {b"authorization": f"Bearer {GHOSTWRITER_KEY}".encode(), b"x-user-id": b"user-a", **session}
        headers[name] = "u\u00e4".encode()
        r = client.get("/files/list", params=listing, headers=headers)
        assert (r.status_code, r.json()) == (
            400,
            {"detail": "invalid session" if name == b"x-session-id" else "missing or invalid user"},
        )


@pytest.mark.parametrize(
    "path",
    [
        "/nowhere",
        "/files/other",
        "/docs",
        "/redoc",
        "/openapi",
    ],
)
def test_unknown_paths_are_not_found(client: TestClient, path: str) -> None:
    """The allow-list is workbench's: everything not listed answers 404 to a caller, 401 to anyone without the key."""
    assert client.get(path, headers=HG).status_code == 404
    assert client.get(path).status_code == 401


#: A path no route serves, a route's other method, and routes that read a JSON body.
GATED = [
    ("GET", "/files/other"),
    ("PUT", "/files/upload"),
    ("DELETE", "/files/list"),
    ("POST", "/mcp"),
    ("POST", "/files/upload"),
]


@pytest.mark.parametrize(("method", "path"), GATED)
def test_without_a_key_every_request_is_401_whatever_its_body(client: TestClient, method: str, path: str) -> None:
    """A malformed body, an unknown path or method: no key, no 404, 405 or 422 that would map the routes (R1 M-7)."""
    nothing: dict[str, str] = {}
    for headers in (nothing, {"Authorization": "Bearer wrong-key-of-thirty-two-characters!!"}):
        r = client.request(method, path, content=b"{not json", headers={**headers, "Content-Type": "application/json"})
        assert (r.status_code, r.json()) == (401, {"detail": "missing or invalid key"})
        assert r.headers["www-authenticate"] == "Bearer"


def test_the_gate_reads_no_body_without_a_key(settings: Settings) -> None:
    """The refusal comes before the first body chunk is asked for, however large the body says it is."""
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
        "path": "/mcp",
        "headers": [(b"content-length", str(40 * 1024 * 1024).encode())],
    }
    asyncio.run(KeyGate(inner, callers=CallerRegistry.from_settings(settings))(scope, receive, send))
    assert asked == []
    assert sent[0]["status"] == 401


@pytest.mark.parametrize("path", ["/healthz", "/metrics"])
def test_the_service_routes_need_no_key(client: TestClient, path: str) -> None:
    """The healthcheck and obs-plane's scraper have no key."""
    assert client.get(path).status_code == 200


def test_a_request_body_over_2_mib_is_413_and_never_parsed(client: TestClient) -> None:
    """With the key, a body past the request bound is refused as it arrives: declared or chunked (R74).

    A file route answers the plain refusal; the MCP door answers it as JSON-RPC, also for a body that grows past it.
    """
    big = b'{"path": "' + b"x" * (2 * 1024 * 1024) + b'"}'
    r = client.request("DELETE", "/files/chat", content=big, headers={**HG, "Content-Type": "application/json"})
    assert (r.status_code, r.json()) == (413, {"detail": "request too large"})

    def chunks() -> Iterator[bytes]:
        yield b'{"path": "'
        for _ in range(3):
            yield b"x" * (1024 * 1024)
        yield b'"}'

    too_large = {"jsonrpc": "2.0", "error": {"code": -32600, "message": "request too large"}}
    for content in (big, chunks()):
        r = client.post("/mcp", content=content, headers={**HG, "Content-Type": "application/json"})
        assert (r.status_code, r.json()) == (413, too_large)


def test_a_request_body_of_2_mib_is_read(client: TestClient) -> None:
    """The bound itself is allowed: the door answers, here refusing an argument the tool does not take."""
    params = {"name": "list_files", "arguments": {"folder": "x" * 300}}
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}).encode()
    padded = body + b" " * (2 * 1024 * 1024 - len(body))
    protocol = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    r = send(client, padded, {**protocol, "MCP-Protocol-Version": "2025-11-25", **HG})
    assert (r.status_code, r.json()["result"]["structuredContent"]["reason"]) == (200, "invalid_arguments")


def test_healthz_needs_no_key(client: TestClient) -> None:
    """The compose healthcheck has no key."""
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (PathError("path is outside the workspace"), 400, "path is outside the workspace"),
        (NotFound("file not found"), 404, "not found"),
        (Conflict("the destination exists"), 409, "the destination exists"),
        (TooLarge("file too large"), 413, "file too large"),
        (QuotaExceeded("workspace quota exceeded"), 507, "workspace quota exceeded"),
        (StorageFull("storage is full"), 507, "storage is full"),
    ],
)
def test_store_errors_answer_short_json_and_log_nothing(
    client: TestClient,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    status: int,
    detail: str,
) -> None:
    """The cause of such an error holds a real path; neither the answer nor the log may carry it."""
    marker = "quarterly-figures-marker.xlsx"

    def refuse(*_args: object, **_kwargs: object) -> list[object]:
        raise error from OSError(f"/data/workspaces/ghostwriter/user-a/chat-1/{marker}")

    monkeypatch.setattr(store, "list_dir", refuse)
    caplog.set_level(logging.DEBUG)
    r = client.get("/files/list", params={"directory": CHAT}, headers=HG)
    assert r.status_code == status
    assert r.json() == {"detail": detail}
    assert marker not in r.text
    assert marker not in caplog.text
    assert [record for record in caplog.records if record.exc_info] == []


def test_lifespan_records_the_limits_and_never_the_key(
    app: FastAPI, settings: Settings, audit_stream: io.StringIO
) -> None:
    """Start writes the limits in force, close ends the chain, and the key is in no line."""
    with TestClient(app) as running:
        assert running.get("/healthz").status_code == 200
        assert [json.loads(line)["operation"] for line in audit_stream.getvalue().splitlines()] == [
            "start",
            "config.audit",
        ]
    events = [json.loads(line) for line in audit_stream.getvalue().splitlines()]
    assert [event["operation"] for event in events] == ["start", "config.audit", "shutdown"]
    assert events[1]["criteria"] == {
        "retention_days": settings.retention_days,
        "executor_slots": settings.executor_slots,
        "queue_max": settings.queue_max,
        "job_wall_s": settings.job_wall_s,
        "upload_max_mb": settings.upload_max_mb,
        "workspace_max_mb": settings.workspace_max_mb,
        "callers": [
            {"name": "docint", "door": "mcp", "tools": ["run_python"], "wall_s": 60, "slots": 2},
            {"name": "ghostwriter", "door": "mcp", "tools": sorted(EIGHT), "wall_s": 120, "slots": 2},
        ],
        "require_purpose": [],
    }
    assert DOCINT_KEY not in audit_stream.getvalue()
    assert GHOSTWRITER_KEY not in audit_stream.getvalue()


def test_a_start_that_fails_after_the_chain_opened_closes_it(
    app: FastAPI, store: Store, audit_stream: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The volume cannot be counted: the app does not start, and the chain that was opened ends with its shutdown."""

    def broken() -> int:
        raise OSError("synthetic database failure")

    monkeypatch.setattr(store, "count_files", broken)
    with pytest.raises(OSError, match="synthetic database failure"):
        with TestClient(app):
            pass
    events = [json.loads(line) for line in audit_stream.getvalue().splitlines()]
    assert [event["operation"] for event in events] == ["start", "config.audit", "shutdown"]


def test_app_factory_builds_the_app_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``uvicorn --factory`` needs the key and the volume from the environment, and the DB lives on the volume."""
    data = tmp_path / "volume"
    monkeypatch.setenv("GHOSTWRITER_WORKBENCH_KEY", GHOSTWRITER_KEY)
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(data))
    monkeypatch.delenv("AUDIT_OUTPUT", raising=False)
    with TestClient(app_factory()) as running:
        assert running.get("/healthz").json() == {"ok": True}
        assert running.get("/files/list", params={"directory": CHAT}, headers=HG).status_code == 200
    assert (data / "workbench.db").is_file()
    out = capsys.readouterr().out
    assert '"config.audit"' in out
    assert GHOSTWRITER_KEY not in out


def test_app_factory_refuses_a_short_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A weak key stops the start, and the message does not hold it."""
    monkeypatch.setenv("GHOSTWRITER_WORKBENCH_KEY", "zz-too-weak")
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    with pytest.raises(ConfigError, match="GHOSTWRITER_WORKBENCH_KEY") as excinfo:
        app_factory()
    assert "zz-too-weak" not in str(excinfo.value)


def test_create_app_has_no_builtin_documentation_routes(settings: Settings, store: Store, audit: AuditLog) -> None:
    """FastAPI's own spec and docs would list every route, the file routes and the MCP door's included."""
    app = create_app(settings, store=store, audit=audit)
    assert (app.openapi_url, app.docs_url, app.redoc_url) == (None, None, None)
