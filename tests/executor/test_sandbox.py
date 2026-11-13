"""The slot sandbox: real child processes under rlimits, a wall clock and a clean environment."""

import json
import os
import resource
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from workbench.executor.sandbox import Slot, clean_env, preexec, run_in_slot
from workbench.protocol import Limits

pytestmark = pytest.mark.anyio

LINUX = sys.platform == "linux"
MB = 1024 * 1024
# A job may fork here: RLIMIT_NPROC counts every process of the test runner's uid, not only the job's.
ROOMY = {"nproc": 2000}


def _limits(**overrides: int) -> Limits:
    """Limits generous enough for a plain interpreter, with ``overrides`` applied."""
    values = {"wall_s": 30, "cpu_s": 30, "memory_mb": 1024, "file_mb": 50, **ROOMY, **overrides}
    return Limits(**values)


async def _run(code: str, cwd: Path, limits: Limits | None = None) -> tuple[str, int]:
    """Run ``code`` in an isolated interpreter in ``cwd`` with no slot."""
    return await run_in_slot([sys.executable, "-I", "-c", code], cwd, None, limits or _limits())


def _alive(pid: int) -> bool:
    """Whether ``pid`` is a running process; a zombie waiting to be reaped is not running."""
    try:
        os.kill(pid, 0)
        if LINUX:
            state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
        else:
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    except (ProcessLookupError, FileNotFoundError):
        return False
    return bool(state) and not state.startswith("Z")


def _wait_gone(pid: int, seconds: float = 5.0) -> bool:
    """Poll until ``pid`` has stopped running; ``False`` if it still runs after ``seconds``."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


async def test_a_clean_exit_is_ok(tmp_path: Path) -> None:
    """Exit code 0 is ``ok``."""
    assert await _run("pass", tmp_path) == ("ok", 0)


async def test_a_failing_exit_is_an_error(tmp_path: Path) -> None:
    """A non-zero exit code is an ``error`` and keeps its code."""
    assert await _run("raise SystemExit(3)", tmp_path) == ("error", 3)


async def test_the_job_runs_in_the_given_cwd_with_no_stdin(tmp_path: Path) -> None:
    """The child starts in ``cwd`` and cannot read from the supervisor's stdin."""
    code = "import os, sys; open('seen.txt', 'w').write(os.getcwd() + '|' + repr(sys.stdin.read()))"
    assert await _run(code, tmp_path) == ("ok", 0)
    cwd, stdin = (tmp_path / "seen.txt").read_text().split("|")
    assert Path(cwd).resolve() == tmp_path.resolve()
    assert stdin == "''"


async def test_wall_timeout_kills_the_process_group(tmp_path: Path) -> None:
    """A job past its wall time is killed together with the processes it started."""
    code = (
        "import subprocess, time\n"
        "p = subprocess.Popen(['sleep', '60'])\n"
        "open('grandchild.pid', 'w').write(str(p.pid))\n"
        "time.sleep(10)\n"
    )
    began = time.monotonic()
    status, returncode = await _run(code, tmp_path, _limits(wall_s=2))
    elapsed = time.monotonic() - began
    assert status == "timeout"
    assert returncode == -signal.SIGKILL
    assert elapsed < 8
    grandchild = int((tmp_path / "grandchild.pid").read_text())
    assert _wait_gone(grandchild), "the grandchild survived its job's wall timeout"


async def test_processes_left_behind_by_a_finished_job_are_killed(tmp_path: Path) -> None:
    """A job that exits but leaves a process running does not keep it, so a slot never carries one over."""
    code = "import subprocess\np = subprocess.Popen(['sleep', '60'])\nopen('straggler.pid', 'w').write(str(p.pid))\n"
    assert await _run(code, tmp_path) == ("ok", 0)
    assert _wait_gone(int((tmp_path / "straggler.pid").read_text())), "a process outlived its job"


async def test_a_job_is_its_own_session_and_process_group_leader(tmp_path: Path) -> None:
    """``setsid`` happens in the child, so the group the wall timeout kills is exactly the job's."""
    code = "import json, os\njson.dump([os.getpid(), os.getsid(0), os.getpgrp()], open('ids.json', 'w'))\n"
    assert await _run(code, tmp_path) == ("ok", 0)
    pid, sid, pgid = json.loads((tmp_path / "ids.json").read_text())
    assert sid == pid
    assert pgid == pid
    assert pid != os.getpgrp()


async def test_a_cpu_hog_is_killed(tmp_path: Path) -> None:
    """A job that burns CPU past ``cpu_s`` is stopped by the kernel and reported as ``killed``."""
    status, returncode = await _run("while True:\n    pass\n", tmp_path, _limits(cpu_s=1, wall_s=20))
    assert status == "killed"
    assert returncode in (-signal.SIGXCPU, -signal.SIGKILL)


async def test_death_by_sigkill_is_killed_and_other_signals_are_errors(tmp_path: Path) -> None:
    """``SIGKILL`` means ``killed``; any other fatal signal is an ordinary ``error``."""
    kill = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    abort = "import os, signal; os.kill(os.getpid(), signal.SIGABRT)"
    assert await _run(kill, tmp_path) == ("killed", -signal.SIGKILL)
    assert await _run(abort, tmp_path) == ("error", -signal.SIGABRT)


async def test_the_file_size_limit_stops_a_big_write(tmp_path: Path) -> None:
    """A job cannot write a file bigger than ``file_mb``."""
    code = "open('big.bin', 'wb').write(b'x' * (4 * 1024 * 1024))"
    status, _ = await _run(code, tmp_path, _limits(file_mb=1))
    assert status == "error"
    assert (tmp_path / "big.bin").stat().st_size <= MB


async def test_rlimits_reach_the_child(tmp_path: Path) -> None:
    """Each limit of the job is set in the child, core dumps are off."""
    code = (
        "import json, resource as r\n"
        "names = ['CPU', 'FSIZE', 'NPROC', 'NOFILE', 'CORE', 'AS']\n"
        "json.dump({n: r.getrlimit(getattr(r, 'RLIMIT_' + n)) for n in names}, open('limits.json', 'w'))\n"
    )
    limits = _limits(cpu_s=7, memory_mb=512, file_mb=9, nproc=1500, nofile=128)
    assert await _run(code, tmp_path, limits) == ("ok", 0)
    got = json.loads((tmp_path / "limits.json").read_text())
    assert got["CPU"] == [7, 7]
    assert got["FSIZE"] == [9 * MB, 9 * MB]
    assert got["NPROC"] == [1500, 1500]
    assert got["NOFILE"] == [128, 128]
    assert got["CORE"] == [0, 0]
    if LINUX:
        assert got["AS"] == [512 * MB, 512 * MB]


@pytest.mark.skipif(not LINUX, reason="RLIMIT_AS is not enforced off Linux")
async def test_memory_limit_kills_a_hog(tmp_path: Path) -> None:
    """A job that allocates past ``memory_mb`` fails with a ``MemoryError`` and does not get the memory."""
    code = (
        "try:\n"
        "    bytearray(2 * 1024 ** 3)\n"
        "except MemoryError:\n"
        "    open('oom.txt', 'w').write('MemoryError')\n"
        "    raise\n"
    )
    status, _ = await _run(code, tmp_path, _limits(memory_mb=256))
    assert status in ("killed", "error")
    assert (tmp_path / "oom.txt").read_text() == "MemoryError"


@pytest.mark.skipif(not LINUX or os.geteuid() == 0, reason="RLIMIT_NPROC binds only unprivileged users on Linux")
async def test_process_limit_stops_a_fork_bomb(tmp_path: Path) -> None:
    """A job that is out of processes cannot start another."""
    code = (
        "import subprocess\n"
        "try:\n"
        "    subprocess.run(['true'])\n"
        "except OSError as e:\n"
        "    open('fork.txt', 'w').write(type(e).__name__)\n"
    )
    assert await _run(code, tmp_path, _limits(nproc=1)) == ("ok", 0)
    assert (tmp_path / "fork.txt").read_text() == "BlockingIOError"


@pytest.mark.skipif(not LINUX or os.geteuid() != 0, reason="switching uids needs root on Linux")
async def test_a_slot_job_runs_as_the_slot_with_no_groups_and_no_capabilities(tmp_path: Path) -> None:
    """A job in a slot runs as the slot's ids, belongs to no supplementary group and holds no capability."""
    for folder in (tmp_path, *tmp_path.parents):
        if folder == Path(tempfile.gettempdir()):
            break
        folder.chmod(0o755)
    tmp_path.chmod(0o777)
    code = (
        "import json, os\n"
        "caps = [line for line in open('/proc/self/status') if line.startswith('Cap')]\n"
        "json.dump([os.getuid(), os.geteuid(), os.getgid(), os.getgroups(), caps], open('ids.json', 'w'))\n"
    )
    slot = Slot(index=0, uid=20001, gid=20001)
    assert await run_in_slot([sys.executable, "-I", "-c", code], tmp_path, slot, _limits()) == ("ok", 0)
    uid, euid, gid, groups, caps = json.loads((tmp_path / "ids.json").read_text())
    assert (uid, euid, gid, groups) == (20001, 20001, 20001, [])
    assert all(int(line.split()[1], 16) == 0 for line in caps if line.startswith(("CapEff", "CapPrm")))


async def test_child_environment_is_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing of the supervisor's environment, its keys and tokens included, reaches the child."""
    monkeypatch.setenv("WORKBENCH_DATA_DIR", "/data")
    monkeypatch.setenv("OPEN_WEBUI_WORKBENCH_KEY", "k" * 32)
    monkeypatch.setenv("PYTHONPATH", "/somewhere")
    monkeypatch.setenv("AUDIT_OUTPUT", "container")
    code = "import json, os; json.dump(dict(os.environ), open('env.json', 'w'))"
    assert await _run(code, tmp_path) == ("ok", 0)
    seen: dict[str, str] = json.loads((tmp_path / "env.json").read_text())
    seen.pop("__CF_USER_TEXT_ENCODING", None)  # macOS adds it to every process itself; it is not inherited
    assert seen == clean_env(tmp_path)


def test_clean_env_holds_the_documented_variables() -> None:
    """The child's environment is these seven variables and nothing else; ``HOME`` is the job directory."""
    env = clean_env(Path("job-dir"))
    assert set(env) == {
        "PATH",
        "HOME",
        "LANG",
        "MPLBACKEND",
        "PYTHONDONTWRITEBYTECODE",
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
    }
    assert env["HOME"] == "job-dir"
    assert env["LANG"] == "C.UTF-8"
    assert env["MPLBACKEND"] == "Agg"
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"
    assert (env["OPENBLAS_NUM_THREADS"], env["OMP_NUM_THREADS"]) == ("1", "1")


def test_preexec_drops_to_the_slot_before_it_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The child leaves its session, drops groups and ids, then lowers every limit; ids come last of the four."""
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(os, "setsid", lambda: calls.append(("setsid",)))
    monkeypatch.setattr(os, "setgroups", lambda groups: calls.append(("setgroups", list(groups))))
    monkeypatch.setattr(os, "setgid", lambda gid: calls.append(("setgid", gid)))
    monkeypatch.setattr(os, "setuid", lambda uid: calls.append(("setuid", uid)))
    monkeypatch.setattr(resource, "setrlimit", lambda which, pair: calls.append(("rlimit", which, pair)))
    limits = Limits(wall_s=9, cpu_s=7, memory_mb=512, file_mb=9, nproc=100, nofile=128)

    preexec(Slot(index=2, uid=20003, gid=20003), limits)()

    assert calls[:4] == [("setsid",), ("setgroups", []), ("setgid", 20003), ("setuid", 20003)]
    expected = {
        resource.RLIMIT_CPU: 7,
        resource.RLIMIT_FSIZE: 9 * MB,
        resource.RLIMIT_NPROC: 100,
        resource.RLIMIT_NOFILE: 128,
        resource.RLIMIT_CORE: 0,
    }
    if LINUX:
        expected[resource.RLIMIT_AS] = 512 * MB
    assert {c[1]: c[2] for c in calls[4:]} == {which: (value, value) for which, value in expected.items()}
    assert len(calls) == 4 + len(expected)


def test_preexec_without_a_slot_keeps_the_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no slot the child still gets its own session and limits, but no id is changed."""
    calls: list[str] = []
    monkeypatch.setattr(os, "setsid", lambda: calls.append("setsid"))
    monkeypatch.setattr(os, "setgroups", lambda groups: calls.append("setgroups"))
    monkeypatch.setattr(os, "setgid", lambda gid: calls.append("setgid"))
    monkeypatch.setattr(os, "setuid", lambda uid: calls.append("setuid"))
    monkeypatch.setattr(resource, "setrlimit", lambda which, pair: calls.append("rlimit"))

    preexec(None, _limits())()

    assert calls[0] == "setsid"
    assert set(calls[1:]) == {"rlimit"}


async def test_a_full_job_runs_through_the_sandbox(tmp_path: Path) -> None:
    """The real entry point starts under the sandbox's environment and limits and leaves its result."""
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"tool": "echo", "args": {"x": 1}, "chat_id": "chat-1", "max_chars": 100, "file_mb": 5}))
    argv = [sys.executable, "-I", "-m", "workbench.executor.job", str(spec)]
    assert await run_in_slot(argv, tmp_path, None, _limits()) == ("ok", 0)
    assert json.loads((tmp_path / "result.json").read_text()) == {"x": 1}
