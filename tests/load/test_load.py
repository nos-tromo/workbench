"""The load test: ten synthetic users send three ``run_python`` jobs each, all at once, to the running test stack.

Manual and not in CI (``make test-load``, on the isolation tests' stack, never on a developer's). It measures what the
queue and the executor do under that load, apart from any model: how long a job waits for its slot and how long it
then holds it, how many jobs run at once, what is refused, and how much memory the executor uses against its limit.
Each job holds 200 MB for 2 s, well inside the stack's 20 s wall time. Half the users come through Ghostwriter's door,
half through docint's: each caller has slots of its own, fewer than the executor's, and the two together fill them.

Three clocks are in play. The test's own times its calls. The api records when it admitted a job (``created_at`` of
the job's record), and the job prints when its code began and ended; both are the containers' clock, which is set
against the test's once, as a ``docker exec`` sees it. Queue wait is admission less the call; job duration is the
answer less the admission; the most jobs running at once is read from the jobs' own times.
"""

import json
import re
import statistics
import subprocess
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import pytest

from tests.isolation.conftest import (
    api_info,
    container_of,
    docker,
    environment,
    executor,
    ghostwriter_mcp,
    inspect,
    mcp_call,
)

__all__ = ["api_info", "executor"]  # fixtures of the test stack, used below by name

pytestmark = pytest.mark.load

USERS: Final = 10
JOBS_EACH: Final = 3
HOLD_MB: Final = 200
HOLD_S: Final = 2

CODE: Final = f"""
import time
began = time.time()
held = b"\\x01" * ({HOLD_MB} * 1024 * 1024)
time.sleep({HOLD_S})
print(began, time.time(), len(held))
"""

#: Why a call may be refused as busy, and what the caller is told then.
BUSY_REASONS: Final = frozenset({"queue_full", "wait_timeout", "executor_busy"})
BUSY: Final = "busy, retry later"
_UNITS: Final = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
    "kB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
}
_USAGE: Final = re.compile(r"([0-9.]+)\s*([A-Za-z]+)\s*/")

#: Seconds to wait for ``docker stats`` to send its first sample before the load starts.
FIRST_SAMPLE_S: Final = 30.0


@dataclass
class Call:
    """One call of the test and what came of it.

    Attributes:
        user: The synthetic user.
        sent: When the test sent it, by the test's clock.
        answered: When the answer came, by the test's clock.
        status: The answer's status.
        body: The answer's JSON: a JSON-RPC answer of the MCP door.
    """

    user: str
    sent: float
    answered: float
    status: int
    body: dict[str, Any]


@dataclass
class Samples:
    """Memory of the executor, as ``docker stats`` reports it.

    Attributes:
        at: When each sample came, by the test's clock.
        used: Its memory use, in bytes.
    """

    at: list[float] = field(default_factory=list)
    used: list[int] = field(default_factory=list)


def _bytes(text: str) -> int | None:
    """Read the use out of a ``docker stats`` ``MemUsage`` cell, such as ``812.4MiB / 6.5GiB``."""
    found = _USAGE.search(text)
    if found is None or found.group(2) not in _UNITS:
        return None
    return int(float(found.group(1)) * _UNITS[found.group(2)])


@contextmanager
def sampled(container: str) -> Iterator[Samples]:
    """Stream ``docker stats`` of a container while the block runs; it sends a sample about every half second.

    The load starts only once the first sample is in. One that does not come within :data:`FIRST_SAMPLE_S`, or a
    ``docker stats`` that ends first, fails the test with the reason instead of waiting for ever.
    """
    samples = Samples()
    stream = subprocess.Popen(
        ["docker", "stats", "--format", "{{.MemUsage}}", container], stdout=subprocess.PIPE, text=True
    )
    assert stream.stdout is not None
    lines = stream.stdout

    def read() -> None:
        for line in lines:
            used = _bytes(line)
            if used is not None:
                samples.at.append(time.time())
                samples.used.append(used)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + FIRST_SAMPLE_S
        while not samples.used:  # the first sample is there before the load starts
            if not reader.is_alive():
                pytest.fail(f"docker stats of the executor ended (status {stream.poll()}) before its first sample")
            if time.monotonic() > deadline:
                pytest.fail(f"docker stats sent no sample of the executor within {FIRST_SAMPLE_S:.0f} s")
            time.sleep(0.1)
        yield samples
        time.sleep(1.5)  # and one more after it ends
    finally:
        stream.terminate()
        stream.wait(timeout=30)
        reader.join(timeout=30)


def clock_offset(container: str) -> tuple[float, float]:
    """Set the containers' clock against the test's: the tightest of five readings through ``docker exec``.

    Returns:
        The containers' clock less the test's, and the bound on its error, in seconds.
    """
    readings = []
    for _ in range(5):
        before = time.time()
        theirs = float(docker("exec", container, "python", "-c", "import time; print(repr(time.time()))"))
        after = time.time()
        readings.append((after - before, theirs - (before + after) / 2))
    spread, offset = min(readings)
    return offset, spread / 2


def admitted(container: str, job_ids: list[str]) -> dict[str, float]:
    """Read when the api admitted each job: its record's ``created_at``, from the database, read-only."""
    script = (
        "import json, sqlite3, sys\n"
        "db = sqlite3.connect('file:/data/workbench.db?mode=ro', uri=True)\n"
        "ids = json.loads(sys.argv[1])\n"
        "rows = db.execute(f\"SELECT id, created_at FROM jobs WHERE id IN ({','.join('?' * len(ids))})\", ids)\n"
        "print(json.dumps(dict(rows.fetchall())))\n"
    )
    return json.loads(docker("exec", container, "python", "-c", script, json.dumps(job_ids)))


def percentiles(values: list[float]) -> tuple[float, float]:
    """The median and the 95th percentile of the values."""
    cuts = statistics.quantiles(values, n=100, method="inclusive")
    return cuts[49], cuts[94]


def most_at_once(spans: list[tuple[float, float]]) -> int:
    """The most spans that cover one moment; a span that ends as another begins does not overlap it."""
    edges = sorted([(begin, 1) for begin, _ in spans] + [(end, -1) for _, end in spans], key=lambda e: (e[0], e[1]))
    running = peak = 0
    for _, step in edges:
        running += step
        peak = max(peak, running)
    return peak


def _ran(c: Call) -> bool:
    """Whether the call's job ran: a result that is no error."""
    return c.status == 200 and c.body.get("result", {}).get("isError") is False


def _busy(c: Call) -> bool:
    """Whether the call was refused as busy: an error result with a busy reason and the busy text."""
    found = c.body.get("result", {})
    told = [block.get("text") for block in found.get("content", [])]
    return (
        c.status == 200
        and found.get("isError") is True
        and found["structuredContent"]["reason"] in BUSY_REASONS
        and told == [BUSY]
    )


def test_ten_users_at_once_are_queued_refused_and_bounded(
    api_info: dict[str, Any], executor: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """At most ``executor_slots`` jobs run at once, every refusal is "busy", the executor stays below its memory.

    Each caller runs at most its own slots; both callers together fill the executor's.
    """
    settings = environment(api_info)
    slots = int(settings["WORKBENCH_EXECUTOR_SLOTS"])
    callers_slots = int(settings["GHOSTWRITER_WORKBENCH_SLOTS"]) + int(settings["DOCINT_WORKBENCH_SLOTS"])
    limit = int(inspect(executor)["HostConfig"]["Memory"])
    api_container = container_of("api")
    offset, error = clock_offset(api_container)
    users = [f"load-user-{n:02d}" for n in range(1, USERS + 1)]
    through_docint = set(users[USERS // 2 :])

    def call(user: str) -> Call:
        sent = time.time()
        if user in through_docint:
            r = mcp_call(api_info, CODE, [], user=user)
        else:
            r = ghostwriter_mcp(api_info, "run_python", {"code": CODE}, chat="load-chat", user=user)
        answered = time.time()
        try:
            body = r.json()
        except ValueError:
            body = {"not json": r.text[:200]}
        return Call(user, sent, answered, r.status_code, body)

    with sampled(executor) as memory, ThreadPoolExecutor(max_workers=USERS * JOBS_EACH) as pool:
        began = time.time()
        calls = list(pool.map(call, [user for user in users for _ in range(JOBS_EACH)]))
        took = time.time() - began

    done = [c for c in calls if _ran(c)]
    refused = [c for c in calls if _busy(c)]
    assert [(c.user, c.status, c.body) for c in calls if c not in done and c not in refused] == []
    ran = {c.body["result"]["structuredContent"]["job_id"]: c for c in done}
    when = admitted(api_container, list(ran))
    assert set(when) == set(ran)
    waits = [when[job] - offset - c.sent for job, c in ran.items()]
    holds = [c.answered - (when[job] - offset) for job, c in ran.items()]
    spans = []
    for c in done:
        start, end, size = c.body["result"]["structuredContent"]["stdout"].split()
        assert int(size) == HOLD_MB * 1024 * 1024
        spans.append((float(start), float(end)))
    at_once = most_at_once(spans)
    gaps = [later - earlier for earlier, later in zip(memory.at, memory.at[1:], strict=False)]
    peak_sampled = max(memory.used)
    cgroup_peak = int(docker("exec", executor, "cat", "/sys/fs/cgroup/memory.peak"))

    wait_p50, wait_p95 = percentiles(waits)
    hold_p50, hold_p95 = percentiles(holds)
    mib = 1024 * 1024
    with capsys.disabled():
        print(
            f"\nload: {USERS} users x {JOBS_EACH} run_python jobs, all at once ({HOLD_MB} MB held {HOLD_S} s each); "
            f"{slots} slots, queue {settings['WORKBENCH_QUEUE_MAX']}, {settings['WORKBENCH_JOBS_PER_USER']} job a user"
            f"\n  {len(done)} ran, {len(refused)} refused busy, 0 other; all answered in {took:.1f} s"
            f"\n  queue wait   p50 {wait_p50:.2f} s   p95 {wait_p95:.2f} s"
            f"\n  job duration p50 {hold_p50:.2f} s   p95 {hold_p95:.2f} s   (admission to answer)"
            f"\n  most jobs running at once: {at_once} (slots: {slots}, the two callers' together: {callers_slots})"
            f"\n  executor memory: sampled peak {peak_sampled / mib:.0f} MiB, cgroup peak {cgroup_peak / mib:.0f} MiB,"
            f" limit {limit / mib:.0f} MiB ({len(memory.used)} samples, largest gap {max(gaps):.2f} s)"
            f"\n  clock offset to the containers {offset * 1000:+.1f} ms (within {error * 1000:.1f} ms)"
        )
    assert at_once == min(slots, callers_slots)
    assert max(gaps) <= 1.0
    assert peak_sampled < limit
    assert cgroup_peak < limit
