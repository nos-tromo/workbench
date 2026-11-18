"""Fixtures for the api tests: the real app on a throwaway data volume, audit lines kept in memory."""

import io
import itertools
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from workbench import __version__
from workbench.api.audit import SERVICE, AuditLog
from workbench.api.main import create_app
from workbench.api.store import Store
from workbench.config import Settings, load_settings
from workbench.models import FileRecord, Principal
from workbench.paths import Workspaces
from workbench.utils.audit_event import AuditEmitter

KEY = "k" * 32
DOCINT_KEY = "d" * 32
HD = {"Authorization": f"Bearer {DOCINT_KEY}", "X-User-Id": "user-a", "X-Session-Id": "chat-1"}
GHOSTWRITER_KEY = "a" * 32
HG = {"Authorization": f"Bearer {GHOSTWRITER_KEY}", "X-User-Id": "user-a", "X-Session-Id": "chat-1"}
HG_B = {**HG, "X-User-Id": "user-b"}
PG = Principal("ghostwriter", "user-a", "chat-1", confined=True)
#: Ghostwriter's user and chat, unconfined: to lay out folders below its chat, which no call of its can make.
PG_SETUP = Principal("ghostwriter", "user-a", "chat-1")
#: A caller no key names, whose files are still on the volume under its own folder: none of them is Ghostwriter's.
FORMER = Principal("former-caller", "user-a", "chat-1")
H = {"Authorization": f"Bearer {KEY}", "X-User-Id": "user-a", "X-Session-Id": "chat-1"}
H_B = {**H, "X-User-Id": "user-b"}
KEY_ONLY = {"Authorization": f"Bearer {KEY}"}
CHAT = "/workspace/chat-1"
ME = Principal("open-webui", "user-a", "chat-1")
MIB = 1024 * 1024

AuditLines = Callable[[], list[dict[str, Any]]]


def unrecorded(*_: object) -> None:
    """Record nothing: for files a test puts in the store directly, where no event is under test."""


def put(store: Store, name: str, data: bytes = b"x", *, directory: str = CHAT, p: Principal = ME) -> FileRecord:
    """Store ``data`` as ``name`` for the synthetic user, by Open WebUI unless ``p`` names another caller."""
    return store.add_upload(
        p, directory, name, io.BytesIO(data), max_bytes=100 * MIB, quota_bytes=1000 * MIB, record=unrecorded
    )


def tick(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the store's clock advance one second per file, so no two files have the same age."""
    seconds = itertools.count(1_000_000)
    monkeypatch.setattr(store, "_clock", lambda: float(next(seconds)))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings for the api with the three synthetic caller keys, on a throwaway data volume."""
    return load_settings(
        {
            "OPEN_WEBUI_WORKBENCH_KEY": KEY,
            "DOCINT_WORKBENCH_KEY": DOCINT_KEY,
            "GHOSTWRITER_WORKBENCH_KEY": GHOSTWRITER_KEY,
            "WORKBENCH_DATA_DIR": str(tmp_path / "data"),
        },
        role="api",
    )


@pytest.fixture
def store(settings: Settings) -> Store:
    """A store on the throwaway data volume."""
    return Store(settings.data_dir / "workbench.db", Workspaces(settings.data_dir))


@pytest.fixture
def audit_stream() -> io.StringIO:
    """Where the app's audit lines go."""
    return io.StringIO()


@pytest.fixture
def audit(audit_stream: io.StringIO) -> AuditLog:
    """An audit log writing to ``audit_stream``, not started, without a heartbeat thread."""
    return AuditLog(AuditEmitter(SERVICE, __version__, stream=audit_stream, heartbeat_interval_s=0))


@pytest.fixture
def app(settings: Settings, store: Store, audit: AuditLog) -> FastAPI:
    """The real app over the fixtures above, not started."""
    return create_app(settings, store=store, audit=audit)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """A client on the started app, so the audit chain is open while the test runs."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def audit_lines(audit_stream: io.StringIO) -> Callable[[], list[dict[str, Any]]]:
    """Read the audit events written so far, parsed, in order."""

    def lines() -> list[dict[str, Any]]:
        return [json.loads(line) for line in audit_stream.getvalue().splitlines()]

    return lines


@pytest.fixture
def broken_audit(client: TestClient, audit_stream: io.StringIO, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every audit write fail from now on, as a full or closed output would.

    Depends on ``client`` so the chain is open first; the patch is undone before the client
    closes the chain.
    """

    def refuse(_text: str, /) -> int:
        raise OSError("synthetic audit stream failure")

    monkeypatch.setattr(audit_stream, "write", refuse)
