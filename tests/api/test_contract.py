"""The terminal handshake Open WebUI runs, the key and header checks, and the tool-only OpenAPI document."""

import asyncio
import dataclasses
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

from tests.api.conftest import DOCINT_KEY, KEY, unrecorded
from workbench.api import auth
from workbench.api.audit import AuditLog
from workbench.api.auth import Caller, CallerRegistry, KeyGate
from workbench.api.contract import system_prompt
from workbench.api.main import app_factory, create_app
from workbench.api.openapi import TOOL_TAG, tool_openapi
from workbench.api.registry import WORKSPACE_TOOLS
from workbench.api.store import Conflict, NotFound, QuotaExceeded, StorageFull, Store, TooLarge
from workbench.config import ConfigError, Settings
from workbench.models import Principal
from workbench.paths import PathError

H = {"Authorization": f"Bearer {KEY}", "X-User-Id": "user-a", "X-Session-Id": "chat-1"}
KEY_ONLY = {"Authorization": f"Bearer {KEY}"}

#: The one route below ``/files/`` that is a tool: ``display_file``.
DISPLAY_TOOL = "/files/display"

HIDDEN_FROM_THE_SPEC = (
    "/files/cwd",
    "/files/read",
    "/api/config",
    "/system",
    "/skills",
    "/skills/read",
    "/healthz",
    "/metrics",
    "/openapi.json",
)

KEYED_ROUTES = [
    ("GET", "/api/config"),
    ("GET", "/system"),
    ("GET", "/openapi.json"),
    ("GET", "/skills"),
    ("GET", "/skills/read?name=x"),
    ("GET", "/files/cwd"),
    ("POST", "/files/cwd"),
    ("GET", "/files/read?path=/workspace/chat-1/a.txt"),
]

BAD_AUTHORIZATION: list[dict[str, str]] = [
    {},
    {"Authorization": "Bearer wrong"},
    {"Authorization": f"Bearer {KEY}x"},
    {"Authorization": f"Bearer {KEY[:-1]}"},
    {"Authorization": KEY},
    {"Authorization": f"Basic {KEY}"},
    {"Authorization": "Bearer"},
    {"Authorization": "Bearer "},
]


def _without(headers: dict[str, str], name: str) -> dict[str, str]:
    """Copy ``headers`` without ``name``."""
    return {key: value for key, value in headers.items() if key != name}


def test_policies_is_not_found_so_open_webui_sees_a_plain_terminal(client: TestClient) -> None:
    """A 2xx there makes Open WebUI treat workbench as an orchestrator."""
    assert client.get("/api/v1/policies", headers=H).status_code == 404


def test_config_announces_a_system_prompt(client: TestClient) -> None:
    """Open WebUI fetches ``/system`` only when the config says the server has one."""
    r = client.get("/api/config", headers=H)
    assert r.status_code == 200
    assert r.json()["features"]["system"] is True


def test_config_and_system_need_only_the_key(client: TestClient) -> None:
    """The connection check sends no user headers."""
    assert client.get("/api/config", headers=KEY_ONLY).status_code == 200
    assert client.get("/system", headers=KEY_ONLY).status_code == 200


def test_system_prompt_names_the_retention_period(client: TestClient) -> None:
    """The assistant is told how long files live, and that projects belong in docint."""
    prompt = client.get("/system", headers=H).json()["prompt"]
    assert "7 days" in prompt
    assert "docint" in prompt
    assert "{retention_days}" not in prompt


def test_system_prompt_follows_the_configured_period(settings: Settings) -> None:
    """The period is filled in from the settings, and a template placeholder is left alone."""
    prompt = system_prompt(dataclasses.replace(settings, retention_days=3))
    assert "3 days" in prompt
    assert "7 days" not in prompt
    assert "{{ field }}" in prompt


def test_system_prompt_carries_the_behaviour_the_spec_asks_for(settings: Settings) -> None:
    """Plan and confirm, cite, never invent, OCR, templates and where Python finds its files."""
    prompt = system_prompt(settings).lower()
    for phrase in ("plan", "confirm", "cite", "never invent", "ocr", "docint", "template", "/workspace/"):
        assert phrase in prompt, phrase


def test_system_prompt_tells_python_where_results_go_and_to_use_threads(settings: Settings) -> None:
    """Results in ``out/``, plots as files, threads instead of processes (R28), and the job id as a source."""
    prompt = system_prompt(settings)
    for phrase in ("`out/`", "savefig", "display_file", "threads", "multiprocessing", "job_id"):
        assert phrase in prompt, phrase


def test_system_prompt_says_how_new_tables_and_documents_are_made(settings: Settings) -> None:
    """With the two output tools, and with run_python for a large table."""
    prompt = system_prompt(settings)
    for phrase in ("`write_table`", "`write_document`", "large table"):
        assert phrase in prompt, phrase


def test_system_prompt_says_who_sees_the_files_without_promising_nobody(settings: Settings) -> None:
    """R2 M5: only this user reaches the workspace; tool results go into the chat and to the configured model."""
    prompt = system_prompt(settings)
    assert "shared with nobody" not in prompt
    for phrase in ("only this user", "part of this chat", "the operator configured"):
        assert phrase in prompt, phrase


def test_openapi_needs_only_the_key(client: TestClient) -> None:
    """Open WebUI fetches the spec with ``Authorization`` alone."""
    assert client.get("/openapi.json", headers=KEY_ONLY).status_code == 200
    assert client.get("/openapi.json").status_code == 401


def test_openapi_lists_only_tools(client: TestClient) -> None:
    """The document has paths, none of them a handshake or browser route, each operation a tagged tool."""
    doc = client.get("/openapi.json", headers=KEY_ONLY).json()
    assert "openapi" in doc
    assert "paths" in doc
    for path in doc["paths"]:
        assert path not in HIDDEN_FROM_THE_SPEC
        assert path == DISPLAY_TOOL or not path.startswith(("/files/", "/api/"))
    for path, item in doc["paths"].items():
        for operation in item.values():
            assert TOOL_TAG in operation["tags"], path
            assert operation["operationId"], path


def test_openapi_holds_exactly_the_routes_tagged_as_tools(app: FastAPI, client: TestClient) -> None:
    """A tagged route off the caller's list stays out; an untagged one and one hidden from the schema, even on it."""

    def echo(text: str) -> dict[str, str]:
        return {"text": text}

    app.add_api_route("/tools/echo", echo, methods=["POST"], tags=[TOOL_TAG], operation_id="echo_tool")
    app.add_api_route("/untagged", echo, methods=["POST"], operation_id="untagged_op")
    app.add_api_route(
        "/hidden", echo, methods=["POST"], tags=[TOOL_TAG], include_in_schema=False, operation_id="hidden_op"
    )
    nine = {
        "/tools/list_files",
        "/tools/read_document",
        "/tools/search_files",
        "/tools/run_python",
        "/tools/inspect_template",
        "/tools/fill_template",
        "/tools/write_table",
        "/tools/write_document",
        DISPLAY_TOOL,
    }
    assert set(client.get("/openapi.json", headers=KEY_ONLY).json()["paths"]) == nine
    listed = Caller("every-tool", KEY, WORKSPACE_TOOLS | {"echo_tool", "untagged_op", "hidden_op"})
    doc = tool_openapi(app, listed)
    assert set(doc["paths"]) == nine | {"/tools/echo"}
    assert doc["paths"]["/tools/echo"]["post"]["operationId"] == "echo_tool"


def test_openapi_is_built_once_and_served_from_memory(app: FastAPI, client: TestClient) -> None:
    """Open WebUI fetches the spec on every chat request."""
    open_webui = app.state.callers.get("open-webui")
    first = tool_openapi(app, open_webui)
    assert tool_openapi(app, open_webui) is first
    assert client.get("/openapi.json", headers=KEY_ONLY).json() == first


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
    assert client.get("/api/config", headers={"Authorization": f"bearer {KEY}"}).status_code == 200


def test_the_key_is_compared_in_constant_time(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The presented key goes through ``hmac.compare_digest``, never ``==``: at the gate, then at the route."""
    seen: list[tuple[bytes, bytes]] = []
    compare_digest = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        seen.append((a, b))
        return compare_digest(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    assert client.get("/api/config", headers=KEY_ONLY).status_code == 200
    assert seen == [(KEY.encode(), KEY.encode()), (KEY.encode(), DOCINT_KEY.encode())] * 2


def test_the_key_is_checked_before_the_user(client: TestClient) -> None:
    """A caller without the key learns nothing about the other headers."""
    r = client.get("/files/cwd", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


@pytest.mark.parametrize("user", ["../x", "", " ", "a b", "x/y", ".hidden", "a" * 129])
def test_user_header_is_checked(client: TestClient, user: str) -> None:
    """The user id becomes a folder name, so anything that is not a plain id is refused."""
    r = client.get("/files/cwd", headers={**H, "X-User-Id": user})
    assert r.status_code == 400
    assert r.json() == {"detail": "missing or invalid user"}


def test_headers_that_are_not_ascii_are_refused_not_crashed(client: TestClient) -> None:
    """A byte outside ASCII in a key or an id is an ordinary refusal."""
    key = client.get("/api/config", headers={b"authorization": "Bearer k\u00e9y".encode("latin-1")})
    assert key.status_code == 401
    user = client.get(
        "/files/cwd", headers={b"authorization": f"Bearer {KEY}".encode(), b"x-user-id": "u\u00e4".encode()}
    )
    assert user.status_code == 400


def test_user_header_is_required(client: TestClient) -> None:
    """Every route but the handshake ones needs a user."""
    r = client.get("/files/cwd", headers=_without(H, "X-User-Id"))
    assert r.status_code == 400
    assert r.json() == {"detail": "missing or invalid user"}


@pytest.mark.parametrize("session", ["../x", "", "a b", "a/b", "a" * 129])
def test_session_header_is_checked(client: TestClient, session: str) -> None:
    """The chat id becomes a folder name too."""
    r = client.get("/files/cwd", headers={**H, "X-Session-Id": session})
    assert r.status_code == 400
    assert r.json() == {"detail": "invalid session"}


def test_cwd_is_the_chats_folder(client: TestClient) -> None:
    """Open WebUI starts the file browser in the chat's folder, below the user's home."""
    r = client.get("/files/cwd", headers=H)
    assert r.status_code == 200
    assert r.json() == {"cwd": "/workspace/chat-1", "home": "/workspace"}


def test_requests_without_a_session_use_the_default_folder(client: TestClient) -> None:
    """A request outside any chat still has a folder."""
    r = client.get("/files/cwd", headers=_without(H, "X-Session-Id"))
    assert r.json() == {"cwd": "/workspace/default", "home": "/workspace"}


def test_local_chat_ids_are_accepted(client: TestClient) -> None:
    """Open WebUI names a chat that is not saved yet ``local:<id>``."""
    r = client.get("/files/cwd", headers={**H, "X-Session-Id": "local:abc123"})
    assert r.status_code == 200
    assert r.json()["cwd"] == "/workspace/local:abc123"


def test_cwd_does_not_create_the_folder(client: TestClient, settings: Settings) -> None:
    """Looking is not writing: the folder appears with the first file."""
    client.get("/files/cwd", headers=H)
    assert not (settings.data_dir / "workspaces").exists()


def test_post_cwd_stays_inside_the_root(client: TestClient) -> None:
    """Moving to another folder of one's own works; leaving the root does not."""
    ok = client.post("/files/cwd", json={"path": "/workspace/chat-2"}, headers=H)
    assert ok.status_code == 200
    assert ok.json() == {"cwd": "/workspace/chat-2"}
    assert client.post("/files/cwd", json={"path": "/workspace/chat-1/../chat-2"}, headers=H).json() == {
        "cwd": "/workspace/chat-2"
    }
    for outside in ("/etc", "/workspace/../etc", "/workspace/chat-1/../../x", "relative", "/workspace/a\x00b"):
        r = client.post("/files/cwd", json={"path": outside}, headers=H)
        assert r.status_code == 400, outside


def test_post_cwd_needs_a_folder_that_exists_below_the_chat_folder(client: TestClient) -> None:
    """A folder inside a chat that was never made is not a place to be."""
    r = client.post("/files/cwd", json={"path": "/workspace/chat-2/reports"}, headers=H)
    assert r.status_code == 404
    assert r.json() == {"detail": "not found"}


def test_agents_md_is_absent(client: TestClient) -> None:
    """Open WebUI probes ``<home>/AGENTS.md`` for every chat; it does not exist."""
    r = client.get("/files/read", params={"path": "/workspace/AGENTS.md"}, headers=H)
    assert r.status_code == 404
    assert r.json() == {"detail": "not found"}


def test_a_file_that_is_not_stored_is_not_found(client: TestClient) -> None:
    """The same answer inside a chat folder."""
    r = client.get("/files/read", params={"path": "/workspace/chat-1/AGENTS.md"}, headers=H)
    assert r.status_code == 404
    assert r.json() == {"detail": "not found"}


def test_read_refuses_paths_outside_the_root(client: TestClient) -> None:
    """Only a file at home is simply absent; a path that leaves the root is an error."""
    r = client.get("/files/read", params={"path": "/workspace/../etc/passwd"}, headers=H)
    assert r.status_code == 400


def test_read_of_a_stored_text_file_returns_its_content(client: TestClient, store: Store) -> None:
    """The file browser serves ``/files/read``: a text file answers its content, any other type 415."""
    me = Principal("open-webui", "user-a", "chat-1")
    store.add_upload(
        me, "/workspace/chat-1", "notes.txt", io.BytesIO(b"hello"), max_bytes=100, quota_bytes=1000, record=unrecorded
    )
    store.add_upload(
        me, "/workspace/chat-1", "q.xlsx", io.BytesIO(b"PK"), max_bytes=100, quota_bytes=1000, record=unrecorded
    )
    r = client.get("/files/read", params={"path": "/workspace/chat-1/notes.txt"}, headers=H)
    assert (r.status_code, r.json()) == (200, {"content": "hello"})
    assert client.get("/files/read", params={"path": "/workspace/chat-1/q.xlsx"}, headers=H).status_code == 415


def test_skills_answer_empty(client: TestClient) -> None:
    """Open WebUI does not catch a connection error here, so the routes must answer."""
    assert client.get("/skills", headers=H).json() == []
    r = client.get("/skills/read", params={"name": "x"}, headers=H)
    assert r.status_code == 404
    assert r.json() == {"detail": "not found"}


@pytest.mark.parametrize(
    "path",
    [
        "/api/terminals/user/output",
        "/files/archive",
        "/system/info",
        "/files/search",
        "/files/glob",
        "/docs",
        "/redoc",
        "/openapi",
        "/api/v1/policies",
    ],
)
def test_unknown_paths_are_not_found(client: TestClient, path: str) -> None:
    """The allow-list is workbench's: everything not listed answers 404 to a caller, 401 to anyone without the key."""
    assert client.get(path, headers=H).status_code == 404
    assert client.get(path).status_code == 401


#: A path no route serves, a route's other method, and routes that read a JSON body.
GATED = [
    ("GET", "/files/archive"),
    ("PUT", "/files/mkdir"),
    ("POST", "/files/mkdir"),
    ("POST", "/tools/run_python"),
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
        "path": "/tools/run_python",
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
    """With the key, a body past the request bound is refused as it arrives: declared or chunked (R74)."""
    big = b'{"path": "' + b"x" * (2 * 1024 * 1024) + b'"}'
    r = client.post("/files/mkdir", content=big, headers={**H, "Content-Type": "application/json"})
    assert (r.status_code, r.json()) == (413, {"detail": "request too large"})

    def chunks() -> Iterator[bytes]:
        yield b'{"path": "'
        for _ in range(3):
            yield b"x" * (1024 * 1024)
        yield b'"}'

    r = client.post("/files/mkdir", content=chunks(), headers={**H, "Content-Type": "application/json"})
    assert (r.status_code, r.json()) == (413, {"detail": "request too large"})


def test_a_request_body_of_2_mib_is_read(client: TestClient) -> None:
    """The bound itself is allowed: the route answers, here with its own refusal of the folder's name."""
    body = b'{"path": "/workspace/chat-1/' + b"x" * 300 + b'"}'
    padded = body + b" " * (2 * 1024 * 1024 - len(body))
    r = client.post("/files/mkdir", content=padded, headers={**H, "Content-Type": "application/json"})
    assert r.status_code == 400


def test_healthz_needs_no_key(client: TestClient) -> None:
    """The compose healthcheck has no key."""
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_callers_are_a_registry_with_open_webui_first(settings: Settings) -> None:
    """Version 1 has one caller that may use every tool; version 2 adds docint without restructuring."""
    registry = CallerRegistry.from_settings(settings)
    caller = registry.authenticate(KEY)
    assert caller is not None
    assert (caller.name, caller.key, caller.tools) == ("open-webui", KEY, WORKSPACE_TOOLS)
    assert {"list_files", "read_document", "run_python", "fill_template", "display_file"} <= WORKSPACE_TOOLS
    assert registry.authenticate("wrong") is None
    assert registry.authenticate("") is None
    assert KEY not in repr(caller)


def test_the_registry_needs_a_key(settings: Settings) -> None:
    """The executor's settings carry no key, and the api refuses to build a registry from them."""
    with pytest.raises(ConfigError, match="OPEN_WEBUI_WORKBENCH_KEY or DOCINT_WORKBENCH_KEY"):
        CallerRegistry.from_settings(dataclasses.replace(settings, open_webui_key=None, docint_key=None))


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
        raise error from OSError(f"/data/workspaces/open-webui/user-a/chat-1/{marker}")

    monkeypatch.setattr(store, "list_dir", refuse)
    caplog.set_level(logging.DEBUG)
    r = client.post("/files/cwd", json={"path": "/workspace/chat-1"}, headers=H)
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
    }
    assert KEY not in audit_stream.getvalue()


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
    monkeypatch.setenv("OPEN_WEBUI_WORKBENCH_KEY", KEY)
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(data))
    monkeypatch.delenv("AUDIT_OUTPUT", raising=False)
    with TestClient(app_factory()) as running:
        assert running.get("/healthz").json() == {"ok": True}
        assert running.get("/api/config", headers=KEY_ONLY).status_code == 200
    assert (data / "workbench.db").is_file()
    out = capsys.readouterr().out
    assert '"config.audit"' in out
    assert KEY not in out


def test_app_factory_refuses_a_short_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A weak key stops the start, and the message does not hold it."""
    monkeypatch.setenv("OPEN_WEBUI_WORKBENCH_KEY", "zz-too-weak")
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    with pytest.raises(ConfigError, match="OPEN_WEBUI_WORKBENCH_KEY") as excinfo:
        app_factory()
    assert "zz-too-weak" not in str(excinfo.value)


def test_create_app_has_no_builtin_documentation_routes(settings: Settings, store: Store, audit: AuditLog) -> None:
    """FastAPI's own spec and docs would list every route, handshake ones included."""
    app = create_app(settings, store=store, audit=audit)
    assert (app.openapi_url, app.docs_url, app.redoc_url) == (None, None, None)
