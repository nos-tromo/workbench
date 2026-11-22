"""Ghostwriter takes back one upload its user has not sent: recorded by id first, then the file and its renders go.

Only an upload of the request's own chat goes; a tool's output, another chat's file and a render are not found. A
job still running on the file keeps no render, as its source is gone. A refusal remembered for the file goes with it.
"""

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api.conftest import HD, HG, HG_B, PG, PG_SETUP, put
from tests.api.test_chat_delete import without_session
from tests.api.test_files_ghostwriter import P2, PURPOSE, keep
from tests.api.test_store import derive, stage
from workbench.api import store as store_module
from workbench.api.audit import AuditLog
from workbench.api.auth import KeyGate
from workbench.api.main import create_app
from workbench.api.store import NotFound, Store, new_file_id
from workbench.config import Settings
from workbench.layout import WORKSPACES
from workbench.models import FileRecord

AuditLines = Callable[[], list[dict[str, Any]]]
NOT_FOUND = (404, {"detail": "not found"})
REFUSED = "unreadable_file: the file is damaged"


def delete(client: TestClient, path: str, headers: Mapping[str, str] = HG) -> Any:
    """Delete the upload at ``path``, as Ghostwriter does unless other headers are given."""
    return client.delete("/files/upload", params={"path": path}, headers=headers)


def delete_events(audit_lines: AuditLines) -> list[dict[str, Any]]:
    """The audit events of upload deletes, in order."""
    return [event for event in audit_lines() if event["operation"] == "file.delete"]


def output(store: Store, tmp_path: Path, name: str = "chart.png") -> FileRecord:
    """A file a ``run_python`` run made in the chat: an output, not an upload."""
    job = store.add_job(PG, "run_python")
    staged = stage(tmp_path, new_file_id(), name, b"synthetic chart bytes")
    return store.register_output(PG_SETUP, staged, name, job.id, file_id=new_file_id(), quota_bytes=10**12)


def test_an_upload_and_its_renders_go_after_their_event(
    client: TestClient, store: Store, audit_lines: AuditLines, tmp_path: Path, settings: Settings
) -> None:
    """The upload, its renders and their files go; another upload of the chat stays.

    One event names the file and its renders by id, with the chat as container, the stated purpose and the request id.
    """
    taken, other = keep(store, PG, "quarterly-figures.csv"), keep(store, PG, "harbour-report.csv")
    renders = [derive(store, tmp_path, taken, n) for n in (1, 2)]
    on_disk = settings.data_dir / WORKSPACES / "ghostwriter" / "user-a" / "chat-1" / "quarterly-figures.csv"
    assert on_disk.is_file()
    r = delete(client, taken.virtual_path, {**HG, **PURPOSE})
    assert (r.status_code, r.json()) == (200, {"deleted": 1, "renders": 2})
    with pytest.raises(NotFound):
        store.get(PG, taken.virtual_path)
    assert store.derived_of(taken) == []
    assert not on_disk.exists()
    assert store.files_in_chat(PG) == [other]

    (event,) = delete_events(audit_lines)
    assert (event["action"], event["outcome"]) == ("delete", "ok")
    assert event["actor"] == {"user": "user-a", "via": "service"}
    assert sorted(event["target"].pop("ids")) == sorted([taken.id, *(render.id for render in renders)])
    assert event["target"] == {"kind": "file", "count": 3, "container": "chat:chat-1"}
    assert event["criteria"] == {"caller": "ghostwriter", "renders": 2}
    assert event["purpose"] == {"reason": "ghostwriter chat"}
    assert event["request_id"] == "req-1"


def test_what_is_no_upload_of_the_chat_is_not_found(
    client: TestClient, store: Store, audit_lines: AuditLines, tmp_path: Path
) -> None:
    """A tool's output, another chat's upload, a render's path and a missing file are 404, and nothing is recorded."""
    made = output(store, tmp_path)
    theirs = keep(store, P2, "harbour-report.csv")
    source = keep(store, PG, "quarterly-figures.csv")
    derive(store, tmp_path, source)
    for path in (
        made.virtual_path,
        theirs.virtual_path,
        f"/workspace/chat-1/.derived/{source.id}/render.jpg",
        "/workspace/chat-1/missing.csv",
    ):
        r = delete(client, path)
        assert (r.status_code, r.json()) == NOT_FOUND, path
    assert store.get(PG, made.virtual_path) == made
    assert store.get(P2, theirs.virtual_path) == theirs
    assert len(store.derived_of(source)) == 1
    assert delete_events(audit_lines) == []


def test_docint_finds_no_upload_delete(client: TestClient, store: Store, audit_lines: AuditLines) -> None:
    """The upload delete is Ghostwriter's alone: docint's key, with the same user and chat, finds no such route."""
    kept = put(store, "q.csv")
    r = delete(client, kept.virtual_path, HD)
    assert (r.status_code, r.json()) == NOT_FOUND
    assert store.get(PG_SETUP, kept.virtual_path) == kept
    assert delete_events(audit_lines) == []


def test_another_user_with_the_same_chat_id_finds_nothing(
    client: TestClient, store: Store, audit_lines: AuditLines
) -> None:
    """A chat is its user's: another user's request for the same chat id and path finds no file."""
    kept = keep(store, PG)
    r = delete(client, kept.virtual_path, HG_B)
    assert (r.status_code, r.json()) == NOT_FOUND
    assert store.get(PG, kept.virtual_path) == kept
    assert delete_events(audit_lines) == []


@pytest.mark.parametrize("session", [None, "default"])
def test_a_missing_or_default_session_is_400(
    client: TestClient, store: Store, audit_lines: AuditLines, session: str | None
) -> None:
    """``default`` is never Ghostwriter's chat: 400 "invalid session", nothing recorded or deleted."""
    kept = keep(store, PG)
    r = delete(client, kept.virtual_path, without_session(session))
    assert (r.status_code, r.json()) == (400, {"detail": "invalid session"})
    assert store.get(PG, kept.virtual_path) == kept
    assert delete_events(audit_lines) == []


def test_the_chat_folder_is_no_file(client: TestClient, store: Store, audit_lines: AuditLines) -> None:
    """The chat folder itself is refused as no file, as a prepare or a view of it is; nothing is recorded."""
    kept = keep(store, PG)
    r = delete(client, "/workspace/chat-1")
    assert (r.status_code, r.json()) == (400, {"detail": "a file is inside a chat folder"})
    assert store.get(PG, kept.virtual_path) == kept
    assert delete_events(audit_lines) == []


def test_without_the_gate_the_route_still_refuses_docint(app: FastAPI, store: Store, audit_lines: AuditLines) -> None:
    """Were :class:`KeyGate` bypassed, the route's own check answers docint the same 404."""
    app.user_middleware = [middleware for middleware in app.user_middleware if middleware.cls is not KeyGate]
    kept = put(store, "q.csv")
    with TestClient(app) as client:
        assert client.get("/nowhere").status_code == 404  # the gate would answer 401: it is really out
        r = delete(client, kept.virtual_path, HD)
    assert (r.status_code, r.json()) == NOT_FOUND
    assert store.get(PG_SETUP, kept.virtual_path) == kept
    assert delete_events(audit_lines) == []


def test_an_upload_taken_back_twice_at_once_is_recorded_once(store: Store) -> None:
    """Both take-backs found it; the second, finding its row gone under the lock, is not found and records nothing."""
    kept = keep(store, PG)
    recorded: list[tuple[list[str], str]] = []

    def event(ids: Sequence[str], outcome: str) -> None:
        recorded.append((list(ids), outcome))

    assert store.remove_upload(kept, event) == 0
    with pytest.raises(NotFound):
        store.remove_upload(kept, event)
    assert recorded == [([kept.id], "ok")]


def test_nothing_goes_when_the_event_cannot_be_written(broken_audit: None, client: TestClient, store: Store) -> None:
    """Without its event, the delete does not happen: the upload and its row stay."""
    kept = keep(store, PG)
    r = delete(client, kept.virtual_path)
    assert (r.status_code, r.json()) == (500, {"detail": "audit unavailable"})
    assert store.get(PG, kept.virtual_path) == kept


def test_a_removal_that_fails_after_the_event_is_recorded_again(
    client: TestClient, store: Store, audit_lines: AuditLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file stays with its row, so it can be taken back again, and the event is followed by an ``error``."""
    kept = keep(store, PG)

    def refuse(*_: object, **__: object) -> None:
        raise OSError("synthetic removal failure")

    monkeypatch.setattr(store_module, "_unlink", refuse)
    r = delete(client, kept.virtual_path)
    assert (r.status_code, r.json()) == (500, {"detail": "internal error"})
    recorded, again = delete_events(audit_lines)
    assert (recorded["outcome"], again["outcome"]) == ("ok", "error")
    assert recorded["target"]["ids"] == again["target"]["ids"] == [kept.id]
    assert store.get(PG, kept.virtual_path) == kept


def test_a_render_made_after_the_delete_is_refused(client: TestClient, store: Store, tmp_path: Path) -> None:
    """A job still running on the upload when it goes keeps no render: its source is gone."""
    kept = keep(store, PG)
    assert delete(client, kept.virtual_path).status_code == 200
    with pytest.raises(NotFound):
        derive(store, tmp_path, kept)


def test_a_refusal_remembered_for_the_upload_goes_with_it(client: TestClient, store: Store) -> None:
    """A refusal the api remembered for the upload is forgotten, so a file uploaded again is tried again."""
    kept = keep(store, PG)
    store.remember_refusal(kept, REFUSED)
    assert store.refusal_of(kept) == REFUSED
    assert delete(client, kept.virtual_path).status_code == 200
    assert store.refusal_of(kept) is None


def test_a_required_purpose_is_428_for_delete_when_listed(
    settings: Settings, store: Store, audit: AuditLog, audit_lines: AuditLines
) -> None:
    """With ``delete`` requiring a purpose, a delete without one is refused and recorded as denied; with one it goes."""
    required = dataclasses.replace(settings, require_purpose=frozenset({"delete"}))
    kept = keep(store, PG)
    with TestClient(create_app(required, store=store, audit=audit)) as strict:
        r = delete(strict, kept.virtual_path)
        assert (r.status_code, r.json()) == (428, {"detail": "a stated purpose is required"})
        (denied,) = delete_events(audit_lines)
        assert (denied["action"], denied["outcome"]) == ("delete", "denied")
        assert store.get(PG, kept.virtual_path) == kept
        r = delete(strict, kept.virtual_path, {**HG, **PURPOSE})
    assert (r.status_code, r.json()) == (200, {"deleted": 1, "renders": 0})
