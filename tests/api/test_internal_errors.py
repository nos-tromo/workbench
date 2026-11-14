"""An error no handler expected is a short 500, and the logs hold its type and route, never its message or traceback.

The message of such an error can name a path on the volume, a file name or a user id. Starlette's own outermost layer
answers it too, but then raises it again, and uvicorn logs it with its traceback; the api answers it itself instead.
"""

import logging
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api.conftest import KEY
from tests.api.raising_app import PATH, RefusingStore
from tests.api.test_files import CHAT, H
from workbench.api.audit import AuditLog
from workbench.api.main import create_app
from workbench.config import Settings
from workbench.paths import Workspaces

ROOT = Path(__file__).resolve().parents[2]

#: The pieces of the error's message that must reach no log.
SECRETS = (PATH, "quarterly-figures", "/data/workspaces", "Permission denied")

#: The test client's own loggers, which name the URL it asked for; the server's log is everything else.
CLIENT_LOGGERS = ("httpx", "httpcore")

#: The two routes that fail, as the browser and the model call them.
ROUTES: dict[str, tuple[str, str, dict[str, Any]]] = {
    "view": ("GET", "/files/view", {"params": {"path": f"{CHAT}/quarterly-figures.xlsx"}}),
    "list_files": ("POST", "/tools/list_files", {"json": {}}),
}


@pytest.fixture
def refusing(settings: Settings, audit: AuditLog) -> Iterator[TestClient]:
    """A client on the real app over a store whose reads fail; it raises whatever the app lets out."""
    store = RefusingStore(settings.data_dir / "workbench.db", Workspaces(settings.data_dir))
    app: FastAPI = create_app(settings, store=store, audit=audit)
    with TestClient(app, raise_server_exceptions=True) as client:
        yield client


@pytest.mark.parametrize("route", sorted(ROUTES))
def test_an_unexpected_error_is_a_short_500_logged_by_its_type_and_route(
    refusing: TestClient, route: str, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """The answer is fixed text; the log line names the method, the route and the type, and nothing of the message."""
    method, path, kwargs = ROUTES[route]
    with caplog.at_level(logging.DEBUG):
        r = refusing.request(method, path, headers=H, **kwargs)
    assert (r.status_code, r.json()) == (500, {"detail": "internal error"})
    server = [rec for rec in caplog.records if not rec.name.startswith(CLIENT_LOGGERS)]
    (record,) = [rec for rec in server if rec.levelno >= logging.ERROR]
    assert record.getMessage() == f"an unexpected error ended {method} {path} (PermissionError)"
    assert all(rec.exc_info is None and rec.exc_text is None for rec in server)
    logged = "\n".join(rec.getMessage() for rec in server)
    printed = capsys.readouterr()
    for text in (logged, printed.out, printed.err, r.text):
        assert [secret for secret in SECRETS if secret in text] == []


def test_the_500_of_a_view_keeps_the_viewing_headers(refusing: TestClient) -> None:
    """The answer is made inside the hardening of ``/files/view``, so it carries its headers like every other."""
    method, path, kwargs = ROUTES["view"]
    r = refusing.request(method, path, headers=H, **kwargs)
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
        "OPEN_WEBUI_WORKBENCH_KEY": KEY,
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
        method, path, kwargs = ROUTES["view"]
        r = httpx.request(method, address + path, headers=H, timeout=30, **kwargs)
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
