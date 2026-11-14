"""The bounded queue in front of the executor, and the client for its socket.

The api hands every file-touching tool call to the executor as a job, over a Unix socket, one
request line and one answer line per connection (:mod:`workbench.protocol`). The executor has a
fixed number of slots, so the api decides who goes first and who is turned away, before a job
is sent:

- ``executor_slots`` jobs run at once, and no more are sent;
- one user (a caller's own user id) has ``jobs_per_user`` jobs running at once; their further
  jobs wait, and while they do they hold no slot, so they never keep another user from one;
- at most ``queue_max`` jobs wait; one more is refused at once (``queue_full``);
- one user has at most ``queue_max // executor_slots`` calls waiting (at least one), so a user
  who sends many calls fills their own share of the queue and no more; their next call is
  refused at once (``queue_full``), and the other users are not affected;
- a job waits at most ``queue_wait_s`` for its turn (``wait_timeout``);
- an executor with no free slot, or one that cannot be reached, is a refusal too
  (``executor_busy``).

Every refusal is :class:`Busy`, which the api answers as ``503``, "busy, retry later". A request
that is too large for the protocol's line is not a refusal and not worth a retry:
:class:`RequestTooLarge`, ``422``. The queue is the only user of the staging folder: it moves what
a job wrote into the chat folder (:meth:`~workbench.api.store.Store.register_output`) and removes
the job's staging folder afterwards, whatever came of the job.
"""

import asyncio
import contextlib
import dataclasses
import logging
import shutil
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final, Literal, get_args

from workbench.api.metrics import Metrics
from workbench.api.store import QuotaExceeded, StorageFull, Store, StoreError
from workbench.config import Settings
from workbench.models import FileRecord, JobRecord, Principal
from workbench.paths import PathError, check_filename
from workbench.protocol import MAX_INPUT_FILES, MAX_LINE, JobRequest, JobResult, Limits, decode_result, encode

_log = logging.getLogger(__name__)

#: The staging folder's name on the data volume; the executor writes ``staging/<job-id>/<name>``.
_STAGING: Final = "staging"

#: Seconds the client waits for the answer beyond the job's wall time: the executor's cleanup of
#: a killed job takes up to about 213 s before it answers.
_READ_GRACE_S: Final = 300

#: Why a job was refused.
Reason = Literal["queue_full", "wait_timeout", "executor_busy"]

#: The tools a job may run; it keeps the metrics' ``tool`` label and the job table to a fixed set.
TOOLS: Final = frozenset(
    {
        "read_document",
        "search_files",
        "run_python",
        "inspect_template",
        "fill_template",
        "write_table",
        "write_document",
    }
)


class Busy(Exception):
    """The job was not run, and the caller may try again later.

    Attributes:
        reason: ``queue_full`` (too many jobs wait), ``wait_timeout`` (no slot came free in time) or
            ``executor_busy`` (the executor has no free slot, or cannot be reached).
    """

    def __init__(self, reason: Reason) -> None:
        """Remember why.

        Args:
            reason: Why the job was refused.
        """
        super().__init__(reason)
        self.reason: Reason = reason


class RequestTooLarge(Exception):
    """The request does not fit the protocol's line, so it was not sent; retrying it cannot help.

    The message is a short fixed text; it never holds the request.
    """


class ExecutorUnavailable(Exception):
    """The executor could not be reached, or did not answer with a result.

    The message is a short fixed text, such as the name of the error that caused it; it never
    holds a path, because the queue logs it.
    """


class ExecutorClient:
    """Talks to the executor's Unix socket: one connection per job."""

    def __init__(self, socket_path: Path) -> None:
        """Remember where the executor listens.

        Args:
            socket_path: The executor's Unix socket.
        """
        self._path = socket_path

    async def run(self, req: JobRequest, timeout_s: float) -> JobResult:
        """Send one job and wait for its answer.

        Args:
            req: The job.
            timeout_s: Seconds to wait for the whole exchange. It must cover the job's wall time
                and the executor's cleanup, which comes before the answer.

        Returns:
            The executor's answer. A ``busy`` answer is returned, not raised.

        Raises:
            RequestTooLarge: The encoded request, newline included, is longer than ``MAX_LINE``;
                nothing is sent.
            ExecutorUnavailable: There is no socket, the connection was refused or dropped, no
                answer came in time, or the answer is not a result.
        """
        line = encode(req)
        if len(line) > MAX_LINE:
            raise RequestTooLarge("request too large")
        writer: asyncio.StreamWriter | None = None
        try:
            async with asyncio.timeout(timeout_s):
                reader, writer = await asyncio.open_unix_connection(str(self._path), limit=MAX_LINE)
                writer.write(line)
                await writer.drain()
                line = await reader.readline()
        except (OSError, ValueError) as exc:  # a timeout is an OSError; so is a refused connection
            raise ExecutorUnavailable(type(exc).__name__) from None
        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
        if not line:
            raise ExecutorUnavailable("connection closed without an answer")
        try:
            return decode_result(line)
        except ValueError:
            raise ExecutorUnavailable("malformed answer") from None


class _UserGate:
    """The jobs of one user: how many may run, and how many calls are in the queue's hands.

    Attributes:
        running: Held while a job of the user runs.
        refs: Calls of the user that are waiting or running; the gate is dropped at zero.
        waiting: Calls of the user that are waiting for their turn or a slot.
    """

    def __init__(self, size: int) -> None:
        """Allow ``size`` jobs at once.

        Args:
            size: ``jobs_per_user``.
        """
        self.running = asyncio.Semaphore(size)
        self.refs = 0
        self.waiting = 0


def _check_inputs(input_files: Sequence[str]) -> None:
    """Refuse input files the executor would refuse, before a job record exists.

    Args:
        input_files: Paths relative to the chat folder.

    Raises:
        PathError: There are more than ``MAX_INPUT_FILES`` of them, or one is absolute or holds a
            segment that is not a plain file name.
    """
    if len(input_files) > MAX_INPUT_FILES:
        raise PathError("too many input files")
    for path in input_files:
        if path.startswith("/"):
            raise PathError("invalid file name")
        for segment in path.split("/"):
            check_filename(segment)


class JobQueue:
    """Runs jobs in the executor within the global, per-user and queue limits.

    Attributes:
        metrics: The metrics the queue reports to; ``/metrics`` serves them.
    """

    def __init__(self, settings: Settings, client: ExecutorClient, store: Store, metrics: Metrics) -> None:
        """Set up the slots and the empty queue.

        Args:
            settings: Supplies the slots, the queue bounds, the per-user limit and each job's limits.
            client: The executor's client.
            store: Job records and the files the jobs make.
            metrics: Where waits, durations and refusals are reported.
        """
        self._settings = settings
        self._client = client
        self._store = store
        self.metrics = metrics
        self._slots = asyncio.Semaphore(settings.executor_slots)
        self._gates: dict[tuple[str, str], _UserGate] = {}
        self._waiting = 0
        self._user_waiting_max = max(1, settings.queue_max // settings.executor_slots)
        self._staging = settings.data_dir / _STAGING
        for reason in get_args(Reason):
            metrics.job_refusals_total.labels(reason)

    @property
    def tracked_users(self) -> int:
        """How many users have a call waiting or running; 0 when the queue is idle."""
        return len(self._gates)

    async def submit(
        self, p: Principal, tool: str, args: dict[str, Any], input_files: Sequence[str]
    ) -> tuple[JobRecord, JobResult, list[FileRecord]]:
        """Run a tool in the executor, when its turn comes, and keep what it makes.

        Args:
            p: The caller, user and chat the job runs for.
            tool: Name of the runner to run.
            args: The runner's arguments.
            input_files: Paths relative to the chat folder that the job may read.

        Returns:
            The job record with its final status, the executor's answer and the files the job
            made, now in the chat folder. ``ok`` is the only status that keeps files. The answer's
            message names outputs that could not be kept; its ``outputs`` are empty, since the
            staged files are gone: the returned files are what the job made.

        Raises:
            ValueError: ``tool`` is not one of :data:`TOOLS`; that is a programming error.
            PathError: An input file is not a plain path below the chat folder, there are more
                than ``MAX_INPUT_FILES`` of them, or the caller, user or chat id is not a safe id.
                No job record is made for a request refused for its input files.
            Busy: The queue is full, the user's share of it is, the turn did not come in time,
                or the executor has no free slot or cannot be reached.
            RequestTooLarge: The request does not fit the protocol's line.
        """
        if tool not in TOOLS:
            raise ValueError("unknown tool")
        _check_inputs(input_files)
        key = (p.caller, p.user_id)
        gate = self._gates.get(key)
        if gate is None:
            gate = self._gates[key] = _UserGate(self._settings.jobs_per_user)
        gate.refs += 1
        try:
            await self._admit(gate)
            try:
                return await self._run(p, tool, args, input_files)
            finally:
                self._slots.release()
                gate.running.release()
        finally:
            gate.refs -= 1
            if gate.refs == 0:
                del self._gates[key]

    def _refuse(self, reason: Reason) -> Busy:
        """Count a refusal.

        Args:
            reason: Why the job is refused.

        Returns:
            The exception to raise.
        """
        self.metrics.job_refusals_total.labels(reason).inc()
        return Busy(reason)

    async def _admit(self, gate: _UserGate) -> None:
        """Wait for the user's turn, then for a slot; hold both on return.

        A call that can start at once does not count as waiting. One that cannot is counted
        against ``queue_max``, and against the user's own share of it, until it starts or gives up.
        The user's turn comes before the slot, so a user's second job waits without holding a slot
        another user could use.

        Args:
            gate: The calling user's gate.

        Raises:
            Busy: ``queue_full`` at once when ``queue_max`` calls wait already, or when the user's
                share (``queue_max // executor_slots``, at least one) is used up; ``wait_timeout``
                after ``queue_wait_s`` seconds.
        """
        waits = gate.running.locked() or self._slots.locked()
        if waits:
            if self._waiting >= self._settings.queue_max or gate.waiting >= self._user_waiting_max:
                raise self._refuse("queue_full")
            self._waiting += 1
            gate.waiting += 1
            self.metrics.queue_length.set(self._waiting)
        began = time.monotonic()
        holds_turn = False
        admitted = False
        try:
            async with asyncio.timeout(self._settings.queue_wait_s):
                await gate.running.acquire()
                holds_turn = True
                await self._slots.acquire()
            admitted = True
        except TimeoutError:
            raise self._refuse("wait_timeout") from None
        finally:
            if holds_turn and not admitted:
                gate.running.release()
            if waits:
                self._waiting -= 1
                gate.waiting -= 1
                self.metrics.queue_length.set(self._waiting)
            self.metrics.queue_wait_seconds.observe(time.monotonic() - began)

    async def _run(
        self, p: Principal, tool: str, args: dict[str, Any], input_files: Sequence[str]
    ) -> tuple[JobRecord, JobResult, list[FileRecord]]:
        """Record the job, run it, keep its outputs and settle its record and staging folder.

        Args:
            p: The caller, user and chat.
            tool: Name of the runner.
            args: The runner's arguments.
            input_files: Paths relative to the chat folder.

        Returns:
            What :meth:`submit` returns.

        Raises:
            Busy: The executor has no free slot or cannot be reached.
            RequestTooLarge: The request does not fit the protocol's line.
        """
        settings = self._settings
        job = await asyncio.to_thread(self._store.add_job, p, tool)
        status = "error"
        completed = False
        try:
            req = JobRequest(
                job_id=job.id,
                tool=tool,
                args=args,
                caller=p.caller,
                user_id=p.user_id,
                chat_id=p.chat_id,
                input_files=list(input_files),
                limits=Limits(
                    wall_s=settings.job_wall_s,
                    cpu_s=settings.job_wall_s,
                    memory_mb=settings.job_memory_mb,
                    file_mb=settings.upload_max_mb,
                ),
                max_chars=settings.result_max_kb * 1024,
            )
            result = await self._execute(req)
            files: list[FileRecord] = []
            if result.status == "ok":
                result, files = await self._keep(p, job.id, result)
            result = result.model_copy(update={"outputs": []})  # staged paths are gone once the job settles
            status = result.status
            completed = True
            return dataclasses.replace(job, status=status), result, files
        except Busy:
            status = "busy"
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            await self._settle(job.id, status, pending=not completed)

    async def _settle(self, job_id: str, status: str, *, pending: bool) -> None:
        """Record the job's final status and remove its staging folder; the folder always goes.

        A record that cannot be written is the answer only when the job itself went well. When an
        error or a cancellation is on its way to the caller, that is what the caller must see, so the
        failure is logged and not raised.

        Args:
            job_id: The job's id.
            status: The final status.
            pending: Whether an error or a cancellation is already on its way out of :meth:`_run`.
        """
        try:
            await asyncio.to_thread(self._store.finish_job, job_id, status)
        except Exception as exc:
            if not pending:
                raise
            _log.warning("could not settle a job's record (%s)", type(exc).__name__)
        finally:
            await asyncio.to_thread(self._clear_staging, job_id)

    async def _execute(self, req: JobRequest) -> JobResult:
        """Send the job to the executor and check the answer is its own.

        Args:
            req: The job.

        Returns:
            The executor's answer, with any status but ``busy``.

        Raises:
            Busy: ``executor_busy``: it has no free slot, cannot be reached, or answered for another job.
        """
        began = time.monotonic()
        try:
            result = await self._client.run(req, self._settings.job_wall_s + _READ_GRACE_S)
        except (ExecutorUnavailable, OSError) as exc:
            detail = str(exc) if isinstance(exc, ExecutorUnavailable) else type(exc).__name__
            _log.warning("the executor is unavailable (%s)", detail)
            raise self._refuse("executor_busy") from None
        if result.job_id != req.job_id:
            _log.warning("the executor answered for another job")
            raise self._refuse("executor_busy")
        if result.status == "busy":
            raise self._refuse("executor_busy")
        self.metrics.job_duration_seconds.labels(req.tool).observe(time.monotonic() - began)
        return result

    async def _keep(self, p: Principal, job_id: str, result: JobResult) -> tuple[JobResult, list[FileRecord]]:
        """Move the job's outputs into the chat folder.

        The store trusts the path it is given, so only a file the executor staged for this job, under
        ``staging/<job-id>/``, is handed to it. An output that cannot be kept does not stop the others.

        Args:
            p: The caller, user and chat the job ran for.
            job_id: The job's id.
            result: The executor's ``ok`` answer.

        Returns:
            The answer, its message extended by a note on outputs that could not be kept, and the new files.
        """
        folder = self._staging / job_id
        kept: list[FileRecord] = []
        lost: dict[str, int] = {}
        for output in result.outputs:
            try:
                staged = folder / check_filename(output.name)
                if Path(output.staged_path) != staged:
                    raise PathError("not the job's staging folder")
                kept.append(await asyncio.to_thread(self._store.register_output, p, staged, output.name, job_id))
            except QuotaExceeded:
                why = "quota"
            except StorageFull:
                why = "storage full"
            except PathError:
                why = "invalid"
            except (StoreError, OSError):
                why = "failed"
            else:
                continue
            lost[why] = lost.get(why, 0) + 1
        if lost:
            count = sum(lost.values())
            note = f"{count} {'output' if count == 1 else 'outputs'} could not be kept ({', '.join(lost)})"
            result = result.model_copy(update={"message": f"{result.message}; {note}" if result.message else note})
        return result, kept

    def _clear_staging(self, job_id: str) -> None:
        """Remove the job's staging folder, if there is one; a failure is logged and never raised.

        Args:
            job_id: The job's id, the folder's name.
        """
        try:
            shutil.rmtree(self._staging / job_id)
        except FileNotFoundError:
            pass
        except OSError as exc:
            _log.warning("could not clear a job's staging folder (%s)", type(exc).__name__)
