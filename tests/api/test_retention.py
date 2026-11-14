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
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import KEY
from tests.api.test_files import ME, AuditLines
from tests.api.test_jobs import FakeClient, until
from workbench.api.audit import AuditLog
from workbench.api.jobs import JobQueue
from workbench.api.main import app_factory, create_app
from workbench.api.metrics import Metrics
from workbench.api.retention import Sweeper, SweepReport, main
from workbench.api.store import NotFound, Store
from workbench.config import Settings
from workbench.models import FileRecord, Principal
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


def put(store: Store, name: str, p: Principal = ME) -> FileRecord:
    """Store a small upload for ``p`` in its chat folder."""
    return store.add_upload(p, f"/workspace/{p.chat_id}", name, io.BytesIO(b"x"), max_bytes=100, quota_bytes=10**9)


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

    def watched(rec: FileRecord) -> None:
        recorded_first.append(any(rec.id in e["target"]["ids"] for e in sweeps(audit_lines)))
        remove(rec)

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
        "caller": "open-webui",
        "owner": "user-a",
        "count": 3,
    }
    assert store.files_in_chat(ME) == [recent]
    assert not any(on_disk(settings, rec).exists() for rec in old)
    assert on_disk(settings, recent).exists()
    assert "quarterly-figures" not in json_text(audit_lines)


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
    assert sorted(store.files_in_chat(ME), key=lambda rec: rec.id) == sorted(files, key=lambda rec: rec.id)
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
) -> None:
    """A file that cannot go keeps its row and is recorded again as an error; the sweep goes on with the others."""
    gone, busy, odd = put(store, "a.txt"), put(store, "b.txt"), put(store, "c.txt")
    elsewhere = Principal("open-webui", "user-a", "chat-2")
    linked = put(store, "d.txt", elsewhere)
    chat = settings.data_dir / "workspaces" / "open-webui" / "user-a" / "chat-2"
    chat.rename(chat.with_name("moved"))
    chat.symlink_to(chat.with_name("moved"))  # a link in the path: the store refuses it with a PathError
    clock.now = T0 + 8 * DAY
    remove = store.remove

    def failing(rec: FileRecord) -> None:
        if rec.id == busy.id:
            raise OSError("synthetic: device busy")
        if rec.id == odd.id:
            raise RuntimeError("synthetic: unexpected")
        remove(rec)

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


def test_batches_never_mix_users(sweeper: Sweeper, store: Store, clock: Clock, audit_lines: AuditLines) -> None:
    """Each event is one caller's user, at most 200 files; together they name every due file once."""
    people = [
        Principal("open-webui", "user-a", "chat-1"),
        Principal("open-webui", "user-b", "chat-1"),
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

    def failing(rec: FileRecord) -> None:
        if rec.id in stuck:
            raise OSError("synthetic: read-only")
        remove(rec)

    monkeypatch.setattr(store, "remove", failing)
    clock.now = T0 + 8 * DAY
    assert sweeper.sweep_once() == SweepReport(deleted=50, errors=200)
    assert {rec.id for rec in store.files_in_chat(ME)} == stuck


def test_a_file_moved_since_it_was_listed_is_deleted_where_it_is_now(
    sweeper: Sweeper, store: Store, settings: Settings, clock: Clock
) -> None:
    """Moving keeps a file's id and creation time: it is still due, and goes from its new place."""
    rec = put(store, "draft.docx")
    store.mkdir(ME, "/workspace/chat-1/final")
    moved = store.move(ME, rec.virtual_path, "/workspace/chat-1/final/report.docx")
    clock.now = T0 + 8 * DAY
    assert sweeper.sweep_once().deleted == 1
    assert not on_disk(settings, moved).exists()


@pytest.mark.anyio
async def test_sweep_during_running_job(
    sweeper: Sweeper, store: Store, settings: Settings, clock: Clock, metrics: Metrics, audit_lines: AuditLines
) -> None:
    """An input expires while its job runs (the job has its copy): it goes with its record; the output is kept."""
    old = put(store, "quarterly-figures.xlsx")
    executor = FakeClient(settings.data_dir)
    executor.hold = {"open-webui/user-a"}
    executor.files = {"chart.png": b"\x89PNG"}
    queue = JobQueue(settings, executor, store, metrics)
    clock.now = T0 + 8 * DAY
    running = asyncio.create_task(queue.submit(ME, "run_python", {"code": "print(1)"}, [old.name]))
    await until(lambda: executor.running == 1)
    report = await asyncio.to_thread(sweeper.sweep_once)
    assert report == SweepReport(deleted=1, errors=0)
    (event,) = sweeps(audit_lines)
    assert event["target"]["ids"] == [old.id]
    executor.gate.set()
    job, result, made = await running
    assert (result.status, [rec.name for rec in made]) == ("ok", ["chart.png"])
    assert store.files_in_chat(ME) == made
    assert store.get_job(ME, job.id).status == "ok"


# --- job records, folders and staging -----------------------------------------------------------------------------


def test_old_job_records_are_forgotten_without_a_record(
    sweeper: Sweeper, store: Store, clock: Clock, audit_lines: AuditLines
) -> None:
    """They hold no content: they go without an event."""
    old = store.add_job(ME, "run_python")
    clock.now = T0 + 2 * DAY
    new = store.add_job(ME, "read_document")
    clock.now = T0 + 8 * DAY
    sweeper.sweep_once()
    with pytest.raises(NotFound):
        store.get_job(ME, old.id)
    assert store.get_job(ME, new.id) == new
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
    root = settings.data_dir / "workspaces" / "open-webui" / "user-a"
    store.mkdir(ME, "/workspace/chat-1/reports/2026/q1")
    store.mkdir(ME, "/workspace/chat-2/fresh")
    store.mkdir(ME, "/workspace/chat-3/kept")
    put(store, "notes.txt", Principal("open-webui", "user-a", "chat-3"))
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
    store.delete(Principal("open-webui", "user-a", "chat-3"), "/workspace/chat-3/notes.txt")
    for chat in ("chat-2", "chat-3", "chat-5"):
        (root / chat / "link").unlink(missing_ok=True)
        for folder in sorted(p for p in (root / chat).rglob("*") if p.is_dir()):
            age(folder, 2 * HOUR, clock.now)
        age(root / chat, 2 * HOUR, clock.now)
    sweeper.sweep_once()
    assert sorted(p.name for p in root.iterdir()) == ["chat-4"]
    assert (settings.data_dir / "workspaces" / "open-webui").is_dir()


def test_staging_folders_older_than_an_hour_go(
    sweeper: Sweeper, settings: Settings, clock: Clock, tmp_path: Path
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
        await until(lambda: store.files_in_chat(ME) == [], seconds=2)
        put(store, "b.txt")
        clock.now = T0 + 16 * DAY
        await asyncio.sleep(0.2)
        assert len(store.files_in_chat(ME)) == 1
        await until(lambda: store.files_in_chat(ME) == [], seconds=3)
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

    def slow(rec: FileRecord) -> None:
        if not in_hand.is_set():
            in_hand.set()
            go_on.wait(5)
        remove(rec)

    monkeypatch.setattr(store, "remove", slow)
    task = asyncio.create_task(sweeper.run_forever())
    await until(in_hand.is_set)
    task.cancel()
    await asyncio.sleep(0.05)
    assert not task.done()  # it waits for the sweep's thread
    go_on.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    left = store.files_in_chat(ME)
    assert len(left) == 2
    assert sweeper.sweep_once() == SweepReport(deleted=2, errors=0)
    first, again = sweeps(audit_lines)
    assert sorted(first["target"]["ids"]) == sorted(rec.id for rec in made)
    assert sorted(again["target"]["ids"]) == sorted(rec.id for rec in left)
    assert store.files_in_chat(ME) == []


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

    def slow(rec: FileRecord) -> None:
        if not in_hand.is_set():
            in_hand.set()
            go_on.wait(5)
            remove(rec)
            done.set()
            return
        remove(rec)

    monkeypatch.setattr(store, "remove", slow)
    app = create_app(settings, store=store, audit=audit, sweeper=a_sweeper(settings, store, audit, clock, exits))
    with TestClient(app):
        wait_for(in_hand.is_set)
        threading.Timer(0.2, go_on.set).start()
    assert done.is_set()
    operations = [event["operation"] for event in audit_lines()]
    assert operations == ["start", "config.audit", "retention.sweep", "shutdown"]
    assert len(store.files_in_chat(ME)) == len(made) - 1


def test_the_app_the_factory_builds_sweeps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``uvicorn --factory`` gets a sweeper: a file due when the api starts goes."""
    data = tmp_path / "volume"
    monkeypatch.setenv("OPEN_WEBUI_WORKBENCH_KEY", KEY)
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(data))
    monkeypatch.delenv("AUDIT_OUTPUT", raising=False)
    monkeypatch.delenv("WORKBENCH_RETENTION_DAYS", raising=False)
    before = Store(data / "workbench.db", Workspaces(data), lambda: time.time() - 8 * DAY)
    put(before, "quarterly-figures.xlsx")
    with TestClient(app_factory()):
        wait_for(lambda: before.files_in_chat(ME) == [])
    assert '"retention.sweep"' in capsys.readouterr().out


# --- the dry run --------------------------------------------------------------------------------------------------


@pytest.fixture
def volume(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A data volume the dry run finds through the environment, as in the api's container."""
    data = tmp_path / "volume"
    monkeypatch.setenv("OPEN_WEBUI_WORKBENCH_KEY", KEY)
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(data))
    monkeypatch.delenv("WORKBENCH_RETENTION_DAYS", raising=False)
    return data


def test_dry_run_prints_counts_and_ages_only(volume: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Per kind, how many are due and how old they are, and how many are kept; and it changes nothing.

    Never a name, an id, a user or a chat.
    """
    now = time.time()
    ages = iter([10.0, 9.0, 3.0, 12.0])
    store = Store(volume / "workbench.db", Workspaces(volume), lambda: now - next(ages) * DAY)
    who = Principal("open-webui", "user-7f3a9c", "chat-5e1b2d")
    secrets = [who.user_id, who.chat_id]
    for name in ("harbour-survey-2026.xlsx", "pier-inspection.docx", "fresh-notes.txt"):
        rec = store.add_upload(who, f"/workspace/{who.chat_id}", name, io.BytesIO(b"x"), max_bytes=10, quota_bytes=100)
        secrets += [name, rec.id]
    staged = volume / "staging" / ("e" * 32) / "dredging-chart.png"
    staged.parent.mkdir(parents=True)
    staged.write_bytes(b"\x89PNG")
    out = store.register_output(who, staged, "dredging-chart.png", "e" * 32)
    secrets += [out.name, out.id, "e" * 32]
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
    assert main(["dry-run", "--days", "30"]) == 0
    text = capsys.readouterr().out
    assert "30 days" in text
    assert "upload: none due; 3 kept" in text
    assert "output: none due; 1 kept" in text


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
