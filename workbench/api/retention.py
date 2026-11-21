"""Retention: every workspace file is deleted ``WORKBENCH_RETENTION_DAYS`` after it was made, and recorded first.

A file is due once its ``created_at`` lies more than the period back, an upload or an output, whether or not its chat
still exists. The api sweeps (:class:`Sweeper`) at start and then every ``WORKBENCH_RETENTION_INTERVAL_S``, off the
event loop. A sweep reads the due files oldest first, in pages of at most 200, and deletes them in batches of one
caller's user. Each batch is first recorded as one ``delete`` / ``retention.sweep`` event of the system, by file id,
with the rule (``period_days``, ``cutoff``), the ``caller``, the ``owner`` (the caller's opaque user id, never an e-mail
or a name) and the ``count``; only then do its files and their rows go. A file that cannot be deleted keeps its row, is
recorded again with ``outcome: error``, and is tried again by the next sweep; this sweep pages on past it, so no number
of such files holds up the others.

A derived file, the render of a picture or of a page, is as old as its source and goes with it, in the same record: the
event names the batch's file ids and then the ids of their renders, and its ``count`` stays the number of files, with
``derived`` the number of renders. A stray is a render whose source is gone: one made after its file's record was
written, which stayed when the file went. A render can also be due while its source stays, when the file could not be
deleted. After the files, the sweep takes every such render, in batches of one caller's user, each recorded first as
``rule: derived`` by the render's id. A render that stays is recorded again as an error, as a file is.

No deletion without its record: a record that cannot be written ends the process (``os._exit(1)``) before anything
more is deleted, and ``restart: unless-stopped`` brings the api back; stderr gets the count. The other way round is
allowed: a sweep stopped between a batch's record and the end of its removals (the api shuts down, or is killed) leaves
the rest of the batch to the next sweep, which records those files again. A double record is acceptable; a deletion
without a record is not.

Every other regular file the sweep removes is recorded first too, on the same halt, once untouched for an hour:

- an orphan, a file in a chat folder that has no row: the ``<id>.part`` of an upload the api died during, or a file a
  kill left between its rename into place and its row's commit. One record per chat folder and at most 200 files,
  with the chat as its container, ``rule: orphan``, the ``caller``, the ``owner`` and the ``count``, and the file ids
  the ``.part`` names carry; never a name. The store looks and removes under its lock
  (:meth:`~workbench.api.store.Store.sweep_user`), which every rename into place holds until its row is committed, so
  a file on its way in is never taken for one;
- the residue of ``staging/<job-id>``: outputs the api never registered (a client that went away, a crash before the
  job settled), made from the user's files. ``rule: staging``, the ``job``, the ``count`` of regular files, and the
  job's ``caller``, ``owner`` and chat while its record exists; then the folder goes.

Here too what could not be removed is recorded again, ``outcome: error``: the orphans that stayed, by id where their
names carry one, and the regular files a staging folder kept, by their count. An entry that cannot be looked at is
skipped with a warning, and the sweep goes on with the next.

Without a record, since they hold no content: job records older than the period are forgotten, and the folders the
store finds empty go once untouched for an hour, under its lock (folders in chat folders, the chat folders and a user's
root), so a file on its way into one never loses it. Nothing is followed: a link, anything but a folder or a regular
file, or anything of another filesystem is left alone, and neither ``workspaces/`` nor a caller's folder ever goes. A
sweep that deleted something ends with a ``TRUNCATE`` checkpoint, so the log's older pages, which hold file names, are
copied over, even when the sweep fails after the deletions; a checkpoint the log was busy for is carried to the next
sweep, whether or not that one deletes anything. Then the metrics' gauges are measured again.

``python -m workbench.api.retention dry-run [--days N]`` (``make retention-dry-run``) says what the period, or ``N``
days, would delete now: counts and ages by kind, never a name, an id, a user or a chat. It reads the database
read-only, and records and deletes nothing.
"""

import argparse
import asyncio
import contextlib
import functools
import logging
import math
import os
import shutil
import sqlite3
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, TextIO

from workbench.api.audit import AuditLog
from workbench.api.jobs import outlast
from workbench.api.metrics import Metrics
from workbench.api.store import DueSummary, Store, summarize_due
from workbench.config import ConfigError, Settings, load_settings
from workbench.layout import DATABASE, STAGING, WORKSPACES
from workbench.models import DerivedRecord, FileRecord
from workbench.paths import PathError
from workbench.protocol import JOB_ID
from workbench.utils.audit_event import MAX_IDS, AuditWriteError

_log = logging.getLogger(__name__)

#: Seconds in a day of the period.
DAY_S: Final = 86_400

#: Most files one batch, and so one event, names: as many ids as an event's target keeps.
BATCH: Final = MAX_IDS

#: Seconds a folder must be untouched before it goes, empty or staged.
FOLDER_GRACE_S: Final = 3600

#: The kinds of file the dry run counts, in its order.
KINDS: Final = ("upload", "output", "derived")


@dataclass
class SweepReport:
    """What one sweep did with the due files.

    Attributes:
        deleted: Files deleted, each after its record, and the strays among the renders, each after its own: the renders
            that went with their source are not counted.
        errors: Files and strays that could not be deleted; each is recorded again as an error and stays for the next
            sweep.
    """

    deleted: int
    errors: int


def cutoff_for(now: float, days: int) -> float:
    """Find the moment a file must have been made before to be due.

    Args:
        now: The time of the sweep, in seconds since the epoch.
        days: The period.

    Returns:
        ``now`` less ``days`` days; minus infinity for a period longer than any time a float holds.
    """
    try:
        return now - days * DAY_S
    except OverflowError:
        return -math.inf


def _iso(moment: float) -> str:
    """Write a time as the audit chain writes its own.

    Args:
        moment: Seconds since the epoch.

    Returns:
        ISO 8601 in UTC, to the millisecond, with ``Z``.
    """
    return datetime.fromtimestamp(moment, tz=UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _batches[T: (FileRecord, DerivedRecord)](page: Sequence[T]) -> list[list[T]]:
    """Split a page of due files, or of due renders, by owner, a caller's user, keeping their order.

    Args:
        page: At most :data:`BATCH` files, or at most :data:`BATCH` renders.

    Returns:
        One batch per owner.
    """
    owners: dict[tuple[str, str], list[T]] = {}
    for rec in page:
        owners.setdefault((rec.caller, rec.user_id), []).append(rec)
    return list(owners.values())


def _folder(path: Path, device: int) -> os.stat_result | None:
    """Look at a path without following it.

    Args:
        path: Any path on the volume.
        device: The data volume's device.

    Returns:
        Its ``lstat`` if it is a folder of the volume; ``None`` for a link, anything but a folder, a folder of
        another filesystem, nothing, or what cannot be looked at, which is logged by the error's type.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        _log.warning("retention skipped a path it could not look at (%s)", type(exc).__name__)
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_dev != device:
        return None
    return st


def _subfolders(path: Path, device: int) -> Iterator[tuple[Path, os.stat_result]]:
    """List the folders of the volume in a folder; links and everything else are left out.

    Args:
        path: A folder already checked with :func:`_folder`.
        device: The data volume's device.

    Yields:
        Each folder's path and ``lstat``.
    """
    try:
        with os.scandir(path) as scan:
            names = [entry.name for entry in scan]
    except OSError as exc:
        _log.warning("retention could not list a folder (%s)", type(exc).__name__)
        return
    for name in names:
        st = _folder(path / name, device)
        if st is not None:
            yield path / name, st


def _regular_files(path: Path, device: int) -> int:
    """Count the regular files in a folder and the folders in it, as :func:`shutil.rmtree` would remove them.

    Args:
        path: A folder already checked with :func:`_folder`.
        device: The data volume's device.

    Returns:
        How many; links, anything but a regular file, and anything of another filesystem are not counted.
    """
    count = 0
    for here, names, files in os.walk(path):
        names[:] = [name for name in names if _folder(Path(here) / name, device) is not None]
        for name in files:
            try:
                st = os.lstat(Path(here) / name)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_dev == device:
                count += 1
    return count


class Sweeper:
    """Deletes the files of every user once the period is over, each batch recorded before it goes."""

    def __init__(
        self,
        store: Store,
        audit: AuditLog,
        settings: Settings,
        metrics: Metrics,
        clock: Callable[[], float] = time.time,
        exit_process: Callable[[int], object] = os._exit,
    ) -> None:
        """Hold what a sweep needs; nothing runs until :meth:`sweep_once` or :meth:`run_forever`.

        Args:
            store: The files and job records.
            audit: The api's audit chain, already open when a sweep runs.
            settings: The period, the interval and the data volume.
            metrics: The gauges a sweep measures again.
            clock: Returns the current time in seconds since the epoch.
            exit_process: Ends the process with a status when a record cannot be written: ``os._exit``, which ends
                it from any thread at once, where ``sys.exit`` in a thread or a task would not end uvicorn.
        """
        self._store = store
        self._audit = audit
        self._settings = settings
        self._metrics = metrics
        self._clock = clock
        self._exit_process = exit_process
        self._checkpoint_due = False

    def sweep_once(self, stop: threading.Event | None = None) -> SweepReport:
        """Delete what is due now, forget old job records, remove stale folders, and measure the volume again.

        A sweep that deleted rows ends with a checkpoint, even when it fails after them; a checkpoint the log was too
        busy for, or that failed, is carried by every later sweep until one empties the log.

        Args:
            stop: When set, the sweep ends before its next record or removal; files of a batch recorded but not yet
                removed are recorded again by the next sweep.

        Returns:
            How many due files were deleted, and how many could not be.

        Raises:
            AuditWriteError: A record could not be written and ``exit_process`` returned; nothing of its batch, and
                nothing after it, was deleted. In production the process has ended before.
        """
        stop = threading.Event() if stop is None else stop
        now = self._clock()
        cutoff = cutoff_for(now, self._settings.retention_days)
        report = SweepReport(deleted=0, errors=0)
        forgotten = 0
        try:
            self._sweep_files(cutoff, stop, report)
            self._sweep_derived(cutoff, stop, report)
            if not stop.is_set():
                forgotten = self._forget_jobs(cutoff)
                self._prune(now - FOLDER_GRACE_S, stop)
        finally:
            if report.deleted or forgotten:
                self._checkpoint_due = True
            if self._checkpoint_due:
                self._checkpoint_due = not self._checkpoint()
        self._metrics.refresh(files=self._store.count_files())
        if report.deleted or report.errors:
            _log.info("retention deleted %d files; %d could not be deleted", report.deleted, report.errors)
        return report

    async def run_forever(self) -> None:
        """Sweep now, then every ``retention_interval_s``, until cancelled.

        Each sweep runs in a thread, a plain future of the loop's executor (what ``asyncio.to_thread`` uses). A
        cancellation, as the api shuts down, asks the sweep in hand to stop before its next record or removal and waits
        for it, so the audit chain is never closed under a sweep; the files of a batch it recorded but did not remove
        yet are recorded again, and removed, by the next sweep. A double record is acceptable; a deletion without a
        record is not. A sweep that fails in any other way than an unwritable record is logged by the error's type, and
        the next interval tries again.

        Raises:
            AuditWriteError: A record could not be written and ``exit_process`` returned (never in production).
        """
        loop = asyncio.get_running_loop()
        while True:
            stop = threading.Event()
            work = loop.run_in_executor(None, self.sweep_once, stop)
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                stop.set()
                with contextlib.suppress(Exception):  # the api is going; its cancellation is the answer
                    await outlast(work)
                raise
            except AuditWriteError:
                raise
            except Exception as exc:
                _log.warning("a retention sweep failed (%s); the next one tries again", type(exc).__name__)
            await asyncio.sleep(self._settings.retention_interval_s)

    # --- files ---------------------------------------------------------------------------------------------------

    def _sweep_files(self, cutoff: float, stop: threading.Event, report: SweepReport) -> None:
        """Delete the due files, page by page, batch by batch, each batch recorded first.

        Args:
            cutoff: Files made before this are due.
            stop: Ends the sweep before its next record or removal.
            report: Counts what was done.
        """
        after: tuple[float, str] | None = None
        while not stop.is_set():
            page = self._store.due(cutoff, BATCH, after=after)
            if not page:
                return
            after = (page[-1].created_at, page[-1].id)
            for batch in _batches(page):
                if stop.is_set():
                    return
                self._delete(batch, cutoff, stop, report)

    def _delete(self, batch: Sequence[FileRecord], cutoff: float, stop: threading.Event, report: SweepReport) -> None:
        """Record one owner's batch and its renders, then delete each file with its renders; errors are recorded again.

        The event names the batch's files and then their renders, so that no render is deleted without its record. A
        render made after the renders were read is not named: it stays when its file goes, a stray the next pass
        takes, recorded.

        Args:
            batch: Due files of one caller's user.
            cutoff: The rule's cutoff.
            stop: Ends the removals before the next file.
            report: Counts what was done.
        """
        first = batch[0]
        renders: dict[str, list[DerivedRecord]] = {rec.id: [] for rec in batch}
        for each in self._store.derived_for(batch):
            renders[each.source_id].append(each)
        criteria: dict[str, object] = {
            "period_days": self._settings.retention_days,
            "cutoff": _iso(cutoff),
            "caller": first.caller,
            "owner": first.user_id,
            "count": len(batch),
            "derived": sum(map(len, renders.values())),
        }
        self._record([*(rec.id for rec in batch), *(each.id for rec in batch for each in renders[rec.id])], criteria)
        failed: list[FileRecord] = []
        for rec in batch:
            if stop.is_set():
                break
            try:
                self._store.remove(rec, derived=renders[rec.id])
            except Exception as exc:  # PathError and OSError above all; whatever it is, the file stays and is named
                _log.warning("retention could not delete a file (%s)", type(exc).__name__)
                failed.append(rec)
            else:
                report.deleted += 1
        if failed:
            left = [each for rec in failed for each in renders[rec.id]]
            self._record(
                [*(rec.id for rec in failed), *(each.id for each in left)],
                {**criteria, "count": len(failed), "derived": len(left)},
                outcome="error",
            )
            report.errors += len(failed)

    def _sweep_derived(self, cutoff: float, stop: threading.Event, report: SweepReport) -> None:
        """Delete the renders that are due on their own, page by page, batch by batch, each batch recorded first.

        After :meth:`_sweep_files`, what is left is a stray, a render whose source is gone, or a render of a file that
        could not be deleted and is as due as it.

        Args:
            cutoff: Renders made before this are due.
            stop: Ends the sweep before its next record or removal.
            report: Counts what was done.
        """
        after: tuple[float, str] | None = None
        while not stop.is_set():
            page = self._store.due_derived(cutoff, BATCH, after=after)
            if not page:
                return
            after = (page[-1].created_at, page[-1].id)
            for batch in _batches(page):
                if stop.is_set():
                    return
                self._delete_derived(batch, cutoff, stop, report)

    def _delete_derived(
        self, batch: Sequence[DerivedRecord], cutoff: float, stop: threading.Event, report: SweepReport
    ) -> None:
        """Record one owner's batch of renders, then delete them; those that could not go are recorded again.

        Args:
            batch: Due renders of one caller's user.
            cutoff: The rule's cutoff.
            stop: Ends the removals before the next render.
            report: Counts what was done.
        """
        first = batch[0]
        criteria: dict[str, object] = {
            "rule": "derived",
            "period_days": self._settings.retention_days,
            "cutoff": _iso(cutoff),
            "caller": first.caller,
            "owner": first.user_id,
            "count": len(batch),
        }
        self._record([each.id for each in batch], criteria)
        failed: list[DerivedRecord] = []
        for each in batch:
            if stop.is_set():
                break
            try:
                self._store.remove_derived(each)
            except Exception as exc:  # PathError and OSError above all; whatever it is, the render stays and is named
                _log.warning("retention could not delete a render (%s)", type(exc).__name__)
                failed.append(each)
            else:
                report.deleted += 1
        if failed:
            self._record([each.id for each in failed], {**criteria, "count": len(failed)}, outcome="error")
            report.errors += len(failed)

    def _record(
        self,
        file_ids: Sequence[str],
        criteria: dict[str, object],
        *,
        outcome: str = "ok",
        count: int | None = None,
        container: str | None = None,
    ) -> None:
        """Write one ``delete`` / ``retention.sweep`` event, or end the process.

        Args:
            file_ids: The ids of the files it names; never a name.
            criteria: The rule, the caller, the owner and the count.
            outcome: ``ok`` before files go, ``error`` for files that did not.
            count: How many files it is about, when not every one has an id; ``len(file_ids)`` by default.
            container: Their chat folder, as ``chat:<chat-id>``.

        Raises:
            AuditWriteError: The event could not be written and ``exit_process`` returned.
        """
        try:
            self._audit.record_system(
                "delete",
                "retention.sweep",
                file_ids=file_ids,
                criteria=criteria,
                outcome=outcome,
                count=count,
                container=container,
            )
        except AuditWriteError:
            self._halt(len(file_ids) if count is None else count)
            raise

    def _halt(self, count: int) -> None:
        """End the process: a record could not be written, and nothing may be deleted without one.

        Args:
            count: The files the unwritten record named.
        """
        noun = "file" if count == 1 else "files"
        with contextlib.suppress(Exception):
            print(
                f"workbench retention: the record of {count} {noun} could not be written; the api stops, and nothing "
                "more is deleted until it can record again",
                file=sys.stderr,
            )
        for stream in (sys.stdout, sys.stderr):
            with contextlib.suppress(Exception):  # stdout carries the audit chain, which has just failed
                stream.flush()
        self._exit_process(1)

    def _checkpoint(self) -> bool:
        """Copy the database's log over, so the deleted rows' older pages leave it; a busy log is left to later.

        Returns:
            Whether the log was emptied; ``False`` when it was busy or the checkpoint failed, and the next sweep tries
            again.
        """
        try:
            emptied = self._store.checkpoint()
        except sqlite3.Error as exc:
            _log.warning(
                "retention could not checkpoint the database (%s); the next sweep tries again", type(exc).__name__
            )
            return False
        if not emptied:
            _log.warning("the database's log was busy at the checkpoint; the next sweep tries again")
        return emptied

    # --- job records and folders ---------------------------------------------------------------------------------

    def _forget_jobs(self, cutoff: float) -> int:
        """Forget the job records older than the period; they hold no content and are not recorded.

        Args:
            cutoff: Jobs started before this are forgotten.

        Returns:
            How many.
        """
        ids = self._store.due_jobs(cutoff)
        if ids:
            self._store.forget_jobs(ids)
        return len(ids)

    def _prune(self, untouched_before: float, stop: threading.Event) -> None:
        """Remove the orphans and the empty folders of every user's root, then the leftover staging folders.

        A user's root or a staging folder that cannot be looked at is skipped with a warning, and the prune goes on.

        Args:
            untouched_before: A file or folder last changed before this may go.
            stop: Ends the prune before the next user's root, batch of a user's orphans, or staging folder.
        """
        data = self._settings.data_dir
        try:
            device = os.stat(data).st_dev
        except OSError as exc:
            _log.warning("retention could not look at the data volume (%s)", type(exc).__name__)
            return
        if _folder(data / WORKSPACES, device) is not None:
            for caller, _ in _subfolders(data / WORKSPACES, device):
                for user, _ in _subfolders(caller, device):
                    if stop.is_set():
                        return
                    record = functools.partial(self._record_orphans, caller.name, user.name)
                    try:
                        _, failed = self._store.sweep_user(
                            caller.name,
                            user.name,
                            untouched_before=untouched_before,
                            device=device,
                            record=record,
                            stop=stop,
                        )
                    except PathError:
                        continue  # not a folder the store made, or a link in its path: left alone
                    except OSError as exc:
                        _log.warning("retention skipped a user's folder it could not look at (%s)", type(exc).__name__)
                        continue
                    if failed:
                        _log.warning("retention could not delete %d files that had no record", failed)
        if _folder(data / STAGING, device) is not None:
            for job, job_st in _subfolders(data / STAGING, device):
                if stop.is_set():
                    return
                if JOB_ID.fullmatch(job.name) is not None and job_st.st_mtime < untouched_before:
                    self._clear_staging(job, device)

    def _record_orphans(
        self, caller: str, user_id: str, chat_id: str, file_ids: Sequence[str], count: int, outcome: str
    ) -> None:
        """Record orphans of one chat folder: before the store removes them, and again for those it could not.

        Args:
            caller: The caller.
            user_id: The caller's id of the user.
            chat_id: The chat folder.
            file_ids: The ids their names carry.
            count: How many there are.
            outcome: ``ok`` before they go, ``error`` for those that stayed.

        Raises:
            AuditWriteError: The event could not be written and ``exit_process`` returned.
        """
        criteria: dict[str, object] = {"rule": "orphan", "caller": caller, "owner": user_id, "count": count}
        self._record(file_ids, criteria, outcome=outcome, count=count, container=f"chat:{chat_id}")

    def _clear_staging(self, job: Path, device: int) -> None:
        """Remove a job's leftover staging folder, its regular files recorded first.

        The regular files a failed removal leaves are recorded again, by their count, as an error; the next sweep tries
        again.

        Args:
            job: ``staging/<job-id>``, older than the grace.
            device: The data volume's device.

        Raises:
            AuditWriteError: A record could not be written and ``exit_process`` returned; nothing was removed after it.
        """
        count = _regular_files(job, device)
        criteria: dict[str, object] = {"rule": "staging", "job": job.name, "count": count}
        container: str | None = None
        if count:
            owner = self._store.find_job(job.name)
            if owner is not None:
                criteria |= {"caller": owner.caller, "owner": owner.user_id}
                container = f"chat:{owner.chat_id}"
            self._record((), criteria, count=count, container=container)
        try:
            shutil.rmtree(job)  # never follows a link inside
        except OSError as exc:
            _log.warning("retention could not remove a staging folder (%s)", type(exc).__name__)
            left = _regular_files(job, device) if _folder(job, device) is not None else 0
            if left:
                self._record((), {**criteria, "count": left}, outcome="error", count=left, container=container)


# --- the dry run --------------------------------------------------------------------------------------------------


def _days_old(now: float, created_at: float) -> str:
    """Say how old a file is.

    Args:
        now: The time of the report.
        created_at: When the file was made.

    Returns:
        Days, to one decimal.
    """
    return f"{(now - created_at) / DAY_S:.1f}"


def dry_run(settings: Settings, out: TextIO, *, days: int | None = None, now: float | None = None) -> int:
    """Say what the period would delete now, by kind: counts and ages only; record, delete and change nothing.

    Args:
        settings: Where the data volume is, and the configured period.
        out: Where the report goes.
        days: Another period to preview; the configured one by default. The header names both.
        now: The time of the report; the clock's by default.

    Returns:
        0 when the database could be read, else 1.
    """
    moment = time.time() if now is None else now
    print("workbench retention, dry run: nothing is recorded, deleted or changed.", file=out)
    if days is None:
        days = settings.retention_days
        print(f"Files are deleted {days} days after they were made.", file=out)
    else:
        print(f"With a period of {days} days (preview; configured: {settings.retention_days}):", file=out)
    database = settings.data_dir / DATABASE
    if not database.is_file():
        print("  no database: there is nothing to sweep", file=out)
        return 1
    try:
        summary = summarize_due(database, cutoff_for(moment, days))
    except sqlite3.Error as exc:
        print(f"  the database cannot be read ({type(exc).__name__})", file=out)
        return 1
    due = kept = 0
    for kind in KINDS:
        counted = summary.get(kind, DueSummary(due=0, oldest=None, newest=None, kept=0))
        due, kept = due + counted.due, kept + counted.kept
        if counted.due and counted.oldest is not None and counted.newest is not None:
            youngest, eldest = _days_old(moment, counted.newest), _days_old(moment, counted.oldest)
            ages = eldest if youngest == eldest else f"{youngest} to {eldest}"
            print(f"  {kind}: {counted.due} due now, aged {ages} days; {counted.kept} kept", file=out)
        else:
            print(f"  {kind}: none due; {counted.kept} kept", file=out)
    print(f"  in all: {due} due now; {kept} kept" if due else f"  in all: none due; {kept} kept", file=out)
    return 0


def _days(text: str) -> int:
    """Read ``--days``.

    Args:
        text: The argument.

    Returns:
        The number of days.

    Raises:
        argparse.ArgumentTypeError: It is not a whole number above zero, in ASCII digits.
    """
    if not (text.isascii() and text.isdigit()) or int(text) < 1:
        raise argparse.ArgumentTypeError("N must be a whole number of days above zero")
    return int(text)


def main(argv: Sequence[str] | None = None) -> int:
    """Run a subcommand: ``dry-run``.

    Args:
        argv: The arguments, without the program's name.

    Returns:
        The exit status: 0, 1 when the database cannot be read, 2 for a usage or configuration error.
    """
    parser = argparse.ArgumentParser(
        prog="python -m workbench.api.retention", description="Retention of the workspace files of workbench."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    report = commands.add_parser("dry-run", help="say what retention would delete now; change nothing")
    report.add_argument("--days", type=_days, metavar="N", help="evaluate N days instead of WORKBENCH_RETENTION_DAYS")
    args = parser.parse_args(argv)
    try:
        settings = load_settings(os.environ, role="api")
    except ConfigError as exc:
        print(f"workbench retention: {exc}", file=sys.stderr)
        return 2
    return dry_run(settings, sys.stdout, days=args.days)


if __name__ == "__main__":
    sys.exit(main())
