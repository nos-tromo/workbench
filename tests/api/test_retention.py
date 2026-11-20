"""Retention: workspace files are deleted the period after they were made, and never without their record first.

The sweeper runs over a real store and a real audit chain on a throwaway data volume, with a clock the test moves; the
exit it would end the process with is a function the test gives it. The dry run reads a database the test built.
"""

import asyncio
import contextlib
import dataclasses
import io
import logging
import os
import shutil
import sqlite3
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import CALL, FORMER, GHOSTWRITER_KEY, PG_SETUP, AuditLines, unrecorded
from tests.api.test_jobs import KEEP, FakeClient, until
from workbench.api import retention as retention_module
from workbench.api import store as store_module
from workbench.api.audit import AuditLog
from workbench.api.jobs import JobQueue
from workbench.api.main import app_factory, create_app
from workbench.api.metrics import Metrics
from workbench.api.retention import Sweeper, SweepReport, main
from workbench.api.store import NotFound, Store, new_file_id
from workbench.config import Settings
from workbench.models import DerivedRecord, DerivedSpec, FileRecord, Principal
from workbench.paths import Workspaces
from workbench.utils.audit_event import AuditWriteError

#: The test's start: 2027-01-15T08:00:00Z.
T0 = 1_800_000_000.0
DAY = 86_400
HOUR = 3600


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: float) -> None:
        """Start at ``now`` seconds."""
        self.now = now

    def __call__(self) -> float:
        """Return the current test time."""
        return self.now


@pytest.fixture
def anyio_backend() -> str:
    """Run the async tests on asyncio, the loop the api runs on."""
    return "asyncio"


@pytest.fixture
def clock() -> Clock:
    """The time of the store and the sweeper."""
    return Clock(T0)


@pytest.fixture
def store(settings: Settings, clock: Clock) -> Store:
    """A store on the throwaway data volume whose files are made at the test's time."""
    return Store(settings.data_dir / "workbench.db", Workspaces(settings.data_dir), clock)


@pytest.fixture
def metrics(settings: Settings) -> Metrics:
    """The metrics a sweep refreshes."""
    return Metrics(settings.data_dir)


@pytest.fixture
def exits() -> list[int]:
    """The statuses the sweeper asked to end the process with."""
    return []


@pytest.fixture
def sweeper(
    store: Store, audit: AuditLog, settings: Settings, metrics: Metrics, clock: Clock, exits: list[int]
) -> Sweeper:
    """A sweeper over the store, its audit chain started."""
    audit.start({})
    return Sweeper(store, audit, settings, metrics, clock, exit_process=exits.append)


@pytest.fixture
def broken_audit(sweeper: Sweeper, audit_stream: io.StringIO, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every audit write fail from now on, as a full or closed output would."""

    def refuse(_text: str, /) -> int:
        raise OSError("synthetic audit stream failure")

    monkeypatch.setattr(audit_stream, "write", refuse)


def put(store: Store, name: str, p: Principal = PG_SETUP) -> FileRecord:
    """Store a small upload for ``p`` in its chat folder."""
    return store.add_upload(
        p, f"/workspace/{p.chat_id}", name, io.BytesIO(b"x"), max_bytes=100, quota_bytes=10**9, record=unrecorded
    )


def render(store: Store, settings: Settings, source: FileRecord, n: int = 1) -> DerivedRecord:
    """Register a render of the page ``n`` of ``source``, staged as a job leaves one."""
    folder = settings.data_dir / "staging" / new_file_id()
    folder.mkdir(parents=True)
    staged = folder / "render.jpg"
    staged.write_bytes(b"synthetic render")
    spec = DerivedSpec("page", n, "image/jpeg", 10, 20, {})
    return store.register_derived(source, staged, spec, file_id=new_file_id(), quota_bytes=10**9)


def render_on_disk(settings: Settings, rec: DerivedRecord) -> Path:
    """Where a render's bytes are."""
    chat = settings.data_dir / "workspaces" / rec.caller / rec.user_id / rec.chat_id
    return chat / ".derived" / rec.source_id / rec.name


def every_render(store: Store) -> list[DerivedRecord]:
    """Every render the store holds, whoever's."""
    return store.due_derived(float("inf"), 10**6)


def sweeps(audit_lines: AuditLines) -> list[dict[str, Any]]:
    """The retention events, in order."""
    return [e for e in audit_lines() if e["operation"] == "retention.sweep"]


def on_disk(settings: Settings, rec: FileRecord) -> Path:
    """Where a file's bytes are."""
    return settings.data_dir / "workspaces" / rec.caller / rec.user_id / rec.chat_id / rec.name


def iso(moment: float) -> str:
    """A time as the audit chain writes one."""
    return datetime.fromtimestamp(moment, tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- the files ----------------------------------------------------------------------------------------------------


def test_due_files_are_recorded_then_deleted(
    sweeper: Sweeper,
    store: Store,
    settings: Settings,
    clock: Clock,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Created more than 7 days ago: one ``delete`` event of the system names them, then file and row go."""
    old = [put(store, f"quarterly-figures-{n}.xlsx") for n in range(3)]
    clock.now = T0 + DAY
    recent = put(store, "notes.txt")
    clock.now = T0 + 7 * DAY + 1
    recorded_first: list[bool] = []
    remove = store.remove

    def watched(rec: FileRecord, derived: Sequence[DerivedRecord] = ()) -> None:
        recorded_first.append(any(rec.id in e["target"]["ids"] for e in sweeps(audit_lines)))
        remove(rec, derived)

    monkeypatch.setattr(store, "remove", watched)
    assert sweeper.sweep_once() == SweepReport(deleted=3, errors=0)
    assert recorded_first == [True, True, True]
    (event,) = sweeps(audit_lines)
    assert (event["action"], event["outcome"], event["actor"]) == ("delete", "ok", {"user": None, "via": "system"})
    assert sorted(event["target"]["ids"]) == sorted(rec.id for rec in old)
    assert event["target"]["count"] == 3
    assert event["criteria"] == {
        "period_days": 7,
        "cutoff": iso(T0 + 1),
        "caller": "ghostwriter",
        "owner": "user-a",
        "count": 3,
        "derived": 0,
    }
    assert store.files_in_chat(PG_SETUP) == [recent]
    assert not any(on_disk(settings, rec).exists() for rec in old)
    assert on_disk(settings, recent).exists()
    assert "quarterly-figures" not in json_text(audit_lines)


def test_a_workspace_left_by_a_former_caller_still_ages_out(
    sweeper: Sweeper, store: Store, settings: Settings, clock: Clock, audit_lines: AuditLines
) -> None:
    """A file under the folder of a caller no key names past the period is swept like any other.

    No key opens that folder; retention still records the file by its id, then deletes it and its row.
    """
    left = FORMER
    rec = put(store, "quarterly-figures.xlsx", left)
    clock.now = T0 + 7 * DAY + 1
    assert sweeper.sweep_once() == SweepReport(deleted=1, errors=0)
    (event,) = sweeps(audit_lines)
    assert (event["action"], event["outcome"], event["target"]["ids"]) == ("delete", "ok", [rec.id])
    assert (event["criteria"]["caller"], event["criteria"]["owner"]) == ("former-caller", "user-a")
    assert store.files_in_chat(left) == []
    assert not on_disk(settings, rec).exists()


def json_text(audit_lines: AuditLines) -> str:
    """Every audit line, as one text."""
    return "\n".join(str(event) for event in audit_lines())


def test_nothing_is_deleted_without_its_record(
    sweeper: Sweeper,
    store: Store,
    settings: Settings,
    clock: Clock,
    broken_audit: None,
    exits: list[int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A record that cannot be written ends the process, before a single file goes; stderr has the count alone."""
    files = [put(store, "quarterly-figures.xlsx"), put(store, "notes.txt")]
    clock.now = T0 + 8 * DAY
    with pytest.raises(AuditWriteError):
        sweeper.sweep_once()
    assert exits == [1]
    assert sorted(store.files_in_chat(PG_SETUP), key=lambda rec: rec.id) == sorted(files, key=lambda rec: rec.id)
    assert all(on_disk(settings, rec).exists() for rec in files)
    err = capsys.readouterr().err
    assert "2 files" in err
    assert "quarterly-figures" not in err
    assert "user-a" not in err


def test_failed_removal_is_recorded_as_error(
    sweeper: Sweeper,
    store: Store,
    settings: Settings,
    clock: Clock,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """A file that cannot go keeps its row and is recorded again as an error; the sweep goes on with the others."""
    gone, busy, odd = put(store, "a.txt"), put(store, "b.txt"), put(store, "c.txt")
    elsewhere = Principal("ghostwriter", "user-a", "chat-2")
    linked = put(store, "d.txt", elsewhere)
    chat = settings.data_dir / "workspaces" / "ghostwriter" / "user-a" / "chat-2"
    chat.rename(tmp_path / "moved")
    chat.symlink_to(tmp_path / "moved")  # a link in the path: the store refuses it with a PathError
    clock.now = T0 + 8 * DAY
    remove = store.remove

    def failing(rec: FileRecord, derived: Sequence[DerivedRecord] = ()) -> None:
        if rec.id == busy.id:
            raise OSError("synthetic: device busy")
        if rec.id == odd.id:
            raise RuntimeError("synthetic: unexpected")
        remove(rec, derived)

    monkeypatch.setattr(store, "remove", failing)
    with caplog.at_level(logging.WARNING):
        assert sweeper.sweep_once() == SweepReport(deleted=1, errors=3)
    ok, error = sweeps(audit_lines)
    assert (ok["outcome"], sorted(ok["target"]["ids"])) == ("ok", sorted([gone.id, busy.id, odd.id, linked.id]))
    assert error["outcome"] == "error"
    assert sorted(error["target"]["ids"]) == sorted([busy.id, odd.id, linked.id])
    assert error["criteria"] == {**ok["criteria"], "count": 3}
    assert sorted(rec.id for rec in store.due(T0 + 1)) == sorted(rec.id for rec in [busy, odd, linked])
    assert {"OSError", "RuntimeError", "PathError"} <= {word.strip("()") for word in caplog.text.split()}
    assert "synthetic" not in caplog.text


def test_an_error_record_that_cannot_be_written_halts_too(
    sweeper: Sweeper,
    store: Store,
    clock: Clock,
    audit_stream: io.StringIO,
    audit_lines: AuditLines,
    exits: list[int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ``ok`` record is written, a removal fails, and its ``error`` record cannot be written: the process ends."""
    kept, stuck = put(store, "a.txt"), put(store, "salaries-of-user-b.txt")
    clock.now = T0 + 8 * DAY
    remove = store.remove

    def failing(rec: FileRecord, derived: Sequence[DerivedRecord] = ()) -> None:
        if rec.id == stuck.id:
            raise OSError("synthetic: device busy")
        remove(rec, derived)

    write = audit_stream.write

    def no_error_record(text: str, /) -> int:
        if '"outcome":"error"' in text.replace(" ", ""):
            raise OSError("synthetic audit stream failure")
        return write(text)

    monkeypatch.setattr(store, "remove", failing)
    monkeypatch.setattr(audit_stream, "write", no_error_record)
    with pytest.raises(AuditWriteError):
        sweeper.sweep_once()
    assert exits == [1]
    (ok,) = sweeps(audit_lines)
    assert (ok["outcome"], sorted(ok["target"]["ids"])) == ("ok", sorted([kept.id, stuck.id]))
    assert [rec.id for rec in store.due(T0 + 1)] == [stuck.id]
    err = capsys.readouterr().err
    assert "1 file " in err
    assert "salaries" not in err


def test_batches_never_mix_users(sweeper: Sweeper, store: Store, clock: Clock, audit_lines: AuditLines) -> None:
    """Each event is one caller's user, at most 200 files; together they name every due file once."""
    people = [
        Principal("ghostwriter", "user-a", "chat-1"),
        Principal("ghostwriter", "user-b", "chat-1"),
        Principal("docint", "user-a", "chat-1"),
    ]
    made: list[FileRecord] = []
    for n in range(330):
        clock.now = T0 + n
        who = people[n % 3] if n < 30 else people[0]
        made.append(put(store, f"f{n:03d}.txt", who))
    clock.now = T0 + 8 * DAY
    assert sweeper.sweep_once() == SweepReport(deleted=330, errors=0)
    owners = {rec.id: (rec.caller, rec.user_id) for rec in made}
    named: list[str] = []
    for event in sweeps(audit_lines):
        ids = event["target"]["ids"]
        assert 1 <= len(ids) <= 200
        assert {owners[i] for i in ids} == {(event["criteria"]["caller"], event["criteria"]["owner"])}
        assert event["criteria"]["count"] == event["target"]["count"] == len(ids)
        named.extend(ids)
    assert sorted(named) == sorted(owners)


def test_files_that_keep_failing_never_stall_the_sweep(
    sweeper: Sweeper, store: Store, clock: Clock, audit_lines: AuditLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 200 oldest cannot be removed; the sweep pages on past them and deletes the other 50 the same time."""
    made = []
    for n in range(250):
        clock.now = T0 + n
        made.append(put(store, f"f{n:03d}.txt"))
    stuck = {rec.id for rec in made[:200]}
    remove = store.remove

    def failing(rec: FileRecord, derived: Sequence[DerivedRecord] = ()) -> None:
        if rec.id in stuck:
            raise OSError("synthetic: read-only")
        remove(rec, derived)

    monkeypatch.setattr(store, "remove", failing)
    clock.now = T0 + 8 * DAY
    assert sweeper.sweep_once() == SweepReport(deleted=50, errors=200)
    assert {rec.id for rec in store.files_in_chat(PG_SETUP)} == stuck


@pytest.mark.anyio
async def test_sweep_during_running_job(
    sweeper: Sweeper, store: Store, settings: Settings, clock: Clock, metrics: Metrics, audit_lines: AuditLines
) -> None:
    """An input expires while its job runs (the job has its copy): it goes with its record; the output is kept."""
    old = put(store, "quarterly-figures.xlsx")
    executor = FakeClient(settings.data_dir)
    executor.hold = {"ghostwriter/user-a"}
    executor.files = {"chart.png": b"\x89PNG"}
    queue = JobQueue(settings, executor, store, metrics)
    clock.now = T0 + 8 * DAY
    running = asyncio.create_task(
        queue.submit(PG_SETUP, "run_python", {"code": "print(1)"}, [old.name], KEEP, call=CALL)
    )
    await until(lambda: executor.running == 1)
    report = await asyncio.to_thread(sweeper.sweep_once)
    assert report == SweepReport(deleted=1, errors=0)
    (event,) = sweeps(audit_lines)
    assert event["target"]["ids"] == [old.id]
    executor.gate.set()
    job, result, made = await running
    assert (result.status, [rec.name for rec in made]) == ("ok", ["chart.png"])
    assert store.files_in_chat(PG_SETUP) == made
    assert store.get_job(PG_SETUP, job.id).status == "ok"


# --- job records, folders and staging -----------------------------------------------------------------------------


def test_old_job_records_are_forgotten_without_a_record(
    sweeper: Sweeper, store: Store, clock: Clock, audit_lines: AuditLines
) -> None:
    """They hold no content: they go without an event."""
    old = store.add_job(PG_SETUP, "run_python")
    clock.now = T0 + 2 * DAY
    new = store.add_job(PG_SETUP, "read_document")
    clock.now = T0 + 8 * DAY
    sweeper.sweep_once()
    with pytest.raises(NotFound):
        store.get_job(PG_SETUP, old.id)
    assert store.get_job(PG_SETUP, new.id) == new
    assert sweeps(audit_lines) == []


def age(path: Path, seconds: float, now: float) -> None:
    """Set a path's times to ``seconds`` before ``now``, without following a link."""
    os.utime(path, (now - seconds, now - seconds), follow_symlinks=False)


def test_empty_folders_untouched_for_an_hour_go_bottom_up(
    sweeper: Sweeper, store: Store, settings: Settings, clock: Clock, tmp_path: Path
) -> None:
    """Empty folders and the chat folder they leave empty go, all levels at once; the user's root never does.

    The clock is the file system's, so a folder whose empty folder was just removed is new again by its own time.
    """
    clock.now = time.time()
    root = settings.data_dir / "workspaces" / "ghostwriter" / "user-a"
    store.mkdir(PG_SETUP, "/workspace/chat-1/reports/2026/q1")
    store.mkdir(PG_SETUP, "/workspace/chat-2/fresh")
    store.mkdir(PG_SETUP, "/workspace/chat-3/kept")
    put(store, "notes.txt", Principal("ghostwriter", "user-a", "chat-3"))
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "chat-4").symlink_to(outside, target_is_directory=True)
    (root / "chat-5").mkdir()
    (root / "chat-5" / "link").symlink_to(outside, target_is_directory=True)
    for folder in [p for p in root.rglob("*") if p.is_dir() and not p.is_symlink()] + [outside]:
        age(folder, 2 * HOUR, clock.now)
    age(root / "chat-4", 2 * HOUR, clock.now)
    age(root / "chat-2" / "fresh", 10 * 60, clock.now)
    sweeper.sweep_once()
    assert not (root / "chat-1").exists()
    assert (root / "chat-2" / "fresh").is_dir()
    assert (root / "chat-3").is_dir()
    assert not (root / "chat-3" / "kept").exists()
    assert (root / "chat-4").is_symlink() and outside.is_dir()
    assert (root / "chat-5" / "link").is_symlink()
    gone = Principal("ghostwriter", "user-a", "chat-3")
    store.remove(store.get(gone, "/workspace/chat-3/notes.txt"))
    for chat in ("chat-2", "chat-3", "chat-5"):
        (root / chat / "link").unlink(missing_ok=True)
        for folder in sorted(p for p in (root / chat).rglob("*") if p.is_dir()):
            age(folder, 2 * HOUR, clock.now)
        age(root / chat, 2 * HOUR, clock.now)
    sweeper.sweep_once()
    assert sorted(p.name for p in root.iterdir()) == ["chat-4"]
    assert (settings.data_dir / "workspaces" / "ghostwriter").is_dir()


def test_staging_folders_older_than_an_hour_go(
    sweeper: Sweeper, settings: Settings, clock: Clock, tmp_path: Path, audit_lines: AuditLines
) -> None:
    """A job's staging folder its client never collected goes with what is in it; nothing a link points to does."""
    staging = settings.data_dir / "staging"
    old, new = staging / ("a" * 32), staging / ("b" * 32)
    for folder in (old, new):
        folder.mkdir(parents=True)
        (folder / "chart.png").write_bytes(b"\x89PNG")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"x")
    (staging / ("c" * 32)).symlink_to(outside, target_is_directory=True)
    (staging / "stray").write_bytes(b"x")
    for path in (old, outside, staging / ("c" * 32), staging / "stray"):
        age(path, 2 * HOUR, clock.now)
    age(new, 10 * 60, clock.now)
    sweeper.sweep_once()
    assert sorted(p.name for p in staging.iterdir()) == sorted(["b" * 32, "c" * 32, "stray"])
    assert (outside / "keep.txt").exists()
    (event,) = sweeps(audit_lines)
    assert event["criteria"] == {"rule": "staging", "job": "a" * 32, "count": 1}
    assert event["target"] == {"kind": "file", "count": 1}


def test_staging_residue_is_recorded_with_its_jobs_owner_then_removed(
    sweeper: Sweeper,
    store: Store,
    settings: Settings,
    clock: Clock,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outputs the api never registered go after a record that names their job and its owner.

    They hold content made from the user's files.
    """
    clock.now = time.time()
    job = store.add_job(PG_SETUP, "run_python")
    residue = settings.data_dir / "staging" / job.id
    (residue / "deep").mkdir(parents=True)
    (residue / "harbour-chart.png").write_bytes(b"\x89PNG")
    (residue / "deep" / "figures.csv").write_bytes(b"a,b\n")
    (residue / "link").symlink_to(residue / "harbour-chart.png")
    for path in (residue, residue / "deep"):
        age(path, 2 * HOUR, clock.now)
    removed_after: list[bool] = []
    rmtree = shutil.rmtree

    def watched(path: Path, *args: Any, **kwargs: Any) -> None:
        removed_after.append(bool(sweeps(audit_lines)))
        rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", watched)
    sweeper.sweep_once()
    assert removed_after == [True]
    assert not residue.exists()
    (event,) = sweeps(audit_lines)
    assert (event["action"], event["outcome"]) == ("delete", "ok")
    assert event["criteria"] == {
        "rule": "staging",
        "job": job.id,
        "count": 2,
        "caller": "ghostwriter",
        "owner": "user-a",
    }
    assert event["target"] == {"kind": "file", "count": 2, "container": "chat:chat-1"}
    assert "harbour-chart" not in json_text(audit_lines)


def orphan(path: Path, now: float, seconds: float = 2 * HOUR) -> Path:
    """Write a file the store has no row for, last changed ``seconds`` before ``now``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic")
    age(path, seconds, now)
    return path


def test_orphans_are_recorded_then_removed(
    sweeper: Sweeper,
    store: Store,
    settings: Settings,
    clock: Clock,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash's ``.part`` and a file whose row was never committed go after an hour, recorded first.

    One record per chat folder, by the ids their names carry, never by a name. A file with its row, or changed within
    the hour, stays.
    """
    clock.now = time.time()
    kept = put(store, "notes.txt")
    chat = on_disk(settings, kept).parent
    age(on_disk(settings, kept), 2 * HOUR, clock.now)
    part = orphan(chat / ("f" * 32 + ".part"), clock.now)
    lost = orphan(chat / "sub" / "lost-report.docx", clock.now)
    age(chat / "sub", 2 * HOUR, clock.now)
    fresh = orphan(chat / "fresh-orphan.txt", clock.now, seconds=10 * 60)
    elsewhere = orphan(chat.with_name("chat-2") / "stray-sheet.xlsx", clock.now)
    unlinked_after: list[bool] = []
    unlink = store_module._unlink
    root = chat.parent

    def watched(path: Path) -> None:
        container = f"chat:{path.relative_to(root).parts[0]}"
        unlinked_after.append(any(e["target"].get("container") == container for e in sweeps(audit_lines)))
        unlink(path)

    monkeypatch.setattr(store_module, "_unlink", watched)
    sweeper.sweep_once()
    assert unlinked_after == [True, True, True]
    assert not part.exists() and not lost.exists() and not elsewhere.exists()
    assert not (chat / "sub").exists()  # emptied by the sweep, untouched for an hour before it
    assert fresh.exists() and on_disk(settings, kept).exists()
    first, second = sweeps(audit_lines)
    assert first["criteria"] == {"rule": "orphan", "caller": "ghostwriter", "owner": "user-a", "count": 2}
    assert first["target"] == {"kind": "file", "ids": ["f" * 32], "count": 2, "container": "chat:chat-1"}
    assert second["criteria"] == {"rule": "orphan", "caller": "ghostwriter", "owner": "user-a", "count": 1}
    assert second["target"] == {"kind": "file", "count": 1, "container": "chat:chat-2"}
    text = json_text(audit_lines)
    assert [name for name in ("lost-report", "stray-sheet", "fresh-orphan", "notes.txt") if name in text] == []


@pytest.mark.parametrize("left", ["orphan", "staging"])
def test_an_unwritable_record_of_an_orphan_or_staging_halts_with_nothing_removed(
    sweeper: Sweeper,
    store: Store,
    settings: Settings,
    clock: Clock,
    broken_audit: None,
    exits: list[int],
    left: str,
) -> None:
    """The same rule as for the files with rows: no record, no removal, and the process ends."""
    clock.now = time.time()
    if left == "orphan":
        path = orphan(settings.data_dir / "workspaces" / "ghostwriter" / "user-a" / "chat-1" / "lost.txt", clock.now)
    else:
        path = orphan(settings.data_dir / "staging" / ("a" * 32) / "chart.png", clock.now)
        age(path.parent, 2 * HOUR, clock.now)
    with pytest.raises(AuditWriteError):
        sweeper.sweep_once()
    assert exits == [1]
    assert path.exists()


@pytest.mark.parametrize("way", ["upload", "output"])
def test_a_file_on_its_way_into_an_old_empty_chat_folder_keeps_its_folder(
    sweeper: Sweeper, store: Store, settings: Settings, clock: Clock, monkeypatch: pytest.MonkeyPatch, way: str
) -> None:
    """A file on its way into an old, empty chat folder never finds the folder gone.

    The store finds or makes the folder and puts the file into it under its lock, which the sweep's removal holds too.
    """
    clock.now = time.time()
    store.mkdir(PG_SETUP, "/workspace/chat-1")
    root = settings.data_dir / "workspaces" / "ghostwriter" / "user-a"
    for folder in (root / "chat-1", root):
        age(folder, 2 * HOUR, clock.now)
    found, go_on = threading.Event(), threading.Event()
    if way == "upload":
        original: Callable[..., Any] = store._require_folder

        def slow(*args: Any) -> Any:
            answer = original(*args)
            found.set()
            go_on.wait(5)
            return answer

        monkeypatch.setattr(store, "_require_folder", slow)
        staged = None
    else:
        original = store._ws.chat_dir

        def slow(*args: Any) -> Any:
            answer = original(*args)
            found.set()
            go_on.wait(5)
            return answer

        monkeypatch.setattr(store._ws, "chat_dir", slow)
        job_id = store.add_job(PG_SETUP, "run_python").id
        staged = settings.data_dir / "staging" / job_id / "chart.png"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"\x89PNG")
        age(staged, 2 * HOUR, clock.now)  # a rename keeps the time: only the lock keeps the sweep off it until its row
    made: list[FileRecord] = []

    def arrive() -> None:
        if staged is None:
            made.append(put(store, "quarterly-figures.xlsx"))
        else:
            made.append(
                store.register_output(PG_SETUP, staged, "chart.png", job_id, file_id=new_file_id(), quota_bytes=10**12)
            )

    arriving = threading.Thread(target=arrive)
    arriving.start()
    assert found.wait(5)
    sweeping = threading.Thread(target=sweeper.sweep_once)
    sweeping.start()
    time.sleep(0.2)
    go_on.set()
    arriving.join(5)
    sweeping.join(5)
    (rec,) = made
    assert on_disk(settings, rec).exists()
    assert store.files_in_chat(PG_SETUP) == [rec]


def test_an_empty_user_root_untouched_for_an_hour_goes(
    sweeper: Sweeper, store: Store, settings: Settings, clock: Clock
) -> None:
    """It names a pseudonymous user past the period; ``workspaces/`` and the caller's folder stay, even empty."""
    clock.now = time.time()
    workspaces = settings.data_dir / "workspaces"
    gone, fresh, busy = (workspaces / "ghostwriter" / name for name in ("user-a", "user-b", "user-c"))
    for folder in (gone, fresh):
        folder.mkdir(parents=True)
    put(store, "notes.txt", Principal("ghostwriter", "user-c", "chat-1"))
    for folder in (gone, busy, busy / "chat-1", workspaces / "ghostwriter", workspaces):
        age(folder, 2 * HOUR, clock.now)
    age(on_disk(settings, store.files_in_chat(Principal("ghostwriter", "user-c", "chat-1"))[0]), 2 * HOUR, clock.now)
    age(fresh, 10 * 60, clock.now)
    sweeper.sweep_once()
    assert sorted(p.name for p in (workspaces / "ghostwriter").iterdir()) == ["user-b", "user-c"]
    user_c = Principal("ghostwriter", "user-c", "chat-1")
    store.remove(store.get(user_c, "/workspace/chat-1/notes.txt"))
    for folder in (fresh, busy / "chat-1", busy):
        age(folder, 2 * HOUR, clock.now)
    sweeper.sweep_once()
    assert list((workspaces / "ghostwriter").iterdir()) == []
    assert workspaces.is_dir() and (workspaces / "ghostwriter").is_dir()


def test_staging_and_workspaces_that_are_links_are_not_followed(
    sweeper: Sweeper, settings: Settings, clock: Clock, tmp_path: Path
) -> None:
    """The volume's own folders must be folders of it: a link in their place is refused, and nothing behind it goes."""
    outside = tmp_path / "outside"
    for name in ("staging", "workspaces"):
        target = outside / name / ("d" * 32) / "user-a" / "chat-1"
        target.mkdir(parents=True)
        (settings.data_dir / name).symlink_to(outside / name, target_is_directory=True)
        for folder in [target, *target.parents]:
            if folder == outside.parent:
                break
            age(folder, 2 * HOUR, clock.now)
    sweeper.sweep_once()
    for name in ("staging", "workspaces"):
        assert (outside / name / ("d" * 32) / "user-a" / "chat-1").is_dir()


def test_an_orphan_that_cannot_be_removed_is_recorded_again_as_an_error(
    sweeper: Sweeper, settings: Settings, clock: Clock, audit_lines: AuditLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As for a file with a row: the orphans that stay are named again, ``outcome: error``, by id where known."""
    clock.now = time.time()
    chat = settings.data_dir / "workspaces" / "ghostwriter" / "user-a" / "chat-1"
    stuck, gone = (orphan(chat / f"{letter * 32}.part", clock.now) for letter in "ef")
    loose = orphan(chat / "lost-report.docx", clock.now)
    unlink = store_module._unlink

    def failing(path: Path) -> None:
        if path in (stuck, loose):
            raise PermissionError("synthetic: busy")
        unlink(path)

    monkeypatch.setattr(store_module, "_unlink", failing)
    sweeper.sweep_once()
    assert stuck.exists() and loose.exists() and not gone.exists()
    ok, error = sweeps(audit_lines)
    assert (ok["outcome"], sorted(ok["target"]["ids"]), ok["target"]["count"]) == ("ok", ["e" * 32, "f" * 32], 3)
    assert error["outcome"] == "error"
    assert error["target"] == {"kind": "file", "ids": ["e" * 32], "count": 2, "container": "chat:chat-1"}
    assert error["criteria"] == {"rule": "orphan", "caller": "ghostwriter", "owner": "user-a", "count": 2}
    assert "lost-report" not in json_text(audit_lines)


def test_a_staging_folder_that_cannot_be_removed_is_recorded_again_as_an_error(
    sweeper: Sweeper, settings: Settings, clock: Clock, audit_lines: AuditLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The files a failed removal left behind are named again by their count, ``outcome: error``."""
    clock.now = time.time()
    residue = settings.data_dir / "staging" / ("a" * 32)
    for name in ("harbour-chart.png", "figures.csv"):
        orphan(residue / name, clock.now)
    age(residue, 2 * HOUR, clock.now)

    def partly(path: Path, *_args: Any, **_kwargs: Any) -> None:
        (path / "harbour-chart.png").unlink()
        raise PermissionError("synthetic: busy")

    monkeypatch.setattr(shutil, "rmtree", partly)
    sweeper.sweep_once()
    ok, error = sweeps(audit_lines)
    assert (ok["outcome"], ok["criteria"]) == ("ok", {"rule": "staging", "job": "a" * 32, "count": 2})
    assert (error["outcome"], error["criteria"]) == ("error", {"rule": "staging", "job": "a" * 32, "count": 1})
    assert error["target"] == {"kind": "file", "count": 1}
    assert (residue / "figures.csv").exists()


def test_more_orphans_than_one_event_names_are_recorded_in_several(
    sweeper: Sweeper, settings: Settings, clock: Clock, audit_lines: AuditLines
) -> None:
    """At most 200 ids an event: 201 ``.part`` orphans of one chat folder are two events, and no id is cut."""
    clock.now = time.time()
    chat = settings.data_dir / "workspaces" / "ghostwriter" / "user-a" / "chat-1"
    ids = [f"{n:032x}" for n in range(201)]
    for file_id in ids:
        orphan(chat / f"{file_id}.part", clock.now)
    sweeper.sweep_once()
    events = sweeps(audit_lines)
    assert sorted(event["target"]["count"] for event in events) == [1, 200]
    for event in events:
        assert "ids_truncated" not in event["target"]
        assert event["criteria"]["count"] == event["target"]["count"] == len(event["target"]["ids"])
    assert sorted(file_id for event in events for file_id in event["target"]["ids"]) == ids
    assert list(chat.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root looks into a folder whatever its mode")
def test_one_entry_that_cannot_be_looked_at_never_stops_the_prune(
    sweeper: Sweeper,
    store: Store,
    settings: Settings,
    clock: Clock,
    metrics: Metrics,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Chat subfolders that cannot be looked into (mode 0600) or listed (0300) are skipped with a warning, by type.

    Only those entries: the orphan beside them in the same chat folder is recorded and removed, and the other users,
    the staging residue, the checkpoint and the gauges still get their turn, every interval. The mode 0600 folder's
    entries cannot be looked at; the mode 0300 folder runs ``os.walk``'s error hook.
    """
    checkpoints: list[int] = []
    monkeypatch.setattr(store, "checkpoint", lambda: checkpoints.append(1) or True)
    clock.now = time.time() - 8 * DAY
    put(store, "quarterly-figures.xlsx")
    clock.now = time.time()
    workspaces = settings.data_dir / "workspaces" / "ghostwriter"
    chat = workspaces / "user-a" / "chat-1"
    sealed, unlisted = chat / "sealed", chat / "unlisted"
    orphan(sealed / "inside.txt", clock.now)
    orphan(unlisted / "hidden.txt", clock.now)
    beside = orphan(chat / "lost-draft.docx", clock.now)
    lost = orphan(workspaces / "user-b" / "chat-1" / "lost-report.docx", clock.now)
    residue = orphan(settings.data_dir / "staging" / ("a" * 32) / "harbour-chart.png", clock.now)
    age(residue.parent, 2 * HOUR, clock.now)
    sealed.chmod(0o600)
    unlisted.chmod(0o300)
    try:
        with caplog.at_level(logging.WARNING):
            assert sweeper.sweep_once() == SweepReport(deleted=1, errors=0)
    finally:
        sealed.chmod(0o700)
        unlisted.chmod(0o700)
    assert not beside.exists() and not lost.exists() and not residue.parent.exists()
    assert (sealed / "inside.txt").exists() and (unlisted / "hidden.txt").exists()
    assert checkpoints == [1]
    assert metrics.registry.get_sample_value("workbench_files_total") == 0
    assert "could not look at (PermissionError)" in caplog.text
    assert "could not list (PermissionError)" in caplog.text
    assert [name for name in ("sealed", "inside", "unlisted", "hidden", "lost-draft") if name in caplog.text] == []
    orphans = [event for event in sweeps(audit_lines) if event["criteria"].get("rule") == "orphan"]
    assert sorted(event["criteria"]["owner"] for event in orphans) == ["user-a", "user-b"]
    rules = sorted(event["criteria"].get("rule", "period") for event in sweeps(audit_lines))
    assert rules == ["orphan", "orphan", "period", "staging"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root looks into a folder whatever its mode")
def test_a_staging_folder_that_cannot_be_looked_at_is_skipped_with_a_warning(
    sweeper: Sweeper, settings: Settings, clock: Clock, audit_lines: AuditLines, caplog: pytest.LogCaptureFixture
) -> None:
    """``staging`` of mode 0600 lists its job folders, but none can be looked at: each is skipped, logged by type."""
    clock.now = time.time()
    staging = settings.data_dir / "staging"
    residue = orphan(staging / ("a" * 32) / "harbour-chart.png", clock.now)
    age(residue.parent, 2 * HOUR, clock.now)
    staging.chmod(0o600)
    try:
        with caplog.at_level(logging.WARNING):
            sweeper.sweep_once()
    finally:
        staging.chmod(0o700)
    assert residue.exists()
    assert sweeps(audit_lines) == []
    assert "could not look at (PermissionError)" in caplog.text
    assert "a" * 32 not in caplog.text and "harbour" not in caplog.text


def test_a_staging_folder_gone_before_it_is_looked_at_is_skipped_silently(
    sweeper: Sweeper,
    settings: Settings,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A job folder the queue removed between the listing and the look is no reason for a warning."""
    clock.now = time.time()
    residue = orphan(settings.data_dir / "staging" / ("a" * 32) / "harbour-chart.png", clock.now)
    age(residue.parent, 2 * HOUR, clock.now)
    lstat = os.lstat

    def gone(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if Path(path).name == "a" * 32:
            raise FileNotFoundError(2, "synthetic")
        return lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", gone)
    with caplog.at_level(logging.WARNING):
        sweeper.sweep_once()
    assert caplog.text == ""


def test_a_stop_during_the_prune_ends_it_before_the_next_user(
    sweeper: Sweeper,
    store: Store,
    settings: Settings,
    clock: Clock,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel asks the prune to stop: no user after the one in hand is swept, and no staging folder."""
    clock.now = time.time()
    workspaces = settings.data_dir / "workspaces" / "ghostwriter"
    lost = [orphan(workspaces / user / "chat-1" / "lost-report.docx", clock.now) for user in ("user-a", "user-b")]
    residue = orphan(settings.data_dir / "staging" / ("a" * 32) / "harbour-chart.png", clock.now)
    age(residue.parent, 2 * HOUR, clock.now)
    stop = threading.Event()
    sweep_user = store.sweep_user

    def then_stop(*args: Any, **kwargs: Any) -> tuple[int, int]:
        try:
            return sweep_user(*args, **kwargs)
        finally:
            stop.set()

    monkeypatch.setattr(store, "sweep_user", then_stop)
    sweeper.sweep_once(stop)
    assert sorted(path.exists() for path in lost) == [False, True]
    assert residue.exists()
    assert len(sweeps(audit_lines)) == 1


def test_a_stop_ends_a_users_orphans_between_two_batches(
    sweeper: Sweeper, settings: Settings, clock: Clock, audit_lines: AuditLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Within one user too, a cancel ends the walk before its next record.

    The second batch of 200 waits for the next sweep, and so do the folders the first left empty: an empty chat folder
    beside it, untouched for two hours, is still there.
    """
    clock.now = time.time()
    chat = settings.data_dir / "workspaces" / "ghostwriter" / "user-a" / "chat-1"
    for n in range(201):
        orphan(chat / f"{n:032x}.part", clock.now)
    age(chat, 2 * HOUR, clock.now)
    empty = chat.parent / "chat-2"
    empty.mkdir()
    age(empty, 2 * HOUR, clock.now)
    stop = threading.Event()
    record = sweeper._record_orphans

    def then_stop(*args: Any) -> None:
        try:
            record(*args)
        finally:
            stop.set()

    monkeypatch.setattr(sweeper, "_record_orphans", then_stop)
    sweeper.sweep_once(stop)
    (event,) = sweeps(audit_lines)
    assert event["target"]["count"] == 200
    assert len(list(chat.iterdir())) == 1
    assert empty.is_dir()


def test_a_stop_during_the_prune_ends_it_before_the_next_staging_folder(
    sweeper: Sweeper, settings: Settings, clock: Clock, audit_lines: AuditLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The staging folders are looked at one by one; a stop between two leaves the second to the next sweep."""
    clock.now = time.time()
    residues = [orphan(settings.data_dir / "staging" / (c * 32) / "chart.png", clock.now) for c in "ab"]
    for path in residues:
        age(path.parent, 2 * HOUR, clock.now)
    stop = threading.Event()
    clear = sweeper._clear_staging

    def then_stop(*args: Any) -> None:
        try:
            clear(*args)
        finally:
            stop.set()

    monkeypatch.setattr(sweeper, "_clear_staging", then_stop)
    sweeper.sweep_once(stop)
    assert sorted(path.exists() for path in residues) == [False, True]
    assert len(sweeps(audit_lines)) == 1


# --- after a sweep ------------------------------------------------------------------------------------------------


def test_a_sweep_that_deleted_ends_with_a_checkpoint_and_refreshes_the_metrics(
    sweeper: Sweeper, store: Store, clock: Clock, metrics: Metrics, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The log's old pages hold file names: they are copied over once something was deleted; the gauges follow."""
    checkpoints: list[int] = []
    monkeypatch.setattr(store, "checkpoint", lambda: checkpoints.append(1) or True)
    put(store, "a.txt")
    put(store, "b.txt")
    sweeper.sweep_once()
    assert checkpoints == []
    assert metrics.registry.get_sample_value("workbench_files_total") == 2
    clock.now = T0 + 2 * DAY
    put(store, "c.txt")
    clock.now = T0 + 8 * DAY
    sweeper.sweep_once()
    assert checkpoints == [1]
    assert metrics.registry.get_sample_value("workbench_files_total") == 1


def test_a_busy_checkpoint_is_left_to_a_later_sweep(
    sweeper: Sweeper, store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A reader that keeps the log busy is not an error: the next checkpoint, or the last connection's, carries it."""
    monkeypatch.setattr(store, "checkpoint", lambda: False)
    put(store, "a.txt")
    clock.now = T0 + 8 * DAY
    with caplog.at_level(logging.WARNING):
        assert sweeper.sweep_once() == SweepReport(deleted=1, errors=0)
    assert "checkpoint" in caplog.text


@pytest.mark.parametrize("first", ["busy", "failed"])
def test_a_checkpoint_left_undone_is_carried_by_the_next_sweep_even_one_that_deletes_nothing(
    sweeper: Sweeper, store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    """A busy log, or a checkpoint that failed, is tried again at the next sweep, until one empties the log."""
    checkpoints: list[str] = []

    def checkpoint() -> bool:
        checkpoints.append("busy" if not checkpoints else "emptied")
        if checkpoints == ["busy"] and first == "failed":
            raise sqlite3.OperationalError("synthetic: disk I/O error")
        return checkpoints[-1] == "emptied"

    monkeypatch.setattr(store, "checkpoint", checkpoint)
    put(store, "a.txt")
    clock.now = T0 + 8 * DAY
    assert sweeper.sweep_once().deleted == 1
    assert sweeper.sweep_once().deleted == 0
    assert sweeper.sweep_once().deleted == 0
    assert checkpoints == ["busy", "emptied"]


def test_a_sweep_that_deleted_and_then_failed_still_checkpoints(
    sweeper: Sweeper, store: Store, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The log's old pages hold the names of the files it deleted: an error after the deletions never keeps them."""
    checkpoints: list[int] = []
    monkeypatch.setattr(store, "checkpoint", lambda: checkpoints.append(1) or True)

    def broken(_cutoff: float) -> list[str]:
        raise sqlite3.OperationalError("synthetic: database is locked")

    monkeypatch.setattr(store, "due_jobs", broken)
    put(store, "a.txt")
    clock.now = T0 + 8 * DAY
    with pytest.raises(sqlite3.OperationalError):
        sweeper.sweep_once()
    assert store.files_in_chat(PG_SETUP) == []
    assert checkpoints == [1]


# --- the loop -----------------------------------------------------------------------------------------------------


def a_sweeper(
    settings: Settings, store: Store, audit: AuditLog, clock: Clock, exits: list[int], **changes: Any
) -> Sweeper:
    """A sweeper over settings with some of them changed; the audit chain is not started."""
    return Sweeper(
        store, audit, dataclasses.replace(settings, **changes), Metrics(settings.data_dir), clock, exits.append
    )


@pytest.mark.anyio
async def test_the_sweep_runs_at_start_and_then_every_interval(
    settings: Settings, store: Store, audit: AuditLog, clock: Clock, exits: list[int]
) -> None:
    """The first sweep does not wait for the interval; the next comes after it."""
    audit.start({})
    sweeper = a_sweeper(settings, store, audit, clock, exits, retention_interval_s=1)
    put(store, "a.txt")
    clock.now = T0 + 8 * DAY
    task = asyncio.create_task(sweeper.run_forever())
    try:
        await until(lambda: store.files_in_chat(PG_SETUP) == [], seconds=2)
        put(store, "b.txt")
        clock.now = T0 + 16 * DAY
        await asyncio.sleep(0.2)
        assert len(store.files_in_chat(PG_SETUP)) == 1
        await until(lambda: store.files_in_chat(PG_SETUP) == [], seconds=3)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.anyio
async def test_a_failed_sweep_is_logged_and_the_next_interval_tries_again(
    settings: Settings,
    store: Store,
    audit: AuditLog,
    clock: Clock,
    exits: list[int],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Any failure but an unwritable record is one sweep's: logged by its type, and the loop goes on."""
    sweeper = a_sweeper(settings, store, audit, clock, exits, retention_interval_s=1)
    calls: list[int] = []

    def flaky(stop: threading.Event | None = None) -> SweepReport:
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("synthetic: database is locked")
        return SweepReport(deleted=0, errors=0)

    monkeypatch.setattr(sweeper, "sweep_once", flaky)
    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(sweeper.run_forever())
        try:
            await until(lambda: len(calls) >= 2, seconds=3)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    assert "OperationalError" in caplog.text
    assert "synthetic" not in caplog.text
    assert exits == []


@pytest.mark.anyio
async def test_a_cancel_after_a_batch_was_recorded_leaves_its_rest_to_be_recorded_again(
    sweeper: Sweeper, store: Store, clock: Clock, audit_lines: AuditLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop after a batch's record leaves its rest to the next sweep, which records it again.

    Stopping waits for the file in hand, then stops. A double record is acceptable; a deletion without one is not.
    """
    made = [put(store, f"f{n}.txt") for n in range(3)]
    clock.now = T0 + 8 * DAY
    in_hand, go_on = threading.Event(), threading.Event()
    remove = store.remove

    def slow(rec: FileRecord, derived: Sequence[DerivedRecord] = ()) -> None:
        if not in_hand.is_set():
            in_hand.set()
            go_on.wait(5)
        remove(rec, derived)

    monkeypatch.setattr(store, "remove", slow)
    task = asyncio.create_task(sweeper.run_forever())
    await until(in_hand.is_set)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()  # it waits for the sweep's thread
    go_on.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    left = store.files_in_chat(PG_SETUP)
    assert len(left) == 2
    assert sweeper.sweep_once() == SweepReport(deleted=2, errors=0)
    first, again = sweeps(audit_lines)
    assert sorted(first["target"]["ids"]) == sorted(rec.id for rec in made)
    assert sorted(again["target"]["ids"]) == sorted(rec.id for rec in left)
    assert store.files_in_chat(PG_SETUP) == []


# --- the renders --------------------------------------------------------------------------------------------------


def test_a_due_file_takes_its_renders_in_the_same_record(
    sweeper: Sweeper,
    store: Store,
    settings: Settings,
    clock: Clock,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One event names the file, then its renders; criteria say how many of each; then every file and row goes."""
    pdf = put(store, "quarterly-scan.pdf")
    first, second = render(store, settings, pdf, 1), render(store, settings, pdf, 2)
    folder = on_disk(settings, pdf).parent / ".derived"
    assert render_on_disk(settings, first).is_file() and render_on_disk(settings, second).is_file()
    clock.now = T0 + 8 * DAY
    recorded_first: list[bool] = []
    remove = store.remove

    def watched(rec: FileRecord, derived: Sequence[DerivedRecord] = ()) -> None:
        named = {rec.id, *(each.id for each in derived)}
        recorded_first.append(any(named <= set(e["target"]["ids"]) for e in sweeps(audit_lines)))
        remove(rec, derived)

    monkeypatch.setattr(store, "remove", watched)
    assert sweeper.sweep_once() == SweepReport(deleted=1, errors=0)
    assert recorded_first == [True]
    (event,) = sweeps(audit_lines)
    assert (event["action"], event["outcome"]) == ("delete", "ok")
    assert event["target"]["ids"] == [pdf.id, first.id, second.id]
    assert event["criteria"] == {
        "period_days": 7,
        "cutoff": iso(T0 + DAY),
        "caller": "ghostwriter",
        "owner": "user-a",
        "count": 1,
        "derived": 2,
    }
    assert store.files_in_chat(PG_SETUP) == []
    assert every_render(store) == []
    assert not on_disk(settings, pdf).exists()
    assert not render_on_disk(settings, first).exists() and not render_on_disk(settings, second).exists()
    assert not folder.exists()
    assert "quarterly-scan" not in json_text(audit_lines)


def test_a_file_with_renders_beyond_one_events_ids_names_them_all(
    sweeper: Sweeper, store: Store, settings: Settings, clock: Clock, audit_lines: AuditLines
) -> None:
    """The ids of a file and its 201 renders do not fit one event: the rest goes on a continuation, none is cut."""
    pdf = put(store, "long-report.pdf")
    renders = [render(store, settings, pdf, n) for n in range(1, 202)]
    clock.now = T0 + 8 * DAY
    assert sweeper.sweep_once() == SweepReport(deleted=1, errors=0)
    events = [e for e in audit_lines() if e["operation"] in {"retention.sweep", "retention.sweep.ids"}]
    main, *continuations = events
    assert main["operation"] == "retention.sweep"
    assert main["criteria"]["count"] == 1 and main["criteria"]["derived"] == 201
    assert [e["operation"] for e in continuations] == ["retention.sweep.ids"]
    named = [i for e in events for i in e["target"]["ids"]]
    assert named == [pdf.id, *(each.id for each in renders)]
    assert every_render(store) == [] and store.files_in_chat(PG_SETUP) == []


def test_renders_of_a_kept_file_are_kept(
    sweeper: Sweeper, store: Store, settings: Settings, clock: Clock, audit_lines: AuditLines
) -> None:
    """A render is as old as its source: the one of a file inside the period stays, with its bytes."""
    old = put(store, "old-scan.pdf")
    old_render = render(store, settings, old)
    clock.now = T0 + 2 * DAY
    young = put(store, "young-scan.pdf")
    kept = render(store, settings, young)
    clock.now = T0 + 8 * DAY
    assert sweeper.sweep_once() == SweepReport(deleted=1, errors=0)
    (event,) = sweeps(audit_lines)
    assert event["target"]["ids"] == [old.id, old_render.id]
    assert store.files_in_chat(PG_SETUP) == [young]
    assert store.derived_of(young) == [kept]
    assert render_on_disk(settings, kept).is_file()
    assert every_render(store) == [kept]


def test_a_render_whose_source_is_gone_is_swept_and_recorded(
    sweeper: Sweeper,
    store: Store,
    settings: Settings,
    clock: Clock,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stray, whatever its age, is recorded with its rule, by its id, and only then is its file removed."""
    pdf = put(store, "quarterly-scan.pdf")
    stray = render(store, settings, pdf)
    store.remove(pdf)  # the render was not named: its row and its file stay
    assert every_render(store) == [stray] and render_on_disk(settings, stray).is_file()
    clock.now = T0 + HOUR  # far inside the period
    recorded_first: list[bool] = []
    remove_derived = store.remove_derived

    def watched(rec: DerivedRecord) -> None:
        recorded_first.append(any(rec.id in e["target"]["ids"] for e in sweeps(audit_lines)))
        remove_derived(rec)

    monkeypatch.setattr(store, "remove_derived", watched)
    assert sweeper.sweep_once() == SweepReport(deleted=1, errors=0)
    assert recorded_first == [True]
    (event,) = sweeps(audit_lines)
    assert (event["action"], event["outcome"], event["actor"]) == ("delete", "ok", {"user": None, "via": "system"})
    assert event["target"]["ids"] == [stray.id]
    assert event["criteria"] == {
        "rule": "derived",
        "period_days": 7,
        "cutoff": iso(T0 + HOUR - 7 * DAY),
        "caller": "ghostwriter",
        "owner": "user-a",
        "count": 1,
    }
    assert every_render(store) == []
    assert not render_on_disk(settings, stray).exists()
    assert not (on_disk(settings, pdf).parent / ".derived").exists()


def test_strays_are_batched_by_caller_and_user(
    sweeper: Sweeper, store: Store, settings: Settings, clock: Clock, audit_lines: AuditLines
) -> None:
    """Each event is one caller's user; together they name every stray once."""
    people = [PG_SETUP, Principal("ghostwriter", "user-b", "chat-1"), Principal("docint", "user-a", "chat-1")]
    strays: dict[str, tuple[str, str]] = {}
    for who in people:
        pdf = put(store, "scan.pdf", who)
        strays[render(store, settings, pdf).id] = (who.caller, who.user_id)
        store.remove(pdf)
    assert sweeper.sweep_once() == SweepReport(deleted=3, errors=0)
    events = sweeps(audit_lines)
    assert len(events) == 3
    for event in events:
        (stray_id,) = event["target"]["ids"]
        assert (event["criteria"]["caller"], event["criteria"]["owner"]) == strays[stray_id]
        assert event["criteria"]["rule"] == "derived"
    assert every_render(store) == []


def test_a_file_that_cannot_go_is_recorded_again_with_its_renders(
    sweeper: Sweeper,
    store: Store,
    settings: Settings,
    clock: Clock,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error record names the file, then its renders; the file keeps its row, and the sweep goes on."""
    stuck = put(store, "stuck.pdf")
    clock.now = T0 + 1  # the sweep takes the older first, so the order of the ids is the test's
    fine = put(store, "fine.pdf")
    stuck_render, fine_render = render(store, settings, stuck), render(store, settings, fine)
    clock.now = T0 + 8 * DAY
    remove = store.remove

    def failing(rec: FileRecord, derived: Sequence[DerivedRecord] = ()) -> None:
        if rec.id == stuck.id:
            raise OSError("synthetic: device busy")
        remove(rec, derived)

    monkeypatch.setattr(store, "remove", failing)
    sweeper.sweep_once()
    ok, error, *rest = sweeps(audit_lines)
    assert (ok["outcome"], ok["target"]["ids"]) == ("ok", [stuck.id, fine.id, stuck_render.id, fine_render.id])
    assert (ok["criteria"]["count"], ok["criteria"]["derived"]) == (2, 2)
    assert (error["outcome"], error["target"]["ids"]) == ("error", [stuck.id, stuck_render.id])
    assert error["criteria"] == {**ok["criteria"], "count": 1, "derived": 1}
    assert [rec.id for rec in store.files_in_chat(PG_SETUP)] == [stuck.id]
    assert not render_on_disk(settings, fine_render).exists()
    # the stuck file's render is as due as the file: the pass over renders takes it, recorded, and the file stays
    (stray,) = rest
    assert (stray["outcome"], stray["criteria"]["rule"], stray["target"]["ids"]) == ("ok", "derived", [stuck_render.id])
    assert store.files_in_chat(PG_SETUP) == [stuck]


def test_a_record_that_cannot_be_written_deletes_no_render(
    sweeper: Sweeper,
    store: Store,
    settings: Settings,
    clock: Clock,
    broken_audit: None,
    exits: list[int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The process ends before the render or its source goes."""
    pdf = put(store, "quarterly-scan.pdf")
    first = render(store, settings, pdf)
    clock.now = T0 + 8 * DAY
    with pytest.raises(AuditWriteError):
        sweeper.sweep_once()
    assert exits == [1]
    assert store.files_in_chat(PG_SETUP) == [pdf]
    assert store.derived_of(pdf) == [first]
    assert on_disk(settings, pdf).exists() and render_on_disk(settings, first).exists()
    err = capsys.readouterr().err
    assert "quarterly-scan" not in err and first.id not in err


def test_a_record_of_a_stray_that_cannot_be_written_deletes_nothing(
    sweeper: Sweeper, store: Store, settings: Settings, broken_audit: None, exits: list[int]
) -> None:
    """A stray stays, row and bytes, until its record can be written."""
    pdf = put(store, "quarterly-scan.pdf")
    stray = render(store, settings, pdf)
    store.remove(pdf)
    with pytest.raises(AuditWriteError):
        sweeper.sweep_once()
    assert exits == [1]
    assert every_render(store) == [stray]
    assert render_on_disk(settings, stray).exists()


def test_a_render_that_cannot_be_removed_is_recorded_again_as_an_error(
    sweeper: Sweeper,
    store: Store,
    settings: Settings,
    clock: Clock,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stray that stays keeps its row and is recorded again; the sweep goes on with the others."""
    strays = []
    for n in range(3):
        clock.now = T0 + n
        pdf = put(store, f"scan-{n}.pdf")
        strays.append(render(store, settings, pdf))
        store.remove(pdf)
    stuck = strays[1]
    remove_derived = store.remove_derived

    def failing(rec: DerivedRecord) -> None:
        if rec.id == stuck.id:
            raise OSError("synthetic: device busy")
        remove_derived(rec)

    monkeypatch.setattr(store, "remove_derived", failing)
    with caplog.at_level(logging.WARNING):
        assert sweeper.sweep_once() == SweepReport(deleted=2, errors=1)
    ok, error = sweeps(audit_lines)
    assert (ok["outcome"], ok["target"]["ids"]) == ("ok", [rec.id for rec in strays])
    assert (error["outcome"], error["target"]["ids"]) == ("error", [stuck.id])
    assert error["criteria"] == {**ok["criteria"], "count": 1}
    assert every_render(store) == [stuck]
    assert "OSError" in caplog.text and "synthetic" not in caplog.text


def test_strays_that_keep_failing_never_stall_the_sweep(
    sweeper: Sweeper, store: Store, settings: Settings, audit_lines: AuditLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep pages on past the strays it could not remove, as it does past files."""
    source = put(store, "scan.pdf")
    strays = [render(store, settings, source, n) for n in range(1, 4)]
    store.remove(source)
    stuck = {strays[0].id}
    remove_derived = store.remove_derived
    monkeypatch.setattr(retention_module, "BATCH", 1)  # a page of one, so the stuck stray comes first

    def failing(rec: DerivedRecord) -> None:
        if rec.id in stuck:
            raise OSError("synthetic: read-only")
        remove_derived(rec)

    monkeypatch.setattr(store, "remove_derived", failing)
    assert sweeper.sweep_once() == SweepReport(deleted=2, errors=1)
    assert {rec.id for rec in every_render(store)} == stuck


# --- the api ------------------------------------------------------------------------------------------------------


def wait_for(condition: Callable[[], bool], seconds: float = 5.0) -> None:
    """Wait in this thread until ``condition()`` holds."""
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_the_api_sweeps_at_start_and_ends_the_sweep_before_its_chain(
    settings: Settings,
    store: Store,
    audit: AuditLog,
    clock: Clock,
    exits: list[int],
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifespan starts the sweep once the chain is open, without a request, and ends it before the chain.

    On shutdown it cancels the sweep and waits for it, so the chain's ``shutdown`` is its last event.
    """
    made = [put(store, "a.txt"), put(store, "b.txt")]
    clock.now = T0 + 8 * DAY
    in_hand, go_on, done = threading.Event(), threading.Event(), threading.Event()
    remove = store.remove

    def slow(rec: FileRecord, derived: Sequence[DerivedRecord] = ()) -> None:
        if not in_hand.is_set():
            in_hand.set()
            go_on.wait(5)
            remove(rec, derived)
            done.set()
            return
        remove(rec, derived)

    monkeypatch.setattr(store, "remove", slow)
    app = create_app(settings, store=store, audit=audit, sweeper=a_sweeper(settings, store, audit, clock, exits))
    with TestClient(app):
        wait_for(in_hand.is_set)
        threading.Timer(0.2, go_on.set).start()
    assert done.is_set()
    operations = [event["operation"] for event in audit_lines()]
    assert operations == ["start", "config.audit", "retention.sweep", "shutdown"]
    assert len(store.files_in_chat(PG_SETUP)) == len(made) - 1


def test_the_app_the_factory_builds_sweeps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``uvicorn --factory`` gets a sweeper: a file due when the api starts goes."""
    data = tmp_path / "volume"
    monkeypatch.setenv("GHOSTWRITER_WORKBENCH_KEY", GHOSTWRITER_KEY)
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(data))
    monkeypatch.delenv("AUDIT_OUTPUT", raising=False)
    monkeypatch.delenv("WORKBENCH_RETENTION_DAYS", raising=False)
    before = Store(data / "workbench.db", Workspaces(data), lambda: time.time() - 8 * DAY)
    put(before, "quarterly-figures.xlsx")
    with TestClient(app_factory()):
        wait_for(lambda: before.files_in_chat(PG_SETUP) == [])
    assert '"retention.sweep"' in capsys.readouterr().out


# --- the dry run --------------------------------------------------------------------------------------------------


@pytest.fixture
def volume(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A data volume the dry run finds through the environment, as in the api's container."""
    data = tmp_path / "volume"
    monkeypatch.setenv("GHOSTWRITER_WORKBENCH_KEY", GHOSTWRITER_KEY)
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(data))
    monkeypatch.delenv("WORKBENCH_RETENTION_DAYS", raising=False)
    return data


def test_dry_run_prints_counts_and_ages_only(volume: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Per kind, how many are due and how old they are, and how many are kept; and it changes nothing.

    Never a name, an id, a user or a chat.
    """
    now = time.time()
    ages = iter([10.0, 9.0, 3.0, 12.0, 12.0])
    store = Store(volume / "workbench.db", Workspaces(volume), lambda: now - next(ages) * DAY)
    who = Principal("ghostwriter", "user-7f3a9c", "chat-5e1b2d")
    secrets = [who.user_id, who.chat_id]
    for name in ("harbour-survey-2026.xlsx", "pier-inspection.docx", "fresh-notes.txt"):
        rec = store.add_upload(
            who, f"/workspace/{who.chat_id}", name, io.BytesIO(b"x"), max_bytes=10, quota_bytes=100, record=unrecorded
        )
        secrets += [name, rec.id]
    job = store.add_job(who, "run_python")
    staged = volume / "staging" / job.id / "dredging-chart.png"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"\x89PNG")
    out = store.register_output(who, staged, "dredging-chart.png", job.id, file_id=new_file_id(), quota_bytes=10**12)
    secrets += [out.name, out.id, job.id]
    database = volume / "workbench.db"
    before, listed = database.read_bytes(), {p.name for p in volume.iterdir()}
    assert main(["dry-run"]) == 0
    text = capsys.readouterr().out
    assert "7 days" in text
    assert "upload: 2 due now, aged 9.0 to 10.0 days; 1 kept" in text
    assert "output: 1 due now, aged 12.0 days; 0 kept" in text
    assert "in all: 3 due now; 1 kept" in text
    assert [secret for secret in secrets if secret in text] == []
    assert database.read_bytes() == before
    assert {p.name for p in volume.iterdir()} - listed <= {"workbench.db-wal", "workbench.db-shm"}
    wal = volume / "workbench.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0
    assert len(store.files_in_chat(who)) == 4
    assert "preview" not in text
    assert main(["dry-run", "--days", "30"]) == 0
    text = capsys.readouterr().out
    assert "With a period of 30 days (preview; configured: 7)" in text
    assert "upload: none due; 3 kept" in text
    assert "output: none due; 1 kept" in text


def test_the_dry_run_counts_renders(volume: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Renders have a line of their own, like the other kinds, and count in the total; never a name or an id."""
    now = time.time()
    store = Store(volume / "workbench.db", Workspaces(volume), lambda: now - 8 * DAY)
    who = Principal("ghostwriter", "user-7f3a9c", "chat-5e1b2d")
    pdf = store.add_upload(
        who, f"/workspace/{who.chat_id}", "harbour-scan.pdf", io.BytesIO(b"x"), max_bytes=10, quota_bytes=100,
        record=unrecorded,
    )  # fmt: skip
    ids = [pdf.id]
    for n in (1, 2):
        folder = volume / "staging" / new_file_id()
        folder.mkdir(parents=True)
        (folder / "render.jpg").write_bytes(b"synthetic render")
        spec = DerivedSpec("page", n, "image/jpeg", 10, 20, {})
        ids.append(
            store.register_derived(pdf, folder / "render.jpg", spec, file_id=new_file_id(), quota_bytes=10**9).id
        )
    assert main(["dry-run"]) == 0
    text = capsys.readouterr().out
    assert "  derived: 2 due now, aged 8.0 days; 0 kept" in text
    assert "  upload: 1 due now, aged 8.0 days; 0 kept" in text
    assert "in all: 3 due now; 0 kept" in text
    assert [secret for secret in [*ids, "harbour-scan", who.user_id, who.chat_id] if secret in text] == []
    assert len(every_render(store)) == 2


@pytest.mark.parametrize("days", ["0", "-1", "x", "1.5", "", "٣"])
def test_dry_run_takes_a_whole_number_of_days_above_zero(
    volume: Path, days: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Anything else is a usage error, before the database is opened."""
    with pytest.raises(SystemExit) as stopped:
        main(["dry-run", "--days", days])
    assert stopped.value.code == 2
    assert "--days" in capsys.readouterr().err


def test_dry_run_without_a_database_says_so(volume: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """It never creates the database it reads."""
    assert main(["dry-run"]) == 1
    assert "no database" in capsys.readouterr().out
    assert not (volume / "workbench.db").exists()
