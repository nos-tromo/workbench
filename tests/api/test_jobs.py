"""The job queue bounds what reaches the executor, registers what comes back and tells the caller when to retry."""

import asyncio
import contextlib
import dataclasses
import io
import logging
import os
import shutil
import socket
import sqlite3
import tempfile
import threading
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, override

import pytest

from tests.api.conftest import unrecorded
from tests.conftest import scaled
from workbench.api.jobs import (
    IMAGE_MAX,
    MCP_QUEUE_WAIT_S,
    PNG_SIGNATURE,
    RETURN_MAX,
    RETURN_TYPES,
    TEXT_MAX,
    Busy,
    CallLimits,
    ExecutorClient,
    ExecutorUnavailable,
    JobQueue,
    RequestTooLarge,
    TakenOutputs,
)
from workbench.api.metrics import Metrics
from workbench.api.store import QuotaExceeded, Store
from workbench.config import Settings
from workbench.models import FileRecord, JobRecord, Principal
from workbench.paths import PathError, Workspaces
from workbench.protocol import (
    JOB_ID,
    MAX_INLINE_BYTES,
    MAX_INLINE_FILES,
    MAX_LINE,
    InlineFile,
    JobRequest,
    JobResult,
    OutputFile,
    decode_request,
    encode,
)
from workbench.utils.audit_event import AuditWriteError

pytestmark = pytest.mark.anyio

ARGS: dict[str, Any] = {"code": "print(1)"}
MIB = 1024 * 1024


@pytest.fixture
def anyio_backend() -> str:
    """Run the async tests on asyncio, the loop the api runs on."""
    return "asyncio"


def user(name: str, chat: str = "chat-1") -> Principal:
    """A synthetic user of Open WebUI."""
    return Principal("open-webui", name, chat)


def sample_result(
    job_id: str, outputs: Sequence[OutputFile] = (), *, status: str = "ok", message: str = ""
) -> JobResult:
    """An executor answer for ``job_id``."""
    return JobResult.model_validate(
        {
            "job_id": job_id,
            "status": status,
            "result": {"answer": 42},
            "outputs": [o.model_dump() for o in outputs],
            "message": message,
        }
    )


class FakeClient(ExecutorClient):
    """An executor client that waits, then answers ``ok`` and stages the files it was told to.

    Attributes:
        requests: Every request it received, in order.
        timeouts: The timeout each request came with.
        running: Jobs in flight now.
        peak: The most jobs in flight at once.
        gate: While set, a job that ``hold`` marks does not finish until the test sets it.
    """

    def __init__(self, data_dir: Path, *, delay: float = 0.0) -> None:
        """Answer after ``delay`` seconds; stage under ``data_dir``, as the executor does."""
        super().__init__(Path("unused.sock"))
        self._data_dir = data_dir
        self.delay = delay
        self.requests: list[JobRequest] = []
        self.timeouts: list[float] = []
        self.running = 0
        self.peak = 0
        self.gate = asyncio.Event()
        self.hold: set[str] = set()
        self.files: dict[str, bytes] = {}
        self.status = "ok"
        self.message = ""
        self.error: Exception | None = None

    def staged(self, job_id: str, name: str) -> Path:
        """Where the executor would stage ``name`` for ``job_id``."""
        return self._data_dir / "staging" / job_id / name

    @override
    async def run(self, req: JobRequest, timeout_s: float) -> JobResult:
        """Count the job, wait, then answer."""
        self.requests.append(req)
        self.timeouts.append(timeout_s)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            if req.caller + "/" + req.user_id in self.hold:
                await self.gate.wait()
            elif self.delay:
                await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            outputs = []
            for name, data in self.files.items():
                path = self.staged(req.job_id, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                outputs.append(OutputFile(name=name, staged_path=str(path), size=len(data)))
            return sample_result(req.job_id, outputs, status=self.status, message=self.message)
        finally:
            self.running -= 1


class KeepAll:
    """A keeper that keeps every output, as a tool that makes files does, and remembers what it was asked.

    Attributes:
        recorded: Per answer recorded: its status, the ids planned, and the names of the chat's files at that moment.
        again: Every list of ids recorded again as errors.
        store: Where it looks, at the moment it records, for files of the chat.
        p: Whose chat it looks into.
    """

    def __init__(self, store: Store | None = None, p: Principal | None = None) -> None:
        """Look into ``p``'s chat in ``store`` when recording, if both are given."""
        self.recorded: list[tuple[str, list[str], list[str]]] = []
        self.again_ids: list[list[str]] = []
        self.store = store
        self.p = p

    def record(self, result: JobResult, planned: Sequence[str]) -> Sequence[str]:
        """Keep every output."""
        seen = [] if self.store is None or self.p is None else [r.name for r in self.store.files_in_chat(self.p)]
        self.recorded.append((result.status, list(planned), seen))
        return planned

    def again(self, ids: Sequence[str]) -> None:
        """Remember the ids."""
        self.again_ids.append(list(ids))


#: A keeper for the tests that only need the outputs kept.
KEEP = KeepAll()


class Exploding(KeepAll):
    """A keeper whose record fails with a ``RuntimeError`` of the given text."""

    def __init__(self, text: str) -> None:
        """Fail with ``text``."""
        super().__init__()
        self.text = text

    @override
    def record(self, result: JobResult, planned: Sequence[str]) -> Sequence[str]:
        """Fail."""
        raise RuntimeError(self.text)


@pytest.fixture
def executor(settings: Settings) -> FakeClient:
    """A fake executor that answers at once."""
    return FakeClient(settings.data_dir)


@pytest.fixture
def metrics(settings: Settings) -> Metrics:
    """The metrics of the queue under test."""
    return Metrics(settings.data_dir)


def queue_for(settings: Settings, executor: ExecutorClient, store: Store, metrics: Metrics, **changes: Any) -> JobQueue:
    """A queue over ``settings`` with some of its limits changed."""
    return JobQueue(dataclasses.replace(settings, **changes), executor, store, metrics)


async def until(condition: Callable[[], bool], *, seconds: float = 2.0) -> None:
    """Let the loop run until ``condition()`` holds."""
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.005)


def sample(metrics: Metrics, name: str, **labels: str) -> float:
    """Read one series of the metrics' registry; 0.0 if it has no samples yet."""
    return metrics.registry.get_sample_value(name, labels) or 0.0


# --- limits ----------------------------------------------------------------


async def test_never_more_than_slots_run_at_once(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Ten users, four slots: four jobs are in flight at the most, and all ten finish."""
    executor.delay = 0.05
    queue = queue_for(settings, executor, store, metrics, executor_slots=4)
    done = await asyncio.gather(*(queue.submit(user(f"user-{n}"), "run_python", ARGS, []) for n in range(10)))
    assert executor.peak == 4
    assert len(executor.requests) == 10
    assert {result.status for _, result, _ in done} == {"ok"}
    assert sample(metrics, "workbench_queue_length") == 0


async def test_full_queue_refuses_at_once(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """One slot and room for one waiter: the third call is refused without waiting."""
    executor.hold = {"open-webui/user-a", "open-webui/user-b", "open-webui/user-c"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=1, queue_max=1)
    first = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, []))
    await until(lambda: executor.running == 1)
    second = asyncio.create_task(queue.submit(user("user-b"), "run_python", ARGS, []))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    async with asyncio.timeout(0.5):
        with pytest.raises(Busy) as refused:
            await queue.submit(user("user-c"), "run_python", ARGS, [])
    assert refused.value.reason == "queue_full"
    assert sample(metrics, "workbench_job_refusals_total", reason="queue_full") == 1
    executor.gate.set()
    await asyncio.gather(first, second)
    assert sample(metrics, "workbench_queue_length") == 0
    assert len(executor.requests) == 2


async def test_wait_timeout(settings: Settings, store: Store, executor: FakeClient, metrics: Metrics) -> None:
    """A job that waits longer than ``queue_wait_s`` for a slot is refused, and gives its place up."""
    executor.hold = {"open-webui/user-a"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=1, queue_wait_s=1)
    first = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, []))
    await until(lambda: executor.running == 1)
    with pytest.raises(Busy) as refused:
        await queue.submit(user("user-b"), "run_python", ARGS, [])
    assert refused.value.reason == "wait_timeout"
    assert sample(metrics, "workbench_job_refusals_total", reason="wait_timeout") == 1
    assert sample(metrics, "workbench_queue_length") == 0
    assert sample(metrics, "workbench_queue_wait_seconds_count") == 2
    assert sample(metrics, "workbench_queue_wait_seconds_sum") >= 1
    assert len(executor.requests) == 1
    executor.gate.set()
    await first
    executor.hold = set()
    await queue.submit(user("user-b"), "run_python", ARGS, [])


async def test_one_job_per_user_others_proceed(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """User A's second job waits for the first, and does not take a slot the other user needs."""
    executor.hold = {"open-webui/user-a"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=2, jobs_per_user=1)
    a1 = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, []))
    await until(lambda: executor.running == 1)
    a2 = asyncio.create_task(queue.submit(user("user-a", "chat-2"), "run_python", ARGS, []))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    async with asyncio.timeout(2):
        job, result, _ = await queue.submit(user("user-b"), "run_python", ARGS, [])
    assert result.status == "ok"
    assert job.user_id == "user-b"
    assert not a2.done()
    assert [r.user_id for r in executor.requests] == ["user-a", "user-b"]
    executor.gate.set()
    await asyncio.gather(a1, a2)
    assert [r.user_id for r in executor.requests] == ["user-a", "user-b", "user-a"]
    assert executor.peak == 2


async def test_two_jobs_per_user_when_configured(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """``jobs_per_user`` is how many of one user's jobs run at once."""
    executor.delay = 0.05
    queue = queue_for(settings, executor, store, metrics, executor_slots=4, jobs_per_user=2)
    await asyncio.gather(*(queue.submit(user("user-a"), "run_python", ARGS, []) for _ in range(5)))
    assert executor.peak == 2


async def test_the_same_user_id_of_another_caller_is_another_user(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The limit is per caller and user, as the workspaces are."""
    executor.hold = {"open-webui/user-a", "docint/user-a"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=2, jobs_per_user=1)
    one = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, []))
    other = asyncio.create_task(queue.submit(Principal("docint", "user-a", "chat-1"), "run_python", ARGS, []))
    await until(lambda: executor.running == 2)
    executor.gate.set()
    await asyncio.gather(one, other)


async def test_a_cancelled_waiter_gives_its_place_up(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A client that goes away while its job waits leaves no waiter and no held slot behind."""
    executor.hold = {"open-webui/user-a"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=1, queue_max=1)
    first = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, []))
    await until(lambda: executor.running == 1)
    waiter = asyncio.create_task(queue.submit(user("user-b"), "run_python", ARGS, []))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert sample(metrics, "workbench_queue_length") == 0
    executor.gate.set()
    await first
    executor.hold = set()
    await queue.submit(user("user-b"), "run_python", ARGS, [])
    assert len(executor.requests) == 2


async def test_a_cancelled_running_job_frees_its_slot_and_is_recorded(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A client that goes away mid-job releases its slot, and the job record does not stay ``running``."""
    executor.hold = {"open-webui/user-a"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=1)
    running = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, []))
    await until(lambda: executor.running == 1)
    job_id = executor.requests[0].job_id
    executor.staged(job_id, "left.txt").parent.mkdir(parents=True)
    executor.staged(job_id, "left.txt").write_bytes(b"x")
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert store.get_job(user("user-a"), job_id).status == "cancelled"
    assert not (settings.data_dir / "staging" / job_id).exists()
    executor.hold = set()
    await queue.submit(user("user-b"), "run_python", ARGS, [])


async def test_user_gates_do_not_accumulate(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A user that has nothing queued or running leaves nothing in the queue's memory."""
    queue = queue_for(settings, executor, store, metrics)
    await asyncio.gather(*(queue.submit(user(f"user-{n}"), "run_python", ARGS, []) for n in range(5)))
    assert queue.tracked_users == 0
    executor.status = "busy"
    with pytest.raises(Busy):
        await queue.submit(user("user-z"), "run_python", ARGS, [])
    assert queue.tracked_users == 0


async def test_one_user_cannot_fill_the_queue(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A user has ``queue_max // executor_slots`` calls waiting; the next is refused at once, theirs only."""
    executor.hold = {"open-webui/user-a", "open-webui/user-b", "open-webui/user-c"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=2, queue_max=4, jobs_per_user=1)
    a1 = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, []))
    await until(lambda: executor.running == 1)
    a_waiting = [asyncio.create_task(queue.submit(user("user-a", f"chat-{n}"), "run_python", ARGS, [])) for n in (2, 3)]
    await until(lambda: sample(metrics, "workbench_queue_length") == 2)
    b1 = asyncio.create_task(queue.submit(user("user-b"), "run_python", ARGS, []))
    await until(lambda: executor.running == 2)
    c1 = asyncio.create_task(queue.submit(user("user-c"), "run_python", ARGS, []))
    await until(lambda: sample(metrics, "workbench_queue_length") == 3)
    async with asyncio.timeout(0.5):
        with pytest.raises(Busy) as refused:
            await queue.submit(user("user-a", "chat-4"), "run_python", ARGS, [])
    assert refused.value.reason == "queue_full"
    assert sample(metrics, "workbench_job_refusals_total", reason="queue_full") == 1
    assert sample(metrics, "workbench_queue_length") == 3
    executor.gate.set()
    await asyncio.gather(a1, *a_waiting, b1, c1)
    assert len(executor.requests) == 5
    assert sample(metrics, "workbench_queue_length") == 0


async def test_the_users_share_of_the_queue_is_at_least_one(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """With more slots than queue places the share rounds up to one, and a waiter that leaves gives it back."""
    executor.hold = {"open-webui/user-a"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=4, queue_max=2, jobs_per_user=1)
    first = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, []))
    await until(lambda: executor.running == 1)
    waiter = asyncio.create_task(queue.submit(user("user-a", "chat-2"), "run_python", ARGS, []))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    async with asyncio.timeout(0.5):
        with pytest.raises(Busy) as refused:
            await queue.submit(user("user-a", "chat-3"), "run_python", ARGS, [])
    assert refused.value.reason == "queue_full"
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert sample(metrics, "workbench_queue_length") == 0
    again = asyncio.create_task(queue.submit(user("user-a", "chat-4"), "run_python", ARGS, []))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    executor.gate.set()
    await asyncio.gather(first, again)


async def test_the_default_share_is_four_waiting_calls(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """16 places over 4 slots: one running and four waiting calls of a user, and the sixth is refused."""
    executor.hold = {"open-webui/user-a"}
    queue = queue_for(settings, executor, store, metrics)
    running = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, []))
    await until(lambda: executor.running == 1)
    waiting = [asyncio.create_task(queue.submit(user("user-a", f"chat-{n}"), "run_python", ARGS, [])) for n in range(4)]
    await until(lambda: sample(metrics, "workbench_queue_length") == 4)
    with pytest.raises(Busy) as refused:
        await queue.submit(user("user-a", "chat-9"), "run_python", ARGS, [])
    assert refused.value.reason == "queue_full"
    executor.gate.set()
    await asyncio.gather(running, *waiting)


# --- the request -----------------------------------------------------------


async def test_the_request_carries_the_users_place_and_the_limits(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Caller, user and chat locate the chat folder; the limits come from the settings."""
    queue = queue_for(
        settings, executor, store, metrics, job_wall_s=30, job_memory_mb=512, upload_max_mb=7, result_max_kb=4
    )
    job, _, _ = await queue.submit(user("user-a", "chat-9"), "run_python", ARGS, ["a.xlsx", "sub/b.docx"])
    (req,) = executor.requests
    assert req.job_id == job.id
    assert JOB_ID.fullmatch(job.id)
    assert (req.tool, req.args) == ("run_python", ARGS)
    assert (req.caller, req.user_id, req.chat_id) == ("open-webui", "user-a", "chat-9")
    assert req.input_files == ["a.xlsx", "sub/b.docx"]
    assert (req.limits.wall_s, req.limits.cpu_s, req.limits.memory_mb, req.limits.file_mb) == (30, 30, 512, 7)
    assert req.max_chars == 4 * 1024


async def test_the_read_timeout_covers_the_cleanup(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The client waits the job's wall time plus the 300 s the executor's cleanup can take."""
    queue = queue_for(settings, executor, store, metrics, job_wall_s=45)
    await queue.submit(user("user-a"), "run_python", ARGS, [])
    assert executor.timeouts == [345]


async def test_the_job_is_recorded_and_finished(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A job record exists for the run and ends with the executor's status."""
    queue = queue_for(settings, executor, store, metrics)
    job, _, _ = await queue.submit(user("user-a"), "run_python", ARGS, [])
    assert (job.tool, job.status) == ("run_python", "ok")
    assert store.get_job(user("user-a"), job.id).status == "ok"
    assert sample(metrics, "workbench_job_duration_seconds_count", tool="run_python") == 1


def jobs_recorded(settings: Settings) -> int:
    """How many job records the volume's database holds."""
    db = sqlite3.connect(settings.data_dir / "workbench.db")
    try:
        return int(db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
    finally:
        db.close()


async def test_a_lone_surrogate_in_the_arguments_is_sent_as_a_replacement_character(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """UTF-8 cannot hold one, so the request could not be encoded: it goes as U+FFFD, in keys and nested values too."""
    queue = queue_for(settings, executor, store, metrics)
    args = {"values": {"a\ud800": {"value": ["b\udc00", 1]}}}
    await queue.submit(user("user-a"), "fill_template", args, [])
    (req,) = executor.requests
    assert req.args == {"values": {"a\ufffd": {"value": ["b\ufffd", 1]}}}
    assert encode(req).endswith(b"\n")


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "/workspace/chat-1/a.txt",
        "../x.txt",
        "a/../b.txt",
        "a//b.txt",
        "",
        ".",
        "a/",
        "x\x00y",
        "n" * 201,
    ],
)
async def test_an_invalid_input_path_is_refused_before_a_job_exists(
    bad: str,
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every input is a relative path of plain names; a bad one is a ``PathError``, with no record and no log line."""
    queue = queue_for(settings, executor, store, metrics)
    with caplog.at_level(logging.DEBUG), pytest.raises(PathError):
        await queue.submit(user("user-a"), "read_document", {}, ["fine.txt", bad])
    assert executor.requests == []
    assert jobs_recorded(settings) == 0
    assert [r for r in caplog.records if r.name == "workbench.api.jobs"] == []
    assert queue.tracked_users == 0


async def test_too_many_input_files_are_refused_before_a_job_exists(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The protocol takes 1000 input files; one more is refused as a bad request, not as a failed job."""
    queue = queue_for(settings, executor, store, metrics)
    with pytest.raises(PathError):
        await queue.submit(user("user-a"), "read_document", {}, [f"f{n}.txt" for n in range(1001)])
    assert jobs_recorded(settings) == 0
    await queue.submit(user("user-a"), "search_files", {}, [f"f{n}.txt" for n in range(1000)])


async def test_valid_input_paths_pass(settings: Settings, store: Store, executor: FakeClient, metrics: Metrics) -> None:
    """Plain names, folders and odd but legal characters are sent as they are."""
    queue = queue_for(settings, executor, store, metrics)
    names = ["a.txt", "reports/q 3 (final).docx", "dir/sub/b-c_d.xlsx", "ä.csv"]
    await queue.submit(user("user-a"), "read_document", {}, names)
    assert executor.requests[0].input_files == names


@pytest.mark.parametrize(
    "tool",
    [
        "read_document",
        "search_files",
        "run_python",
        "inspect_template",
        "fill_template",
        "write_table",
        "write_document",
    ],
)
async def test_the_known_tools_are_accepted(
    tool: str, settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The allow-list is the seven tools of version 1."""
    queue = queue_for(settings, executor, store, metrics)
    job, _, _ = await queue.submit(user("user-a"), tool, ARGS, [])
    assert job.tool == tool


@pytest.mark.parametrize("tool", ["", "echo", "run_python ", "READ_DOCUMENT", "tool-" + "x" * 50])
async def test_an_unknown_tool_is_a_programming_error(
    tool: str, settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A tool name that is not on the list never reaches the executor, the metrics' labels or the job table."""
    queue = queue_for(settings, executor, store, metrics)
    with pytest.raises(ValueError, match="unknown tool"):
        await queue.submit(user("user-a"), tool, ARGS, [])
    assert executor.requests == []
    assert jobs_recorded(settings) == 0
    assert sample(metrics, "workbench_job_duration_seconds_count", tool=tool) == 0
    assert queue.tracked_users == 0


# --- the size of a request --------------------------------------------------


def padded_request(size: int) -> JobRequest:
    """A valid request whose encoding, newline included, is exactly ``size`` bytes."""
    req = request_for()
    base = len(encode(req.model_copy(update={"args": {"pad": ""}})))
    return req.model_copy(update={"args": {"pad": "x" * (size - base)}})


async def test_a_request_over_the_line_limit_is_not_sent(sock_dir: Path) -> None:
    """The client refuses before it connects: with no socket, the error is the size, not an unreachable executor."""
    too_big = padded_request(MAX_LINE + 1)
    assert len(encode(too_big)) == MAX_LINE + 1
    with pytest.raises(RequestTooLarge):
        await ExecutorClient(sock_dir / "none.sock").run(too_big, timeout_s=5)
    assert not issubclass(RequestTooLarge, (Busy, ExecutorUnavailable, OSError))


async def test_a_request_of_exactly_the_line_limit_is_sent(sock_dir: Path) -> None:
    """The limit is the largest line the executor accepts, newline included."""
    seen: list[JobRequest] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        req = decode_request(await reader.readline())
        seen.append(req)
        writer.write(encode(sample_result(req.job_id)))
        await writer.drain()
        writer.close()

    path = sock_dir / "e.sock"
    biggest = padded_request(MAX_LINE)
    assert len(encode(biggest)) == MAX_LINE
    async with serve(path, handler):
        result = await ExecutorClient(path).run(biggest, timeout_s=5)
    assert result.status == "ok"
    assert seen == [biggest]


async def test_a_request_too_large_is_not_a_refusal(
    settings: Settings, store: Store, metrics: Metrics, sock_dir: Path
) -> None:
    """Through the queue it is its own error, not ``Busy``: no refusal is counted, and the job ends as an error."""
    queue = JobQueue(settings, ExecutorClient(sock_dir / "none.sock"), store, metrics)
    p = user("user-a")
    with pytest.raises(RequestTooLarge):
        await queue.submit(p, "run_python", {"code": "x" * MAX_LINE}, [])
    for reason in ("queue_full", "wait_timeout", "executor_busy"):
        assert sample(metrics, "workbench_job_refusals_total", reason=reason) == 0
    db = sqlite3.connect(settings.data_dir / "workbench.db")
    try:
        assert db.execute("SELECT status FROM jobs").fetchall() == [("error",)]
    finally:
        db.close()
    assert queue.tracked_users == 0


# --- results ---------------------------------------------------------------


def chat_files(store: Store, p: Principal) -> list[FileRecord]:
    """The files of the principal's chat."""
    return store.files_in_chat(p)


async def test_outputs_are_registered_as_files(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Each output becomes an ``output`` file of the chat, tied to its job, and its staging folder goes."""
    executor.files = {"summary.docx": b"doc", "chart.png": b"png!"}
    queue = queue_for(settings, executor, store, metrics)
    p = user("user-a")
    job, result, files = await queue.submit(p, "run_python", ARGS, [], KEEP)
    assert result.status == "ok"
    assert {(f.name, f.kind, f.job_id, f.size) for f in files} == {
        ("summary.docx", "output", job.id, 3),
        ("chart.png", "output", job.id, 4),
    }
    assert sorted(files, key=lambda f: f.name) == sorted(chat_files(store, p), key=lambda f: f.name)
    folder = Workspaces(settings.data_dir).chat_dir(p)
    assert (folder / "summary.docx").read_bytes() == b"doc"
    assert not (settings.data_dir / "staging" / job.id).exists()
    assert result.message == ""
    assert result.outputs == []


async def test_a_name_clash_gets_a_numbered_name(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A second run's output of the same name does not replace the first."""
    executor.files = {"out.txt": b"1"}
    queue = queue_for(settings, executor, store, metrics)
    _, _, first = await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP)
    _, _, second = await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP)
    assert [f.name for f in first] == ["out.txt"]
    assert [f.name for f in second] == ["out (1).txt"]


async def test_the_executors_own_note_is_kept(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """An ``ok`` answer with a message such as "dropped 1 outputs" passes it on."""
    executor.message = "dropped 1 outputs"
    queue = queue_for(settings, executor, store, metrics)
    _, result, _ = await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP)
    assert result.message == "dropped 1 outputs"


@pytest.mark.parametrize("status", ["error", "timeout", "killed"])
async def test_a_failed_job_is_returned_without_registering_outputs(
    status: str, settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """``error``, ``timeout`` and ``killed`` finish the job with that status and keep nothing it staged."""
    executor.files = {"half.txt": b"x"}
    executor.status = status
    executor.message = "the job did not finish"
    queue = queue_for(settings, executor, store, metrics)
    p = user("user-a")
    job, result, files = await queue.submit(p, "run_python", ARGS, [], KEEP)
    assert (result.status, result.message, files) == (status, "the job did not finish", [])
    assert result.outputs == []
    assert chat_files(store, p) == []
    assert store.get_job(p, job.id).status == status
    assert not (settings.data_dir / "staging" / job.id).exists()
    assert sample(metrics, "workbench_job_duration_seconds_count", tool="run_python") == 1


async def test_a_busy_executor_is_a_refusal(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """``busy`` from the executor raises ``Busy("executor_busy")`` and leaves no running record."""
    executor.status = "busy"
    executor.files = {"x.txt": b"x"}
    queue = queue_for(settings, executor, store, metrics)
    p = user("user-a")
    with pytest.raises(Busy) as refused:
        await queue.submit(p, "run_python", ARGS, [], KEEP)
    assert refused.value.reason == "executor_busy"
    assert store.get_job(p, executor.requests[0].job_id).status == "busy"
    assert chat_files(store, p) == []
    assert sample(metrics, "workbench_job_refusals_total", reason="executor_busy") == 1
    assert not (settings.data_dir / "staging").exists() or not any((settings.data_dir / "staging").iterdir())


def _failures(socket_path: Path) -> list[Exception]:
    """The ways an executor that is not there fails; the errors name the socket, as the system's do."""
    named = f"{socket_path}: synthetic failure"
    return [
        ConnectionRefusedError(named),
        FileNotFoundError(named),
        TimeoutError(named),
        ExecutorUnavailable("ConnectionRefusedError"),
    ]


@pytest.mark.parametrize("which", range(4))
async def test_an_unreachable_executor_is_busy_and_logs_no_path(
    which: int,
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No socket, a refused connection or no answer in time: ``Busy("executor_busy")``, one short log line."""
    executor.error = _failures(settings.executor_socket)[which]
    queue = queue_for(settings, executor, store, metrics)
    p = user("user-a")
    with caplog.at_level(logging.DEBUG), pytest.raises(Busy) as refused:
        await queue.submit(p, "run_python", ARGS, [], KEEP)
    assert refused.value.reason == "executor_busy"
    assert store.get_job(p, executor.requests[0].job_id).status == "busy"
    assert sample(metrics, "workbench_job_refusals_total", reason="executor_busy") == 1
    ours = [r for r in caplog.records if r.name == "workbench.api.jobs"]
    assert len(ours) == 1
    assert str(settings.executor_socket) not in caplog.text
    assert "user-a" not in caplog.text
    assert "synthetic failure" not in caplog.text


async def test_one_output_over_quota_does_not_stop_the_rest(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused output is counted in a short note; the others are kept and the staging folder is cleared."""
    executor.files = {"a.txt": b"a", "big.bin": b"b" * 5, "c.txt": b"c"}
    real = store.register_output

    def register(p: Principal, staged: Path, name: str, job_id: str, **more: Any) -> FileRecord:
        if name == "big.bin":
            raise QuotaExceeded("quota exceeded")
        return real(p, staged, name, job_id, **more)

    monkeypatch.setattr(store, "register_output", register)
    queue = queue_for(settings, executor, store, metrics)
    p = user("user-a")
    job, result, files = await queue.submit(p, "run_python", ARGS, [], KEEP)
    assert result.status == "ok"
    assert sorted(f.name for f in files) == ["a.txt", "c.txt"]
    assert result.message == "1 output could not be kept (quota)"
    assert store.get_job(p, job.id).status == "ok"
    assert not (settings.data_dir / "staging" / job.id).exists()


async def test_the_note_counts_and_joins_the_executors(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two refused outputs: "2 outputs ..."; the executor's own note stays in front."""
    executor.files = {"a.txt": b"a", "b.txt": b"b"}
    executor.message = "dropped 1 outputs"

    def register(_p: Principal, _staged: Path, _name: str, _job_id: str, **_more: Any) -> FileRecord:
        raise QuotaExceeded("quota exceeded")

    monkeypatch.setattr(store, "register_output", register)
    queue = queue_for(settings, executor, store, metrics)
    _, result, files = await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP)
    assert files == []
    assert result.message == "dropped 1 outputs; 2 outputs could not be kept (quota)"


async def test_an_output_that_is_not_in_the_jobs_staging_folder_is_not_registered(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The store trusts the path it is given, so the queue only hands it files from ``staging/<job-id>/``."""
    secret = settings.data_dir / "elsewhere.txt"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_bytes(b"not an output")

    class Lying(FakeClient):
        @override
        async def run(self, req: JobRequest, timeout_s: float) -> JobResult:
            outputs = [
                OutputFile(name="elsewhere.txt", staged_path=str(secret), size=13),
                OutputFile(name="../up.txt", staged_path=str(self.staged(req.job_id, "../up.txt")), size=1),
            ]
            return sample_result(req.job_id, outputs)

    queue = queue_for(settings, Lying(settings.data_dir), store, metrics)
    p = user("user-a")
    _, result, files = await queue.submit(p, "run_python", ARGS, [], KEEP)
    assert files == []
    assert chat_files(store, p) == []
    assert secret.read_bytes() == b"not an output"
    assert result.message == "2 outputs could not be kept (invalid)"
    assert result.outputs == []


async def test_an_answer_for_another_job_is_refused(settings: Settings, store: Store, metrics: Metrics) -> None:
    """An answer that names another job's id must never put that job's files into this user's chat."""

    class Wrong(FakeClient):
        @override
        async def run(self, req: JobRequest, timeout_s: float) -> JobResult:
            return sample_result("0" * 32)

    queue = queue_for(settings, Wrong(settings.data_dir), store, metrics)
    with pytest.raises(Busy) as refused:
        await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP)
    assert refused.value.reason == "executor_busy"


async def test_an_unexpected_failure_finishes_the_job_as_an_error(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure while the outputs are kept is not hidden, and the job record does not stay ``running``.

    A single output that cannot be registered is recorded again instead (``test_an_output_whose_registration_fails_…``),
    so the failure here is the keeper's own.
    """
    executor.files = {"a.txt": b"a"}
    queue = queue_for(settings, executor, store, metrics)
    with pytest.raises(RuntimeError):
        await queue.submit(user("user-a"), "run_python", ARGS, [], Exploding("synthetic keeper failure"))
    assert store.get_job(user("user-a"), executor.requests[0].job_id).status == "error"
    assert not (settings.data_dir / "staging" / executor.requests[0].job_id).exists()


def break_finish(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the store fail to settle a job record."""

    def explode(_job_id: str, _status: str) -> None:
        raise sqlite3.OperationalError("synthetic: database is locked")

    monkeypatch.setattr(store, "finish_job", explode)


async def test_a_failed_job_record_does_not_hide_a_busy_executor(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``Busy`` is what the caller must see, the staging folder is cleared, and the log names no path."""
    executor.status = "busy"
    executor.files = {"half.txt": b"x"}
    break_finish(store, monkeypatch)
    queue = queue_for(settings, executor, store, metrics)
    with caplog.at_level(logging.DEBUG), pytest.raises(Busy) as refused:
        await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP)
    assert refused.value.reason == "executor_busy"
    assert not (settings.data_dir / "staging" / executor.requests[0].job_id).exists()
    assert "synthetic" not in caplog.text
    assert str(settings.data_dir) not in caplog.text


async def test_a_failed_job_record_does_not_hide_the_original_error(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error that ended the job is the one raised, and the staging folder is cleared all the same."""
    executor.files = {"a.txt": b"a"}
    break_finish(store, monkeypatch)
    queue = queue_for(settings, executor, store, metrics)
    with pytest.raises(RuntimeError, match="the original failure"):
        await queue.submit(user("user-a"), "run_python", ARGS, [], Exploding("the original failure"))
    assert not (settings.data_dir / "staging" / executor.requests[0].job_id).exists()


async def test_a_failed_job_record_does_not_hide_a_cancellation(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that goes away is still cancelled, its slot is freed and its staging folder cleared."""
    executor.hold = {"open-webui/user-a"}
    break_finish(store, monkeypatch)
    queue = queue_for(settings, executor, store, metrics, executor_slots=1)
    running = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, [], KEEP))
    await until(lambda: executor.running == 1)
    job_id = executor.requests[0].job_id
    executor.staged(job_id, "left.txt").parent.mkdir(parents=True)
    executor.staged(job_id, "left.txt").write_bytes(b"x")
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert not (settings.data_dir / "staging" / job_id).exists()
    executor.hold = set()
    monkeypatch.undo()
    await queue.submit(user("user-b"), "run_python", ARGS, [], KEEP)


async def test_a_failed_job_record_after_a_good_run_is_raised_and_the_staging_is_cleared(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With nothing else pending, the store's failure is the answer; it is never swallowed silently."""
    executor.files = {"a.txt": b"a"}
    break_finish(store, monkeypatch)
    queue = queue_for(settings, executor, store, metrics)
    with pytest.raises(sqlite3.OperationalError):
        await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP)
    assert not (settings.data_dir / "staging" / executor.requests[0].job_id).exists()
    assert queue.tracked_users == 0


def _chat_folder(settings: Settings, p: Principal) -> Path:
    """Where the store keeps a chat's files: ``workspaces/<caller>/<user>/<chat>`` on the volume."""
    return settings.data_dir / "workspaces" / p.caller / p.user_id / p.chat_id


async def test_outputs_kept_before_a_failed_job_record_are_removed(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller gets the store's failure and records no event, so no file the job made may stay behind."""
    me = user("user-a")
    executor.files = {"a.txt": b"a", "b.txt": b"b"}
    break_finish(store, monkeypatch)
    queue = queue_for(settings, executor, store, metrics)
    with pytest.raises(sqlite3.OperationalError):
        await queue.submit(me, "run_python", ARGS, [], KEEP)
    assert store.files_in_chat(me) == []
    assert [path.name for path in _chat_folder(settings, me).glob("*")] == []
    assert not (settings.data_dir / "staging" / executor.requests[0].job_id).exists()


async def test_a_cancel_while_outputs_are_registered_leaves_none_of_them(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller goes away after the first output is moved in: the registration ends, and what it moved goes again.

    A cancelled caller records no event, so no file the job made may stay. The moves are not cut off half way (one
    of them may already be in the chat folder when the cancel comes); they finish and are then undone.
    """
    me = user("user-a")
    executor.files = {"a.txt": b"a", "b.txt": b"b"}
    moved = threading.Event()
    go_on = threading.Event()
    real = store.register_output

    def slow(p: Principal, staged: Path, name: str, job_id: str, **more: Any) -> FileRecord:
        record = real(p, staged, name, job_id, **more)
        if not moved.is_set():
            moved.set()
            go_on.wait(10)
        return record

    monkeypatch.setattr(store, "register_output", slow)
    queue = queue_for(settings, executor, store, metrics)
    running = asyncio.create_task(queue.submit(me, "run_python", ARGS, [], KEEP))
    await until(moved.is_set)
    running.cancel()
    await asyncio.sleep(0.05)
    go_on.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert store.files_in_chat(me) == []
    assert [path.name for path in _chat_folder(settings, me).glob("*")] == []
    assert store.get_job(me, executor.requests[0].job_id).status == "cancelled"
    assert not (settings.data_dir / "staging" / executor.requests[0].job_id).exists()
    assert queue.tracked_users == 0


@dataclasses.dataclass
class HeldMoves:
    """A registration held after its first move, until the test lets it go on.

    Attributes:
        moved: Set once the first output is in the chat folder.
        go_on: The test sets it to let the registration go on.
        ended: Each call of the store's registration that has returned or raised.
    """

    moved: threading.Event
    go_on: threading.Event
    ended: list[str]


def hold_the_first_move(store: Store, monkeypatch: pytest.MonkeyPatch) -> HeldMoves:
    """Make the store's registration stop after its first move, until the test sets ``go_on``."""
    held = HeldMoves(threading.Event(), threading.Event(), [])
    real = store.register_output

    def slow(p: Principal, staged: Path, name: str, job_id: str, **more: Any) -> FileRecord:
        try:
            record = real(p, staged, name, job_id, **more)
            if not held.moved.is_set():
                held.moved.set()
                held.go_on.wait(10)
            return record
        finally:
            held.ended.append(name)

    monkeypatch.setattr(store, "register_output", slow)
    return held


async def test_outputs_registered_while_the_loop_shuts_down_are_removed(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``asyncio.run`` cancels every task as it ends; the registration is not one, so it ends and its files go again.

    A registration that is a task of its own is cancelled with the caller: its thread goes on moving files in, and
    nothing is left to remove them.
    """
    me = user("user-a")
    executor.files = {"a.txt": b"a", "b.txt": b"b"}
    held = hold_the_first_move(store, monkeypatch)
    queue = queue_for(settings, executor, store, metrics)
    before = asyncio.all_tasks()
    running = asyncio.create_task(queue.submit(me, "run_python", ARGS, [], KEEP))
    await until(held.moved.is_set)
    for task in asyncio.all_tasks() - before:
        task.cancel()  # every task the call made, as asyncio.run's shutdown cancels every task there is
    await asyncio.sleep(0.05)
    held.go_on.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    await until(lambda: len(held.ended) == 2)
    assert store.files_in_chat(me) == []
    assert [path.name for path in _chat_folder(settings, me).glob("*")] == []
    assert store.get_job(me, executor.requests[0].job_id).status == "cancelled"


async def test_a_second_cancel_while_outputs_are_registered_still_leaves_none_of_them(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel that comes while the caller waits out the registration does not stop the wait: the files still go."""
    me = user("user-a")
    executor.files = {"a.txt": b"a", "b.txt": b"b"}
    held = hold_the_first_move(store, monkeypatch)
    queue = queue_for(settings, executor, store, metrics)
    running = asyncio.create_task(queue.submit(me, "run_python", ARGS, [], KEEP))
    await until(held.moved.is_set)
    running.cancel()
    await asyncio.sleep(0.05)
    running.cancel()
    await asyncio.sleep(0.05)
    held.go_on.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert held.ended == ["a.txt", "b.txt"]
    assert store.files_in_chat(me) == []
    assert [path.name for path in _chat_folder(settings, me).glob("*")] == []
    assert store.get_job(me, executor.requests[0].job_id).status == "cancelled"
    assert queue.tracked_users == 0


@pytest.mark.parametrize(
    "failure",
    [
        sqlite3.OperationalError("synthetic: disk I/O error"),
        RuntimeError("synthetic: an unexpected failure"),
    ],
    ids=["database", "unexpected"],
)
async def test_an_output_whose_registration_fails_in_any_way_is_recorded_again(
    failure: Exception,
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R86 N4: the middle output fails as the store does not foresee; it is named again, the others are kept.

    Its event named it ``ok`` before the move, so it is recorded again as ``error``, like an output past the quota. The
    log names the error's type, never a file.
    """
    me = user("user-a")
    executor.files = {"a.txt": b"a", "salaries-of-user-b.txt": b"b", "c.txt": b"c"}
    real = store.register_output

    def fail_the_second(p: Principal, staged: Path, name: str, job_id: str, **more: Any) -> FileRecord:
        if name.startswith("salaries"):
            raise failure
        return real(p, staged, name, job_id, **more)

    monkeypatch.setattr(store, "register_output", fail_the_second)
    keeper = KeepAll()
    queue = queue_for(settings, executor, store, metrics)
    with caplog.at_level(logging.DEBUG):
        _, result, files = await queue.submit(me, "run_python", ARGS, [], keeper)
    ((_, planned, _),) = keeper.recorded
    assert sorted(rec.name for rec in files) == ["a.txt", "c.txt"]
    assert keeper.again_ids == [[planned[1]]]
    assert result.message == "1 output could not be kept (failed)"
    assert sorted(path.name for path in _chat_folder(settings, me).glob("*")) == ["a.txt", "c.txt"]
    assert store.get_job(me, executor.requests[0].job_id).status == "ok"
    assert not (settings.data_dir / "staging" / executor.requests[0].job_id).exists()
    assert type(failure).__name__ in caplog.text
    assert "salaries" not in caplog.text


async def test_outputs_removed_again_after_a_failed_job_record_are_recorded_again(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R86 N4: the outputs the event named ``ok`` are kept, then removed when the job cannot be settled.

    They did not end as the event said, so an ``error`` event names them again, whether or not they could be removed.
    """
    me = user("user-a")
    executor.files = {"a.txt": b"a", "b.txt": b"b"}
    break_finish(store, monkeypatch)
    keeper = KeepAll()
    with pytest.raises(sqlite3.OperationalError):
        await queue_for(settings, executor, store, metrics).submit(me, "run_python", ARGS, [], keeper)
    ((_, planned, _),) = keeper.recorded
    assert keeper.again_ids == [planned]
    assert store.files_in_chat(me) == []


async def test_a_failed_removal_of_those_outputs_does_not_hide_the_store_failure(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The original error is the answer even when an output cannot be removed; the log names no file."""
    executor.files = {"a.txt": b"a"}
    break_finish(store, monkeypatch)

    def refuse(_record: FileRecord) -> None:
        raise OSError("synthetic: read-only file system")

    monkeypatch.setattr(store, "remove", refuse)
    queue = queue_for(settings, executor, store, metrics)
    with caplog.at_level(logging.DEBUG), pytest.raises(sqlite3.OperationalError):
        await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP)
    assert not (settings.data_dir / "staging" / executor.requests[0].job_id).exists()
    assert "a.txt" not in caplog.text
    assert str(settings.data_dir) not in caplog.text


# --- the client ------------------------------------------------------------


@pytest.fixture
def sock_dir() -> Iterator[Path]:
    """A short folder for a Unix socket; pytest's own temporary paths can be longer than a socket path may be."""
    folder = Path(tempfile.mkdtemp(prefix="wb", dir="/tmp"))
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def request_for(job_id: str = "a" * 32) -> JobRequest:
    """A valid synthetic request."""
    return JobRequest.model_validate(
        {
            "job_id": job_id,
            "tool": "run_python",
            "args": ARGS,
            "caller": "open-webui",
            "user_id": "user-a",
            "chat_id": "chat-1",
            "input_files": [],
            "limits": {"wall_s": 10, "cpu_s": 10, "memory_mb": 64, "file_mb": 1},
            "max_chars": 1000,
        }
    )


@contextlib.asynccontextmanager
async def serve(path: Path, handler: Any) -> AsyncIterator[None]:
    """Serve one-line requests on a Unix socket with ``handler(reader, writer)``."""
    server = await asyncio.start_unix_server(handler, path=str(path), limit=MAX_LINE)
    try:
        yield
    finally:
        server.close()
        await server.wait_closed()


async def test_the_client_sends_one_line_and_reads_one_line(sock_dir: Path) -> None:
    """The request goes out as the protocol encodes it, the answer comes back decoded."""
    seen: list[JobRequest] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        req = decode_request(await reader.readline())
        seen.append(req)
        writer.write(encode(sample_result(req.job_id, message="fine")))
        await writer.drain()
        writer.close()

    path = sock_dir / "e.sock"
    async with serve(path, handler):
        result = await ExecutorClient(path).run(request_for(), timeout_s=5)
    assert result.status == "ok"
    assert result.message == "fine"
    assert result.result == {"answer": 42}
    assert seen == [request_for()]


async def test_the_client_reads_an_answer_of_the_largest_size(sock_dir: Path) -> None:
    """The reader's buffer is the protocol's line limit, not asyncio's 64 KiB default."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        req = decode_request(await reader.readline())
        big = JobResult(job_id=req.job_id, status="ok", result={"text": "x" * (MAX_LINE - 200)}, outputs=[])
        writer.write(encode(big))
        await writer.drain()
        writer.close()

    path = sock_dir / "e.sock"
    async with serve(path, handler):
        result = await ExecutorClient(path).run(request_for(), timeout_s=5)
    assert len(result.result["text"]) == MAX_LINE - 200


async def test_a_missing_socket_is_unavailable(sock_dir: Path) -> None:
    """The executor is not running: no socket file."""
    with pytest.raises(ExecutorUnavailable):
        await ExecutorClient(sock_dir / "none.sock").run(request_for(), timeout_s=5)


async def test_a_socket_nobody_listens_on_is_unavailable(sock_dir: Path) -> None:
    """A stale socket file: the connection is refused."""
    path = sock_dir / "stale.sock"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as bound:
        bound.bind(str(path))
    assert path.exists()
    with pytest.raises(ExecutorUnavailable):
        await ExecutorClient(path).run(request_for(), timeout_s=5)


async def test_no_answer_in_time_is_unavailable(sock_dir: Path) -> None:
    """The executor takes the request and says nothing: the client gives up at its timeout."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        await reader.read()
        writer.close()

    path = sock_dir / "e.sock"
    async with serve(path, handler):
        async with asyncio.timeout(3):
            with pytest.raises(ExecutorUnavailable):
                await ExecutorClient(path).run(request_for(), timeout_s=0.2)


@pytest.mark.parametrize("reply", [b"", b"not json\n", b'{"job_id": "x"}\n', b"x" * (MAX_LINE + 10) + b"\n"])
async def test_an_empty_or_malformed_answer_is_unavailable(sock_dir: Path, reply: bytes) -> None:
    """A connection closed without an answer, or an answer that is not a result, is not trusted."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.readline()
        writer.write(reply)
        with contextlib.suppress(OSError):
            await writer.drain()
        writer.close()

    path = sock_dir / "e.sock"
    async with serve(path, handler):
        with pytest.raises(ExecutorUnavailable):
            await ExecutorClient(path).run(request_for(), timeout_s=5)


async def test_the_unavailable_message_holds_no_path(sock_dir: Path) -> None:
    """What the caller may log of a failure is a short fixed text, never the socket's path."""
    path = sock_dir / "none.sock"
    with pytest.raises(ExecutorUnavailable) as excinfo:
        await ExecutorClient(path).run(request_for(), timeout_s=5)
    assert str(sock_dir) not in str(excinfo.value)


async def test_the_real_client_works_through_the_queue(
    settings: Settings, store: Store, metrics: Metrics, sock_dir: Path
) -> None:
    """The queue over a real socket: the supervisor's side is a few lines here, the answer is registered."""
    path = sock_dir / "e.sock"

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        req = decode_request(await reader.readline())
        staged = settings.data_dir / "staging" / req.job_id / "made.txt"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(b"made")
        out = OutputFile(name="made.txt", staged_path=str(staged), size=4)
        writer.write(encode(sample_result(req.job_id, [out])))
        await writer.drain()
        writer.close()

    queue = JobQueue(settings, ExecutorClient(path), store, metrics)
    async with serve(path, handler):
        job, result, files = await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP)
    assert result.status == "ok"
    assert [(f.name, f.job_id) for f in files] == [("made.txt", job.id)]


# --- the keeper: recorded before visible (R73) --------------------------------------------------------------------


async def test_without_a_keeper_no_output_becomes_a_file(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A tool that makes no file has no keeper: whatever its job left in out/ never reaches the chat."""
    me = user("user-a")
    executor.files = {"planted.txt": b"x"}
    queue = queue_for(settings, executor, store, metrics)
    _, result, files = await queue.submit(me, "read_document", {}, [])
    assert (result.status, files) == ("ok", [])
    assert store.files_in_chat(me) == []
    assert not (settings.data_dir / "staging" / executor.requests[0].job_id).exists()


async def test_the_keeper_records_before_any_output_is_a_file_and_by_the_ids_they_get(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The keeper sees the answer and the planned ids while the chat holds none of the outputs; they keep those ids."""
    me = user("user-a")
    executor.files = {"a.txt": b"a", "b.txt": b"b"}
    keeper = KeepAll(store, me)
    _, _, files = await queue_for(settings, executor, store, metrics).submit(me, "run_python", ARGS, [], keeper)
    ((status, planned, seen),) = keeper.recorded
    assert (status, seen) == ("ok", [])
    assert sorted(planned) == sorted(rec.id for rec in files)
    assert keeper.again_ids == []


async def test_a_keeper_that_cannot_record_keeps_nothing(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The keeper's failure is what the caller gets, and no output became a file."""
    me = user("user-a")
    executor.files = {"a.txt": b"a"}

    class Broken(KeepAll):
        @override
        def record(self, result: JobResult, planned: Sequence[str]) -> Sequence[str]:
            raise AuditWriteError("synthetic audit stream failure")

    with pytest.raises(AuditWriteError):
        await queue_for(settings, executor, store, metrics).submit(me, "run_python", ARGS, [], Broken())
    assert store.files_in_chat(me) == []
    assert [path.name for path in _chat_folder(settings, me).glob("*")] == []
    assert not (settings.data_dir / "staging" / executor.requests[0].job_id).exists()


async def test_an_output_past_the_users_quota_is_not_kept_and_is_recorded_again(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Outputs count against the quota like uploads; the one that does not fit is named again, by its planned id."""
    me = user("user-a")
    store.add_upload(
        me,
        "/workspace/chat-1",
        "q.bin",
        io.BytesIO(b"x" * (MIB - 8)),
        max_bytes=MIB,
        quota_bytes=MIB,
        record=unrecorded,
    )
    executor.files = {"small.txt": b"1234", "large.txt": b"y" * 64}
    keeper = KeepAll()
    queue = queue_for(settings, executor, store, metrics, workspace_max_mb=1)
    _, result, files = await queue.submit(me, "run_python", ARGS, [], keeper)
    assert [rec.name for rec in files] == ["small.txt"]
    assert result.message == "1 output could not be kept (quota)"
    ((_, planned, _),) = keeper.recorded
    assert keeper.again_ids == [[planned[1]]]
    assert store.usage(me) <= MIB


async def test_an_output_that_stays_after_a_failed_job_record_is_recorded_again(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The output must go again and cannot: the log names the error's type, the keeper's error event the file."""
    me = user("user-a")
    executor.files = {"salaries-of-user-b.txt": b"a"}
    break_finish(store, monkeypatch)

    def refuse(_record: FileRecord) -> None:
        raise OSError("synthetic: read-only file system")

    monkeypatch.setattr(store, "remove", refuse)
    keeper = KeepAll()
    with caplog.at_level(logging.WARNING), pytest.raises(sqlite3.OperationalError):
        await queue_for(settings, executor, store, metrics).submit(me, "run_python", ARGS, [], keeper)
    (left,) = store.files_in_chat(me)
    assert keeper.again_ids == [[left.id]]
    assert "OSError" in caplog.text
    assert "salaries" not in caplog.text


async def test_a_failed_error_record_and_a_failed_removal_do_not_hide_the_audit_failure(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lost output's error record fails, and so does the removal of the kept one: the audit failure is raised.

    The removal's error never replaces it; it is logged by its type.
    """
    me = user("user-a")
    executor.files = {"fits.txt": b"1", "large.txt": b"y" * (2 * MIB)}

    class NoAgain(KeepAll):
        @override
        def again(self, ids: Sequence[str]) -> None:
            raise AuditWriteError("synthetic audit stream failure")

    def refuse(_record: FileRecord) -> None:
        raise OSError("synthetic: read-only file system")

    monkeypatch.setattr(store, "remove", refuse)
    queue = queue_for(settings, executor, store, metrics, workspace_max_mb=1)
    with caplog.at_level(logging.WARNING), pytest.raises(AuditWriteError):
        await queue.submit(me, "run_python", ARGS, [], NoAgain())
    assert "OSError" in caplog.text


# --- version 2: inline jobs, outputs taken back, a cap per caller ------------------------------------------------

#: docint's limits as the MCP door passes them: its wall cap, the MCP wait and its slots.
DOCINT = CallLimits(wall_s=60, wait_s=5, slots=2)
PASSAGES = InlineFile(name="passages.json", content='[{"author": "user-a", "text": "synthetic harbour notes"}]')
PNG = PNG_SIGNATURE + b"\x00" * 16


def docint_user(n: int) -> Principal:
    """A synthetic docint user: a pseudonym of 64 hex digits, in the default session."""
    return Principal("docint", f"{n:064x}", "default")


class Takes:
    """A taker that remembers what it was handed and what the job's staging folder held when it ran.

    Attributes:
        calls: Per call, the answer's status and the outputs taken.
        seen: Per call, the names in the job's staging folder at that moment.
        fail: Raised after remembering, if set.
    """

    def __init__(self, settings: Settings, fail: Exception | None = None) -> None:
        """Look into the staging folder of ``settings``' volume; raise ``fail``, if given."""
        self.staging = settings.data_dir / "staging"
        self.fail = fail
        self.calls: list[tuple[str, TakenOutputs]] = []
        self.seen: list[list[str]] = []

    def take(self, result: JobResult, taken: TakenOutputs) -> None:
        """Remember, then fail if told to."""
        folder = self.staging / result.job_id
        self.seen.append(sorted(p.name for p in folder.iterdir()) if folder.is_dir() else [])
        self.calls.append((result.status, taken))
        if self.fail is not None:
            raise self.fail


async def run_inline(
    queue: JobQueue, taker: Takes, p: Principal | None = None, call: CallLimits = DOCINT
) -> tuple[JobRecord, JobResult, TakenOutputs]:
    """``run_python`` on the passages for ``p``, docint's first user by default."""
    return await queue.submit_inline(p or docint_user(1), "run_python", ARGS, [PASSAGES], taker, call)


async def take_back(
    files: dict[str, bytes], settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> TakenOutputs:
    """What one inline job whose ``out/`` holds ``files`` takes back."""
    executor.files = files
    _, _, taken = await run_inline(queue_for(settings, executor, store, metrics), Takes(settings))
    return taken


def returned(taken: TakenOutputs) -> list[str]:
    """The names that come back, in order."""
    return [f.name for f in taken.files]


def skipped(taken: TakenOutputs) -> set[tuple[str, str]]:
    """The names that do not come back, each with its reason."""
    return {(s.name, s.reason) for s in taken.skipped}


def test_mcp_callers_wait_five_seconds() -> None:
    """An MCP call waits at most five seconds for its turn (§3.5)."""
    assert MCP_QUEUE_WAIT_S == 5


@pytest.mark.parametrize(
    ("wall_s", "wait_s", "slots"), [(0, 5, 2), (-1, 5, None), (60, -1, 2), (60, 5, 0), (60, 5, -2)]
)
def test_call_limits_refuse_what_no_call_can_have(wall_s: int, wait_s: int, slots: int | None) -> None:
    """A wall under a second, a negative wait or slots below one are a door's bug, refused where the limits are made."""
    with pytest.raises(ValueError, match="call limits"):
        CallLimits(wall_s, wait_s, slots)


def test_call_limits_take_the_least_a_call_can_have() -> None:
    """One second of wall, no wait, one slot or no cap of the caller's own."""
    assert (CallLimits(1, 0).slots, CallLimits(1, 0, 1).slots) == (None, 1)


@pytest.mark.parametrize(
    "inline",
    [
        [PASSAGES, PASSAGES],
        [InlineFile(name="big.txt", content="x" * (MAX_INLINE_BYTES + 1))],
        [InlineFile(name=f"p{n}.json", content="[]") for n in range(MAX_INLINE_FILES + 1)],
        [InlineFile.model_construct(name="../passages.json", content="[]")],
        [InlineFile.model_construct(name="out", content="[]")],
    ],
    ids=["repeated", "too-large", "too-many", "not-a-name", "out"],
)
async def test_inline_data_the_protocol_refuses_is_refused_before_a_turn_or_a_job_row(
    inline: list[InlineFile], settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """As ``submit`` refuses its inputs: at once, while the user's turn is taken, with no job row and nothing sent."""
    p = docint_user(1)
    executor.hold = {f"docint/{p.user_id}"}
    queue = queue_for(settings, executor, store, metrics)
    first = asyncio.create_task(run_inline(queue, Takes(settings), p))
    await until(lambda: executor.running == 1)
    taker = Takes(settings)
    async with asyncio.timeout(scaled(1)):
        with pytest.raises(ValueError):
            await queue.submit_inline(p, "run_python", ARGS, inline, taker, DOCINT)
    assert (jobs_recorded(settings), len(executor.requests), taker.calls) == (1, 1, [])
    executor.gate.set()
    await first
    assert queue.tracked_users == 0


async def test_a_capped_caller_runs_at_most_its_slots_and_leaves_room_for_others(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Four docint users, two slots of its own: two run, two wait without a global slot, and Open WebUI still runs."""
    executor.hold = {f"docint/{docint_user(n).user_id}" for n in range(4)}
    queue = queue_for(settings, executor, store, metrics)
    calls = [asyncio.create_task(run_inline(queue, Takes(settings), docint_user(n))) for n in range(4)]
    await until(lambda: executor.running == 2 and sample(metrics, "workbench_queue_length") == 2)
    async with asyncio.timeout(scaled(2)):
        _, result, _ = await queue.submit(user("user-a"), "run_python", ARGS, [])
    assert result.status == "ok"
    assert executor.running == 2
    assert [r.caller for r in executor.requests] == ["docint", "docint", "open-webui"]
    executor.gate.set()
    await asyncio.gather(*calls)
    assert len(executor.requests) == 5
    assert sample(metrics, "workbench_queue_length") == 0


async def test_the_callers_waiting_calls_are_capped_across_many_users(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Docint's users are pseudonyms each: 2 run, 8 wait (2 * 16 // 4), the 11th is refused, and places come back."""
    executor.hold = {f"docint/{docint_user(n).user_id}" for n in range(10)} | {"open-webui/user-a"}
    queue = queue_for(settings, executor, store, metrics)
    calls = [asyncio.create_task(run_inline(queue, Takes(settings), docint_user(n))) for n in range(10)]
    await until(lambda: executor.running == 2 and sample(metrics, "workbench_queue_length") == 8)
    async with asyncio.timeout(0.5):
        with pytest.raises(Busy) as refused:
            await run_inline(queue, Takes(settings), docint_user(10))
    assert refused.value.reason == "queue_full"
    assert sample(metrics, "workbench_job_refusals_total", reason="queue_full") == 1
    other = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, []))
    await until(lambda: executor.running == 3)
    executor.gate.set()
    await asyncio.gather(*calls, other)
    assert len(executor.requests) == 11
    assert sample(metrics, "workbench_queue_length") == 0
    executor.gate.clear()
    again = [asyncio.create_task(run_inline(queue, Takes(settings), docint_user(n))) for n in range(10)]
    await until(lambda: executor.running == 2 and sample(metrics, "workbench_queue_length") == 8)
    executor.gate.set()
    await asyncio.gather(*again)


async def test_a_per_call_wait_replaces_the_queue_wait(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The queue waits 60 s, the call 1 s: it is refused after its own wait, and gives the caller's slot back."""
    executor.hold = {"open-webui/user-a", "open-webui/user-b"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=2, queue_wait_s=60)
    held = [asyncio.create_task(queue.submit(user(name), "run_python", ARGS, [])) for name in ("user-a", "user-b")]
    await until(lambda: executor.running == 2)
    async with asyncio.timeout(scaled(3)):
        with pytest.raises(Busy) as refused:
            await run_inline(queue, Takes(settings), call=dataclasses.replace(DOCINT, wait_s=1))
    assert refused.value.reason == "wait_timeout"
    executor.gate.set()
    await asyncio.gather(*held)
    executor.gate.clear()
    executor.hold = {f"docint/{docint_user(n).user_id}" for n in range(2)}
    both = [asyncio.create_task(run_inline(queue, Takes(settings), docint_user(n))) for n in range(2)]
    await until(lambda: executor.running == 2)
    executor.gate.set()
    await asyncio.gather(*both)


async def test_cancelled_docint_calls_give_the_callers_places_back(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """One call cancelled while it waits, one while it runs: then both slots and all 8 waiting places serve again."""
    executor.hold = {f"docint/{docint_user(n).user_id}" for n in range(10)}
    queue = queue_for(settings, executor, store, metrics)
    calls = {n: asyncio.create_task(run_inline(queue, Takes(settings), docint_user(n))) for n in range(4)}
    await until(lambda: executor.running == 2 and sample(metrics, "workbench_queue_length") == 2)
    started = {req.user_id for req in executor.requests}
    running = next(n for n in calls if docint_user(n).user_id in started)
    waiting = next(n for n in calls if docint_user(n).user_id not in started)
    calls[waiting].cancel()
    with pytest.raises(asyncio.CancelledError):
        await calls[waiting]
    assert sample(metrics, "workbench_queue_length") == 1
    calls[running].cancel()
    with pytest.raises(asyncio.CancelledError):
        await calls[running]
    await until(lambda: executor.running == 2 and sample(metrics, "workbench_queue_length") == 0)
    executor.gate.set()
    await asyncio.gather(*(call for n, call in calls.items() if n not in {waiting, running}))
    statuses = [store.get_job(Principal("docint", r.user_id, "default"), r.job_id).status for r in executor.requests]
    assert sorted(statuses) == ["cancelled", "ok", "ok"]
    executor.gate.clear()
    again = [asyncio.create_task(run_inline(queue, Takes(settings), docint_user(n))) for n in range(10)]
    await until(lambda: executor.running == 2 and sample(metrics, "workbench_queue_length") == 8)
    async with asyncio.timeout(0.5):
        with pytest.raises(Busy) as refused:
            await run_inline(queue, Takes(settings), docint_user(10))
    assert refused.value.reason == "queue_full"
    executor.gate.set()
    await asyncio.gather(*again)
    assert queue.tracked_users == 0


async def test_a_cancel_while_the_taker_records_waits_for_it_before_staging_goes(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The caller goes away while the taker records: the taker ends with the outputs still staged, then they go."""

    class Held(Takes):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings)
            self.taking = threading.Event()
            self.go_on = threading.Event()

        @override
        def take(self, result: JobResult, taken: TakenOutputs) -> None:
            self.taking.set()
            self.go_on.wait(10)
            super().take(result, taken)

    executor.files = {"counts.csv": b"a,b\n"}
    taker = Held(settings)
    queue = queue_for(settings, executor, store, metrics)
    call = asyncio.create_task(run_inline(queue, taker))
    await until(taker.taking.is_set)
    job_id = executor.requests[0].job_id
    call.cancel()
    await asyncio.sleep(0.05)
    assert not call.done()
    assert (settings.data_dir / "staging" / job_id).is_dir()
    taker.go_on.set()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert taker.seen == [["counts.csv"]]
    assert not (settings.data_dir / "staging" / job_id).exists()
    assert store.get_job(docint_user(1), job_id).status == "cancelled"
    assert queue.tracked_users == 0


@pytest.mark.parametrize(("wall", "job_wall", "expected"), [(30, 120, 30), (200, 120, 120)])
async def test_the_wall_is_the_least_of_the_call_and_the_setting(
    wall: int, job_wall: int, expected: int, settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The job gets the smaller wall as its wall and CPU time; the client waits that plus the cleanup's 300 s."""
    queue = queue_for(settings, executor, store, metrics, job_wall_s=job_wall)
    await run_inline(queue, Takes(settings), call=dataclasses.replace(DOCINT, wall_s=wall))
    (req,) = executor.requests
    assert req.limits.wall_s == req.limits.cpu_s == expected
    assert executor.timeouts[-1] == expected + 300


async def test_a_workspace_call_with_limits_takes_them_too(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """``submit`` with ``call`` applies its wall, its wait and its slots, as ``submit_inline`` does."""
    executor.hold = {"docint/user-a", "docint/user-b"}
    queue = queue_for(settings, executor, store, metrics, job_wall_s=120)
    limits = CallLimits(wall_s=30, wait_s=1, slots=1)
    first = asyncio.create_task(
        queue.submit(Principal("docint", "user-a", "chat-1"), "run_python", ARGS, [], call=limits)
    )
    await until(lambda: executor.running == 1)
    async with asyncio.timeout(scaled(3)):
        with pytest.raises(Busy) as refused:
            await queue.submit(Principal("docint", "user-b", "chat-1"), "run_python", ARGS, [], call=limits)
    assert refused.value.reason == "wait_timeout"
    executor.gate.set()
    await first
    assert (executor.requests[0].limits.wall_s, executor.timeouts[0]) == (30, 330)


async def test_inline_inputs_reach_the_request_and_nothing_the_workspace(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The data travels in the request; no input file is named, no folder made below ``workspaces/``, no file kept."""
    executor.files = {"counts.csv": b"author,posts\nuser-a,12\n"}
    p = docint_user(1)
    job, result, taken = await run_inline(queue_for(settings, executor, store, metrics), Takes(settings), p)
    (req,) = executor.requests
    assert (req.inline_inputs, req.input_files, req.caller) == ([PASSAGES], [], "docint")
    assert (job.caller, job.user_id, job.status, result.status, result.outputs) == ("docint", p.user_id, "ok", "ok", [])
    assert returned(taken) == ["counts.csv"]
    assert not (settings.data_dir / "workspaces" / "docint").exists()
    assert store.files_in_chat(p) == []


async def test_outputs_come_back_png_then_csv_then_the_rest_by_name(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Only the five types come back, charts first, then tables, then the rest; any other type is skipped."""
    names = ["z.txt", "b.csv", "a.png", "c.json", "n.md", "x.xlsx", "y.html"]
    taken = await take_back(
        {name: PNG if name.endswith(".png") else b"x" for name in names}, settings, store, executor, metrics
    )
    assert returned(taken) == ["a.png", "b.csv", "c.json", "n.md", "z.txt"]
    assert skipped(taken) == {("x.xlsx", "type"), ("y.html", "type")}
    assert [f.mime for f in taken.files] == [RETURN_TYPES[Path(f.name).suffix] for f in taken.files]
    assert [f.data for f in taken.files] == [PNG, b"x", b"x", b"x", b"x"]


async def test_upper_case_suffixes_come_back(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A model writes ``Chart.PNG`` as often as ``chart.png``: the suffix counts lower-cased, the name stays as is."""
    taken = await take_back({"Chart.PNG": PNG, "Counts.CSV": b"a,b\n"}, settings, store, executor, metrics)
    assert [(f.name, f.mime) for f in taken.files] == [("Chart.PNG", "image/png"), ("Counts.CSV", "text/csv")]
    assert taken.skipped == ()


async def test_a_png_needs_its_signature_and_text_must_be_strict_utf8(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A GIF named ``.png`` and a Latin-1 table are skipped; a UTF-8 table with a byte-order mark comes back as is."""
    files = {
        "fake.png": b"GIF89a\x01\x00\x01\x00",
        "t.csv": "Müller\n".encode("latin-1"),
        "bom.csv": b"\xef\xbb\xbfa,b\n",
    }
    taken = await take_back(files, settings, store, executor, metrics)
    assert [(f.name, f.data) for f in taken.files] == [("bom.csv", b"\xef\xbb\xbfa,b\n")]
    assert skipped(taken) == {("fake.png", "signature"), ("t.csv", "encoding")}


@pytest.mark.parametrize("char", ["\x00", "\x01", "\x08", "\x0b", "\x0c", "\x1b", "\x1f", "\x7f"])
async def test_a_text_with_a_control_character_is_skipped_as_encoding(
    char: str, settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A C0 control character other than tab, LF and CR, or DEL, keeps a text back: its JSON escape would grow it."""
    taken = await take_back({"t.csv": f"a,b\n1,x{char}y\n".encode()}, settings, store, executor, metrics)
    assert taken.files == ()
    assert skipped(taken) == {("t.csv", "encoding")}


async def test_a_text_with_tabs_and_line_ends_comes_back(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Tab, LF and CR are text, and so are the C1 controls and any other UTF-8: they come back as written."""
    files = {"t.csv": b"a\tb\r\n1\t2\r\n", "u.txt": "caf\u00e9\u0085\u2028\n".encode()}
    taken = await take_back(files, settings, store, executor, metrics)
    assert [(f.name, f.data) for f in taken.files] == [(n, d) for n, d in files.items()]
    assert taken.skipped == ()


async def test_the_return_caps(settings: Settings, store: Store, executor: FakeClient, metrics: Metrics) -> None:
    """At most 8 files, an image up to 512 KiB, a text up to 256 KiB, 1 MiB in all; past the total the next is tried."""
    files = {f"{c}.csv": b"a,b\n" for c in "abcdefghi"} | {"0.csv": "Müller\n".encode("latin-1"), "x.xlsx": b"x"}
    many = await take_back(files, settings, store, executor, metrics)
    assert returned(many) == [f"{c}.csv" for c in "abcdefgh"]
    assert skipped(many) == {("0.csv", "encoding"), ("i.csv", "count"), ("x.xlsx", "type")}

    big = PNG_SIGNATURE + b"\x00" * (IMAGE_MAX + 1 - len(PNG_SIGNATURE))
    large = await take_back({"big.png": big, "long.txt": b"x" * (TEXT_MAX + 1)}, settings, store, executor, metrics)
    assert large.files == ()
    assert {(s.name, s.size, s.reason) for s in large.skipped} == {
        ("big.png", IMAGE_MAX + 1, "size"),
        ("long.txt", TEXT_MAX + 1, "size"),
    }

    full = PNG_SIGNATURE + b"\x00" * (IMAGE_MAX - len(PNG_SIGNATURE))
    total = await take_back({"a.png": full, "b.png": full, "c.csv": b"a,b\n"}, settings, store, executor, metrics)
    assert returned(total) == ["a.png", "b.png"]
    assert sum(len(f.data) for f in total.files) == RETURN_MAX
    assert skipped(total) == {("c.csv", "total")}

    part = PNG_SIGNATURE + b"\x00" * (400 * 1024)
    files = {"a.png": full, "b.png": part, "c.csv": b"x" * (200 * 1024), "d.json": b"[]"}
    tried = await take_back(files, settings, store, executor, metrics)
    assert returned(tried) == ["a.png", "b.png", "d.json"]
    assert skipped(tried) == {("c.csv", "total")}


async def test_only_the_jobs_own_staged_regular_files_are_read(
    settings: Settings, store: Store, metrics: Metrics
) -> None:
    """A path outside ``staging/<job-id>/``, a symlink, a folder and a FIFO in a staged file's place are not read.

    ``../elsewhere.csv`` names a regular file next to the job's staging folder, so only the name check refuses it.
    """
    secret = settings.data_dir / "elsewhere.csv"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_bytes(b"not an output\n")
    beside = settings.data_dir / "staging" / "elsewhere.csv"
    beside.parent.mkdir(parents=True)
    beside.write_bytes(b"not an output either\n")

    class Planted(FakeClient):
        @override
        async def run(self, req: JobRequest, timeout_s: float) -> JobResult:
            self.requests.append(req)
            folder = self.staged(req.job_id, "")
            folder.mkdir(parents=True)
            (folder / "own.csv").write_bytes(b"a,b\n")
            (folder / "link.csv").symlink_to(secret)
            (folder / "folder.csv").mkdir()
            os.mkfifo(folder / "fifo.csv")
            outputs = [
                OutputFile(name=name, staged_path=str(folder / name), size=4)
                for name in ("own.csv", "link.csv", "folder.csv", "fifo.csv")
            ]
            outputs += [
                OutputFile(name="elsewhere.csv", staged_path=str(secret), size=14),
                OutputFile(name="../elsewhere.csv", staged_path=str(folder / "../elsewhere.csv"), size=14),
            ]
            return sample_result(req.job_id, outputs)

    queue = queue_for(settings, Planted(settings.data_dir), store, metrics)
    async with asyncio.timeout(scaled(5)):
        _, _, taken = await run_inline(queue, Takes(settings))
    assert [(f.name, f.data) for f in taken.files] == [("own.csv", b"a,b\n")]
    assert skipped(taken) == {
        (name, "unreadable") for name in ("link.csv", "folder.csv", "fifo.csv", "elsewhere.csv", "../elsewhere.csv")
    }
    assert secret.read_bytes() == b"not an output\n"
    assert beside.read_bytes() == b"not an output either\n"


async def test_the_taker_records_before_anything_returns_and_its_failure_sends_nothing(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The taker runs while the outputs are still staged; when it cannot record, its error is the answer."""
    executor.files = {"counts.csv": b"a,b\n", "chart.png": PNG}
    taker = Takes(settings, fail=AuditWriteError("x"))
    queue = queue_for(settings, executor, store, metrics)
    with pytest.raises(AuditWriteError):
        await run_inline(queue, taker)
    job_id = executor.requests[0].job_id
    assert taker.seen == [["chart.png", "counts.csv"]]
    ((status, taken),) = taker.calls
    assert (status, returned(taken)) == ("ok", ["chart.png", "counts.csv"])
    assert not (settings.data_dir / "staging" / job_id).exists()
    assert store.get_job(docint_user(1), job_id).status == "error"
    assert queue.tracked_users == 0


@pytest.mark.parametrize("ending", ["ok", "error", "cancelled"])
async def test_staging_is_removed_on_success_error_and_cancellation(
    ending: str, settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """However the call ends, nothing of the job's outputs stays, and its record says how it ended."""
    p = docint_user(1)
    executor.files = {"counts.csv": b"a,b\n"}
    if ending == "error":
        executor.status = "error"
    if ending == "cancelled":
        executor.hold = {f"docint/{p.user_id}"}
    queue = queue_for(settings, executor, store, metrics)
    call = asyncio.create_task(run_inline(queue, Takes(settings), p))
    await until(lambda: len(executor.requests) == 1)
    job_id = executor.requests[0].job_id
    if ending == "cancelled":
        executor.staged(job_id, "left.csv").parent.mkdir(parents=True)
        executor.staged(job_id, "left.csv").write_bytes(b"x")
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
    else:
        await call
    assert not (settings.data_dir / "staging" / job_id).exists()
    assert store.get_job(p, job_id).status == ending
    assert queue.tracked_users == 0


async def test_a_failed_job_takes_no_output_but_is_still_handed_to_the_taker(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A job that timed out is recorded like any other the executor answered; what it staged does not come back."""
    executor.files = {"counts.csv": b"a,b\n"}
    executor.status = "timeout"
    taker = Takes(settings)
    _, result, taken = await run_inline(queue_for(settings, executor, store, metrics), taker)
    assert (result.status, taken) == ("timeout", TakenOutputs())
    assert taker.calls == [("timeout", TakenOutputs())]
    assert not (settings.data_dir / "staging" / executor.requests[0].job_id).exists()


async def test_a_refused_call_is_not_handed_to_the_taker(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """An executor with no free slot answered no job: ``Busy``, and nothing for the taker to record."""
    executor.status = "busy"
    taker = Takes(settings)
    with pytest.raises(Busy) as refused:
        await run_inline(queue_for(settings, executor, store, metrics), taker)
    assert refused.value.reason == "executor_busy"
    assert taker.calls == []


async def test_inline_data_past_the_line_is_too_large_and_not_sent(
    settings: Settings, store: Store, metrics: Metrics, sock_dir: Path
) -> None:
    """512 KiB of quotes passes the protocol's bound but doubles on the line: the client's own error, nothing taken."""
    quotes = InlineFile(name="passages.json", content='"' * MAX_INLINE_BYTES)
    queue = JobQueue(settings, ExecutorClient(sock_dir / "none.sock"), store, metrics)
    taker = Takes(settings)
    with pytest.raises(RequestTooLarge):
        await queue.submit_inline(docint_user(1), "run_python", ARGS, [quotes], taker, DOCINT)
    assert taker.calls == []
    assert sample(metrics, "workbench_job_refusals_total", reason="executor_busy") == 0
    assert queue.tracked_users == 0
    (job_id,) = store.due_jobs(float("inf"))
    record = store.find_job(job_id)
    assert record is not None and record.status == "error"


async def test_open_webui_is_never_capped(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Docint's gate holds docint alone: four Open WebUI users run at once while docint's calls wait for a slot."""
    queue = queue_for(settings, executor, store, metrics)
    await run_inline(queue, Takes(settings))
    executor.hold = {f"open-webui/user-{n}" for n in range(4)}
    others = [asyncio.create_task(queue.submit(user(f"user-{n}"), "run_python", ARGS, [])) for n in range(4)]
    await until(lambda: executor.running == 4)
    waiting = [asyncio.create_task(run_inline(queue, Takes(settings), docint_user(n))) for n in (2, 3)]
    await until(lambda: sample(metrics, "workbench_queue_length") == 2)
    executor.gate.set()
    await asyncio.gather(*others, *waiting)
    assert executor.peak == 4
    assert len(executor.requests) == 7
