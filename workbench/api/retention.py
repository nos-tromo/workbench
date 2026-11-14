"""Retention: every workspace file is deleted ``WORKBENCH_RETENTION_DAYS`` after it was made, and recorded first.

A file is due once its ``created_at`` lies more than the period back, an upload or an output, whether or not its chat
still exists; moving a file keeps its time. The api sweeps (:class:`Sweeper`) at start and then every
``WORKBENCH_RETENTION_INTERVAL_S``, off the event loop. A sweep reads the due files oldest first, in pages of at most
200, and deletes them in batches of one caller's user. Each batch is first recorded as one ``delete`` /
``retention.sweep`` event of the system, by file id, with the rule (``period_days``, ``cutoff``), the ``caller``, the
``owner`` (the caller's opaque user id, never an e-mail or a name) and the ``count``; only then do its files and their
rows go. A file that cannot be deleted keeps its row, is recorded again with ``outcome: error``, and is tried again by
the next sweep; this sweep pages on past it, so no number of such files holds up the others.

No deletion without its record: a record that cannot be written ends the process (``os._exit(1)``) before anything
more is deleted, and ``restart: unless-stopped`` brings the api back; stderr gets the count. The other way round is
allowed: a sweep stopped between a batch's record and the end of its removals (the api shuts down, or is killed) leaves
the rest of the batch to the next sweep, which records those files again. A double record is acceptable; a deletion
without a record is not.

Besides the files, and without a record, since they hold no content: job records older than the period are forgotten;
empty folders in chat folders, the chat folders they leave empty, and ``staging/<job-id>`` folders whose outputs were
never collected (a client that went away) go once untouched for an hour. Nothing is followed: a link, a file or a
folder of another filesystem is left alone, and neither ``workspaces/``, a caller's folder nor a user's root ever goes.
A sweep that deleted something ends with a ``TRUNCATE`` checkpoint, so the log's older pages, which hold file names, are
copied over; then the metrics' gauges are measured again.

``python -m workbench.api.retention dry-run [--days N]`` (``make retention-dry-run``) says what the period, or ``N``
days, would delete now: counts and ages by kind, never a name, an id, a user or a chat. It reads the database
read-only, and records and deletes nothing.
"""

import argparse
import asyncio
import contextlib
import errno
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
from workbench.api.jobs import _STAGING, _outlast
from workbench.api.metrics import Metrics
from workbench.api.store import DATABASE_NAME, DueSummary, Store, summarize_due
from workbench.config import ConfigError, Settings, load_settings
from workbench.models import FileRecord
from workbench.utils.audit_event import MAX_IDS, AuditWriteError

_log = logging.getLogger(__name__)

#: Seconds in a day of the period.
DAY_S: Final = 86_400

#: Most files one batch, and so one event, names: as many ids as an event's target keeps.
BATCH: Final = MAX_IDS

#: Seconds a folder must be untouched before it goes, empty or staged.
FOLDER_GRACE_S: Final = 3600

#: The kinds of file the dry run counts, in its order.
KINDS: Final = ("upload", "output")

_WORKSPACES: Final = "workspaces"
_NOT_EMPTY: Final = frozenset({errno.ENOTEMPTY, errno.EEXIST})


@dataclass
class SweepReport:
    """What one sweep did with the due files.

    Attributes:
        deleted: Files deleted, each after its record.
        errors: Files that could not be deleted; each is recorded again as an error and stays for the next sweep.
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


def _batches(page: Sequence[FileRecord]) -> list[list[FileRecord]]:
    """Split a page of due files by owner, a caller's user, keeping their order.

    Args:
        page: At most :data:`BATCH` files.

    Returns:
        One batch per owner.
    """
    owners: dict[tuple[str, str], list[FileRecord]] = {}
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
        another filesystem, or nothing.
    """
    try:
        st = os.lstat(path)
    except OSError:
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


def _remove_empty(path: Path) -> None:
    """Remove a folder if it is empty; one that is not stays, silently.

    Args:
        path: The folder.
    """
    try:
        os.rmdir(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        if exc.errno not in _NOT_EMPTY:
            _log.warning("retention could not remove an empty folder (%s)", type(exc).__name__)


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

    def sweep_once(self, stop: threading.Event | None = None) -> SweepReport:
        """Delete what is due now, forget old job records, remove stale folders, and measure the volume again.

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
        self._sweep_files(cutoff, stop, report)
        forgotten = 0
        if not stop.is_set():
            forgotten = self._forget_jobs(cutoff)
            self._prune(now - FOLDER_GRACE_S)
        if report.deleted or forgotten:
            self._checkpoint()
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
                    await _outlast(work)
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
        """Record one owner's batch, then delete its files; those that could not go are recorded again as errors.

        Args:
            batch: Due files of one caller's user.
            cutoff: The rule's cutoff.
            stop: Ends the removals before the next file.
            report: Counts what was done.
        """
        first = batch[0]
        criteria: dict[str, object] = {
            "period_days": self._settings.retention_days,
            "cutoff": _iso(cutoff),
            "caller": first.caller,
            "owner": first.user_id,
            "count": len(batch),
        }
        self._record(batch, criteria, "ok")
        failed: list[FileRecord] = []
        for rec in batch:
            if stop.is_set():
                break
            try:
                self._store.remove(rec)
            except Exception as exc:  # PathError and OSError above all; whatever it is, the file stays and is named
                _log.warning("retention could not delete a file (%s)", type(exc).__name__)
                failed.append(rec)
            else:
                report.deleted += 1
        if failed:
            self._record(failed, {**criteria, "count": len(failed)}, "error")
            report.errors += len(failed)

    def _record(self, files: Sequence[FileRecord], criteria: dict[str, object], outcome: str) -> None:
        """Write one ``delete`` / ``retention.sweep`` event, or end the process.

        Args:
            files: The files it names, by id.
            criteria: The rule, the caller, the owner and the count.
            outcome: ``ok`` before a batch goes, ``error`` for the files that did not.

        Raises:
            AuditWriteError: The event could not be written and ``exit_process`` returned.
        """
        try:
            self._audit.record_system(
                "delete", "retention.sweep", file_ids=[rec.id for rec in files], criteria=criteria, outcome=outcome
            )
        except AuditWriteError:
            self._halt(len(files))
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

    def _checkpoint(self) -> None:
        """Copy the database's log over, so the deleted rows' older pages leave it; a busy log is left to later."""
        try:
            emptied = self._store.checkpoint()
        except sqlite3.Error as exc:
            _log.warning("retention could not checkpoint the database (%s)", type(exc).__name__)
            return
        if not emptied:
            _log.warning("the database's log was busy at the checkpoint; a later checkpoint carries it")

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

    def _prune(self, untouched_before: float) -> None:
        """Remove the empty folders of chat folders and the stale staging folders; nothing of it is recorded.

        Args:
            untouched_before: A folder last changed before this may go.
        """
        data = self._settings.data_dir
        try:
            device = os.stat(data).st_dev
        except OSError as exc:
            _log.warning("retention could not look at the data volume (%s)", type(exc).__name__)
            return
        if _folder(data / _WORKSPACES, device) is not None:
            for caller, _ in _subfolders(data / _WORKSPACES, device):
                for user, _ in _subfolders(caller, device):
                    for chat, chat_st in _subfolders(user, device):
                        self._prune_chat(chat, chat_st, device, untouched_before)
        if _folder(data / _STAGING, device) is not None:
            for job, job_st in _subfolders(data / _STAGING, device):
                if job_st.st_mtime < untouched_before:
                    try:
                        shutil.rmtree(job)  # never follows a link inside
                    except OSError as exc:
                        _log.warning("retention could not remove a staging folder (%s)", type(exc).__name__)

    @staticmethod
    def _prune_chat(chat: Path, chat_st: os.stat_result, device: int, untouched_before: float) -> None:
        """Remove the empty folders of one chat folder, deepest first, and the chat folder if that leaves it empty.

        Every folder's age is taken before anything is removed, so an old tree of empty folders goes in one sweep: the
        removal of a folder's own empty folders does not make it new.

        Args:
            chat: The chat folder.
            chat_st: Its ``lstat``.
            device: The data volume's device.
            untouched_before: A folder last changed before this may go.
        """
        found: list[tuple[Path, os.stat_result]] = [(chat, chat_st)]
        for here, names, _ in os.walk(chat):
            kept: list[str] = []
            for name in names:
                st = _folder(Path(here) / name, device)
                if st is not None:
                    kept.append(name)
                    found.append((Path(here) / name, st))
            names[:] = kept  # never into a link or another filesystem
        for folder, st in reversed(found):  # a folder comes after every folder above it
            if st.st_mtime < untouched_before:
                _remove_empty(folder)


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


def dry_run(settings: Settings, out: TextIO, *, days: int, now: float | None = None) -> int:
    """Say what the period would delete now, by kind: counts and ages only; record, delete and change nothing.

    Args:
        settings: Where the data volume is.
        out: Where the report goes.
        days: The period to evaluate.
        now: The time of the report; the clock's by default.

    Returns:
        0 when the database could be read, else 1.
    """
    moment = time.time() if now is None else now
    print("workbench retention, dry run: nothing is recorded, deleted or changed.", file=out)
    print(f"Files are deleted {days} days after they were made.", file=out)
    database = settings.data_dir / DATABASE_NAME
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
    days: int = settings.retention_days if args.days is None else args.days
    return dry_run(settings, sys.stdout, days=days)


if __name__ == "__main__":
    sys.exit(main())
