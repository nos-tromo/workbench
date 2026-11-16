"""Ghostwriter's chat delete: recorded by id first, then the chat's files, job rows and folder go.

A job of that chat still running when it goes keeps no output: its output's row would have no job row to belong to.
"""

import dataclasses
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api.conftest import HD, HG, PG
from tests.api.test_files import ME, H, put
from tests.api.test_files_ghostwriter import P2, PURPOSE, keep
from tests.api.test_jobs import FakeClient, until
from workbench.api.audit import AuditLog
from workbench.api.auth import KeyGate
from workbench.api.jobs import JobQueue
from workbench.api.main import create_app
from workbench.api.metrics import Metrics
from workbench.api.store import Store
from workbench.api.tools import OutputKeeper, python_args
from workbench.config import Settings
from workbench.layout import WORKSPACES
from workbench.protocol import JobResult

AuditLines = Callable[[], list[dict[str, Any]]]
NOT_FOUND = (404, {"detail": "not found"})


def delete(client: TestClient, headers: Mapping[str, str] = HG) -> Any:
    """Delete the chat ``headers`` name, as Ghostwriter does unless other headers are given."""
    return client.delete("/files/chat", headers=headers)


def chat_folder(settings: Settings, chat: str = "chat-1") -> Path:
    """Ghostwriter's folder of ``chat`` for ``user-a`` on the data volume."""
    return settings.data_dir / WORKSPACES / "ghostwriter" / "user-a" / chat


def chat_events(audit_lines: AuditLines) -> list[dict[str, Any]]:
    """The audit events of chat deletes, in order."""
    return [event for event in audit_lines() if event["operation"].startswith("chat.")]


def without_session(session: str | None) -> dict[str, str]:
    """Ghostwriter's headers with no ``X-Session-Id``, or with ``session`` as it."""
    headers = {name: value for name, value in HG.items() if name != "X-Session-Id"}
    if session is not None:
        headers["X-Session-Id"] = session
    return headers


def test_delete_removes_files_jobs_and_folder_after_its_event(
    client: TestClient, store: Store, audit_lines: AuditLines, settings: Settings
) -> None:
    """The chat's files, its job rows and its folder go; another chat of the user keeps its own.

    One event names the files by id, with the chat as container, the stated purpose and the request id.
    """
    first, second = keep(store, PG, "quarterly-figures.csv"), keep(store, PG, "harbour-report.csv")
    other = keep(store, P2)
    job, theirs = store.add_job(PG, "run_python"), store.add_job(P2, "run_python")
    r = delete(client, {**HG, **PURPOSE})
    assert (r.status_code, r.json()) == (200, {"deleted": 2})
    assert store.files_in_chat(PG) == []
    assert store.find_job(job.id) is None
    assert not chat_folder(settings).exists()
    assert store.files_in_chat(P2) == [other]
    assert store.find_job(theirs.id) == theirs
    assert chat_folder(settings, "chat-2").is_dir()

    (event,) = chat_events(audit_lines)
    assert (event["action"], event["operation"], event["outcome"]) == ("delete", "chat.delete", "ok")
    assert event["actor"] == {"user": "user-a", "via": "service"}
    assert sorted(event["target"].pop("ids")) == sorted([first.id, second.id])
    assert event["target"] == {"kind": "file", "count": 2, "container": "chat:chat-1"}
    assert event["criteria"] == {"caller": "ghostwriter"}
    assert event["purpose"] == {"reason": "ghostwriter chat"}
    assert event["request_id"] == "req-1"


def test_an_empty_chat_answers_zero(client: TestClient, audit_lines: AuditLines) -> None:
    """A chat that holds nothing, and has no folder, is deleted too, and its event says so."""
    r = delete(client)
    assert (r.status_code, r.json()) == (200, {"deleted": 0})
    (event,) = chat_events(audit_lines)
    assert (event["operation"], event["outcome"], event["target"]["count"]) == ("chat.delete", "ok", 0)
    assert event["target"].get("ids", []) == []


def test_nothing_goes_when_the_event_cannot_be_written(
    broken_audit: None, client: TestClient, store: Store, settings: Settings
) -> None:
    """Without its event, the delete does not happen: the files, their rows, the job row and the folder stay."""
    kept = [keep(store, PG, "quarterly-figures.csv"), keep(store, PG, "harbour-report.csv")]
    job = store.add_job(PG, "run_python")
    r = delete(client)
    assert (r.status_code, r.json()) == (500, {"detail": "audit unavailable"})
    assert sorted(rec.id for rec in store.files_in_chat(PG)) == sorted(rec.id for rec in kept)
    assert store.find_job(job.id) == job
    assert sorted(path.name for path in chat_folder(settings).iterdir()) == [
        "harbour-report.csv",
        "quarterly-figures.csv",
    ]


def test_a_removal_that_fails_after_the_event_is_recorded_again(
    client: TestClient, store: Store, audit_lines: AuditLines, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows are gone, the folder stays for retention's orphan sweep, and the event is followed by an ``error``."""
    kept = [keep(store, PG, "quarterly-figures.csv"), keep(store, PG, "harbour-report.csv")]

    def refuse(*_: object, **__: object) -> None:
        raise OSError("synthetic removal failure")

    monkeypatch.setattr(shutil, "rmtree", refuse)
    r = delete(client)
    assert (r.status_code, r.json()) == (500, {"detail": "internal error"})
    recorded, again = chat_events(audit_lines)
    assert (recorded["outcome"], again["outcome"]) == ("ok", "error")
    ids = sorted(rec.id for rec in kept)
    assert sorted(recorded["target"]["ids"]) == sorted(again["target"]["ids"]) == ids
    assert store.files_in_chat(PG) == []
    assert chat_folder(settings).is_dir()


@pytest.mark.parametrize("session", [None, "default"])
def test_a_missing_or_default_session_is_400(
    client: TestClient, store: Store, audit_lines: AuditLines, session: str | None
) -> None:
    """``default`` is never Ghostwriter's chat, so it is never deleted: 400 "invalid session", nothing recorded."""
    own = keep(store, PG)
    r = delete(client, without_session(session))
    assert (r.status_code, r.json()) == (400, {"detail": "invalid session"})
    assert store.files_in_chat(PG) == [own]
    assert chat_events(audit_lines) == []


@pytest.mark.parametrize("headers", [H, HD], ids=["open-webui", "docint"])
def test_open_webui_and_docint_find_no_chat_delete(
    client: TestClient, store: Store, audit_lines: AuditLines, headers: dict[str, str]
) -> None:
    """The chat delete is Ghostwriter's alone: the others' keys find no such route, and nothing goes."""
    theirs = put(store, "q.csv")
    r = delete(client, headers)
    assert (r.status_code, r.json()) == NOT_FOUND
    assert store.get(ME, theirs.virtual_path) == theirs
    assert chat_events(audit_lines) == []


@pytest.mark.parametrize("headers", [H, HD], ids=["open-webui", "docint"])
def test_without_the_gate_the_route_still_refuses_them(
    app: FastAPI, store: Store, audit_lines: AuditLines, headers: dict[str, str]
) -> None:
    """Were :class:`KeyGate` bypassed, the route's own check answers Open WebUI and docint the same 404."""
    app.user_middleware = [middleware for middleware in app.user_middleware if middleware.cls is not KeyGate]
    theirs = put(store, "q.csv")
    with TestClient(app) as client:
        assert client.get("/nowhere").status_code == 404  # the gate would answer 401: it is really out
        r = delete(client, headers)
    assert (r.status_code, r.json()) == NOT_FOUND
    assert store.get(ME, theirs.virtual_path) == theirs
    assert chat_events(audit_lines) == []


def test_a_late_output_of_a_running_job_is_refused(
    settings: Settings, store: Store, audit: AuditLog, audit_lines: AuditLines
) -> None:
    """A job of the chat still running when the chat goes keeps no output, and the chat folder is not made again.

    The job's event named the output it planned; that output is named again as ``error``.
    """
    keep(store, PG)
    fake = FakeClient(settings.data_dir)
    fake.hold = {"ghostwriter/user-a"}
    fake.files = {"r.csv": b"a\n1\n"}
    jobs = JobQueue(settings, fake, store, Metrics(settings.data_dir))

    def decide(result: JobResult, planned: Sequence[str]) -> tuple[list[str], str, dict[str, object]]:
        return list(planned), "ok", {"outputs": len(planned)}

    keeper = OutputKeeper(audit, PG, "run_python", [], decide)
    with TestClient(create_app(settings, store=store, audit=audit, jobs=jobs)) as client:
        portal = client.portal
        assert portal is not None
        running = portal.start_task_soon(jobs.submit, PG, "run_python", python_args("print(1)", 60), [], keeper)
        try:
            portal.call(until, lambda: fake.running == 1)
            r = delete(client)
        finally:
            portal.call(fake.gate.set)
        _, _, made = running.result(timeout=10)
    assert (r.status_code, r.json()) == (200, {"deleted": 1})
    assert made == []
    assert store.files_in_chat(PG) == []
    assert not chat_folder(settings).exists()
    ran = [event for event in audit_lines() if event["operation"] == "tool.run_python"]
    assert [event["outcome"] for event in ran] == ["ok", "error"]
    (planned,) = ran[0]["target"]["ids"]
    assert ran[1]["target"]["ids"] == [planned]


def test_a_required_purpose_is_428_for_delete_when_listed(
    settings: Settings, store: Store, audit: AuditLog, audit_lines: AuditLines
) -> None:
    """With ``delete`` requiring a purpose, a delete without one is refused and recorded as denied; with one it goes."""
    required = dataclasses.replace(settings, require_purpose=frozenset({"delete"}))
    own = keep(store, PG)
    with TestClient(create_app(required, store=store, audit=audit)) as strict:
        r = delete(strict)
        assert (r.status_code, r.json()) == (428, {"detail": "a stated purpose is required"})
        (denied,) = chat_events(audit_lines)
        assert (denied["operation"], denied["outcome"]) == ("chat.delete", "denied")
        assert "target" not in denied
        assert denied["criteria"] == {
            "caller": "ghostwriter",
            "refused": "purpose_required",
            "container": "chat:chat-1",
        }
        assert store.files_in_chat(PG) == [own]

        r = delete(strict, {**HG, **PURPOSE})
        assert (r.status_code, r.json()) == (200, {"deleted": 1})
        assert store.files_in_chat(PG) == []
