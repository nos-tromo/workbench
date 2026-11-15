"""Fixtures for the api tests: the real app on a throwaway data volume, audit lines kept in memory."""

import io
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
from workbench.paths import Workspaces
from workbench.utils.audit_event import AuditEmitter

KEY = "k" * 32
DOCINT_KEY = "d" * 32
HD = {"Authorization": f"Bearer {DOCINT_KEY}", "X-User-Id": "user-a", "X-Session-Id": "chat-1"}


def unrecorded(*_: object) -> None:
    """Record nothing: for files a test puts in the store directly, where no event is under test."""


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings for the api with both synthetic caller keys, on a throwaway data volume."""
    return load_settings(
        {
            "OPEN_WEBUI_WORKBENCH_KEY": KEY,
            "DOCINT_WORKBENCH_KEY": DOCINT_KEY,
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
