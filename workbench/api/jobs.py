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
- a caller with slots of its own (:class:`CallLimits`) has that many jobs running at once over
  all its users, and that many times a user's share waiting; its further calls wait without a
  slot, and past its share they are refused at once (``queue_full``);
- a job waits at most ``queue_wait_s`` for its turn, or its call's own wait (``wait_timeout``);
- an executor with no free slot, or one that cannot be reached, is a refusal too
  (``executor_busy``).

Every refusal is :class:`Busy`, which the api answers as ``503``, "busy, retry later". A request
that is too large for the protocol's line is not a refusal and not worth a retry:
:class:`RequestTooLarge`, ``422``. The queue is the only user of the staging folder: it moves what
a job wrote into the chat folder (:meth:`~workbench.api.store.Store.register_output`), or reads it
back for the answer (:meth:`JobQueue.submit_inline`), and removes the job's staging folder
afterwards, whatever came of the job.

No output becomes a file without its record. Once the executor has answered, the caller's
:class:`Keeper` records the job by the ids the queue chose for its outputs, and only then are they
moved into the chat folder; one that cannot be kept, for any reason, or is removed again because
the call failed, is recorded again as an error. A call without a keeper keeps no output at all.
An inline job keeps none either: its :class:`Taker` records it, with the outputs that come back,
before anything of it goes out.
"""

import asyncio
import contextlib
import dataclasses
import logging
import os
import re
import shutil
import stat
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal, Protocol, get_args

from workbench.api.metrics import Metrics
from workbench.api.store import QuotaExceeded, StorageFull, Store, StoreError, new_file_id
from workbench.config import Settings
from workbench.layout import STAGING
from workbench.models import FileRecord, JobRecord, Principal
from workbench.paths import PathError, check_filename
from workbench.protocol import (
    MAX_INPUT_FILES,
    MAX_LINE,
    InlineFile,
    JobRequest,
    JobResult,
    Limits,
    OutputFile,
    check_inline_inputs,
    decode_result,
    encodable,
    encode,
)

_log = logging.getLogger(__name__)

#: Seconds the client waits for the answer beyond the job's wall time: the executor's cleanup of
#: a killed job takes up to about 213 s before it answers.
_READ_GRACE_S: Final = 300

#: Why a job was refused.
Reason = Literal["queue_full", "wait_timeout", "executor_busy"]

_MIB: Final = 1024 * 1024

#: Seconds a call through the MCP door waits for its turn at the most.
MCP_QUEUE_WAIT_S: Final = 5

#: The media type of each output type an inline job gives back, by its suffix, lower-cased.
RETURN_TYPES: Final[Mapping[str, str]] = MappingProxyType(
    {".png": "image/png", ".csv": "text/csv", ".txt": "text/plain", ".md": "text/markdown", ".json": "application/json"}
)
#: Most outputs that come back.
MAX_RETURNED: Final = 8
#: Largest image that comes back, in bytes.
IMAGE_MAX: Final = 512 * 1024
#: Largest text that comes back, in bytes.
TEXT_MAX: Final = 256 * 1024
#: Most bytes that come back in all.
RETURN_MAX: Final = 1024 * 1024
#: The first bytes of every PNG file.
PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"

#: A C0 control character other than tab, LF and CR, or DEL. A text holding one does not come back: JSON escapes
#: it to six bytes, which would grow the answer past what a caller accepts.
_CONTROL: Final = re.compile(rb"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Why an output does not come back.
SkipReason = Literal["type", "signature", "encoding", "count", "size", "total", "unreadable"]


@dataclasses.dataclass(frozen=True)
class CallLimits:
    """What one call may take of the queue: its caller's limits, as its door applies them.

    Attributes:
        wall_s: The most wall-clock seconds its job may run; the job gets the least of this and ``job_wall_s``.
        wait_s: The most seconds it waits for its turn, in place of ``queue_wait_s``.
        slots: The most jobs of its caller that run at once; ``None`` for no cap of the caller's own.
    """

    wall_s: int
    wait_s: int
    slots: int | None = None

    def __post_init__(self) -> None:
        """Refuse limits no call can have: they are a door's bug, caught here rather than in the queue.

        Raises:
            ValueError: ``wall_s`` is below 1, ``wait_s`` below 0, or ``slots`` is set and below 1.
        """
        if self.wall_s < 1 or self.wait_s < 0 or (self.slots is not None and self.slots < 1):
            raise ValueError("call limits need a wall of at least 1 s, a wait of at least 0 s and at least 1 slot")


@dataclasses.dataclass(frozen=True)
class TakenFile:
    """An output that comes back with the answer.

    Attributes:
        name: Its name in the job's ``out/``, as the job wrote it.
        mime: Its media type, from :data:`RETURN_TYPES`.
        data: Its bytes, as the job wrote them; left out of ``repr``.
    """

    name: str
    mime: str
    data: bytes = dataclasses.field(repr=False)


@dataclasses.dataclass(frozen=True)
class SkippedOutput:
    """An output that does not come back.

    Attributes:
        name: Its name in the job's ``out/``.
        size: Its size in bytes, as the executor staged it.
        reason: Why it does not come back.
    """

    name: str
    size: int
    reason: SkipReason


@dataclasses.dataclass(frozen=True)
class TakenOutputs:
    """What an inline job's outputs come to.

    Attributes:
        files: The outputs that come back, in return order.
        skipped: The outputs that do not, in return order.
    """

    files: tuple[TakenFile, ...] = ()
    skipped: tuple[SkippedOutput, ...] = ()


class Taker(Protocol):
    """What the caller of :meth:`JobQueue.submit_inline` does with a job's answer before anything of it goes out.

    It runs in a worker thread of the loop, under the queue's guard against cancellation: once called, it runs to its
    end.
    """

    def take(self, result: JobResult, taken: TakenOutputs) -> None:
        """Record the job.

        Args:
            result: The executor's answer, whatever its status.
            taken: The outputs that come back and those that do not; empty unless the job is ``ok``.

        Raises:
            Exception: The event could not be written (``AuditWriteError``); then ``submit_inline`` raises it, and
                nothing of the answer goes out.
        """
        ...


class Keeper(Protocol):
    """What the caller of :meth:`JobQueue.submit` does with a job's answer before any file of it becomes visible.

    Both methods run in a worker thread of the loop, under the queue's guard against cancellation: once called, they
    run to their end.
    """

    def record(self, result: JobResult, planned: Sequence[str]) -> Sequence[str]:
        """Record the job, by the ids its outputs will have, and say which outputs to keep.

        Args:
            result: The executor's answer, whatever its status.
            planned: One new file id per staged output, in order; empty unless the job is ``ok``.

        Returns:
            The ids of the outputs to keep: some or all of ``planned``, each named by the event just written.

        Raises:
            Exception: The event could not be written (``AuditWriteError``); then no output is kept, and the error is
                what ``submit`` raises.
        """
        ...

    def again(self, ids: Sequence[str]) -> None:
        """Record as ``error`` outputs the event named that did not end as it said: not kept, or kept and then removed.

        Args:
            ids: Their file ids.

        Raises:
            Exception: The event could not be written.
        """
        ...


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


class _CallerGate:
    """The jobs of one capped caller, over all its users: how many may run, and how many of its calls may wait.

    Attributes:
        running: Held while a job of the caller runs.
        waiting: Calls of the caller that are waiting for their turn, the gate or a slot.
        waiting_max: The most calls of the caller that may wait.
    """

    def __init__(self, slots: int, waiting_max: int) -> None:
        """Allow ``slots`` jobs at once and ``waiting_max`` calls waiting.

        Args:
            slots: The caller's slots.
            waiting_max: ``slots`` times the user's share of the queue.
        """
        self.running = asyncio.Semaphore(slots)
        self.waiting = 0
        self.waiting_max = waiting_max


def suffix_of(name: str) -> str:
    """The suffix of an output's name, lower-cased, by which its type is known.

    Args:
        name: The name, as the job wrote it.

    Returns:
        The suffix with its dot, such as ``.png``; empty when the name has none.
    """
    return os.path.splitext(name)[1].lower()


def _return_order(output: OutputFile) -> tuple[int, str]:
    """Charts first, then tables, then the rest, each by name.

    Args:
        output: A staged output.

    Returns:
        Its sort key.
    """
    return {".png": 0, ".csv": 1}.get(suffix_of(output.name), 2), output.name


def _read_regular(path: Path, limit: int) -> bytes:
    """Read at most ``limit`` bytes of a regular file, never through a symlink in its place and never waiting on one.

    Args:
        path: The file.
        limit: The most bytes to read.

    Returns:
        Its first ``limit`` bytes, or all of it.

    Raises:
        OSError: It cannot be opened or read, is a symlink or is not a regular file.
    """
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        chunks: list[bytes] = []
        left = limit
        while left > 0:
            chunk = os.read(fd, left)
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


class _Skip(Exception):
    """An output does not come back.

    Attributes:
        reason: Why.
    """

    def __init__(self, reason: SkipReason) -> None:
        """Remember why.

        Args:
            reason: Why the output does not come back.
        """
        super().__init__(reason)
        self.reason: SkipReason = reason


async def outlast[T](work: asyncio.Future[T]) -> T:
    """Wait for work to end, whatever cancellation comes meanwhile; the caller has one on its way out already.

    Args:
        work: Work running on its own, such as a call in a thread.

    Returns:
        What the work returned.

    Raises:
        Exception: What the work raised.
    """
    while True:
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            if work.done():
                return work.result()


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
    """Runs jobs in the executor within the global, per-caller, per-user and queue limits.

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
        self._caller_gates: dict[str, _CallerGate] = {}
        self._waiting = 0
        self._user_waiting_max = max(1, settings.queue_max // settings.executor_slots)
        self._staging = settings.data_dir / STAGING
        for reason in get_args(Reason):
            metrics.job_refusals_total.labels(reason)

    @property
    def tracked_users(self) -> int:
        """How many users have a call waiting or running; 0 when the queue is idle."""
        return len(self._gates)

    async def submit(
        self,
        p: Principal,
        tool: str,
        args: dict[str, Any],
        input_files: Sequence[str],
        keeper: Keeper | None = None,
        *,
        call: CallLimits | None = None,
    ) -> tuple[JobRecord, JobResult, list[FileRecord]]:
        """Run a tool in the executor, when its turn comes, and keep what it makes.

        Args:
            p: The caller, user and chat the job runs for.
            tool: Name of the runner to run.
            args: The runner's arguments; every lone surrogate in their strings is sent as U+FFFD.
            input_files: Paths relative to the chat folder that the job may read.
            keeper: Records the job once the executor has answered, before any output is moved into the chat folder,
                and picks the outputs to keep. Without one, no output is kept: a tool that makes no file has none.
            call: The call's wall, wait and caller slots; without it, ``job_wall_s``, ``queue_wait_s`` and no cap of
                the caller's own.

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
            Busy: The queue is full, the user's or the caller's share of it is, the turn did not come in time,
                or the executor has no free slot or cannot be reached.
            RequestTooLarge: The request does not fit the protocol's line.
            Exception: What the keeper raised (no output is kept), or what the store raises, such as a failure to
                settle the job's record. Files the job made are removed again first, and the keeper records each of
                them again.
        """
        if tool not in TOOLS:
            raise ValueError("unknown tool")
        _check_inputs(input_files)
        async with self._turn(p, call):
            return await self._run(p, tool, args, input_files, keeper, call)

    async def submit_inline(
        self,
        p: Principal,
        tool: str,
        args: dict[str, Any],
        inline_inputs: Sequence[InlineFile],
        taker: Taker,
        call: CallLimits,
    ) -> tuple[JobRecord, JobResult, TakenOutputs]:
        """Run a tool in the executor on data the call brings, when its turn comes, and take its outputs back.

        No input is copied from a workspace, and no output is kept: the outputs of an ``ok`` job are read from the
        staging folder by type and cap (:meth:`_take`), the taker records the job, and only then does anything of it
        go out. The staging folder goes whatever came of the job.

        Args:
            p: The caller, user and session the job runs for.
            tool: Name of the runner to run.
            args: The runner's arguments; every lone surrogate in their strings is sent as U+FFFD.
            inline_inputs: The data, each entry written as one read-only file into the job's working folder.
            taker: Records every job the executor answered, whatever its status, before anything of it goes out.
            call: The call's wall, wait and caller slots.

        Returns:
            The job record with its final status, the executor's answer without its staged outputs, and the outputs
            that come back and those that do not.

        Raises:
            ValueError: ``tool`` is not one of :data:`TOOLS`, or the inline inputs are not what the protocol takes
                (:func:`~workbench.protocol.check_inline_inputs`; the door checks them first). Like ``submit``'s
                inputs, they are checked before the call waits for its turn, and no job record is made for them.
            PathError: The caller, user or session id is not a safe id.
            Busy: The queue is full, the user's or the caller's share of it is, the turn did not come in time, or the
                executor has no free slot or cannot be reached.
            RequestTooLarge: The request does not fit the protocol's line; escaping can make data of 512 KiB that.
            Exception: What the taker raised; nothing of the answer goes out. Or what the store raises, such as a
                failure to settle the job's record.
        """
        if tool not in TOOLS:
            raise ValueError("unknown tool")
        check_inline_inputs(inline_inputs)
        async with self._turn(p, call):
            return await self._run_inline(p, tool, args, inline_inputs, taker, call)

    @contextlib.asynccontextmanager
    async def _turn(self, p: Principal, call: CallLimits | None) -> AsyncIterator[None]:
        """Hold the user's turn, the caller's gate and a slot while the job runs; give them back whatever happens.

        Args:
            p: The caller and user.
            call: The call's wait and caller slots; ``None`` for ``queue_wait_s`` and no cap of the caller's own.

        Yields:
            Once the job may run.

        Raises:
            Busy: ``queue_full`` or ``wait_timeout`` (:meth:`_admit`).
        """
        key = (p.caller, p.user_id)
        gate = self._gates.get(key)
        if gate is None:
            gate = self._gates[key] = _UserGate(self._settings.jobs_per_user)
        caller_gate = self._caller_gate(p.caller, call)
        gate.refs += 1
        try:
            await self._admit(gate, caller_gate, self._settings.queue_wait_s if call is None else call.wait_s)
            try:
                yield
            finally:
                self._slots.release()
                if caller_gate is not None:
                    caller_gate.running.release()
                gate.running.release()
        finally:
            gate.refs -= 1
            if gate.refs == 0:
                del self._gates[key]

    def _caller_gate(self, caller: str, call: CallLimits | None) -> _CallerGate | None:
        """The gate of a capped caller, made on its first capped call.

        Args:
            caller: The caller's name.
            call: The call's caller slots.

        Returns:
            The gate; ``None`` for a call with no cap of its caller's own.
        """
        if call is None or call.slots is None:
            return None
        gate = self._caller_gates.get(caller)
        if gate is None:
            gate = self._caller_gates[caller] = _CallerGate(call.slots, call.slots * self._user_waiting_max)
        return gate

    def _refuse(self, reason: Reason) -> Busy:
        """Count a refusal.

        Args:
            reason: Why the job is refused.

        Returns:
            The exception to raise.
        """
        self.metrics.job_refusals_total.labels(reason).inc()
        return Busy(reason)

    async def _admit(self, gate: _UserGate, caller_gate: _CallerGate | None, wait_s: float) -> None:
        """Wait for the user's turn, then for the caller's gate, then for a slot; hold them all on return.

        A call that can start at once does not count as waiting. One that cannot is counted against
        ``queue_max``, against the user's own share of it and against the caller's, until it starts or
        gives up. The user's turn and the caller's gate come before the slot, so a call that waits for
        either holds no slot another caller could use.

        Args:
            gate: The calling user's gate.
            caller_gate: The calling caller's gate; ``None`` for a caller with no cap of its own.
            wait_s: The most seconds to wait.

        Raises:
            Busy: ``queue_full`` at once when ``queue_max`` calls wait already, or when the user's
                share (``queue_max // executor_slots``, at least one) or the caller's (its slots times
                the user's share) is used up; ``wait_timeout`` after ``wait_s`` seconds.
        """
        turns = [gate.running, *([caller_gate.running] if caller_gate is not None else []), self._slots]
        waits = any(turn.locked() for turn in turns)
        if waits:
            if (
                self._waiting >= self._settings.queue_max
                or gate.waiting >= self._user_waiting_max
                or (caller_gate is not None and caller_gate.waiting >= caller_gate.waiting_max)
            ):
                raise self._refuse("queue_full")
            self._waiting += 1
            gate.waiting += 1
            if caller_gate is not None:
                caller_gate.waiting += 1
            self.metrics.queue_length.set(self._waiting)
        began = time.monotonic()
        held: list[asyncio.Semaphore] = []
        admitted = False
        try:
            async with asyncio.timeout(wait_s):
                for turn in turns:
                    await turn.acquire()
                    held.append(turn)
            admitted = True
        except TimeoutError:
            raise self._refuse("wait_timeout") from None
        finally:
            if not admitted:
                for turn in reversed(held):
                    turn.release()
            if waits:
                self._waiting -= 1
                gate.waiting -= 1
                if caller_gate is not None:
                    caller_gate.waiting -= 1
                self.metrics.queue_length.set(self._waiting)
            self.metrics.queue_wait_seconds.observe(time.monotonic() - began)

    def _limits(self, call: CallLimits | None) -> Limits:
        """The limits of a job: its wall, the least of the call's and ``job_wall_s``, as wall and CPU time.

        Args:
            call: The call's wall; ``None`` for ``job_wall_s``.

        Returns:
            The job's limits.
        """
        settings = self._settings
        wall = settings.job_wall_s if call is None else min(call.wall_s, settings.job_wall_s)
        return Limits(wall_s=wall, cpu_s=wall, memory_mb=settings.job_memory_mb, file_mb=settings.upload_max_mb)

    def _request(
        self,
        job_id: str,
        p: Principal,
        tool: str,
        args: dict[str, Any],
        call: CallLimits | None,
        *,
        input_files: Sequence[str] = (),
        inline_inputs: Sequence[InlineFile] = (),
    ) -> JobRequest:
        """Build a job's request: the inputs it copies or the data it carries, within its limits.

        Args:
            job_id: The job's id.
            p: The caller, user and chat.
            tool: Name of the runner.
            args: The runner's arguments; every lone surrogate in their strings is sent as U+FFFD.
            call: The call's wall; ``None`` for ``job_wall_s``.
            input_files: Paths relative to the chat folder.
            inline_inputs: The data.

        Returns:
            The request.
        """
        return JobRequest(
            job_id=job_id,
            tool=tool,
            args=encodable(args),  # a lone surrogate from the model would make the request unsendable
            caller=p.caller,
            user_id=p.user_id,
            chat_id=p.chat_id,
            input_files=list(input_files),
            inline_inputs=list(inline_inputs),
            limits=self._limits(call),
            max_chars=self._settings.result_max_kb * 1024,
        )

    async def _run(
        self,
        p: Principal,
        tool: str,
        args: dict[str, Any],
        input_files: Sequence[str],
        keeper: Keeper | None,
        call: CallLimits | None,
    ) -> tuple[JobRecord, JobResult, list[FileRecord]]:
        """Record the job, run it, keep its outputs and settle its record and staging folder.

        Args:
            p: The caller, user and chat.
            tool: Name of the runner.
            args: The runner's arguments.
            input_files: Paths relative to the chat folder.
            keeper: Records the answer and picks the outputs to keep; ``None`` keeps none.
            call: The call's wall; ``None`` for ``job_wall_s``.

        Returns:
            What :meth:`submit` returns.

        Raises:
            Busy: The executor has no free slot or cannot be reached.
            RequestTooLarge: The request does not fit the protocol's line.
        """
        job = await asyncio.to_thread(self._store.add_job, p, tool)
        status = "error"
        completed = False
        files: list[FileRecord] = []
        try:
            req = self._request(job.id, p, tool, args, call, input_files=input_files)
            result = await self._execute(req)
            if keeper is not None:
                result, files = await self._keep(p, job.id, result, keeper)
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
            try:
                await self._settle(job.id, status, pending=not completed)
            except BaseException:
                # The caller gets this error and tells the model of no file, so no file the job made may stay.
                await asyncio.to_thread(self._discard, files, keeper)
                raise

    async def _run_inline(
        self,
        p: Principal,
        tool: str,
        args: dict[str, Any],
        inline_inputs: Sequence[InlineFile],
        taker: Taker,
        call: CallLimits,
    ) -> tuple[JobRecord, JobResult, TakenOutputs]:
        """Record the job, run it, hand its answer and outputs to the taker and settle its record and staging folder.

        Args:
            p: The caller, user and session.
            tool: Name of the runner.
            args: The runner's arguments.
            inline_inputs: The data.
            taker: Records the answer.
            call: The call's wall.

        Returns:
            What :meth:`submit_inline` returns.

        Raises:
            Busy: The executor has no free slot or cannot be reached.
            RequestTooLarge: The request does not fit the protocol's line.
        """
        job = await asyncio.to_thread(self._store.add_job, p, tool)
        status = "error"
        completed = False
        try:
            req = self._request(job.id, p, tool, args, call, inline_inputs=inline_inputs)
            result = await self._execute(req)
            taken = await self._hand_over(job.id, result, taker)
            result = result.model_copy(update={"outputs": []})  # staged paths are gone once the job settles
            status = result.status
            completed = True
            return dataclasses.replace(job, status=status), result, taken
        except Busy:
            status = "busy"
            raise
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        finally:
            await self._settle(job.id, status, pending=not completed)

    async def _hand_over(self, job_id: str, result: JobResult, taker: Taker) -> TakenOutputs:
        """Take the outputs back and have the taker record the job, as one call in a thread.

        A cancellation does not cut it off half way: it finishes, and then the cancellation goes on. It is a plain
        future of the loop's executor, not a task, as in :meth:`_keep`.

        Args:
            job_id: The job's id.
            result: The executor's answer, whatever its status.
            taker: Records the answer.

        Returns:
            The outputs that come back and those that do not.
        """
        loop = asyncio.get_running_loop()
        handing = loop.run_in_executor(None, self._take_and_hand, job_id, result, taker)
        try:
            return await asyncio.shield(handing)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await outlast(handing)
            raise

    def _take_and_hand(self, job_id: str, result: JobResult, taker: Taker) -> TakenOutputs:
        """Take the outputs back, then hand them and the answer to the taker.

        Args:
            job_id: The job's id.
            result: The executor's answer.
            taker: Records the answer.

        Returns:
            What :meth:`_take` returns.
        """
        taken = self._take(job_id, result)
        taker.take(result, taken)
        return taken

    def _take(self, job_id: str, result: JobResult) -> TakenOutputs:
        """Read back the outputs of an ``ok`` job that come back, by type, order and cap.

        A type outside :data:`RETURN_TYPES` is skipped before any cap applies. The rest go in return order (charts,
        then tables, then the rest, each by name): past :data:`MAX_RETURNED` kept files, past its own cap
        (:data:`IMAGE_MAX` or :data:`TEXT_MAX`), a PNG without its signature, a text that is not strict UTF-8 or holds
        a control character (:data:`_CONTROL`), or a file past :data:`RETURN_MAX` in all is skipped, and the next file
        is still tried. Only a regular file the executor staged for this job is read, at most its cap and one byte;
        anything else is ``unreadable``.

        Args:
            job_id: The job's id.
            result: The executor's answer.

        Returns:
            The outputs that come back and those that do not; empty unless the job is ``ok``.
        """
        if result.status != "ok":
            return TakenOutputs()
        files: list[TakenFile] = []
        skipped: list[SkippedOutput] = []
        total = 0
        for output in sorted(result.outputs, key=_return_order):
            try:
                file = self._take_one(job_id, output, kept=len(files), room=RETURN_MAX - total)
            except _Skip as skip:
                skipped.append(SkippedOutput(output.name, output.size, skip.reason))
                continue
            files.append(file)
            total += len(file.data)
        return TakenOutputs(tuple(files), tuple(skipped))

    def _take_one(self, job_id: str, output: OutputFile, *, kept: int, room: int) -> TakenFile:
        """Read back one output, if it comes back.

        Args:
            job_id: The job's id.
            output: The staged output.
            kept: How many outputs come back before it.
            room: How many bytes may still come back.

        Returns:
            The output.

        Raises:
            _Skip: It does not come back, and why.
        """
        suffix = suffix_of(output.name)
        mime = RETURN_TYPES.get(suffix)
        if mime is None:
            raise _Skip("type")
        if kept >= MAX_RETURNED:
            raise _Skip("count")
        cap = IMAGE_MAX if suffix == ".png" else TEXT_MAX
        try:
            data = _read_regular(self._staged(job_id, output), cap + 1)
        except (PathError, OSError):
            raise _Skip("unreadable") from None
        if len(data) > cap:
            raise _Skip("size")
        if suffix == ".png":
            if not data.startswith(PNG_SIGNATURE):
                raise _Skip("signature")
        else:
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                raise _Skip("encoding") from None
            if _CONTROL.search(data):
                raise _Skip("encoding")
        if len(data) > room:
            raise _Skip("total")
        return TakenFile(output.name, mime, data)

    def _staged(self, job_id: str, output: OutputFile) -> Path:
        """The staged file of an output, which must be where the executor stages this job's files.

        The store and the reader trust the path they are given, so only ``staging/<job-id>/<name>`` passes.

        Args:
            job_id: The job's id.
            output: The staged output.

        Returns:
            Its path.

        Raises:
            PathError: Its name is not one plain file name, or its path is not that one.
        """
        staged = self._staging / job_id / check_filename(output.name)
        if Path(output.staged_path) != staged:
            raise PathError("not the job's staging folder")
        return staged

    def _discard(self, files: Sequence[FileRecord], keeper: Keeper | None) -> None:
        """Remove outputs the model will not be told of: the job could not be settled, or its caller went away.

        Their event, written before they were kept, named them as kept; they did not end so, so each is named again by
        an ``error`` event of the keeper, whether it was removed or could not be. A failure is logged by its type and
        never raised.

        Args:
            files: The outputs already moved into the chat folder.
            keeper: Records them again.
        """
        for rec in files:
            try:
                self._store.remove(rec)
            except Exception as exc:
                _log.warning("could not remove an output the model will not be told of (%s)", type(exc).__name__)
        if files and keeper is not None:
            try:
                keeper.again([rec.id for rec in files])
            except Exception as exc:
                _log.warning("could not record outputs that were not kept (%s)", type(exc).__name__)

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
            result = await self._client.run(req, req.limits.wall_s + _READ_GRACE_S)
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

    async def _keep(
        self, p: Principal, job_id: str, result: JobResult, keeper: Keeper
    ) -> tuple[JobResult, list[FileRecord]]:
        """Record the job's answer, then move the outputs the keeper picked into the chat folder.

        The record and the registration run as one call in a thread. A cancellation does not cut it off half way: it
        finishes, what it registered is removed again and recorded again (the caller that is going away tells the
        model of none of it), and then the cancellation goes on. Both calls are plain futures of the loop's executor,
        not tasks: ``asyncio.run`` cancels every task as it shuts down, and a cancelled task would stop waiting while
        its thread goes on moving files.

        Args:
            p: The caller, user and chat the job ran for.
            job_id: The job's id.
            result: The executor's answer, whatever its status.
            keeper: Records the answer and picks the outputs to keep.

        Returns:
            The answer, its message extended by a note on outputs that could not be kept, and the new files.
        """
        loop = asyncio.get_running_loop()
        registering = loop.run_in_executor(None, self._register, p, job_id, result, keeper)
        try:
            kept, lost = await asyncio.shield(registering)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):  # a registration that failed has removed its files itself
                kept, _ = await outlast(registering)
                await outlast(loop.run_in_executor(None, self._discard, kept, keeper))
            raise
        if lost:
            count = sum(lost.values())
            note = f"{count} {'output' if count == 1 else 'outputs'} could not be kept ({', '.join(lost)})"
            result = result.model_copy(update={"message": f"{result.message}; {note}" if result.message else note})
        return result, kept

    def _register(
        self, p: Principal, job_id: str, result: JobResult, keeper: Keeper
    ) -> tuple[list[FileRecord], dict[str, int]]:
        """Have the keeper record the job, then register each output it picked as a file of the chat.

        Each staged output gets its file id before anything is recorded, so the keeper's event names every file that
        may become visible. The store trusts the path it is given, so only a file the executor staged for this job,
        under ``staging/<job-id>/``, is handed to it. An output that cannot be kept, for whatever reason (the user's
        quota, a full volume, an invalid name, or an error the store does not foresee, which is logged by its type),
        does not stop the others; those are recorded again as errors once all are through. An error record that
        cannot be written removes the files registered before it (``_discard``), then goes on up.

        Args:
            p: The caller, user and chat the job ran for.
            job_id: The job's id.
            result: The executor's answer; only an ``ok`` one has outputs.
            keeper: Records the answer and picks the outputs to keep.

        Returns:
            The new files, and how many outputs could not be kept, by reason.
        """
        outputs: list[OutputFile] = result.outputs if result.status == "ok" else []
        planned = [new_file_id() for _ in outputs]
        chosen = set(keeper.record(result, planned))
        quota = self._settings.workspace_max_mb * _MIB
        kept: list[FileRecord] = []
        lost: dict[str, int] = {}
        missed: list[str] = []
        try:
            for output, file_id in zip(outputs, planned, strict=True):
                if file_id not in chosen:
                    continue
                try:
                    staged = self._staged(job_id, output)
                    kept.append(
                        self._store.register_output(p, staged, output.name, job_id, file_id=file_id, quota_bytes=quota)
                    )
                except QuotaExceeded:
                    why = "quota"
                except StorageFull:
                    why = "storage full"
                except PathError:
                    why = "invalid"
                except (StoreError, OSError):
                    why = "failed"
                except Exception as exc:  # the event named the output, so it is named again whatever the error
                    _log.warning("could not keep an output (%s)", type(exc).__name__)
                    why = "failed"
                else:
                    continue
                lost[why] = lost.get(why, 0) + 1
                missed.append(file_id)
            if missed:
                keeper.again(missed)
        except BaseException:
            self._discard(kept, keeper)
            raise
        return kept, lost

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
