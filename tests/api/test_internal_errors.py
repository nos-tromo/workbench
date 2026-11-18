"""An error no handler expected is a short 500, and the logs hold its type and route, never its message or traceback.

The message of such an error can name a path on the volume, a file name or a user id. Starlette's own outermost layer
answers it too, but then raises it again, and uvicorn logs it with its traceback; the api answers it itself instead.
"""

import asyncio
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message

from tests.api.conftest import CHAT, GHOSTWRITER_KEY, HG
from tests.api.mcp_calls import acall
from tests.api.raising_app import PATH, RefusingStore
from workbench.api.audit import AuditLog
from workbench.api.errors import QuietErrors
from workbench.api.main import create_app
from workbench.config import Settings
from workbench.paths import Workspaces

ROOT = Path(__file__).resolve().parents[2]

#: The pieces of the error's message that must reach no log.
SECRETS = (PATH, "quarterly-figures", "/data/workspaces", "Permission denied")

#: The test client's own loggers, which name the URL it asked for; the server's log is everything else.
CLIENT_LOGGERS = ("httpx", "httpcore")

#: The route that fails as the browser calls it.
VIEW: tuple[str, str, dict[str, Any]] = ("GET", "/files/view", {"params": {"path": f"{CHAT}/quarterly-figures.xlsx"}})

#: Each failure as its caller meets it: the request, the body of the 500, and the one line logged.
FAILURES: dict[str, tuple[Callable[[TestClient], httpx2.Response], dict[str, Any], str]] = {
    "view": (
        lambda client: client.request(VIEW[0], VIEW[1], headers=HG, **VIEW[2]),
        {"detail": "internal error"},
        "an unexpected error ended GET /files/view (PermissionError)",
    ),
    "list_files": (
        lambda client: acall(client, "list_files"),
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": "internal error", "data": {"reason": "internal"}},
        },
        "an unexpected error ended tools/call on /mcp (PermissionError)",
    ),
}


@pytest.fixture
def refusing(settings: Settings, audit: AuditLog) -> Iterator[TestClient]:
    """A client on the real app over a store whose reads fail; it raises whatever the app lets out."""
    store = RefusingStore(settings.data_dir / "workbench.db", Workspaces(settings.data_dir))
    app: FastAPI = create_app(settings, store=store, audit=audit)
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client


@pytest.mark.parametrize("route", sorted(FAILURES))
def test_an_unexpected_error_is_a_short_500_logged_by_its_type_and_route(
    refusing: TestClient, route: str, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """The answer is fixed text; the log line names the method and the route or door, the type, never the message."""
    request, answer, line = FAILURES[route]
    with caplog.at_level(logging.DEBUG):
        r = request(refusing)
    assert (r.status_code, r.json()) == (500, answer)
    server = [rec for rec in caplog.records if not rec.name.startswith(CLIENT_LOGGERS)]
    (record,) = [rec for rec in server if rec.levelno >= logging.ERROR]
    assert record.getMessage() == line
    assert all(rec.exc_info is None and rec.exc_text is None for rec in server)
    logged = "\n".join(rec.getMessage() for rec in server)
    printed = capsys.readouterr()
    for text in (logged, printed.out, printed.err, r.text):
        assert [secret for secret in SECRETS if secret in text] == []


def test_the_500_of_a_view_keeps_the_viewing_headers(refusing: TestClient) -> None:
    """The answer is made inside the hardening of ``/files/view``, so it carries its headers like every other."""
    method, path, kwargs = VIEW
    r = refusing.request(method, path, headers=HG, **kwargs)
    assert r.status_code == 500
    assert r.headers["content-security-policy"].startswith("sandbox;")
    assert r.headers["x-content-type-options"] == "nosniff"


def _read(stream: Any, into: list[str]) -> None:
    """Keep every line a stream gives until it ends."""
    for line in stream:
        into.append(line)


def test_under_uvicorn_the_error_leaves_one_line_and_no_traceback(tmp_path: Path) -> None:
    """The api as its image runs it: one log line with the type, and neither the traceback nor the path on any stream.

    The server is started on a free port of the loopback, without an access log, exactly as ``docker/Dockerfile.api``
    starts it otherwise; its standard output carries the audit chain, its standard error the log.
    """
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "GHOSTWRITER_WORKBENCH_KEY": GHOSTWRITER_KEY,
        "WORKBENCH_DATA_DIR": str(tmp_path / "data"),
        "PYTHONUNBUFFERED": "1",
    }
    command = [sys.executable, "-m", "uvicorn", "--factory", "tests.api.raising_app:factory"]
    command += ["--host", "127.0.0.1", "--port", "0", "--workers", "1", "--no-access-log"]
    server = subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out: list[str] = []
    err: list[str] = []
    readers = [
        threading.Thread(target=_read, args=(s, b), daemon=True)
        for s, b in ((server.stdout, out), (server.stderr, err))
    ]
    for reader in readers:
        reader.start()
    try:
        deadline = time.monotonic() + 60
        while not any("Uvicorn running on" in line for line in err):
            if server.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f"uvicorn did not start: {''.join(err)[-2000:]}")
            time.sleep(0.05)
        address = next(line for line in err if "Uvicorn running on" in line).split("running on ", 1)[1].split()[0]
        method, path, kwargs = VIEW
        r = httpx.request(method, address + path, headers=HG, timeout=30, **kwargs)
    finally:
        server.terminate()
        server.wait(timeout=30)
        for reader in readers:
            reader.join(timeout=30)
    logged, chain = "".join(err), "".join(out)
    assert "Traceback" not in logged and "Exception in ASGI application" not in logged
    assert [secret for secret in SECRETS if secret in logged + chain] == []
    assert "an unexpected error ended GET /files/view (PermissionError)" in logged
    assert (r.status_code, r.json()) == (500, {"detail": "internal error"})


# --- the errors' layer on its own -------------------------------------------------------------------------------


class _Route:
    """What the router leaves in the scope once a route matched: its template."""

    path = "/files/view"


def _scope() -> dict[str, Any]:
    """The scope of a request the router matched to ``/files/view``."""
    return {"type": "http", "method": "GET", "path": "/files/view", "route": _Route()}


async def _nothing_more() -> dict[str, Any]:
    """Receive: the request has no body."""
    return {"type": "http.request", "body": b"", "more_body": False}


def test_an_error_after_the_answer_started_sends_no_second_start(caplog: pytest.LogCaptureFixture) -> None:
    """A route that has begun its answer and then fails: the answer stops where it was, one line is logged.

    No second ``http.response.start`` follows the first, which the server would refuse; nothing of the error's message
    and no traceback reaches the log.
    """

    async def starts_then_fails(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/csv")]})
        await send({"type": "http.response.body", "body": b"item,total\n", "more_body": True})
        raise PermissionError(13, "Permission denied", PATH)

    sent: list[Message] = []

    async def keep(message: Message) -> None:
        sent.append(message)

    with caplog.at_level(logging.DEBUG):
        asyncio.run(QuietErrors(starts_then_fails)(_scope(), _nothing_more, keep))
    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[0]["status"] == 200
    (record,) = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert record.getMessage() == "an unexpected error ended GET /files/view (PermissionError)"
    assert all(rec.exc_info is None and rec.exc_text is None for rec in caplog.records)
    assert [secret for secret in SECRETS if secret in caplog.text] == []


def test_a_fallback_answer_that_cannot_be_sent_carries_nothing_of_the_error() -> None:
    """The 500 is sent after the error is handled: if that send fails, its error is not chained to the first one.

    A chained error would put the first error's message, and its path, into the server's traceback of the second.
    """

    async def fails(scope: Any, receive: Any, send: Any) -> None:
        raise PermissionError(13, "Permission denied", PATH)

    async def gone(message: Message) -> None:
        raise RuntimeError("synthetic: the client went away")

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(QuietErrors(fails)(_scope(), _nothing_more, gone))
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None
