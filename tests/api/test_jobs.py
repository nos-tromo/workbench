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

from tests.api.conftest import CALL, PG_SETUP, put, unrecorded
from tests.api.test_store import keep_artifact
from tests.conftest import scaled
from workbench.api import tools_mcp
from workbench.api.jobs import (
    IMAGE_MAX,
    MCP_QUEUE_WAIT_S,
    PNG_SIGNATURE,
    PREPARE_QUEUE_WAIT_S,
    PREPARE_WAITING_MAX,
    RETURN_MAX,
    RETURN_TYPES,
    SOURCE_LOCK_WAIT_S,
    TEXT_MAX,
    Busy,
    CallLimits,
    ExecutorClient,
    ExecutorUnavailable,
    Gate,
    JobQueue,
    Keeper,
    RequestTooLarge,
    TakenOutputs,
    Turn,
)
from workbench.api.metrics import Metrics
from workbench.api.store import QuotaExceeded, Store
from workbench.api.tools_mcp import CallJobs
from workbench.config import Settings
from workbench.models import DerivedRecord, FileRecord, JobRecord, Principal
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
    TablePreview,
    decode_request,
    encode,
)
from workbench.renders import (
    CONVERTED_MAX_BYTES,
    CONVERTED_MIME,
    CONVERTED_NAME,
    CONVERTED_SIGNATURE,
    RENDER_MAX_BYTES,
    SIGNATURES,
    TRANSCRIPT_MAX_BYTES,
    TRANSCRIPT_NAME,
    render_name,
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
    """A synthetic user of Ghostwriter's, called with no limits of its caller's."""
    return Principal("ghostwriter", name, chat)


def sample_result(
    job_id: str,
    outputs: Sequence[OutputFile] = (),
    *,
    status: str = "ok",
    message: str = "",
    result: dict[str, Any] | None = None,
) -> JobResult:
    """An executor answer for ``job_id``; its runner's answer is ``result``, or ``{"answer": 42}``."""
    return JobResult.model_validate(
        {
            "job_id": job_id,
            "status": status,
            "result": {"answer": 42} if result is None else result,
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
        previews: A preview, as the arguments of ``TablePreview``, by the name of the output that has it; a job that
            asked for previews (``req.previews``) gets each staged output's.
        answer: The runner's answer it gives, such as a render answer; ``None`` for ``{"answer": 42}``.
        scripts: The answer and the files to stage of a job of one tool, by the tool's name, in place of ``answer``
            and ``files``: a conversion and a render job of one turn each get their own.
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
        self.previews: dict[str, dict[str, Any]] = {}
        self.status = "ok"
        self.message = ""
        self.error: Exception | None = None
        self.answer: dict[str, Any] | None = None
        self.scripts: dict[str, tuple[dict[str, Any], dict[str, bytes]]] = {}

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
            answer, files = self.scripts.get(req.tool, (self.answer, self.files))
            outputs = []
            for name, data in files.items():
                path = self.staged(req.job_id, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                preview = TablePreview(**self.previews[name]) if req.previews and name in self.previews else None
                outputs.append(OutputFile(name=name, staged_path=str(path), size=len(data), preview=preview))
            return sample_result(req.job_id, outputs, status=self.status, message=self.message, result=answer)
        finally:
            self.running -= 1


class KeepAll:
    """A keeper that keeps every output, as a tool that makes files does, and remembers what it was asked.

    Attributes:
        recorded: Per answer recorded: its status, the ids planned, and the names of the chat's files at that moment.
        results: Every answer recorded, as the executor gave it.
        again: Every list of ids recorded again as errors.
        store: Where it looks, at the moment it records, for files of the chat.
        p: Whose chat it looks into.
    """

    def __init__(self, store: Store | None = None, p: Principal | None = None) -> None:
        """Look into ``p``'s chat in ``store`` when recording, if both are given."""
        self.recorded: list[tuple[str, list[str], list[str]]] = []
        self.results: list[JobResult] = []
        self.again_ids: list[list[str]] = []
        self.store = store
        self.p = p

    def record(self, result: JobResult, planned: Sequence[str]) -> Sequence[str]:
        """Keep every output."""
        seen = [] if self.store is None or self.p is None else [r.name for r in self.store.files_in_chat(self.p)]
        self.recorded.append((result.status, list(planned), seen))
        self.results.append(result)
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
    done = await asyncio.gather(
        *(queue.submit(user(f"user-{n}"), "run_python", ARGS, [], call=CALL) for n in range(10))
    )
    assert executor.peak == 4
    assert len(executor.requests) == 10
    assert {result.status for _, result, _ in done} == {"ok"}
    assert sample(metrics, "workbench_queue_length") == 0


async def test_full_queue_refuses_at_once(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """One slot and room for one waiter: the third call is refused without waiting."""
    executor.hold = {"ghostwriter/user-a", "ghostwriter/user-b", "ghostwriter/user-c"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=1, queue_max=1)
    first = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, [], call=CALL))
    await until(lambda: executor.running == 1)
    second = asyncio.create_task(queue.submit(user("user-b"), "run_python", ARGS, [], call=CALL))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    async with asyncio.timeout(0.5):
        with pytest.raises(Busy) as refused:
            await queue.submit(user("user-c"), "run_python", ARGS, [], call=CALL)
    assert refused.value.reason == "queue_full"
    assert sample(metrics, "workbench_job_refusals_total", reason="queue_full") == 1
    executor.gate.set()
    await asyncio.gather(first, second)
    assert sample(metrics, "workbench_queue_length") == 0
    assert len(executor.requests) == 2


async def test_wait_timeout(settings: Settings, store: Store, executor: FakeClient, metrics: Metrics) -> None:
    """A job that waits longer than its call's wait for a slot is refused, and gives its place up."""
    executor.hold = {"ghostwriter/user-a"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=1)
    first = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, [], call=CALL))
    await until(lambda: executor.running == 1)
    with pytest.raises(Busy) as refused:
        await queue.submit(user("user-b"), "run_python", ARGS, [], call=dataclasses.replace(CALL, wait_s=1))
    assert refused.value.reason == "wait_timeout"
    assert sample(metrics, "workbench_job_refusals_total", reason="wait_timeout") == 1
    assert sample(metrics, "workbench_queue_length") == 0
    assert sample(metrics, "workbench_queue_wait_seconds_count") == 2
    assert sample(metrics, "workbench_queue_wait_seconds_sum") >= 1
    assert len(executor.requests) == 1
    executor.gate.set()
    await first
    executor.hold = set()
    await queue.submit(user("user-b"), "run_python", ARGS, [], call=CALL)


async def test_one_job_per_user_others_proceed(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """User A's second job waits for the first, and does not take a slot the other user needs."""
    executor.hold = {"ghostwriter/user-a"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=2, jobs_per_user=1)
    a1 = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, [], call=CALL))
    await until(lambda: executor.running == 1)
    a2 = asyncio.create_task(queue.submit(user("user-a", "chat-2"), "run_python", ARGS, [], call=CALL))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    async with asyncio.timeout(2):
        job, result, _ = await queue.submit(user("user-b"), "run_python", ARGS, [], call=CALL)
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
    await asyncio.gather(*(queue.submit(user("user-a"), "run_python", ARGS, [], call=CALL) for _ in range(5)))
    assert executor.peak == 2


async def test_the_same_user_id_of_another_caller_is_another_user(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The limit is per caller and user, as the workspaces are."""
    executor.hold = {"ghostwriter/user-a", "docint/user-a"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=2, jobs_per_user=1)
    one = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, [], call=CALL))
    other = asyncio.create_task(
        queue.submit(Principal("docint", "user-a", "chat-1"), "run_python", ARGS, [], call=CALL)
    )
    await until(lambda: executor.running == 2)
    executor.gate.set()
    await asyncio.gather(one, other)


async def test_a_cancelled_waiter_gives_its_place_up(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A client that goes away while its job waits leaves no waiter and no held slot behind."""
    executor.hold = {"ghostwriter/user-a"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=1, queue_max=1)
    first = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, [], call=CALL))
    await until(lambda: executor.running == 1)
    waiter = asyncio.create_task(queue.submit(user("user-b"), "run_python", ARGS, [], call=CALL))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert sample(metrics, "workbench_queue_length") == 0
    executor.gate.set()
    await first
    executor.hold = set()
    await queue.submit(user("user-b"), "run_python", ARGS, [], call=CALL)
    assert len(executor.requests) == 2


async def test_a_cancelled_running_job_frees_its_slot_and_is_recorded(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A client that goes away mid-job releases its slot, and the job record does not stay ``running``."""
    executor.hold = {"ghostwriter/user-a"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=1)
    running = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, [], call=CALL))
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
    await queue.submit(user("user-b"), "run_python", ARGS, [], call=CALL)


async def test_user_gates_do_not_accumulate(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A user that has nothing queued or running leaves nothing in the queue's memory."""
    queue = queue_for(settings, executor, store, metrics)
    await asyncio.gather(*(queue.submit(user(f"user-{n}"), "run_python", ARGS, [], call=CALL) for n in range(5)))
    assert queue.tracked_users == 0
    executor.status = "busy"
    with pytest.raises(Busy):
        await queue.submit(user("user-z"), "run_python", ARGS, [], call=CALL)
    assert queue.tracked_users == 0


async def test_one_user_cannot_fill_the_queue(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A user has ``queue_max // executor_slots`` calls waiting; the next is refused at once, theirs only."""
    executor.hold = {"ghostwriter/user-a", "ghostwriter/user-b", "ghostwriter/user-c"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=2, queue_max=4, jobs_per_user=1)
    a1 = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, [], call=CALL))
    await until(lambda: executor.running == 1)
    a_waiting = [
        asyncio.create_task(queue.submit(user("user-a", f"chat-{n}"), "run_python", ARGS, [], call=CALL))
        for n in (2, 3)
    ]
    await until(lambda: sample(metrics, "workbench_queue_length") == 2)
    b1 = asyncio.create_task(queue.submit(user("user-b"), "run_python", ARGS, [], call=CALL))
    await until(lambda: executor.running == 2)
    c1 = asyncio.create_task(queue.submit(user("user-c"), "run_python", ARGS, [], call=CALL))
    await until(lambda: sample(metrics, "workbench_queue_length") == 3)
    async with asyncio.timeout(0.5):
        with pytest.raises(Busy) as refused:
            await queue.submit(user("user-a", "chat-4"), "run_python", ARGS, [], call=CALL)
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
    executor.hold = {"ghostwriter/user-a"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=4, queue_max=2, jobs_per_user=1)
    first = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, [], call=CALL))
    await until(lambda: executor.running == 1)
    waiter = asyncio.create_task(queue.submit(user("user-a", "chat-2"), "run_python", ARGS, [], call=CALL))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    async with asyncio.timeout(0.5):
        with pytest.raises(Busy) as refused:
            await queue.submit(user("user-a", "chat-3"), "run_python", ARGS, [], call=CALL)
    assert refused.value.reason == "queue_full"
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert sample(metrics, "workbench_queue_length") == 0
    again = asyncio.create_task(queue.submit(user("user-a", "chat-4"), "run_python", ARGS, [], call=CALL))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    executor.gate.set()
    await asyncio.gather(first, again)


async def test_the_default_share_is_four_waiting_calls(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """16 places over 4 slots: one running and four waiting calls of a user, and the sixth is refused."""
    executor.hold = {"ghostwriter/user-a"}
    queue = queue_for(settings, executor, store, metrics)
    running = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, [], call=CALL))
    await until(lambda: executor.running == 1)
    waiting = [
        asyncio.create_task(queue.submit(user("user-a", f"chat-{n}"), "run_python", ARGS, [], call=CALL))
        for n in range(4)
    ]
    await until(lambda: sample(metrics, "workbench_queue_length") == 4)
    with pytest.raises(Busy) as refused:
        await queue.submit(user("user-a", "chat-9"), "run_python", ARGS, [], call=CALL)
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
    job, _, _ = await queue.submit(user("user-a", "chat-9"), "run_python", ARGS, ["a.xlsx", "sub/b.docx"], call=CALL)
    (req,) = executor.requests
    assert req.job_id == job.id
    assert JOB_ID.fullmatch(job.id)
    assert (req.tool, req.args) == ("run_python", ARGS)
    assert (req.caller, req.user_id, req.chat_id) == ("ghostwriter", "user-a", "chat-9")
    assert req.input_files == ["a.xlsx", "sub/b.docx"]
    assert (req.limits.wall_s, req.limits.cpu_s, req.limits.memory_mb, req.limits.file_mb) == (30, 30, 512, 7)
    assert req.max_chars == 4 * 1024


async def test_the_read_timeout_covers_the_cleanup(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The client waits the job's wall time plus the 300 s the executor's cleanup can take."""
    queue = queue_for(settings, executor, store, metrics, job_wall_s=45)
    await queue.submit(user("user-a"), "run_python", ARGS, [], call=CALL)
    assert executor.timeouts == [345]


async def test_the_job_is_recorded_and_finished(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A job record exists for the run and ends with the executor's status."""
    queue = queue_for(settings, executor, store, metrics)
    job, _, _ = await queue.submit(user("user-a"), "run_python", ARGS, [], call=CALL)
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
    await queue.submit(user("user-a"), "fill_template", args, [], call=CALL)
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
        await queue.submit(user("user-a"), "read_document", {}, ["fine.txt", bad], call=CALL)
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
        await queue.submit(user("user-a"), "read_document", {}, [f"f{n}.txt" for n in range(1001)], call=CALL)
    assert jobs_recorded(settings) == 0
    await queue.submit(user("user-a"), "search_files", {}, [f"f{n}.txt" for n in range(1000)], call=CALL)


async def test_valid_input_paths_pass(settings: Settings, store: Store, executor: FakeClient, metrics: Metrics) -> None:
    """Plain names, folders and odd but legal characters are sent as they are."""
    queue = queue_for(settings, executor, store, metrics)
    names = ["a.txt", "reports/q 3 (final).docx", "dir/sub/b-c_d.xlsx", "ä.csv"]
    await queue.submit(user("user-a"), "read_document", {}, names, call=CALL)
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
        "write_presentation",
    ],
)
async def test_the_known_tools_are_accepted(
    tool: str, settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The allow-list is the tools that run in the executor."""
    queue = queue_for(settings, executor, store, metrics)
    job, _, _ = await queue.submit(user("user-a"), tool, ARGS, [], call=CALL)
    assert job.tool == tool


@pytest.mark.parametrize("tool", ["", "echo", "run_python ", "READ_DOCUMENT", "tool-" + "x" * 50])
async def test_an_unknown_tool_is_a_programming_error(
    tool: str, settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A tool name that is not on the list never reaches the executor, the metrics' labels or the job table."""
    queue = queue_for(settings, executor, store, metrics)
    with pytest.raises(ValueError, match="unknown tool"):
        await queue.submit(user("user-a"), tool, ARGS, [], call=CALL)
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
        await queue.submit(p, "run_python", {"code": "x" * MAX_LINE}, [], call=CALL)
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
    job, result, files = await queue.submit(p, "run_python", ARGS, [], KEEP, call=CALL)
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
    _, _, first = await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP, call=CALL)
    _, _, second = await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP, call=CALL)
    assert [f.name for f in first] == ["out.txt"]
    assert [f.name for f in second] == ["out (1).txt"]


async def test_the_executors_own_note_is_kept(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """An ``ok`` answer with a message such as "dropped 1 outputs" passes it on."""
    executor.message = "dropped 1 outputs"
    queue = queue_for(settings, executor, store, metrics)
    _, result, _ = await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP, call=CALL)
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
    job, result, files = await queue.submit(p, "run_python", ARGS, [], KEEP, call=CALL)
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
        await queue.submit(p, "run_python", ARGS, [], KEEP, call=CALL)
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
        await queue.submit(p, "run_python", ARGS, [], KEEP, call=CALL)
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
    job, result, files = await queue.submit(p, "run_python", ARGS, [], KEEP, call=CALL)
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
    _, result, files = await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP, call=CALL)
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
    _, result, files = await queue.submit(p, "run_python", ARGS, [], KEEP, call=CALL)
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
        await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP, call=CALL)
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
        await queue.submit(user("user-a"), "run_python", ARGS, [], Exploding("synthetic keeper failure"), call=CALL)
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
        await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP, call=CALL)
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
        await queue.submit(user("user-a"), "run_python", ARGS, [], Exploding("the original failure"), call=CALL)
    assert not (settings.data_dir / "staging" / executor.requests[0].job_id).exists()


async def test_a_failed_job_record_does_not_hide_a_cancellation(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that goes away is still cancelled, its slot is freed and its staging folder cleared."""
    executor.hold = {"ghostwriter/user-a"}
    break_finish(store, monkeypatch)
    queue = queue_for(settings, executor, store, metrics, executor_slots=1)
    running = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, [], KEEP, call=CALL))
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
    await queue.submit(user("user-b"), "run_python", ARGS, [], KEEP, call=CALL)


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
        await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP, call=CALL)
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
        await queue.submit(me, "run_python", ARGS, [], KEEP, call=CALL)
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
    running = asyncio.create_task(queue.submit(me, "run_python", ARGS, [], KEEP, call=CALL))
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
    running = asyncio.create_task(queue.submit(me, "run_python", ARGS, [], KEEP, call=CALL))
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
    running = asyncio.create_task(queue.submit(me, "run_python", ARGS, [], KEEP, call=CALL))
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
        _, result, files = await queue.submit(me, "run_python", ARGS, [], keeper, call=CALL)
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
        await queue_for(settings, executor, store, metrics).submit(me, "run_python", ARGS, [], keeper, call=CALL)
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
        await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP, call=CALL)
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
            "caller": "ghostwriter",
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
        job, result, files = await queue.submit(user("user-a"), "run_python", ARGS, [], KEEP, call=CALL)
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
    _, result, files = await queue.submit(me, "read_document", {}, [], call=CALL)
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
    _, _, files = await queue_for(settings, executor, store, metrics).submit(
        me, "run_python", ARGS, [], keeper, call=CALL
    )
    ((status, planned, seen),) = keeper.recorded
    assert (status, seen) == ("ok", [])
    assert sorted(planned) == sorted(rec.id for rec in files)
    assert keeper.again_ids == []


COUNTS_PREVIEW: dict[str, Any] = {
    "columns": ["author", "posts"],
    "rows": [["user-a", "12"], ["user-b", "7"]],
    "row_count": 2,
    "truncated": False,
}
TOTALS_PREVIEW: dict[str, Any] = {"columns": ["total"], "rows": [["19"]], "row_count": 1, "truncated": False}


async def test_the_keeper_sees_each_outputs_preview(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The keeper records the answer with each output's preview, in the order of the ids it plans."""
    me = user("user-a")
    executor.files = {"counts.csv": b"x", "notes.txt": b"x", "totals.csv": b"x"}
    executor.previews = {"counts.csv": COUNTS_PREVIEW, "totals.csv": TOTALS_PREVIEW}
    queue = queue_for(settings, executor, store, metrics)
    keeper = KeepAll()
    _, _, files = await queue.submit(me, "run_python", ARGS, [], keeper, call=CALL, previews=True)
    (result,) = keeper.results
    (_, planned, _) = keeper.recorded[0]
    assert [(o.name, o.preview) for o in result.outputs] == [
        ("counts.csv", TablePreview(**COUNTS_PREVIEW)),
        ("notes.txt", None),
        ("totals.csv", TablePreview(**TOTALS_PREVIEW)),
    ]
    assert planned == [f.id for f in files]
    assert [f.name for f in files] == [o.name for o in result.outputs]


async def test_without_the_ask_the_keeper_sees_no_preview(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Previews that the executor could give are not asked for, and the keeper sees none."""
    executor.files = {"counts.csv": b"x"}
    executor.previews = {"counts.csv": COUNTS_PREVIEW}
    keeper = KeepAll()
    await queue_for(settings, executor, store, metrics).submit(
        user("user-a"), "run_python", ARGS, [], keeper, call=CALL
    )
    (result,) = keeper.results
    assert [o.preview for o in result.outputs] == [None]


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
        await queue_for(settings, executor, store, metrics).submit(me, "run_python", ARGS, [], Broken(), call=CALL)
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
    _, result, files = await queue.submit(me, "run_python", ARGS, [], keeper, call=CALL)
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
        await queue_for(settings, executor, store, metrics).submit(me, "run_python", ARGS, [], keeper, call=CALL)
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
        await queue.submit(me, "run_python", ARGS, [], NoAgain(), call=CALL)
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
    """Four docint users, two slots of its own: two run, two wait without a global slot, and Ghostwriter still runs."""
    executor.hold = {f"docint/{docint_user(n).user_id}" for n in range(4)}
    queue = queue_for(settings, executor, store, metrics)
    calls = [asyncio.create_task(run_inline(queue, Takes(settings), docint_user(n))) for n in range(4)]
    await until(lambda: executor.running == 2 and sample(metrics, "workbench_queue_length") == 2)
    async with asyncio.timeout(scaled(2)):
        _, result, _ = await queue.submit(user("user-a"), "run_python", ARGS, [], call=CALL)
    assert result.status == "ok"
    assert executor.running == 2
    assert [r.caller for r in executor.requests] == ["docint", "docint", "ghostwriter"]
    executor.gate.set()
    await asyncio.gather(*calls)
    assert len(executor.requests) == 5
    assert sample(metrics, "workbench_queue_length") == 0


async def test_the_callers_waiting_calls_are_capped_across_many_users(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Docint's users are pseudonyms each: 2 run, 8 wait (2 * 16 // 4), the 11th is refused, and places come back."""
    executor.hold = {f"docint/{docint_user(n).user_id}" for n in range(10)} | {"ghostwriter/user-a"}
    queue = queue_for(settings, executor, store, metrics)
    calls = [asyncio.create_task(run_inline(queue, Takes(settings), docint_user(n))) for n in range(10)]
    await until(lambda: executor.running == 2 and sample(metrics, "workbench_queue_length") == 8)
    async with asyncio.timeout(0.5):
        with pytest.raises(Busy) as refused:
            await run_inline(queue, Takes(settings), docint_user(10))
    assert refused.value.reason == "queue_full"
    assert sample(metrics, "workbench_job_refusals_total", reason="queue_full") == 1
    other = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, [], call=CALL))
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


async def test_a_call_waits_its_own_wait_then_gives_the_callers_slot_back(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Ghostwriter's calls may wait 60 s, docint's 1 s: it is refused after its own wait, and gives its slot back."""
    executor.hold = {"ghostwriter/user-a", "ghostwriter/user-b"}
    queue = queue_for(settings, executor, store, metrics, executor_slots=2)
    held = [
        asyncio.create_task(queue.submit(user(name), "run_python", ARGS, [], call=CALL))
        for name in ("user-a", "user-b")
    ]
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


async def test_previews_are_asked_only_when_told(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A job asks the executor for previews when its call says so; the default, and an inline job, never do."""
    me = user("user-a")
    queue = queue_for(settings, executor, store, metrics)
    await queue.submit(me, "run_python", ARGS, [], call=CALL)
    assert executor.requests[-1].previews is False
    await queue.submit(me, "run_python", ARGS, [], call=CALL, previews=True)
    assert executor.requests[-1].previews is True
    await queue.submit(me, "run_python", ARGS, [], call=DOCINT, previews=True)
    assert executor.requests[-1].previews is True
    await queue.submit(me, "run_python", ARGS, [], call=DOCINT)
    assert executor.requests[-1].previews is False
    await run_inline(queue, Takes(settings))
    assert executor.requests[-1].previews is False
    assert [r.previews for r in executor.requests] == [False, True, True, False, False]


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
async def test_a_text_with_a_control_character_is_skipped_as_control(
    char: str, settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A C0 control character other than tab, LF and CR, or DEL, keeps a text back: its JSON escape would grow it."""
    taken = await take_back({"t.csv": f"a,b\n1,x{char}y\n".encode()}, settings, store, executor, metrics)
    assert taken.files == ()
    assert skipped(taken) == {("t.csv", "control")}


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


async def test_a_call_without_caller_limits_is_never_capped(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Docint's gate holds docint alone: four users of a call without limits run at once while docint's wait."""
    queue = queue_for(settings, executor, store, metrics)
    await run_inline(queue, Takes(settings))
    executor.hold = {f"ghostwriter/user-{n}" for n in range(4)}
    others = [asyncio.create_task(queue.submit(user(f"user-{n}"), "run_python", ARGS, [], call=CALL)) for n in range(4)]
    await until(lambda: executor.running == 4)
    waiting = [asyncio.create_task(run_inline(queue, Takes(settings), docint_user(n))) for n in (2, 3)]
    await until(lambda: sample(metrics, "workbench_queue_length") == 2)
    executor.gate.set()
    await asyncio.gather(*others, *waiting)
    assert executor.peak == 4
    assert len(executor.requests) == 7


# --- version 0.7: render jobs, the prepare gate, a lock per source -------------------------------------------------

#: The first bytes of a JPEG, then invented ones: the api checks a render's signature and size and never decodes it.
JPEG = SIGNATURES["image/jpeg"] + b"\xe0" + b"\x00" * 27
#: Ghostwriter's limits as its doors pass them: two slots of its own.
GHOSTWRITER = CallLimits(wall_s=120, wait_s=60, slots=2)
#: What the head render of :func:`rendered`'s answer carries: the answer's facts.
FACTS: dict[str, Any] = {"kind": "pdf", "total": 3, "pages_without_text": [2], "frames": None, "hint": None}


def scan(store: Store, p: Principal = PG_SETUP, name: str = "scan.pdf") -> FileRecord:
    """An invented PDF of ``p``'s chat: the source of the renders."""
    return put(store, name, b"%PDF-1.7 synthetic", p=p)


def page(n: int, *, strip: int = 1, strips: int = 1, mime: str = "image/jpeg") -> dict[str, Any]:
    """One entry of a render answer, named as ``render_name`` names it."""
    return {
        "name": render_name(n, strip, strips, mime),
        "role": "page",
        "n": n,
        "strip": strip,
        "strips": strips,
        "width": 120,
        "height": 160,
        "mime": mime,
    }


def rendered(executor: FakeClient, *entries: dict[str, Any]) -> None:
    """Script a render job: the answer of a 3-page PDF whose page 2 has no text, and a small JPEG staged per entry."""
    executor.answer = {**FACTS, "renders": list(entries)}
    executor.files = {entry["name"]: JPEG for entry in entries}


async def prepare(
    queue: JobQueue, p: Principal, source: FileRecord, keeper: Keeper | None = None, *, call: CallLimits = CALL
) -> tuple[JobRecord, JobResult, list[DerivedRecord]]:
    """Prepare ``source`` as an attachment's prepare does: the first pages, whole, on the prepare gate."""
    return await queue.submit_renders(
        p, source, {"pages": None, "strips": 1}, keeper or KeepAll(), call=call, gate="prepare"
    )


def renders_kept(settings: Settings) -> int:
    """How many derived rows the volume's database holds, whatever their source."""
    db = sqlite3.connect(settings.data_dir / "workbench.db")
    try:
        return int(db.execute("SELECT COUNT(*) FROM derived").fetchone()[0])
    finally:
        db.close()


def test_prepares_wait_two_minutes_and_32_of_them_at_most() -> None:
    """One prepare takes a few seconds: 120 s covers a burst behind the one prepare slot, and 32 wait at the most."""
    assert (PREPARE_QUEUE_WAIT_S, PREPARE_WAITING_MAX) == (120, 32)
    assert PNG_SIGNATURE == SIGNATURES["image/png"]


async def test_a_prepare_never_takes_a_users_turn(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """One job per user, and theirs running: their prepare starts at once, while an on-demand render waits."""
    me = user("user-a")
    source = scan(store)
    executor.hold = {"ghostwriter/user-a"}
    queue = queue_for(settings, executor, store, metrics, jobs_per_user=1)
    interactive = asyncio.create_task(queue.submit(me, "run_python", ARGS, [], call=CALL))
    await until(lambda: executor.running == 1)
    prepared = asyncio.create_task(prepare(queue, me, source))
    await until(lambda: executor.running == 2)
    assert [req.tool for req in executor.requests] == ["run_python", "prepare_file"]
    assert sample(metrics, "workbench_queue_length") == 0
    on_demand = asyncio.create_task(queue.submit_renders(me, source, {"pages": [3]}, KeepAll(), call=CALL))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    assert executor.running == 2
    executor.gate.set()
    await asyncio.gather(interactive, prepared, on_demand)
    assert len(executor.requests) == 3
    assert queue.tracked_users == 0


async def test_one_prepare_runs_at_a_time_across_users(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The prepare gate is the caller's, so no user id shares it: user B's prepare starts only after user A's ends."""
    b = Principal("ghostwriter", "user-b", "chat-1")
    executor.hold = {"ghostwriter/user-a", "ghostwriter/user-b"}
    queue = queue_for(settings, executor, store, metrics)
    first = asyncio.create_task(prepare(queue, user("user-a"), scan(store)))
    await until(lambda: executor.running == 1)
    second = asyncio.create_task(prepare(queue, b, scan(store, b)))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    await asyncio.sleep(0.05)
    assert (executor.running, len(executor.requests)) == (1, 1)
    executor.gate.set()
    await asyncio.gather(first, second)
    assert [req.user_id for req in executor.requests] == ["user-a", "user-b"]
    assert executor.peak == 1
    assert sample(metrics, "workbench_queue_length") == 0


async def test_prepare_counts_within_the_callers_slots(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Two caller slots: with two interactive jobs running a prepare waits; with a prepare running one more runs."""
    c = Principal("ghostwriter", "user-c", "chat-1")
    source = scan(store, c)
    executor.hold = {"ghostwriter/user-a", "ghostwriter/user-b", "ghostwriter/user-c"}
    queue = queue_for(settings, executor, store, metrics)
    interactive = [
        asyncio.create_task(queue.submit(user(name), "run_python", ARGS, [], call=GHOSTWRITER))
        for name in ("user-a", "user-b")
    ]
    await until(lambda: executor.running == 2)
    waiting = asyncio.create_task(prepare(queue, c, source, call=GHOSTWRITER))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    assert executor.running == 2
    executor.gate.set()
    await asyncio.gather(*interactive, waiting)
    executor.gate.clear()
    running = asyncio.create_task(prepare(queue, c, source, call=GHOSTWRITER))
    await until(lambda: executor.running == 1)
    one = asyncio.create_task(queue.submit(user("user-a"), "run_python", ARGS, [], call=GHOSTWRITER))
    await until(lambda: executor.running == 2)
    two = asyncio.create_task(queue.submit(user("user-b"), "run_python", ARGS, [], call=GHOSTWRITER))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    assert executor.running == 2
    executor.gate.set()
    await asyncio.gather(running, one, two)
    assert executor.peak == 2
    assert [req.tool for req in executor.requests][3:] == ["prepare_file", "run_python", "run_python"]


async def test_waiting_prepares_do_not_fill_the_queue(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """32 prepares of one user wait, past the queue's 16 places, the user's 4 and the caller's 12; the 33rd is full.

    Meanwhile another user's interactive call that must wait is still let into the queue, and runs.
    """
    me = user("user-a")
    source = scan(store)
    capped = CallLimits(wall_s=120, wait_s=60, slots=3)
    executor.hold = {"ghostwriter/user-a", "ghostwriter/user-b"}
    queue = queue_for(settings, executor, store, metrics)
    running = asyncio.create_task(prepare(queue, me, source, call=capped))
    await until(lambda: executor.running == 1)
    waiting = [asyncio.create_task(prepare(queue, me, source, call=capped)) for _ in range(PREPARE_WAITING_MAX)]
    await until(lambda: sample(metrics, "workbench_queue_length") == PREPARE_WAITING_MAX)
    b_running = asyncio.create_task(queue.submit(user("user-b"), "run_python", ARGS, [], call=capped))
    await until(lambda: executor.running == 2)
    b_waiting = asyncio.create_task(queue.submit(user("user-b", "chat-2"), "run_python", ARGS, [], call=capped))
    await until(lambda: sample(metrics, "workbench_queue_length") == PREPARE_WAITING_MAX + 1)
    async with asyncio.timeout(0.5):
        with pytest.raises(Busy) as refused:
            await prepare(queue, me, source, call=capped)
    assert refused.value.reason == "queue_full"
    assert sample(metrics, "workbench_job_refusals_total", reason="queue_full") == 1
    executor.gate.set()
    done = await asyncio.gather(running, *waiting, b_running, b_waiting)
    assert {result.status for _, result, _ in done} == {"ok"}
    assert len(executor.requests) == PREPARE_WAITING_MAX + 3
    assert sample(metrics, "workbench_queue_length") == 0


async def test_a_prepare_waits_up_to_its_call_wait(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Behind a prepare that holds the gate, another waits its call's wait, is refused, and gives its place back."""
    b = Principal("ghostwriter", "user-b", "chat-1")
    executor.hold = {"ghostwriter/user-a"}
    queue = queue_for(settings, executor, store, metrics)
    first = asyncio.create_task(prepare(queue, user("user-a"), scan(store)))
    await until(lambda: executor.running == 1)
    async with asyncio.timeout(scaled(3)):
        with pytest.raises(Busy) as refused:
            await prepare(queue, b, scan(store, b), call=dataclasses.replace(CALL, wait_s=1))
    assert refused.value.reason == "wait_timeout"
    assert sample(metrics, "workbench_job_refusals_total", reason="wait_timeout") == 1
    assert sample(metrics, "workbench_queue_wait_seconds_sum") >= 1
    assert sample(metrics, "workbench_queue_length") == 0
    assert len(executor.requests) == 1
    executor.gate.set()
    await first
    await prepare(queue, b, scan(store, b, "other.pdf"))
    assert len(executor.requests) == 2


async def test_a_render_job_reads_its_source_alone(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The job is ``prepare_file`` over the source, its one input and its ``path``, whatever path the args name."""
    me = user("user-a")
    source = scan(store)
    queue = queue_for(settings, executor, store, metrics)
    job, _, _ = await queue.submit_renders(
        me, source, {"pages": [2], "strips": 2, "path": "other.pdf"}, KeepAll(), call=CALL
    )
    (req,) = executor.requests
    assert (job.tool, req.tool, req.input_files) == ("prepare_file", "prepare_file", ["scan.pdf"])
    assert req.args == {"pages": [2], "strips": 2, "path": "scan.pdf"}
    assert req.previews is False


async def test_a_render_job_runs_only_for_its_sources_chat(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A source of another user or chat is a programming error: no turn, no job row, nothing sent."""
    source = scan(store)
    queue = queue_for(settings, executor, store, metrics)
    for other in (user("user-b"), user("user-a", "chat-2"), Principal("docint", "user-a", "chat-1")):
        with pytest.raises(ValueError, match="source"):
            await queue.submit_renders(other, source, {}, KeepAll(), call=CALL, gate="prepare")
    assert (executor.requests, jobs_recorded(settings), queue.tracked_users) == ([], 0, 0)


async def test_renders_are_recorded_before_they_are_kept(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The keeper sees the planned ids while no render exists; then they are derived rows, never files of the chat."""
    me = user("user-a")
    source = scan(store)
    rendered(executor, page(1), page(2), page(3))

    class Watching(KeepAll):
        """Also notes the source's renders when it records."""

        def __init__(self) -> None:
            super().__init__(store, me)
            self.derived_seen: list[DerivedRecord] | None = None

        @override
        def record(self, result: JobResult, planned: Sequence[str]) -> Sequence[str]:
            self.derived_seen = store.derived_of(source)
            return super().record(result, planned)

    keeper = Watching()
    queue = queue_for(settings, executor, store, metrics)
    job, result, kept = await prepare(queue, me, source, keeper)
    ((status, planned, seen),) = keeper.recorded
    assert (status, seen, keeper.derived_seen) == ("ok", ["scan.pdf"], [])
    rows = store.derived_of(source)
    assert [rec.id for rec in kept] == [rec.id for rec in rows] == planned
    assert [(r.role, r.ordinal, r.mime, r.width, r.height, r.size) for r in rows] == [
        ("page", n, "image/jpeg", 120, 160, len(JPEG)) for n in (1, 2, 3)
    ]
    # an answer without a unit reads as pages, and keeps it; one without a duration keeps none
    assert rows[0].meta["facts"] == {**FACTS, "unit": "page", "duration_s": None}
    assert [dict(rec.meta) for rec in rows[1:]] == [{}, {}]
    assert [rec.id for rec in store.files_in_chat(me)] == [source.id]
    assert (job.status, result.message, result.outputs, keeper.again_ids) == ("ok", "", [], [])
    assert not (settings.data_dir / "staging" / job.id).exists()


async def test_a_strip_keeps_its_place_and_no_facts(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A strip of page 1 is not the whole render: it keeps its strip and strips, and the facts stay off it."""
    me = user("user-a")
    source = scan(store)
    rendered(executor, page(1, strip=1, strips=2), page(1, strip=2, strips=2))
    queue = queue_for(settings, executor, store, metrics)
    _, _, kept = await queue.submit_renders(me, source, {"pages": [1], "strips": 2}, KeepAll(), call=CALL)
    assert [dict(rec.meta) for rec in kept] == [{"strip": 1, "strips": 2}, {"strip": 2, "strips": 2}]
    assert [(rec.ordinal, rec.strip, rec.strips) for rec in store.derived_of(source)] == [(1, 1, 2), (1, 2, 2)]


async def test_a_render_made_without_facts_stores_none(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A job told ``facts: False`` answers no real list of pages without text: its whole page 1 carries no facts."""
    me = user("user-a")
    source = scan(store)
    rendered(executor, page(1))
    queue = queue_for(settings, executor, store, metrics)
    _, _, kept = await queue.submit_renders(me, source, {"pages": [1], "facts": False}, KeepAll(), call=CALL)
    assert [dict(rec.meta) for rec in kept] == [{}]
    assert [dict(rec.meta) for rec in store.derived_of(source)] == [{}]


async def test_a_render_without_its_entry_or_signature_is_not_kept(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """An output the answer does not name, a PNG staged as a JPEG and one of the cap's size are lost as invalid.

    Each is named again by the keeper, by its planned id; the render one byte under the cap is kept.
    """
    me = user("user-a")
    source = scan(store)
    rendered(executor, page(1), page(2), page(3), page(4))
    executor.files = {
        page(1)["name"]: JPEG,
        "p0009.jpg": JPEG,
        page(2)["name"]: PNG,
        page(3)["name"]: JPEG.ljust(RENDER_MAX_BYTES, b"\x00"),
        page(4)["name"]: JPEG.ljust(RENDER_MAX_BYTES - 1, b"\x00"),
    }
    keeper = KeepAll()
    queue = queue_for(settings, executor, store, metrics)
    _, result, kept = await prepare(queue, me, source, keeper)
    ((_, planned, _),) = keeper.recorded
    assert [rec.id for rec in kept] == [planned[0], planned[4]]
    assert [(rec.ordinal, rec.size) for rec in store.derived_of(source)] == [(1, len(JPEG)), (4, RENDER_MAX_BYTES - 1)]
    assert keeper.again_ids == [planned[1:4]]
    assert result.message == "3 outputs could not be kept (invalid)"


@pytest.mark.parametrize(
    "answer",
    [{"answer": 42}, {**FACTS, "renders": [{**page(1), "name": "../p0001.jpg"}]}, {**FACTS, "renders": []}],
    ids=["no-render-answer", "a-name-not-its-own", "no-entries"],
)
async def test_an_answer_that_names_no_render_keeps_nothing(
    answer: dict[str, Any], settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """An answer that is no render answer, or names no staged render, keeps none of what the job staged."""
    me = user("user-a")
    source = scan(store)
    executor.answer = answer
    executor.files = {page(1)["name"]: JPEG}
    keeper = KeepAll()
    queue = queue_for(settings, executor, store, metrics)
    _, result, kept = await prepare(queue, me, source, keeper)
    ((_, planned, _),) = keeper.recorded
    assert (kept, renders_kept(settings), keeper.again_ids) == ([], 0, [planned])
    assert result.message == "1 output could not be kept (invalid)"


async def test_a_render_that_exists_or_passes_the_cap_is_lost_as_duplicate_or_full(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A page already rendered is ``duplicate``; one past the source's cap of renders is ``full``; both named again."""
    me = user("user-a")
    source = scan(store)
    real = store.register_derived

    def capped(*args: Any, **more: Any) -> DerivedRecord:
        return real(*args, **more, cap_bytes=2 * len(JPEG))

    monkeypatch.setattr(store, "register_derived", capped)
    queue = queue_for(settings, executor, store, metrics)
    rendered(executor, page(1))
    await prepare(queue, me, source)
    rendered(executor, page(1), page(2), page(3))
    keeper = KeepAll()
    _, result, kept = await queue.submit_renders(me, source, {"pages": [1, 2, 3]}, keeper, call=CALL)
    ((_, planned, _),) = keeper.recorded
    assert [rec.id for rec in kept] == [planned[1]]
    assert [rec.ordinal for rec in store.derived_of(source)] == [1, 2]
    assert keeper.again_ids == [[planned[0], planned[2]]]
    assert result.message == "2 outputs could not be kept (duplicate, full)"


async def test_a_render_of_a_deleted_chat_is_not_kept(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The chat is deleted while the job runs: no derived row, no folder, and the render is named again, as gone."""
    me = user("user-a")
    source = scan(store)
    rendered(executor, page(1))
    executor.hold = {"ghostwriter/user-a"}
    keeper = KeepAll()
    queue = queue_for(settings, executor, store, metrics)
    running = asyncio.create_task(prepare(queue, me, source, keeper))
    await until(lambda: executor.running == 1)
    store.delete_chat(me, unrecorded)
    executor.gate.set()
    _, result, kept = await running
    ((_, planned, _),) = keeper.recorded
    assert (kept, renders_kept(settings), keeper.again_ids) == ([], 0, [planned])
    assert result.message == "1 output could not be kept (gone)"
    assert not _chat_folder(settings, me).exists()


async def test_renders_kept_before_a_failed_job_record_are_removed_and_named_again(
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller gets the store's failure and tells the model of no render, so none stays, and each is named again."""
    me = user("user-a")
    source = scan(store)
    rendered(executor, page(1), page(2))
    break_finish(store, monkeypatch)
    keeper = KeepAll()
    queue = queue_for(settings, executor, store, metrics)
    with pytest.raises(sqlite3.OperationalError):
        await prepare(queue, me, source, keeper)
    ((_, planned, _),) = keeper.recorded
    assert keeper.again_ids == [planned]
    assert (renders_kept(settings), [rec.id for rec in store.files_in_chat(me)]) == (0, [source.id])
    assert not (_chat_folder(settings, me) / ".derived").exists()


async def test_the_source_lock_serialises_and_times_out(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """One holder per source: the next waits its turn, one past its wait is refused, another source is free.

    A waiter that times out or is cancelled leaves nothing behind, and the lock is dropped once no one holds or waits.
    """
    queue = queue_for(settings, executor, store, metrics)
    one, other = "1" * 32, "2" * 32
    order: list[str] = []
    let_go = asyncio.Event()

    async def hold(name: str, release: asyncio.Event) -> None:
        async with queue.source_lock(one, 5):
            order.append(f"{name} in")
            await release.wait()
            order.append(f"{name} out")

    first = asyncio.create_task(hold("first", let_go))
    await until(lambda: order == ["first in"])
    free = asyncio.Event()
    free.set()
    second = asyncio.create_task(hold("second", free))
    cancelled = asyncio.create_task(hold("cancelled", free))
    await asyncio.sleep(0.05)
    async with asyncio.timeout(0.5), queue.source_lock(other, 0):
        assert queue.tracked_sources == 2
    with pytest.raises(Busy) as refused:
        async with queue.source_lock(one, 0.1):
            pytest.fail("the lock is held")
    assert refused.value.reason == "wait_timeout"
    assert sample(metrics, "workbench_job_refusals_total", reason="wait_timeout") == 1
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert order == ["first in"]
    let_go.set()
    await asyncio.gather(first, second)
    assert order == ["first in", "first out", "second in", "second out"]
    assert queue.tracked_sources == 0


async def test_a_calls_jobs_render_on_the_users_turn_and_name_no_output(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``CallJobs`` renders within its call's limits on the user's own turn; renders are never the call's outputs.

    It waits for its turn its call's wait, and for a source's lock ``SOURCE_LOCK_WAIT_S`` (lowered here to none).
    """
    monkeypatch.setattr(tools_mcp, "SOURCE_LOCK_WAIT_S", 0)
    me = user("user-a")
    source = scan(store)
    queue = queue_for(settings, executor, store, metrics, jobs_per_user=1)
    jobs = CallJobs(queue, dataclasses.replace(GHOSTWRITER, wall_s=30, wait_s=0))
    rendered(executor, page(1))
    _, _, kept = await jobs.submit_renders(me, source, {"pages": [1]}, KeepAll())
    assert [rec.ordinal for rec in kept] == [1]
    assert jobs.outputs() == []
    assert executor.requests[0].limits.wall_s == 30
    executor.hold = {"ghostwriter/user-a"}
    interactive = asyncio.create_task(queue.submit(me, "run_python", ARGS, [], call=GHOSTWRITER))
    await until(lambda: executor.running == 1)
    with pytest.raises(Busy) as refused:
        await jobs.submit_renders(me, source, {"pages": [2]}, KeepAll())
    assert refused.value.reason == "wait_timeout"
    async with queue.source_lock(source.id, 0):
        with pytest.raises(Busy) as locked:
            async with jobs.source_lock(source.id):
                pytest.fail("the lock is held")
    assert locked.value.reason == "wait_timeout"
    executor.gate.set()
    await interactive


# --- version 0.8: an Office file's PDF, and one turn for its conversion and its renders -----------------------------

#: The first bytes of a PDF, then invented ones: the api checks the converted PDF's signature and size alone.
CONVERTED = CONVERTED_SIGNATURE + b"1.7 synthetic converted deck"


def deck(store: Store, p: Principal = PG_SETUP, name: str = "harbour-deck.pptx") -> FileRecord:
    """An invented deck of ``p``'s chat: the source of a conversion."""
    return put(store, name, b"PK synthetic deck", p=p)


def converts(executor: FakeClient, data: bytes = CONVERTED, *, name: str = CONVERTED_NAME) -> None:
    """Script a conversion: the answer of a 3-slide deck converted to 3 pages, and ``data`` staged as ``name``."""
    executor.scripts["convert_office"] = ({"name": CONVERTED_NAME, "pages": 3, "slides": 3}, {name: data})


async def convert(
    queue: JobQueue, p: Principal, source: FileRecord, keeper: Keeper | None = None, **more: Any
) -> tuple[JobRecord, JobResult, list[DerivedRecord]]:
    """Convert ``source`` on the user's own turn, unless ``more`` names a gate or a turn."""
    return await queue.submit_conversion(p, source, {}, keeper or KeepAll(), call=CALL, **more)


class SeesDerived(KeepAll):
    """A keeper that also notes, when it records, the derived files its source has at that moment.

    Attributes:
        derived_seen: What :meth:`Store.derived_of` gave as it recorded; ``None`` before it records.
    """

    def __init__(self, store: Store, p: Principal, source: FileRecord) -> None:
        """Look into ``p``'s chat and at ``source``'s derived files when recording."""
        super().__init__(store, p)
        self.source = source
        self.derived_seen: list[DerivedRecord] | None = None

    @override
    def record(self, result: JobResult, planned: Sequence[str]) -> Sequence[str]:
        """Note the source's derived files, then keep every output."""
        assert self.store is not None
        self.derived_seen = self.store.derived_of(self.source)
        return super().record(result, planned)


async def test_a_conversion_keeps_one_pdf_after_its_record(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The job is ``convert_office`` over the source; the keeper sees the planned id while no row exists.

    Then the source has one ``pdf`` row, with the answer's pages and slides, and the chat holds the source alone.
    """
    me = user("user-a")
    source = deck(store)
    converts(executor)
    keeper = SeesDerived(store, me, source)
    queue = queue_for(settings, executor, store, metrics)
    job, result, kept = await queue.submit_conversion(
        me, source, {"budget_s": 90, "path": "other.pptx"}, keeper, call=CALL
    )
    (req,) = executor.requests
    assert (job.tool, req.tool, req.input_files) == ("convert_office", "convert_office", ["harbour-deck.pptx"])
    assert req.args == {"budget_s": 90, "path": "harbour-deck.pptx"}
    ((status, planned, seen),) = keeper.recorded
    assert (status, seen, keeper.derived_seen) == ("ok", ["harbour-deck.pptx"], [])
    (pdf,) = store.derived_of(source)
    assert [rec.id for rec in kept] == [pdf.id] == planned
    assert (pdf.role, pdf.ordinal, pdf.mime, pdf.width, pdf.height, pdf.size) == (
        "pdf",
        0,
        CONVERTED_MIME,
        None,
        None,
        len(CONVERTED),
    )
    assert (pdf.meta["pages"], pdf.meta["slides"]) == (3, 3)
    assert pdf.relative_path == f".derived/{source.id}/{pdf.id}.pdf"
    assert [rec.id for rec in store.files_in_chat(me)] == [source.id]
    assert (job.status, result.message, result.outputs, keeper.again_ids) == ("ok", "", [], [])
    assert not (settings.data_dir / "staging" / job.id).exists()


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("other.pdf", CONVERTED),
        (CONVERTED_NAME, JPEG),
        (CONVERTED_NAME, CONVERTED.ljust(CONVERTED_MAX_BYTES + 1, b"\x00")),
    ],
    ids=["another-name", "a-jpeg", "past-32-mib"],
)
async def test_a_conversion_output_that_is_no_pdf_is_not_kept(
    name: str, data: bytes, settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Another name, another signature or one byte past the cap: no row, and the keeper names the output again."""
    me = user("user-a")
    source = deck(store)
    converts(executor, data, name=name)
    keeper = KeepAll()
    queue = queue_for(settings, executor, store, metrics)
    _, result, kept = await convert(queue, me, source, keeper)
    ((_, planned, _),) = keeper.recorded
    assert (kept, renders_kept(settings), keeper.again_ids) == ([], 0, [planned])
    assert result.message == "1 output could not be kept (invalid)"
    assert not (_chat_folder(settings, me) / ".derived").exists()


async def test_a_converted_pdf_of_exactly_the_cap_is_kept(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The cap is the most a converted PDF may have: one of exactly 32 MiB is kept."""
    me = user("user-a")
    source = deck(store)
    converts(executor, CONVERTED.ljust(CONVERTED_MAX_BYTES, b"\x00"))
    queue = queue_for(settings, executor, store, metrics)
    _, _, kept = await convert(queue, me, source)
    assert [(rec.role, rec.size) for rec in kept] == [("pdf", CONVERTED_MAX_BYTES)]


@pytest.mark.parametrize(
    ("status", "answer"),
    [
        ("error", {"error": "read_timeout: the work ran past its time limit"}),
        ("timeout", {"error": "read_timeout: the work ran past its time limit"}),
        ("killed", {"error": "hidden"}),
    ],
)
async def test_a_failed_conversion_keeps_nothing_it_staged(
    status: str, answer: dict[str, Any], settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A conversion that answers no success keeps no row, even with ``converted.pdf`` staged by a deadline race.

    No output is planned, so the keeper records none and names none again; the staging goes.
    """
    me = user("user-a")
    source = deck(store)
    converts(executor)
    executor.scripts["convert_office"] = (answer, {CONVERTED_NAME: CONVERTED})
    executor.status = status
    keeper = KeepAll()
    queue = queue_for(settings, executor, store, metrics)
    job, result, kept = await convert(queue, me, source, keeper)
    assert keeper.recorded == [(status, [], [])]
    assert (kept, renders_kept(settings), keeper.again_ids) == ([], 0, [])
    assert (job.status, result.outputs) == (status, [])
    assert not (settings.data_dir / "staging" / job.id).exists()


@pytest.mark.parametrize(
    "answer",
    [
        {"answer": 42},
        {"name": "other.pdf", "pages": 3},
        {"name": CONVERTED_NAME, "pages": 0},
        {"name": CONVERTED_NAME, "pages": 3, "renders": []},
    ],
    ids=["no-conversion-answer", "another-name", "no-pages", "an-unknown-field"],
)
async def test_an_answer_that_is_no_conversion_keeps_nothing(
    answer: dict[str, Any], settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """An ``ok`` answer that does not read as a ``ConvertAnswer`` keeps no PDF it staged; the keeper names it again."""
    me = user("user-a")
    source = deck(store)
    executor.scripts["convert_office"] = (answer, {CONVERTED_NAME: CONVERTED})
    keeper = KeepAll()
    queue = queue_for(settings, executor, store, metrics)
    _, result, kept = await convert(queue, me, source, keeper)
    ((_, planned, _),) = keeper.recorded
    assert (kept, renders_kept(settings), keeper.again_ids) == ([], 0, [planned])
    assert result.message == "1 output could not be kept (invalid)"


async def test_a_conversion_runs_only_for_its_sources_chat(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A source of another user, chat or caller is a programming error: no turn, no job row, nothing sent."""
    source = deck(store)
    queue = queue_for(settings, executor, store, metrics)
    for other in (user("user-b"), user("user-a", "chat-2"), Principal("docint", "user-a", "chat-1")):
        with pytest.raises(ValueError, match="source"):
            await convert(queue, other, source, gate="prepare")
    assert (executor.requests, jobs_recorded(settings), queue.tracked_users) == ([], 0, 0)


async def test_renders_of_a_converted_pdf_read_it_as_their_input(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The render job's one input and its ``path`` are the PDF's derived path, whatever path the args name.

    The renders are of the Office file itself, beside its PDF.
    """
    me = user("user-a")
    source = deck(store)
    converts(executor)
    queue = queue_for(settings, executor, store, metrics)
    _, _, (pdf,) = await convert(queue, me, source)
    rendered(executor, page(1), page(2))
    office = {"unit": "slide", "slides": 3}
    _, _, kept = await queue.submit_renders(
        me, source, {"pages": [1, 2], "path": "other.pdf", "office": office}, KeepAll(), call=CALL, converted=pdf
    )
    req = executor.requests[-1]
    assert (req.tool, req.input_files) == ("prepare_file", [pdf.relative_path])
    assert req.args == {"pages": [1, 2], "path": pdf.relative_path, "office": office}
    assert {rec.source_id for rec in kept} == {source.id}
    assert [(rec.role, rec.ordinal) for rec in store.derived_of(source)] == [("page", 1), ("page", 2), ("pdf", 0)]


async def test_a_render_input_must_be_the_sources_pdf(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Another source's PDF, a page render, or a record changed from the PDF's row is a programming error.

    Nothing is sent and no job row is made for any of them.
    """
    me = user("user-a")
    source = deck(store)
    other = deck(store, name="other-deck.pptx")
    converts(executor)
    queue = queue_for(settings, executor, store, metrics)
    _, _, (pdf,) = await convert(queue, me, source)
    _, _, (other_pdf,) = await convert(queue, me, other)
    rendered(executor, page(1))
    _, _, (a_page,) = await queue.submit_renders(me, source, {"pages": [1]}, KeepAll(), call=CALL)
    before = (len(executor.requests), jobs_recorded(settings))
    for wrong in (
        other_pdf,
        a_page,
        dataclasses.replace(pdf, name="harbour-deck.pptx"),
        dataclasses.replace(pdf, mime="image/jpeg"),
        dataclasses.replace(pdf, chat_id="chat-2"),
    ):
        with pytest.raises(ValueError, match="PDF"):
            await queue.submit_renders(me, source, {"pages": [2]}, KeepAll(), call=CALL, converted=wrong)
    assert (len(executor.requests), jobs_recorded(settings), queue.tracked_users) == (*before, 0)


async def test_one_turn_holds_a_conversion_and_its_renders(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Under the prepare gate, user B's prepare waits while user A's conversion runs, and after A's renders too.

    A's two jobs run in one turn of the gate, so B never slips in between them.
    """
    a = user("user-a")
    b = Principal("ghostwriter", "user-b", "chat-1")
    source = deck(store, a)
    other = scan(store, b)
    converts(executor)
    rendered(executor, page(1))
    executor.hold = {"ghostwriter/user-a"}
    queue = queue_for(settings, executor, store, metrics)

    async def prepare_office() -> list[DerivedRecord]:
        async with queue.turn(a, call=CALL, gate="prepare") as held:
            assert held == Turn("ghostwriter", "user-a", "prepare")
            _, _, (pdf,) = await convert(queue, a, source, gate="prepare", turn=held)
            _, _, renders = await queue.submit_renders(
                a, source, {"pages": None}, KeepAll(), call=CALL, gate="prepare", turn=held, converted=pdf
            )
        return [pdf, *renders]

    first = asyncio.create_task(prepare_office())
    await until(lambda: executor.running == 1)
    second = asyncio.create_task(prepare(queue, b, other))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    await asyncio.sleep(0.05)
    assert len(executor.requests) == 1
    executor.gate.set()
    kept, _ = await asyncio.gather(first, second)
    assert [(req.user_id, req.tool) for req in executor.requests] == [
        ("user-a", "convert_office"),
        ("user-a", "prepare_file"),
        ("user-b", "prepare_file"),
    ]
    assert [rec.role for rec in kept] == ["pdf", "page"]
    assert executor.peak == 1
    assert (queue.tracked_users, sample(metrics, "workbench_queue_length")) == (0, 0)


async def test_a_turn_serves_only_its_own_user(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A turn serves its own caller, user and gate while it is held: any other, a copy or a turn let go is refused.

    Each refusal comes before a job row, and nothing is sent.
    """
    a = user("user-a")
    b = user("user-b")
    docint = Principal("docint", "user-a", "chat-1")
    sources = {a: deck(store, a), b: deck(store, b), docint: deck(store, docint)}
    queue = queue_for(settings, executor, store, metrics)

    async def refused(p: Principal, held: Turn, gate: Gate = "user") -> None:
        with pytest.raises(ValueError, match="turn"):
            await convert(queue, p, sources[p], gate=gate, turn=held)
        with pytest.raises(ValueError, match="turn"):
            await queue.submit_renders(p, sources[p], {}, KeepAll(), call=CALL, gate=gate, turn=held)

    async with queue.turn(a, call=CALL) as held:
        assert held == Turn("ghostwriter", "user-a", "user")
        await refused(b, held)
        await refused(docint, held)
        await refused(a, held, "prepare")
        await refused(a, held, "conversion")
        await refused(a, Turn("ghostwriter", "user-a", "user"))
    await refused(a, held)
    assert (executor.requests, jobs_recorded(settings), queue.tracked_users) == ([], 0, 0)


async def test_a_turn_runs_one_job_at_a_time(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A turn holds one slot: a second job sent in it while one of it runs is refused, and the first runs on."""
    me = user("user-a")
    source = deck(store)
    converts(executor)
    executor.hold = {"ghostwriter/user-a"}
    queue = queue_for(settings, executor, store, metrics)
    async with queue.turn(me, call=CALL) as held:
        running = asyncio.create_task(convert(queue, me, source, turn=held))
        await until(lambda: executor.running == 1)
        with pytest.raises(ValueError, match="one job"):
            await convert(queue, me, source, turn=held)
        executor.gate.set()
        _, _, kept = await running
        assert [rec.role for rec in kept] == ["pdf"]
    assert (len(executor.requests), jobs_recorded(settings), queue.tracked_users) == (1, 1, 0)


async def test_a_calls_jobs_convert_and_render_in_one_turn_of_the_user(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """``CallJobs`` holds the user's own turn with its call's limits; both jobs run in it, and neither is an output.

    One job per user and no wait: a job that took the user's turn again inside the one held would be refused.
    """
    me = user("user-a")
    source = deck(store)
    converts(executor)
    rendered(executor, page(1))
    queue = queue_for(settings, executor, store, metrics, jobs_per_user=1)
    jobs = CallJobs(queue, dataclasses.replace(GHOSTWRITER, wall_s=30, wait_s=0))
    async with jobs.turn(me) as held:
        assert held == Turn("ghostwriter", "user-a", "user")
        _, _, (pdf,) = await jobs.submit_conversion(me, source, {}, KeepAll(), turn=held)
        _, _, kept = await jobs.submit_renders(me, source, {"pages": [1]}, KeepAll(), turn=held, converted=pdf)
    assert [req.tool for req in executor.requests] == ["convert_office", "prepare_file"]
    assert [req.limits.wall_s for req in executor.requests] == [30, 30]
    assert executor.requests[1].input_files == [pdf.relative_path]
    assert [rec.ordinal for rec in kept] == [1]
    assert jobs.outputs() == []
    assert queue.tracked_users == 0


# --- the conversion gate: an Office file's conversion and its drawing, apart from the prepare gate --------------------

#: How long Ghostwriter waits for a tool call (its ``WORKBENCH_TIMEOUT``): a wait for a source's lock ends before it.
GHOSTWRITER_TOOL_TIMEOUT_S = 150


def ghostwriter(settings: Settings, wait_s: int = 60) -> CallLimits:
    """Ghostwriter's limits as its doors pass them, with its slots as the settings have them."""
    return CallLimits(wall_s=120, wait_s=wait_s, slots=settings.ghostwriter_slots)


async def office_prepare(queue: JobQueue, p: Principal, source: FileRecord, call: CallLimits) -> list[DerivedRecord]:
    """Prepare an Office file as the prepare route does: its conversion and its renders in one turn of its gate."""
    async with queue.turn(p, call=call, gate="conversion") as held:
        _, _, (pdf,) = await convert(queue, p, source, gate="conversion", turn=held)
        _, _, renders = await queue.submit_renders(
            p, source, {"pages": None}, KeepAll(), call=call, gate="conversion", turn=held, converted=pdf
        )
    return [pdf, *renders]


async def turn_only(queue: JobQueue, p: Principal, call: CallLimits, gate: Gate) -> None:
    """Take a turn of ``gate`` and let it go at once, sending no job."""
    async with queue.turn(p, call=call, gate=gate):
        pass


def test_a_call_waits_for_a_sources_lock_up_to_a_job_wall() -> None:
    """The lock's holder may be converting what the call needs: the call waits as long as it would itself have.

    That is the default job wall, longer than a call's 5 s for its own turn and short of Ghostwriter's 150 s for a call.
    """
    assert SOURCE_LOCK_WAIT_S == 120
    assert MCP_QUEUE_WAIT_S < SOURCE_LOCK_WAIT_S < GHOSTWRITER_TOOL_TIMEOUT_S


async def test_a_pictures_prepare_runs_while_a_conversion_runs(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The conversion gate is not the prepare gate: user B's prepare runs and ends while user A's conversion runs."""
    a, b = user("user-a"), Principal("ghostwriter", "user-b", "chat-1")
    source, picture = deck(store, a), scan(store, b)
    converts(executor)
    rendered(executor, page(1))
    executor.hold = {"ghostwriter/user-a"}
    call = ghostwriter(settings)
    queue = queue_for(settings, executor, store, metrics)
    converting = asyncio.create_task(office_prepare(queue, a, source, call))
    await until(lambda: executor.running == 1)

    _, result, kept = await asyncio.wait_for(prepare(queue, b, picture, call=call), scaled(2))
    assert (result.status, [rec.ordinal for rec in kept]) == ("ok", [1])
    assert executor.running == 1  # the conversion still runs
    executor.gate.set()
    await converting
    assert [(req.user_id, req.tool) for req in executor.requests] == [
        ("user-a", "convert_office"),
        ("user-b", "prepare_file"),
        ("user-a", "prepare_file"),
    ]


async def test_one_conversion_of_a_caller_runs_at_a_time_on_demand_or_not(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """User C's conversion waits for user A's; so does user D's on demand, a call's: one gate, over all the users.

    Both wait in the conversion gate's own line, and each runs when the one before it ends.
    """
    a, c, d = user("user-a"), user("user-c"), user("user-d")
    sources = {p: deck(store, p) for p in (a, c, d)}
    converts(executor)
    rendered(executor, page(1))
    executor.hold = {"ghostwriter/user-a"}
    call = ghostwriter(settings)
    queue = queue_for(settings, executor, store, metrics)
    first = asyncio.create_task(office_prepare(queue, a, sources[a], call))
    await until(lambda: executor.running == 1)
    second = asyncio.create_task(office_prepare(queue, c, sources[c], call))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    jobs = CallJobs(queue, call)

    async def on_demand() -> list[DerivedRecord]:
        async with jobs.turn(d, converting=True) as held:
            assert held.gate == "conversion"
            _, _, (pdf,) = await jobs.submit_conversion(d, sources[d], {}, KeepAll(), turn=held)
            _, _, renders = await jobs.submit_renders(
                d, sources[d], {"pages": [1]}, KeepAll(), turn=held, converted=pdf
            )
        return [pdf, *renders]

    third = asyncio.create_task(on_demand())
    await until(lambda: sample(metrics, "workbench_queue_length") == 2)
    await asyncio.sleep(0.05)
    assert (executor.running, len(executor.requests)) == (1, 1)
    executor.gate.set()
    await asyncio.gather(first, second, third)
    assert [(req.user_id, req.tool) for req in executor.requests] == [
        (name, tool) for name in ("user-a", "user-c", "user-d") for tool in ("convert_office", "prepare_file")
    ]
    assert executor.peak == 1
    assert (queue.tracked_users, sample(metrics, "workbench_queue_length")) == (0, 0)


async def test_a_tool_call_gets_a_slot_beside_a_conversion_and_a_prepare(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Ghostwriter's default slots, 3: a conversion and a picture's prepare run, and a third user's call still runs.

    They all count within the caller's slots; one more call of Ghostwriter's waits for one of them.
    """
    assert settings.ghostwriter_slots == 3
    a, b = user("user-a"), Principal("ghostwriter", "user-b", "chat-1")
    source, picture = deck(store, a), scan(store, b)
    converts(executor)
    rendered(executor, page(1))
    executor.hold = {"ghostwriter/user-a", "ghostwriter/user-b", "ghostwriter/user-c", "ghostwriter/user-d"}
    call = ghostwriter(settings)
    queue = queue_for(settings, executor, store, metrics)
    converting = asyncio.create_task(office_prepare(queue, a, source, call))
    preparing = asyncio.create_task(prepare(queue, b, picture, call=call))
    await until(lambda: executor.running == 2)
    tool_call = asyncio.create_task(queue.submit(user("user-c"), "run_python", ARGS, [], call=call))
    await until(lambda: executor.running == 3)
    one_more = asyncio.create_task(queue.submit(user("user-d"), "run_python", ARGS, [], call=call))
    await until(lambda: sample(metrics, "workbench_queue_length") == 1)
    assert executor.running == 3
    executor.gate.set()
    await asyncio.gather(converting, preparing, tool_call, one_more)
    assert executor.peak == 3


async def test_waiting_conversions_are_counted_apart_from_the_queue_and_the_prepares(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """32 conversions and 32 prepares of one user wait, past the queue's 16 places and the user's 4; a 33rd is full.

    Each line is counted on its own: one more of either is refused at once (``queue_full``).
    """
    me = user("user-a")
    source, picture = deck(store, me), scan(store, me)
    converts(executor)
    rendered(executor, page(1))
    executor.hold = {"ghostwriter/user-a"}
    call = ghostwriter(settings)
    queue = queue_for(settings, executor, store, metrics)
    converting = asyncio.create_task(office_prepare(queue, me, source, call))
    preparing = asyncio.create_task(prepare(queue, me, picture, call=call))
    await until(lambda: executor.running == 2)
    waiting = [
        asyncio.create_task(turn_only(queue, me, call, gate))
        for gate in ("conversion", "prepare")
        for _ in range(PREPARE_WAITING_MAX)
    ]
    await until(lambda: sample(metrics, "workbench_queue_length") == 2 * PREPARE_WAITING_MAX)
    for gate in ("conversion", "prepare"):
        async with asyncio.timeout(0.5):
            with pytest.raises(Busy) as refused:
                await turn_only(queue, me, call, gate)
        assert refused.value.reason == "queue_full"
    assert sample(metrics, "workbench_job_refusals_total", reason="queue_full") == 2
    executor.gate.set()
    await asyncio.gather(converting, preparing, *waiting)
    assert sample(metrics, "workbench_queue_length") == 0
    assert queue.tracked_users == 0


# --- version 0.9: a clip's artifacts are the only input of its job, its frames and transcript the renders -----------

#: One line of Nextext's JSON Lines and the first bytes of its zip: the api reads neither, only the executor does.
JSONL_LINE = b'{"text": "an invented sentence"}\n'
ZIP_BYTES = b"PK\x03\x04" + bytes(40)
#: The facts a clip's answer carries; every one of them is a clip's.
CLIP_FACTS: dict[str, Any] = {
    "kind": "media",
    "unit": "frame",
    "total": 2,
    "pages_without_text": [],
    "frames": None,
    "hint": None,
    "duration_s": 12.0,
}


def clip(store: Store, p: Principal = PG_SETUP, name: str = "harbour-tour.mp4") -> FileRecord:
    """An invented clip of ``p``'s chat: the source of a clip's renders, which no job ever reads."""
    return put(store, name, b"synthetic clip bytes", p=p)


def artifacts(store: Store, source: FileRecord, *, jsonl: bool = True, zipped: bool = True) -> list[DerivedRecord]:
    """Keep the artifacts of a clip as ``POST /files/derived`` does, the JSON Lines first."""
    held = [keep_artifact(store, source, JSONL_LINE, "nextext_jsonl")[0]] if jsonl else []
    return held + ([keep_artifact(store, source, ZIP_BYTES, "nextext_zip")[0]] if zipped else [])


def frame(n: int, time_s: float | None = None) -> dict[str, Any]:
    """One entry of a clip's answer: frame ``n``, named as ``render_name`` names it."""
    return {
        "name": render_name(n, 1, 1, "image/jpeg"),
        "role": "frame",
        "n": n,
        "strip": 1,
        "strips": 1,
        "width": 128,
        "height": 72,
        "mime": "image/jpeg",
        "time_s": time_s,
    }


def clip_made(
    executor: FakeClient, *entries: dict[str, Any], lines: int | None = 3, transcript: bytes = b"[00:00] Hello\n"
) -> None:
    """Script a clip's prepare: the answer for ``entries`` and, with ``lines``, a transcript of that many lines."""
    answer: dict[str, Any] = {**CLIP_FACTS, "total": len(entries), "renders": list(entries)}
    files = {entry["name"]: JPEG for entry in entries}
    if lines is not None:
        answer["transcript"] = {"name": TRANSCRIPT_NAME, "lines": lines}
        files[TRANSCRIPT_NAME] = transcript
    executor.answer, executor.files = answer, files


async def prepare_clip(
    queue: JobQueue, p: Principal, source: FileRecord, held: Sequence[DerivedRecord], keeper: Keeper | None = None
) -> tuple[JobRecord, JobResult, list[DerivedRecord]]:
    """Prepare a clip from its artifacts, as the prepare route does: on the prepare gate."""
    return await queue.submit_renders(
        p, source, {"budget_s": 90, "facts": True}, keeper or KeepAll(), call=CALL, gate="prepare", artifacts=held
    )


async def test_a_clips_artifacts_are_its_jobs_only_inputs(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The input files are the artifacts' derived paths, JSON Lines first, and the clip's own name is not among them.

    ``media`` is made from the rows alone, whatever the arguments name; ``path`` stays the clip's name, which is read
    for its kind and nothing else.
    """
    me = user("user-a")
    source = clip(store)
    jsonl, zipped = artifacts(store, source)
    clip_made(executor, frame(1, 0.0), frame(2, 4.0))
    queue = queue_for(settings, executor, store, metrics)
    forged = {
        "budget_s": 90,
        "facts": True,
        "path": "other.mp4",
        "media": {"nextext_jsonl": "../x.jsonl", "video": False},
    }
    await queue.submit_renders(me, source, forged, KeepAll(), call=CALL, gate="prepare", artifacts=[zipped, jsonl])

    (req,) = executor.requests
    assert (req.tool, req.input_files) == ("prepare_file", [jsonl.relative_path, zipped.relative_path])
    assert source.name not in req.input_files
    assert req.args == {
        "budget_s": 90,
        "facts": True,
        "path": source.name,
        "media": {"nextext_jsonl": jsonl.relative_path, "nextext_zip": zipped.relative_path, "video": True},
    }


@pytest.mark.parametrize(
    ("name", "jsonl", "zipped", "video"),
    [
        ("harbour-tour.mp4", True, False, True),
        ("harbour-tour.MKV", False, True, True),
        ("voice-memo.m4a", True, False, False),
    ],
    ids=["video-without-pictures", "uppercase-suffix", "audio"],
)
async def test_a_clip_with_one_artifact_names_the_other_none(
    name: str,
    jsonl: bool,
    zipped: bool,
    video: bool,
    settings: Settings,
    store: Store,
    executor: FakeClient,
    metrics: Metrics,
) -> None:
    """A role the clip has no row of is ``None`` in ``media``; ``video`` is told by the clip's suffix."""
    me = user("user-a")
    source = clip(store, name=name)
    held = artifacts(store, source, jsonl=jsonl, zipped=zipped)
    clip_made(executor, lines=1)
    queue = queue_for(settings, executor, store, metrics)
    await prepare_clip(queue, me, source, held)

    (req,) = executor.requests
    assert req.input_files == [rec.relative_path for rec in held]
    assert req.args["media"] == {
        "nextext_jsonl": held[0].relative_path if jsonl else None,
        "nextext_zip": held[-1].relative_path if zipped else None,
        "video": video,
    }


async def test_an_artifact_of_another_source_is_refused(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """Another clip's artifact, a row that is no artifact, a changed record or two of a role: a programming error.

    Nothing more is sent, no job row is made, and no turn is left held.
    """
    me = user("user-a")
    source, other = clip(store), clip(store, name="quay-tour.mp4")
    jsonl, zipped = artifacts(store, source)
    (foreign,) = artifacts(store, other, zipped=False)
    clip_made(executor, frame(1, 0.0))
    queue = queue_for(settings, executor, store, metrics)
    _, _, kept = await prepare_clip(queue, me, source, [jsonl, zipped])
    a_frame = next(rec for rec in kept if rec.role == "frame")
    before = (len(executor.requests), jobs_recorded(settings))

    for wrong in (
        [foreign, zipped],
        [jsonl, a_frame],
        [dataclasses.replace(jsonl, name="harbour-tour.mp4")],
        [dataclasses.replace(jsonl, mime="application/zip")],
        [dataclasses.replace(zipped, chat_id="chat-2")],
        [dataclasses.replace(zipped, role="nextext_jsonl")],
        [jsonl, jsonl],
    ):
        with pytest.raises(ValueError, match="artifacts"):
            await prepare_clip(queue, me, source, wrong)
    assert (len(executor.requests), jobs_recorded(settings), queue.tracked_users) == (*before, 0)


async def test_a_clip_is_never_its_own_input_and_nothing_else_reads_artifacts(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A clip's job without its artifacts, artifacts beside a converted PDF or for a file that is no clip: refused.

    Each is a programming error: nothing is sent, no job row is made, and the clip's bytes are never copied to a slot.
    """
    me = user("user-a")
    source = clip(store)
    held = artifacts(store, source)
    deck_source = deck(store)
    converts(executor)
    queue = queue_for(settings, executor, store, metrics)
    _, _, (pdf,) = await convert(queue, me, deck_source)
    before = (len(executor.requests), jobs_recorded(settings))

    with pytest.raises(ValueError, match="artifacts"):
        await queue.submit_renders(me, source, {"pages": None}, KeepAll(), call=CALL, gate="prepare")
    with pytest.raises(ValueError, match="artifacts"):
        await queue.submit_renders(me, deck_source, {}, KeepAll(), call=CALL, converted=pdf, artifacts=held)
    with pytest.raises(ValueError, match="artifacts"):
        await queue.submit_renders(me, deck_source, {}, KeepAll(), call=CALL, artifacts=held)
    assert (len(executor.requests), jobs_recorded(settings), queue.tracked_users) == (*before, 0)


async def test_a_frame_keeps_its_time(settings: Settings, store: Store, executor: FakeClient, metrics: Metrics) -> None:
    """A frame is a ``frame`` row of the clip with its time in ``meta``, and none where the zip gave none."""
    me = user("user-a")
    source = clip(store)
    held = artifacts(store, source, jsonl=False)
    clip_made(executor, frame(1, 0.0), frame(2, 4.0), frame(3), lines=None)
    executor.answer = {**(executor.answer or {}), "total": 3}
    queue = queue_for(settings, executor, store, metrics)
    _, result, kept = await prepare_clip(queue, me, source, held)

    frames = [rec for rec in store.derived_of(source) if rec.role == "frame"]
    assert [(rec.ordinal, rec.mime, rec.width, rec.height, rec.size) for rec in frames] == [
        (n, "image/jpeg", 128, 72, len(JPEG)) for n in (1, 2, 3)
    ]
    assert [rec.meta.get("time_s") for rec in frames] == [0.0, 4.0, None]
    assert frames[1].meta["time_s"] == 4.0
    assert {rec.id for rec in kept} == {rec.id for rec in frames}
    assert result.message == ""
    assert [rec.role for rec in store.derived_of(source)].count("nextext_zip") == 1  # the artifact is not touched


async def test_the_facts_ride_on_the_transcript_else_on_frame_one(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """With a transcript the facts are on it, with its line count; without one they are on frame 1 alone."""
    me = user("user-a")
    queue = queue_for(settings, executor, store, metrics)
    spoken = clip(store)
    clip_made(executor, frame(1, 0.0), frame(2, 4.0), lines=3)
    await prepare_clip(queue, me, spoken, artifacts(store, spoken))
    transcript = next(rec for rec in store.derived_of(spoken) if rec.role == "transcript")
    assert (transcript.ordinal, transcript.mime, transcript.width, transcript.height) == (0, "text/plain", None, None)
    assert transcript.relative_path == f".derived/{spoken.id}/{transcript.id}.txt"
    assert dict(transcript.meta) == {"lines": 3, "facts": {**CLIP_FACTS, "total": 2}}
    assert all("facts" not in rec.meta for rec in store.derived_of(spoken) if rec.role == "frame")

    silent = clip(store, name="quay-tour.mp4")
    clip_made(executor, frame(1, 0.0), frame(2, 4.0), lines=None)
    await prepare_clip(queue, me, silent, artifacts(store, silent, jsonl=False))
    rows = {(rec.role, rec.ordinal): rec for rec in store.derived_of(silent)}
    assert ("transcript", 0) not in rows
    assert rows["frame", 1].meta["facts"] == {**CLIP_FACTS, "total": 2}
    assert "facts" not in rows["frame", 2].meta


async def test_a_job_that_did_not_look_for_facts_stores_none_on_a_clip(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """``facts: False`` keeps no facts on the transcript either."""
    me = user("user-a")
    source = clip(store)
    held = artifacts(store, source)
    clip_made(executor, frame(1, 0.0))
    queue = queue_for(settings, executor, store, metrics)
    await queue.submit_renders(me, source, {"facts": False}, KeepAll(), call=CALL, gate="prepare", artifacts=held)
    assert [dict(rec.meta) for rec in store.derived_of(source) if rec.role == "transcript"] == [{"lines": 3}]


async def test_a_transcript_over_its_cap_is_not_kept(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """One byte past ``TRANSCRIPT_MAX_BYTES`` is lost as invalid, named again by its id; the frames are kept."""
    me = user("user-a")
    source = clip(store)
    held = artifacts(store, source)
    clip_made(executor, frame(1, 0.0), transcript=b"x" * (TRANSCRIPT_MAX_BYTES + 1))
    keeper = KeepAll()
    queue = queue_for(settings, executor, store, metrics)
    _, result, kept = await prepare_clip(queue, me, source, held, keeper)

    ((_, planned, _),) = keeper.recorded
    assert keeper.again_ids == [[planned[list(executor.files).index(TRANSCRIPT_NAME)]]]
    assert result.message == "1 output could not be kept (invalid)"
    assert [rec.role for rec in kept] == ["frame"]
    assert [rec.role for rec in store.derived_of(source)].count("transcript") == 0


async def test_a_transcript_of_exactly_its_cap_is_kept(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """The cap is the most a transcript may have."""
    me = user("user-a")
    source = clip(store)
    held = artifacts(store, source)
    clip_made(executor, frame(1, 0.0), transcript=b"x" * TRANSCRIPT_MAX_BYTES)
    queue = queue_for(settings, executor, store, metrics)
    _, result, _ = await prepare_clip(queue, me, source, held)
    (transcript,) = (rec for rec in store.derived_of(source) if rec.role == "transcript")
    assert (transcript.size, result.message) == (TRANSCRIPT_MAX_BYTES, "")


@pytest.mark.parametrize(
    "case",
    [
        "no-transcript-in-the-answer",
        "a-transcript-of-a-picture",
        "a-frame-of-a-pdf",
        "a-page-of-a-clip",
        "a-pdfs-facts-for-a-clip",
        "a-clips-facts-for-a-pdf",
    ],
)
async def test_what_is_not_a_clips_is_not_kept_as_a_clips(
    case: str, settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A ``transcript.txt`` the answer does not name, a transcript, a frame or facts that are not the source's kind.

    A transcript of a PDF, a frame of a PDF, a page of a clip, a PDF's facts for a clip and a clip's for a PDF: each is
    lost as invalid and named again, and the facts of a clip that are not a clip's are never stored.
    """
    me = user("user-a")
    queue = queue_for(settings, executor, store, metrics)
    keeper = KeepAll()
    if case in ("no-transcript-in-the-answer", "a-page-of-a-clip", "a-pdfs-facts-for-a-clip"):
        source = clip(store)
        held = artifacts(store, source)
        clip_made(executor, frame(1, 0.0), lines=None)
        if case == "no-transcript-in-the-answer":
            executor.files[TRANSCRIPT_NAME] = b"[00:00] Hello\n"
        elif case == "a-page-of-a-clip":
            executor.answer = {**CLIP_FACTS, "total": 1, "renders": [page(1)]}
            executor.files = {page(1)["name"]: JPEG}
        else:
            executor.answer = {**FACTS, "renders": [frame(1, 0.0)]}
        _, result, kept = await prepare_clip(queue, me, source, held, keeper)
        assert [rec.role for rec in kept] == (["frame"] if case == "no-transcript-in-the-answer" else [])
    else:
        source = scan(store)
        entries = [frame(1, 0.0)] if case in ("a-frame-of-a-pdf", "a-clips-facts-for-a-pdf") else [page(1)]
        executor.answer = {**(CLIP_FACTS if case == "a-clips-facts-for-a-pdf" else FACTS), "renders": entries}
        executor.files = {entry["name"]: JPEG for entry in entries}
        if case == "a-transcript-of-a-picture":
            executor.answer = {**FACTS, "renders": entries, "transcript": {"name": TRANSCRIPT_NAME, "lines": 1}}
            executor.files[TRANSCRIPT_NAME] = b"[00:00] Hello\n"
        _, result, kept = await prepare(queue, me, source, keeper)
        assert [rec.role for rec in kept] == (["page"] if case == "a-transcript-of-a-picture" else [])
    assert "could not be kept (invalid)" in result.message
    assert [rec.role for rec in store.derived_of(source)].count("transcript") == 0


async def test_what_a_clips_job_kept_is_removed_when_its_record_fails_and_the_artifacts_stay(
    settings: Settings, store: Store, executor: FakeClient, metrics: Metrics
) -> None:
    """A frame kept before the transcript is lost, and an event of that loss that cannot be written: no row stays.

    The frame's row and file go; the two artifacts, which the job only read, are never touched.
    """
    me = user("user-a")
    source = clip(store)
    held = artifacts(store, source)
    clip_made(executor, frame(1, 0.0), transcript=b"x" * (TRANSCRIPT_MAX_BYTES + 1))

    class NoAgain(KeepAll):
        @override
        def again(self, ids: Sequence[str]) -> None:
            raise AuditWriteError("synthetic audit stream failure")

    queue = queue_for(settings, executor, store, metrics)
    with pytest.raises(AuditWriteError):
        await prepare_clip(queue, me, source, held, NoAgain())

    assert [rec.role for rec in store.derived_of(source)] == ["nextext_jsonl", "nextext_zip"]
    folder = _chat_folder(settings, me) / ".derived" / source.id
    assert sorted(each.name for each in folder.iterdir()) == sorted(rec.name for rec in held)
