"""Fixtures for the api tests: the real app on a throwaway data volume, audit lines kept in memory."""

import io
from collections.abc import Iterator
from pathlib import Path

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


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings for the api with the synthetic caller key, on a throwaway data volume."""
    return load_settings(
        {"OPEN_WEBUI_WORKBENCH_KEY": KEY, "WORKBENCH_DATA_DIR": str(tmp_path / "data")},
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
