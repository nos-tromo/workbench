"""``/metrics`` is Prometheus text with counts and durations only; a busy queue is a 503 the caller can retry."""

import asyncio
import io
import logging
import shutil
from collections import namedtuple
from pathlib import Path
from typing import override

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient

from tests.api.conftest import GHOSTWRITER_KEY, unrecorded
from workbench.api.audit import AuditLog
from workbench.api.auth import KeyGate
from workbench.api.jobs import Busy, ExecutorClient, JobQueue, Reason, RequestTooLarge
from workbench.api.main import app_factory, create_app
from workbench.api.metrics import Metrics
from workbench.api.store import Store
from workbench.config import Settings
from workbench.models import Principal
from workbench.protocol import JobRequest, JobResult

DiskUsage = namedtuple("DiskUsage", "total used free")


class OkClient(ExecutorClient):
    """An executor client that answers ``ok`` with nothing to register."""

    def __init__(self) -> None:
        """Make a client that never touches a socket."""
        super().__init__(Path("unused.sock"))

    @override
    async def run(self, req: JobRequest, timeout_s: float) -> JobResult:
        """Answer ``ok`` at once."""
        return JobResult(job_id=req.job_id, status="ok", result={}, outputs=[])


def test_metrics_expose_counts_only(client: TestClient) -> None:
    """No key is needed, the answer is Prometheus text, and no user is in it."""
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    for name in (
        "workbench_queue_length",
        "workbench_queue_wait_seconds_bucket",
        "workbench_job_duration_seconds",
        "workbench_job_refusals_total",
        "workbench_disk_used_bytes",
        "workbench_disk_capacity_bytes",
        "workbench_files_total",
    ):
        assert name in response.text
    assert "user-a" not in response.text


def test_metrics_need_no_key_and_are_not_in_the_spec(app: FastAPI, client: TestClient) -> None:
    """Prometheus scrapes it unauthenticated from workbench-net; no caller is offered it as a tool."""
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 200
    (route,) = [r for r in iter_route_contexts(app.routes) if r.path == "/metrics"]
    assert isinstance(route.original_route, APIRoute)
    assert route.include_in_schema is False
    assert route.original_route.methods == {"GET"}


def test_metrics_after_jobs_name_the_tool_but_no_user_chat_or_caller(
    settings: Settings, store: Store, audit: AuditLog
) -> None:
    """Durations are by tool, refusals by reason; who asked never becomes a label."""
    metrics = Metrics(settings.data_dir)
    queue = JobQueue(settings, OkClient(), store, metrics)
    app = create_app(settings, store=store, audit=audit, jobs=queue)
    asyncio.run(queue.submit(Principal("ghostwriter", "user-a", "chat-1"), "read_document", {}, []))
    with TestClient(app) as client:
        text = client.get("/metrics").text
    assert 'workbench_job_duration_seconds_count{tool="read_document"} 1.0' in text
    for reason in ("queue_full", "wait_timeout", "executor_busy"):
        assert f'workbench_job_refusals_total{{reason="{reason}"}} 0.0' in text
    for identity in ("user-a", "chat-1", "ghostwriter"):
        assert identity not in text


def test_app_factory_wires_the_queue_and_its_metrics(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The app ``uvicorn --factory`` builds has a queue, and ``/metrics`` serves that queue's series."""
    monkeypatch.setenv("GHOSTWRITER_WORKBENCH_KEY", GHOSTWRITER_KEY)
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path / "volume"))
    monkeypatch.delenv("AUDIT_OUTPUT", raising=False)
    app = app_factory()
    assert isinstance(app.state.jobs, JobQueue)
    with TestClient(app) as running:
        text = running.get("/metrics").text
    assert 'workbench_job_refusals_total{reason="queue_full"} 0.0' in text
    assert "workbench_disk_capacity_bytes" in text
    capsys.readouterr()


def test_disk_gauges_are_set_on_start_and_follow_a_refresh(
    settings: Settings, store: Store, audit: AuditLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gauges come from the data volume when the app starts, and again when the sweep refreshes them."""
    asked: list[Path] = []
    usage = [DiskUsage(1000, 400, 600)]

    def disk_usage(path: Path) -> DiskUsage:
        asked.append(path)
        return usage[0]

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)
    app = create_app(settings, store=store, audit=audit)
    with TestClient(app) as client:
        text = client.get("/metrics").text
        assert "workbench_disk_capacity_bytes 1000.0" in text
        assert "workbench_disk_used_bytes 400.0" in text
        usage[0] = DiskUsage(1000, 900, 100)
        app.state.metrics.refresh(files=7)
        text = client.get("/metrics").text
    assert "workbench_disk_used_bytes 900.0" in text
    assert "workbench_files_total 7.0" in text
    assert set(asked) == {settings.data_dir}


def test_files_total_is_counted_when_the_app_starts(settings: Settings, store: Store, audit: AuditLog) -> None:
    """Files that are on the volume already are in the gauge before the first sweep."""
    for name in ("one.txt", "two.txt"):
        store.add_upload(
            Principal("ghostwriter", "user-a", "chat-1"),
            "/workspace/chat-1",
            name,
            io.BytesIO(b"x"),
            max_bytes=100,
            quota_bytes=1000,
            record=unrecorded,
        )
    with TestClient(create_app(settings, store=store, audit=audit)) as client:
        assert "workbench_files_total 2.0" in client.get("/metrics").text


def test_a_volume_that_cannot_be_measured_leaves_the_gauges_alone(
    settings: Settings, store: Store, audit: AuditLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing ``statvfs`` must not stop the app from starting."""

    def broken(_path: Path) -> DiskUsage:
        raise OSError("synthetic failure")

    monkeypatch.setattr(shutil, "disk_usage", broken)
    with TestClient(create_app(settings, store=store, audit=audit)) as client:
        assert "workbench_disk_capacity_bytes 0.0" in client.get("/metrics").text


def _without_the_gate(app: FastAPI) -> FastAPI:
    """The app without :class:`KeyGate`, so a route a test adds is reached: no caller may reach a path off its door."""
    app.user_middleware = [middleware for middleware in app.user_middleware if middleware.cls is not KeyGate]
    return app


@pytest.mark.parametrize("reason", ["queue_full", "wait_timeout", "executor_busy"])
def test_busy_is_a_503_the_caller_can_retry(app: FastAPI, reason: Reason) -> None:
    """Whatever the reason, the model is told the same short thing."""

    async def refuse() -> None:
        raise Busy(reason)

    app.add_api_route("/refuse", refuse)
    with TestClient(_without_the_gate(app)) as client:
        response = client.get("/refuse")
    assert response.status_code == 503
    assert response.json() == {"detail": "busy, retry later"}


def test_a_request_too_large_is_a_422_and_not_a_busy(app: FastAPI, caplog: pytest.LogCaptureFixture) -> None:
    """The caller is told the request is too large, not to retry; nothing about it is logged."""

    async def refuse() -> None:
        raise RequestTooLarge("synthetic: /data/secret-path")

    app.add_api_route("/refuse", refuse)
    caplog.set_level(logging.DEBUG)
    with TestClient(_without_the_gate(app)) as client:
        response = client.get("/refuse")
    assert response.status_code == 422
    assert response.json() == {"detail": "request too large"}
    assert "secret-path" not in caplog.text
    assert [record for record in caplog.records if record.exc_info] == []
