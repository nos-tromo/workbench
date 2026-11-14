"""The slot sandbox: real child processes under rlimits, a wall clock and a clean environment."""

import ast
import json
import os
import resource
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from workbench.executor import sandbox
from workbench.executor.sandbox import Slot, clean_env, kill_slot_processes, preexec, run_in_slot
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


def _stat_state(stat_line: bytes) -> str:
    """The state letter of a ``/proc/<pid>/stat`` line; the command name may hold spaces, ``)`` or any byte."""
    return stat_line.rpartition(b")")[2].split()[0].decode("ascii")


def _state(pid: int) -> str | None:
    """The process state letter of ``pid`` (``R``, ``S``, ``Z`` for a zombie, ...), or ``None`` once it is gone."""
    try:
        if LINUX:
            return _stat_state(Path(f"/proc/{pid}/stat").read_bytes())
        ps = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    return ps.stdout.strip()[:1] or None


def _alive(pid: int) -> bool:
    """Whether ``pid`` is running; a zombie, which only waits to be reaped, is not."""
    return _state(pid) not in (None, "Z", "X")


def _open_to_slot(folder: Path) -> None:
    """Let a slot uid enter and write ``folder``, as the supervisor will for a job directory."""
    for parent in (folder, *folder.parents):
        if parent == Path(tempfile.gettempdir()):
            break
        parent.chmod(0o755)
    folder.chmod(0o777)


def _live_tasks_of(uid: int) -> list[str]:
    """``pid/tid`` of every running (not zombie, not dead) task whose process has real uid ``uid``.

    Looks at each thread, since the leader of a process can be a zombie while its other threads run.
    Reads bytes: a process can name itself with any byte, and that must not stop the check.
    """
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            lines = (entry / "status").read_bytes().split(b"\n")
            real_uid = next(int(line.split()[1]) for line in lines if line.startswith(b"Uid:"))
            if real_uid != uid:
                continue
            for task in (entry / "task").iterdir():
                if _stat_state((task / "stat").read_bytes()) not in ("Z", "X"):
                    found.append(f"{entry.name}/{task.name}")
        except (FileNotFoundError, ProcessLookupError):
            continue
    return found


def _cpus() -> set[int]:
    """The CPUs this process may run on; off Linux, as many as the host reports."""
    if sys.platform == "linux":
        return os.sched_getaffinity(0)
    return set(range(os.cpu_count() or 1))


def _pin(cpus: set[int]) -> None:
    """Keep the calling thread, and the threads and processes it starts from now on, on ``cpus`` (Linux only)."""
    if sys.platform == "linux":
        os.sched_setaffinity(0, cpus)


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


async def test_processes_left_in_the_group_by_a_finished_job_are_killed(tmp_path: Path) -> None:
    """A job that exits but leaves a process in its group running does not keep it."""
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
    _open_to_slot(tmp_path)
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


@pytest.mark.skipif(not LINUX or os.geteuid() != 0, reason="switching uids needs root on Linux")
async def test_a_process_that_left_the_group_dies_with_the_slot_cleanup(tmp_path: Path) -> None:
    """A descendant in a session of its own survives the group kill, but not the end of a slot job."""
    _open_to_slot(tmp_path)
    code = (
        "import subprocess\n"
        "p = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
        "open('escaped.pid', 'w').write(str(p.pid))\n"
    )
    slot = Slot(index=0, uid=20001, gid=20001)
    assert await run_in_slot([sys.executable, "-I", "-c", code], tmp_path, slot, _limits()) == ("ok", 0)
    escaped = int((tmp_path / "escaped.pid").read_text())
    assert not _alive(escaped), "a process escaped the slot cleanup"
    assert _live_tasks_of(20001) == []


@pytest.mark.skipif(not LINUX or os.geteuid() != 0, reason="switching uids needs root on Linux")
async def test_slot_cleanup_also_runs_after_a_wall_timeout(tmp_path: Path) -> None:
    """The cleanup does not depend on the job ending by itself."""
    _open_to_slot(tmp_path)
    code = (
        "import subprocess, time\n"
        "p = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
        "open('escaped.pid', 'w').write(str(p.pid))\n"
        "time.sleep(30)\n"
    )
    slot = Slot(index=1, uid=20002, gid=20002)
    status, _ = await run_in_slot([sys.executable, "-I", "-c", code], tmp_path, slot, _limits(wall_s=2))
    assert status == "timeout"
    assert not _alive(int((tmp_path / "escaped.pid").read_text()))
    assert _live_tasks_of(20002) == []


@pytest.mark.skipif(not LINUX or os.geteuid() != 0, reason="switching uids needs root on Linux")
async def test_a_zombie_leader_with_a_running_thread_does_not_survive_the_slot_cleanup(tmp_path: Path) -> None:
    """A process whose main thread has exited keeps running through its other thread; the cleanup kills it."""
    _open_to_slot(tmp_path)
    grandchild = (
        "import ctypes, threading, time\n"
        "threading.Thread(target=time.sleep, args=(60,)).start()\n"
        "ctypes.CDLL(None).pthread_exit(None)\n"
    )
    code = (
        "import subprocess, sys, time\n"
        f"p = subprocess.Popen([sys.executable, '-I', '-c', {grandchild!r}], start_new_session=True)\n"
        "open('zleader.pid', 'w').write(str(p.pid))\n"
        "for _ in range(100):\n"
        "    if 'State:\\tZ' in open(f'/proc/{p.pid}/status').read():\n"
        "        break\n"
        "    time.sleep(0.05)\n"
        "else:\n"
        "    raise SystemExit(7)\n"
    )
    slot = Slot(index=5, uid=20006, gid=20006)
    assert await run_in_slot([sys.executable, "-I", "-c", code], tmp_path, slot, _limits()) == ("ok", 0)
    assert _live_tasks_of(20006) == []


ATTACKER = """\
import ctypes, signal, threading, time
libc = ctypes.CDLL(None)
libc.kill.argtypes = [ctypes.c_int, ctypes.c_int]
def spin():
    while True:
        libc.kill(-1, int(signal.SIGSTOP))
for _ in range(12):
    threading.Thread(target=spin, daemon=True).start()
time.sleep(3600)
"""


def _cleanup_in_thread(slot: Slot, seconds: float) -> tuple[bool, BaseException | None]:
    """Run ``kill_slot_processes`` in a thread; report whether it finished in ``seconds`` and what it raised."""
    raised: list[BaseException | None] = []

    def target() -> None:
        """Run the cleanup and keep what it raised."""
        try:
            kill_slot_processes(slot)
            raised.append(None)
        except BaseException as exc:
            raised.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    return not thread.is_alive(), raised[0] if raised else None


@pytest.mark.skipif(not LINUX or os.geteuid() != 0, reason="switching uids needs root on Linux")
@pytest.mark.skipif(len(_cpus()) < 2, reason="the attacker needs a CPU the helper is not on to stop it in time")
def test_a_slot_process_that_stops_the_helper_cannot_hang_the_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Slot processes spamming ``SIGSTOP`` at their uid stop the helper; the cleanup still ends, with an error.

    The helper is the slot's uid only for the few microseconds between ``setuid`` and ``kill``. On one
    CPU it runs through them before the attacker is scheduled again, so the two are kept on different
    CPUs: the helper on the first, the attacker's threads on the others.
    """
    slot = Slot(index=0, uid=20001, gid=20001)
    monkeypatch.setattr(sandbox, "_HELPER_TIMEOUT_S", 2.0)
    cpus = _cpus()
    helper_cpu = {min(cpus)}

    def drop() -> None:
        """Move the attacker off the helper's CPU and make it the slot's uid before it execs."""
        os.setsid()
        _pin(cpus - helper_cpu)
        os.setgroups([])
        os.setgid(slot.gid)
        os.setuid(slot.uid)

    reported = False
    _pin(helper_cpu)  # the cleanup thread and its helper inherit it
    try:
        for _ in range(20):
            attacker = subprocess.Popen(
                [sys.executable, "-I", "-c", ATTACKER],
                preexec_fn=drop,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(100):  # until all 12 spinning threads run
                    if b"Threads:\t13" in Path(f"/proc/{attacker.pid}/status").read_bytes():
                        break
                    time.sleep(0.05)
                finished, failure = _cleanup_in_thread(slot, 2.0 + 8.0)
            finally:
                attacker.kill()
                attacker.wait()
            assert finished, "the cleanup hung on a stopped helper"
            if failure is not None:
                assert isinstance(failure, RuntimeError)
                assert str(failure) == "could not clear the processes of slot 0"
                reported = True
                break
    finally:
        _pin(cpus)
    assert reported, "the helper was never stopped, so the timeout path did not run"
    assert _live_tasks_of(20001) == []


def _status(state: str, uid: int, name: bytes = b"python", threads: int = 1) -> bytes:
    """The relevant lines of a ``/proc/<pid>/status`` file."""
    ids = f"{uid}\t{uid}\t{uid}\t{uid}"
    rest = f"\nState:\t{state}\nTgid:\t4242\nUid:\t{ids}\nGid:\t{ids}\nThreads:\t{threads}\n"
    return b"Name:\t" + name + rest.encode()


@pytest.mark.parametrize(
    ("state", "uid", "live"),
    [
        ("R (running)", 20001, True),
        ("S (sleeping)", 20001, True),
        ("D (disk sleep)", 20001, True),
        ("T (stopped)", 20001, True),
        ("t (tracing stop)", 20001, True),
        ("Z (zombie)", 20001, False),
        ("X (dead)", 20001, False),
        ("S (sleeping)", 20002, False),
        ("S (sleeping)", 0, False),
    ],
)
def test_only_running_processes_of_the_slot_uid_count_as_alive(state: str, uid: int, live: bool) -> None:
    """A zombie waits only to be reaped by init, so it never keeps a slot from being reused."""
    assert sandbox._is_live_process_of(_status(state, uid), 20001) is live


def test_a_zombie_leader_counts_as_alive_while_another_thread_runs() -> None:
    """The file shows the leader's state; a zombie leader with a second thread is still a running process."""
    assert sandbox._is_live_process_of(_status("Z (zombie)", 20001, threads=2), 20001) is True
    assert sandbox._is_live_process_of(_status("Z (zombie)", 20001, threads=1), 20001) is False
    assert sandbox._is_live_process_of(_status("X (dead)", 20001, threads=3), 20001) is True
    assert sandbox._is_live_process_of(_status("Z (zombie)", 20002, threads=2), 20001) is False


def test_a_command_name_cannot_pass_for_a_state_or_a_uid() -> None:
    """The name is user-chosen text; only the ``State:`` and ``Uid:`` lines count."""
    assert sandbox._is_live_process_of(_status("Z (zombie)", 20001, b"State: S (sleeping)"), 20001) is False
    assert sandbox._is_live_process_of(_status("S (sleeping)", 20002, b"Uid: 20001"), 20001) is False


@pytest.mark.parametrize(
    "name",
    [b"\xff\xfe", b"\x80abc\xc3", b"caf\xe9", b"\xed\xa0\x80", b"a\rState:\tS (sleeping)", b"a\x0b\x0c\x1c\x85b"],
    ids=["invalid-start", "truncated-utf8", "latin1", "surrogate", "carriage-return", "odd-separators"],
)
def test_a_process_name_in_any_bytes_cannot_stop_or_fool_the_check(name: bytes) -> None:
    """The kernel escapes only newline and backslash in the name, so it can hold bytes that are not UTF-8.

    One job could otherwise make every slot's check fail, by naming itself with such a byte.
    """
    assert sandbox._is_live_process_of(_status("S (sleeping)", 20001, name), 20001) is True
    assert sandbox._is_live_process_of(_status("Z (zombie)", 20001, name), 20001) is False
    assert sandbox._is_live_process_of(_status("S (sleeping)", 20002, name), 20001) is False


@pytest.mark.skipif(not LINUX, reason="needs /proc and prctl")
def test_a_real_process_with_a_non_utf8_name_does_not_break_the_look_at_proc(tmp_path: Path) -> None:
    """Any process can rename itself with ``prctl``; the check of every slot still reads ``/proc`` fine."""
    code = (
        "import ctypes, pathlib, time\n"
        "ctypes.CDLL(None).prctl(15, b'\\xff\\xfe\\x80', 0, 0, 0)\n"
        f"pathlib.Path({str(tmp_path / 'named')!r}).write_text('x')\n"
        "time.sleep(60)\n"
    )
    process = subprocess.Popen([sys.executable, "-I", "-c", code])
    try:
        for _ in range(100):
            if (tmp_path / "named").exists():
                break
            time.sleep(0.05)
        assert b"Name:\t\xff\xfe\x80\n" in Path(f"/proc/{process.pid}/status").read_bytes()
        assert sandbox._slot_has_live_processes(2_000_000_000) is False  # no such uid, so every status is read
    finally:
        process.kill()
        process.wait()


def test_the_tests_tell_a_zombie_from_a_running_process() -> None:
    """``_stat_state`` reads the state after the last ``)``, so a command name cannot confuse it."""
    assert _stat_state(b"4242 (sleep) S 1 4242 4242 0 -1 4194304") == "S"
    assert _stat_state(b"4242 (sleep) Z 1 4242 4242 0 -1 4194304") == "Z"
    assert _stat_state(b"4242 (x) Z y) S 1 4242 4242 0 -1 4194304") == "S"
    assert _stat_state(b"4242 (x) S y) Z 1 4242 4242 0 -1 4194304") == "Z"
    assert _stat_state(b"4242 (\xff\xfe) Z 1 4242 4242 0 -1 4194304") == "Z"


def test_slot_cleanup_does_nothing_without_a_slot_or_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """No slot means nothing to clear; off Linux there is no ``/proc`` to check."""
    monkeypatch.setattr(sandbox, "_kill_as_slot", lambda slot: pytest.fail("signalled"))
    kill_slot_processes(None)
    monkeypatch.setattr(sys, "platform", "darwin")
    kill_slot_processes(Slot(index=0, uid=20001, gid=20001))


@pytest.mark.parametrize(("uid", "gid"), [(0, 0), (0, 20001), (20001, 0)])
def test_slot_cleanup_refuses_root(uid: int, gid: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """``kill(-1)`` as uid 0 would reach every process, so a slot with uid or gid 0 is refused."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_kill_as_slot", lambda slot: pytest.fail("signalled"))
    with pytest.raises(RuntimeError, match="slot 0 must not run as root"):
        kill_slot_processes(Slot(index=0, uid=uid, gid=gid))


@pytest.mark.parametrize(("uid", "gid"), [("0", "20001"), ("20001", "0"), ("-1", "20001"), ("20001", "-5")])
def test_the_helper_itself_refuses_root_and_negative_ids(uid: str, gid: str) -> None:
    """The helper program checks its ids before it drops, so it never runs ``kill(-1)`` as root.

    ``os.kill`` is replaced to exit 9, so a helper missing the check fails the test instead of
    signalling every process of the runner (as root) or failing on ``setgroups`` (exit 1).
    """
    disarmed = "import os\nos.kill = lambda pid, sig: os._exit(9)\n" + sandbox._DROP_AND_KILL
    done = subprocess.run([sys.executable, "-I", "-S", "-c", disarmed, uid, gid], check=False, capture_output=True)
    assert done.returncode == 3, done.stderr


def test_the_helper_imports_only_what_needs_no_compiling() -> None:
    """``os`` is frozen and ``sys`` built in; ``signal`` would compile enum, functools and more at every start."""
    tree = ast.parse(sandbox._DROP_AND_KILL)
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert imported == {"os", "sys"}
    assert not any(isinstance(node, ast.ImportFrom) for node in ast.walk(tree))


def test_slot_cleanup_gives_up_after_bounded_retries_and_names_only_the_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Processes that will not die raise ``RuntimeError`` after six passes, with pauses growing in between."""
    kills: list[int] = []
    pauses: list[float] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_slot_has_live_processes", lambda uid: True)
    monkeypatch.setattr(sandbox, "_kill_as_slot", lambda slot: kills.append(slot.uid))
    monkeypatch.setattr(sandbox.time, "sleep", pauses.append)
    with pytest.raises(RuntimeError) as raised:
        kill_slot_processes(Slot(index=3, uid=20004, gid=20004))
    assert str(raised.value) == "processes of slot 3 survived the cleanup"
    assert len(kills) == 6
    assert pauses == pytest.approx([0.1, 0.2, 0.4, 0.8, 1.6])


def test_slot_cleanup_ends_within_its_documented_bound() -> None:
    """Six kill runs and one IPC run, each killed after 30 s, and 3.1 s of pauses: 213 s at worst, before the looks."""
    pauses = sum(sandbox._SLOT_KILL_PAUSE_S * 2**attempt for attempt in range(sandbox._SLOT_KILL_ATTEMPTS - 1))
    assert sandbox._HELPER_TIMEOUT_S == 30
    assert (sandbox._SLOT_KILL_ATTEMPTS + 1) * sandbox._HELPER_TIMEOUT_S + pauses == pytest.approx(213.1)


def test_slot_cleanup_stops_as_soon_as_nothing_is_left(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each pass signals once and then looks; the cleanup returns on the first clean look at ``/proc``."""
    looks = iter([True, True, False])
    kills: list[int] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_slot_has_live_processes", lambda uid: next(looks))
    monkeypatch.setattr(sandbox, "_kill_as_slot", lambda slot: kills.append(slot.uid))
    monkeypatch.setattr(sandbox, "_slot_has_ipc", lambda uid: False)
    monkeypatch.setattr(sandbox.time, "sleep", lambda seconds: None)
    kill_slot_processes(Slot(index=0, uid=20001, gid=20001))
    assert kills == [20001, 20001, 20001]


def test_slot_cleanup_signals_before_it_looks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a slot that looks empty gets the signal first, since what ``/proc`` shows can be misleading."""
    events: list[str] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_slot_has_live_processes", lambda uid: events.append("look") or False)
    monkeypatch.setattr(sandbox, "_kill_as_slot", lambda slot: events.append("kill"))
    monkeypatch.setattr(sandbox, "_slot_has_ipc", lambda uid: False)
    kill_slot_processes(Slot(index=0, uid=20001, gid=20001))
    assert events == ["kill", "look"]


@pytest.mark.parametrize("error", [OSError("no /proc"), KeyError("Threads"), ValueError("x"), IndexError("y")])
def test_a_slot_that_cannot_be_checked_fails_with_a_runtime_error_naming_only_the_slot(
    error: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever goes wrong reading ``/proc``, the caller sees one failure type and retires the slot."""

    def look(uid: int) -> bool:
        """Fail as a broken ``/proc`` entry does."""
        raise error

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_slot_has_live_processes", look)
    monkeypatch.setattr(sandbox, "_kill_as_slot", lambda slot: None)
    with pytest.raises(RuntimeError) as raised:
        kill_slot_processes(Slot(index=1, uid=20002, gid=20002))
    assert str(raised.value) == "processes of slot 1 could not be checked"
    assert raised.value.__cause__ is error


def test_a_stopped_helper_is_given_up_on_and_the_slot_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """The helper runs under a timeout; its expiry is a ``RuntimeError`` naming only the slot."""
    seen: dict[str, Any] = {}

    def run(*args: Any, **kwargs: Any) -> None:
        """Time out as a stopped helper does, after noting how it was called."""
        seen.update(kwargs, argv=args[0])
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(sandbox.subprocess, "run", run)
    with pytest.raises(RuntimeError) as raised:
        sandbox._kill_as_slot(Slot(index=3, uid=20004, gid=20005))
    assert str(raised.value) == "could not clear the processes of slot 3"
    assert seen["timeout"] == 30, "only finite, and generous: busy jobs in other slots slow the helper down"
    assert "preexec_fn" not in seen, "dropping privileges before the exec puts the helper's start-up beyond the timeout"
    assert seen["argv"][-2:] == ["20004", "20005"]
    assert seen["env"] == dict(sandbox.CLEAN_ENV)


@pytest.mark.parametrize("code", ["pass", "import time; time.sleep(10)"])
async def test_run_in_slot_clears_the_slot_after_the_group_kill(
    code: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slot is cleared once, after the group kill, whether the job exited or timed out."""
    events: list[str] = []
    real_preexec = sandbox.preexec
    real_kill_group = sandbox._kill_group

    def kill_group(pgid: int) -> None:
        """Note the group kill, then do it."""
        events.append("group")
        real_kill_group(pgid)

    monkeypatch.setattr(sandbox, "preexec", lambda slot, limits: real_preexec(None, limits))  # no uid switch
    monkeypatch.setattr(sandbox, "_kill_group", kill_group)
    monkeypatch.setattr(sandbox, "kill_slot_processes", lambda slot: events.append(f"slot {slot and slot.index}"))
    status, _ = await run_in_slot(
        [sys.executable, "-I", "-c", code], tmp_path, Slot(2, 20003, 20003), _limits(wall_s=1)
    )
    assert status == ("ok" if code == "pass" else "timeout")
    assert events == ["group", "slot 2"]


async def test_run_in_slot_does_not_clear_without_a_slot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With no slot there is no uid to clear."""
    monkeypatch.setattr(sandbox, "kill_slot_processes", lambda slot: pytest.fail("cleared"))
    assert await _run("pass", tmp_path) == ("ok", 0)


async def test_a_failed_slot_cleanup_reaches_the_caller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A slot that cannot be cleared must not be reused, so the supervisor has to hear of it."""
    real_preexec = sandbox.preexec

    def refuse(slot: Slot | None) -> None:
        """Fail the way an uncleared slot does."""
        raise RuntimeError("processes of slot 2 survived the cleanup")

    monkeypatch.setattr(sandbox, "preexec", lambda slot, limits: real_preexec(None, limits))
    monkeypatch.setattr(sandbox, "kill_slot_processes", refuse)
    with pytest.raises(RuntimeError, match="slot 2"):
        await run_in_slot([sys.executable, "-I", "-c", "pass"], tmp_path, Slot(2, 20003, 20003), _limits())


async def test_what_a_job_creates_is_private_to_its_uid(tmp_path: Path) -> None:
    """The job runs with umask 077: files are 0600 and directories 0700, whatever the supervisor's umask."""
    code = "import os; open('made.txt', 'w').close(); os.mkdir('made.d')"
    previous = os.umask(0o022)
    try:
        assert await _run(code, tmp_path) == ("ok", 0)
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tmp_path / "made.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "made.d").stat().st_mode) == 0o700


async def test_the_job_gets_its_private_tmp_dir_when_one_is_given(tmp_path: Path) -> None:
    """``TMPDIR``, ``TEMP`` and ``TMP`` point at the job's own temporary directory."""
    private = tmp_path / "tmp"
    private.mkdir()
    code = "import json, os, tempfile; json.dump([dict(os.environ), tempfile.gettempdir()], open('tmp.json', 'w'))"
    argv = [sys.executable, "-I", "-c", code]
    assert await run_in_slot(argv, tmp_path, None, _limits(), tmp_dir=private) == ("ok", 0)
    env, tempdir = json.loads((tmp_path / "tmp.json").read_text())
    assert (env["TMPDIR"], env["TEMP"], env["TMP"]) == (str(private),) * 3
    assert Path(tempdir).resolve() == private.resolve()


def test_clean_env_sets_the_tmp_variables_only_when_asked() -> None:
    """Without a private directory the job gets no temporary-directory variable at all."""
    plain = clean_env(Path("job-dir"))
    assert not {"TMPDIR", "TEMP", "TMP"} & set(plain)
    private = clean_env(Path("job-dir"), Path("job-tmp"))
    assert (private["TMPDIR"], private["TEMP"], private["TMP"]) == ("job-tmp",) * 3
    assert {k: v for k, v in private.items() if k not in ("TMPDIR", "TEMP", "TMP")} == plain


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


def _pretend_ids(monkeypatch: pytest.MonkeyPatch, uid: int, gid: int) -> None:
    """Make the process report ``uid`` and ``gid`` as its real, effective and saved ids."""
    for name in ("getresuid", "getresgid"):
        if hasattr(os, name):
            ids = uid if name == "getresuid" else gid
            monkeypatch.setattr(os, name, lambda ids=ids: (ids, ids, ids))
    for name, value in (("getuid", uid), ("geteuid", uid), ("getgid", gid), ("getegid", gid)):
        monkeypatch.setattr(os, name, lambda value=value: value)


def _record_priority(monkeypatch: pytest.MonkeyPatch, calls: list[Any], current: int = 0) -> None:
    """Record ``setpriority`` instead of renicing the test runner, which could never undo it."""
    monkeypatch.setattr(os, "getpriority", lambda which, who: current)
    monkeypatch.setattr(os, "setpriority", lambda which, who, prio: calls.append(("setpriority", which, who, prio)))


def test_preexec_drops_to_the_slot_before_it_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The child leaves its session, lowers its priority, drops groups and ids, then lowers every limit."""
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(os, "setsid", lambda: calls.append(("setsid",)))
    _record_priority(monkeypatch, calls)
    monkeypatch.setattr(os, "setgroups", lambda groups: calls.append(("setgroups", list(groups))))
    monkeypatch.setattr(os, "setgid", lambda gid: calls.append(("setgid", gid)))
    monkeypatch.setattr(os, "setuid", lambda uid: calls.append(("setuid", uid)))
    _pretend_ids(monkeypatch, 20003, 20003)
    monkeypatch.setattr(resource, "setrlimit", lambda which, pair: calls.append(("rlimit", which, pair)))
    limits = Limits(wall_s=9, cpu_s=7, memory_mb=512, file_mb=9, nproc=100, nofile=128)

    preexec(Slot(index=2, uid=20003, gid=20003), limits)()

    assert calls[:5] == [
        ("setsid",),
        ("setpriority", os.PRIO_PROCESS, 0, 10),
        ("setgroups", []),
        ("setgid", 20003),
        ("setuid", 20003),
    ]
    expected = {
        resource.RLIMIT_CPU: 7,
        resource.RLIMIT_FSIZE: 9 * MB,
        resource.RLIMIT_NPROC: 100,
        resource.RLIMIT_NOFILE: 128,
        resource.RLIMIT_CORE: 0,
    }
    if LINUX:
        expected[resource.RLIMIT_AS] = 512 * MB
    assert {c[1]: c[2] for c in calls[5:]} == {which: (value, value) for which, value in expected.items()}
    assert len(calls) == 5 + len(expected)


def test_preexec_without_a_slot_keeps_the_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no slot the child still gets its own session, its lower priority and limits, but no id is changed."""
    calls: list[Any] = []
    monkeypatch.setattr(os, "setsid", lambda: calls.append("setsid"))
    _record_priority(monkeypatch, calls)
    monkeypatch.setattr(os, "setgroups", lambda groups: calls.append("setgroups"))
    monkeypatch.setattr(os, "setgid", lambda gid: calls.append("setgid"))
    monkeypatch.setattr(os, "setuid", lambda uid: calls.append("setuid"))
    monkeypatch.setattr(resource, "setrlimit", lambda which, pair: calls.append("rlimit"))

    preexec(None, _limits())()

    assert calls[:2] == ["setsid", ("setpriority", os.PRIO_PROCESS, 0, 10)]
    assert set(calls[2:]) == {"rlimit"}


def test_preexec_never_raises_the_priority_of_a_child_that_is_already_lower(monkeypatch: pytest.MonkeyPatch) -> None:
    """A supervisor already at niceness 15 starts its jobs at 15, not 10: raising it back needs privilege."""
    calls: list[Any] = []
    monkeypatch.setattr(os, "setsid", lambda: None)
    _record_priority(monkeypatch, calls, current=15)
    monkeypatch.setattr(resource, "setrlimit", lambda which, pair: None)

    preexec(None, _limits())()

    assert calls == [("setpriority", os.PRIO_PROCESS, 0, 15)]


@pytest.mark.parametrize(("uid", "gid"), [(0, 0), (0, 20001), (20001, 0), (-1, 20001), (20001, -1)])
def test_preexec_refuses_a_slot_of_root_before_it_drops(uid: int, gid: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """A slot with uid or gid 0 would run user code as root; the child refuses before it changes any id."""
    calls: list[Any] = []
    monkeypatch.setattr(os, "setsid", lambda: None)
    _record_priority(monkeypatch, calls)
    monkeypatch.setattr(os, "setgroups", lambda groups: calls.append("setgroups"))
    monkeypatch.setattr(os, "setgid", lambda gid: calls.append("setgid"))
    monkeypatch.setattr(os, "setuid", lambda uid: calls.append("setuid"))
    _pretend_ids(monkeypatch, uid, gid)  # what a drop to root would report: the check must not rely on it

    with pytest.raises(RuntimeError, match="must not run as root"):
        preexec(Slot(index=0, uid=uid, gid=gid), _limits())()

    assert not {"setgroups", "setgid", "setuid"} & set(calls)


@pytest.mark.parametrize(
    ("uids", "gids"),
    [
        ((20003, 20003, 0), (20003, 20003, 20003)),
        ((20003, 0, 20003), (20003, 20003, 20003)),
        ((20003, 20003, 20003), (20003, 20003, 0)),
        ((0, 0, 0), (0, 0, 0)),
    ],
    ids=["saved-uid", "effective-uid", "saved-gid", "nothing-changed"],
)
@pytest.mark.skipif(not hasattr(os, "getresuid"), reason="getresuid exists only on Linux")
def test_preexec_refuses_a_drop_that_kept_any_other_id(
    uids: tuple[int, int, int], gids: tuple[int, int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the drop, real, effective and saved ids must all be the slot's, or the job does not start."""
    monkeypatch.setattr(os, "setsid", lambda: None)
    _record_priority(monkeypatch, [])
    for name in ("setgroups", "setgid", "setuid"):
        monkeypatch.setattr(os, name, lambda value: None)
    monkeypatch.setattr(os, "getresuid", lambda: uids)
    monkeypatch.setattr(os, "getresgid", lambda: gids)
    monkeypatch.setattr(resource, "setrlimit", lambda which, pair: pytest.fail("limits set after a failed drop"))

    with pytest.raises(RuntimeError, match="did not become slot 2"):
        preexec(Slot(index=2, uid=20003, gid=20003), _limits())()


async def test_a_full_job_runs_through_the_sandbox(tmp_path: Path) -> None:
    """The real entry point starts under the sandbox's environment and limits and leaves its result."""
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"tool": "echo", "args": {"x": 1}, "chat_id": "chat-1", "max_chars": 100, "file_mb": 5}))
    argv = [sys.executable, "-I", "-m", "workbench.executor.job", str(spec)]
    assert await run_in_slot(argv, tmp_path, None, _limits()) == ("ok", 0)
    assert json.loads((tmp_path / "result.json").read_text()) == {"x": 1}


async def test_a_job_runs_at_a_lower_priority(tmp_path: Path) -> None:
    """Jobs run at niceness 10 or more, so the supervisor and slot cleanups stay responsive under load."""
    code = "import os; open('nice.txt', 'w').write(str(os.getpriority(os.PRIO_PROCESS, 0)))"
    assert await _run(code, tmp_path) == ("ok", 0)
    assert int((tmp_path / "nice.txt").read_text()) >= 10


def _refuse_setgroups(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the drop fail in the child, as it does for a supervisor that lacks the privilege."""

    def refuse(groups: list[int]) -> None:
        """Fail like ``setgroups`` without ``CAP_SETGID``."""
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "setgroups", refuse)


async def test_a_failed_drop_is_an_error_and_the_slot_is_still_cleared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drop that fails in the child is reported as ``error`` with ``SPAWN_FAILED``, not raised, and runs nothing."""
    cleared: list[int] = []
    _refuse_setgroups(monkeypatch)
    monkeypatch.setattr(sandbox, "kill_slot_processes", lambda slot: cleared.append(slot.index))
    code = "open('ran.txt', 'w').write('ran')"
    argv = [sys.executable, "-I", "-c", code]

    assert await run_in_slot(argv, tmp_path, Slot(3, 20004, 20004), _limits()) == ("error", sandbox.SPAWN_FAILED)

    assert sandbox.SPAWN_FAILED == 126
    assert not (tmp_path / "ran.txt").exists()
    assert cleared == [3]


async def test_a_drop_that_leaves_the_supervisor_ids_runs_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the id calls do nothing, the child still has the supervisor's ids; the check refuses to exec."""
    for name in ("setgroups", "setgid", "setuid"):
        monkeypatch.setattr(os, name, lambda value: None)
    monkeypatch.setattr(sandbox, "kill_slot_processes", lambda slot: None)
    code = "open('ran.txt', 'w').write('ran')"
    argv = [sys.executable, "-I", "-c", code]

    assert await run_in_slot(argv, tmp_path, Slot(0, 20001, 20001), _limits()) == ("error", sandbox.SPAWN_FAILED)

    assert not (tmp_path / "ran.txt").exists()


async def test_a_slot_of_root_runs_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A slot with uid 0 is refused in the child even where the drop calls would succeed."""
    for name in ("setgroups", "setgid", "setuid"):
        monkeypatch.setattr(os, name, lambda value: None)
    monkeypatch.setattr(sandbox, "kill_slot_processes", lambda slot: None)
    _pretend_ids(monkeypatch, 0, 0)
    code = "open('ran.txt', 'w').write('ran')"
    argv = [sys.executable, "-I", "-c", code]

    assert await run_in_slot(argv, tmp_path, Slot(0, 0, 0), _limits()) == ("error", sandbox.SPAWN_FAILED)

    assert not (tmp_path / "ran.txt").exists()


@pytest.mark.parametrize("code", ["pass", "import time; time.sleep(10)"])
async def test_the_slot_cleanup_runs_on_the_pool_it_is_given(
    code: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor hands over a pool of its own, so stopped cleanups never take the default pool's threads."""
    threads: list[str] = []
    real_preexec = sandbox.preexec
    monkeypatch.setattr(sandbox, "preexec", lambda slot, limits: real_preexec(None, limits))
    monkeypatch.setattr(sandbox, "kill_slot_processes", lambda slot: threads.append(threading.current_thread().name))
    argv = [sys.executable, "-I", "-c", code]

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="slot-cleanup-test") as pool:
        await run_in_slot(argv, tmp_path, Slot(1, 20002, 20002), _limits(wall_s=1), cleanup_pool=pool)

    assert len(threads) == 1
    assert threads[0].startswith("slot-cleanup-test")


async def test_a_failed_drop_is_cleared_on_the_given_pool_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cleanup after a failed drop runs where every other cleanup runs."""
    threads: list[str] = []
    _refuse_setgroups(monkeypatch)
    monkeypatch.setattr(sandbox, "kill_slot_processes", lambda slot: threads.append(threading.current_thread().name))
    argv = [sys.executable, "-I", "-c", "pass"]

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="slot-cleanup-test") as pool:
        status = await run_in_slot(argv, tmp_path, Slot(1, 20002, 20002), _limits(), cleanup_pool=pool)

    assert status == ("error", sandbox.SPAWN_FAILED)
    assert [name.startswith("slot-cleanup-test") for name in threads] == [True]


# --- what a slot leaves besides processes: SysV IPC, /dev/shm, /dev/mqueue ---------------------


def _linux_cleanup(monkeypatch: pytest.MonkeyPatch, events: list[str], *, ipc: list[bool]) -> None:
    """Stand in for every step of the slot cleanup, noting the order; ``ipc`` answers the IPC looks."""
    answers = iter(ipc)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_kill_as_slot", lambda slot: events.append("kill"))
    monkeypatch.setattr(sandbox, "_slot_has_live_processes", lambda uid: events.append("look") or False)
    monkeypatch.setattr(sandbox, "_slot_has_ipc", lambda uid: events.append("ipc?") or next(answers))
    monkeypatch.setattr(sandbox, "_clear_ipc_as_slot", lambda slot: events.append("clear"))


def test_slot_cleanup_clears_ipc_once_no_process_is_left_and_looks_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """IPC is cleared after the last process is gone, so nothing can make more; a second look verifies it."""
    events: list[str] = []
    _linux_cleanup(monkeypatch, events, ipc=[True, False])
    kill_slot_processes(Slot(index=0, uid=20001, gid=20001))
    assert events == ["kill", "look", "ipc?", "clear", "ipc?"]


def test_slot_cleanup_runs_no_ipc_helper_when_the_slot_left_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The common case costs one look and no second helper."""
    events: list[str] = []
    _linux_cleanup(monkeypatch, events, ipc=[False])
    kill_slot_processes(Slot(index=0, uid=20001, gid=20001))
    assert events == ["kill", "look", "ipc?"]


def test_ipc_that_survives_the_clear_fails_the_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever stays behind could reach the slot's next job, another user's perhaps: the slot is retired."""
    events: list[str] = []
    _linux_cleanup(monkeypatch, events, ipc=[True, True])
    with pytest.raises(RuntimeError) as raised:
        kill_slot_processes(Slot(index=2, uid=20003, gid=20003))
    assert str(raised.value) == "IPC objects of slot 2 survived the cleanup"


def test_ipc_is_not_touched_while_processes_survive(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live process could make new objects, so the slot fails before any IPC step."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_kill_as_slot", lambda slot: None)
    monkeypatch.setattr(sandbox, "_slot_has_live_processes", lambda uid: True)
    monkeypatch.setattr(sandbox, "_slot_has_ipc", lambda uid: pytest.fail("looked at IPC"))
    monkeypatch.setattr(sandbox.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError, match="processes of slot 0 survived the cleanup"):
        kill_slot_processes(Slot(index=0, uid=20001, gid=20001))


@pytest.mark.parametrize("error", [OSError("no /proc/sysvipc"), ValueError("x"), IndexError("y")])
def test_ipc_that_cannot_be_checked_fails_the_cleanup(error: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    """A look that fails is not a clean look."""

    def look(uid: int) -> bool:
        """Fail as an unreadable table does."""
        raise error

    events: list[str] = []
    _linux_cleanup(monkeypatch, events, ipc=[])
    monkeypatch.setattr(sandbox, "_slot_has_ipc", look)
    with pytest.raises(RuntimeError) as raised:
        kill_slot_processes(Slot(index=1, uid=20002, gid=20002))
    assert str(raised.value) == "processes of slot 1 could not be checked"


@pytest.mark.parametrize("outcome", ["exit 1", "timeout"])
def test_an_ipc_helper_that_fails_or_hangs_fails_the_cleanup(outcome: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The IPC helper is exec'd before it drops, as the kill helper is, and runs under the same timeout."""
    seen: dict[str, Any] = {}

    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        """End as told, after noting how it was called."""
        seen.update(kwargs, argv=args[0])
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        return subprocess.CompletedProcess(args[0], 1)

    monkeypatch.setattr(sandbox.subprocess, "run", run)
    with pytest.raises(RuntimeError) as raised:
        sandbox._clear_ipc_as_slot(Slot(index=3, uid=20004, gid=20005))
    assert str(raised.value) == "could not clear the IPC objects of slot 3"
    assert seen["timeout"] == 30
    assert "preexec_fn" not in seen
    assert seen["argv"][4] == sandbox._DROP_AND_CLEAR
    assert seen["argv"][5:7] == ["20004", "20005"]
    assert seen["env"] == dict(sandbox.CLEAN_ENV)


@pytest.mark.parametrize(("uid", "gid"), [("0", "20001"), ("20001", "0"), ("-1", "20001"), ("20001", "-5")])
def test_the_ipc_helper_refuses_root_and_negative_ids(uid: str, gid: str) -> None:
    """As root it would remove root's objects; ``setgroups`` is replaced to exit 9 so a missing check shows."""
    disarmed = "import os\nos.setgroups = lambda groups: os._exit(9)\n" + sandbox._DROP_AND_CLEAR
    done = subprocess.run([sys.executable, "-I", "-S", "-c", disarmed, uid, gid], check=False, capture_output=True)
    assert done.returncode == 3, done.stderr


def test_the_ipc_helper_imports_ctypes_only_when_there_is_sysv_ipc_to_remove() -> None:
    """Its start-up stays as cheap as the kill helper's; ``ctypes`` loads only for ``IPC_RMID`` calls."""
    tree = ast.parse(sandbox._DROP_AND_CLEAR)
    top = {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
    nested = {
        alias.name
        for branch in tree.body
        if isinstance(branch, ast.If)
        for node in ast.walk(branch)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    everywhere = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    assert top == {"os", "sys"}
    assert nested == {"ctypes"}
    assert everywhere == {"os", "sys", "ctypes"}


SYSV_SHM = (
    b"       key      shmid perms                  size  cpid  lpid nattch   uid   gid  cuid  cgid      atime\n"
    b"     22337          5   600                  4096     8     0      0 20009 20009 20001 20001          0\n"
    b"         0          6   600                  4096     8     0      0     0     0     0     0          0\n"
)
SYSV_SEM = (
    b"       key      semid perms      nsems   uid   gid  cuid  cgid      otime      ctime\n"
    b"     22339          0   600          1 20002 20002 20002 20002          0 1794616376\n"
)


@pytest.mark.parametrize(
    ("table", "uid", "held"),
    [
        (SYSV_SHM, 20001, True),  # the creator, though it handed the owner away
        (SYSV_SHM, 20009, True),  # the owner
        (SYSV_SHM, 20002, False),
        (SYSV_SEM, 20002, True),  # another column layout, read from its header
        (SYSV_SEM, 20001, False),
        (SYSV_SEM.splitlines(keepends=True)[0], 20002, False),
    ],
)
def test_sysv_objects_of_a_slot_are_found_by_owner_or_creator(table: bytes, uid: int, held: bool) -> None:
    """``IPC_SET`` can hand an object's owner to another uid; the creator stays, so both columns count."""
    assert sandbox._sysv_table_holds(table, uid) is held


def test_a_slot_file_is_found_anywhere_below_an_ipc_folder(tmp_path: Path) -> None:
    """A folder is walked through, without following links; ownership is what counts."""
    me = os.getuid()
    assert sandbox._folder_holds(str(tmp_path), me) is False
    assert sandbox._folder_holds(str(tmp_path / "missing"), me) is False
    (tmp_path / "a" / "b").mkdir(parents=True)
    assert sandbox._folder_holds(str(tmp_path), me) is True  # the folders are the uid's too
    assert sandbox._folder_holds(str(tmp_path), me + 12345) is False


MAKE_IPC = """\
import ctypes, os, pathlib, sys
libc = ctypes.CDLL(None, use_errno=True)
tag, key = sys.argv[1], int(sys.argv[2])
pathlib.Path(f"/dev/shm/wb-{tag}").write_text("synthetic note")
pathlib.Path(f"/dev/shm/wb-{tag}-d/inner").mkdir(parents=True)
pathlib.Path(f"/dev/shm/wb-{tag}-d/inner/note").write_text("synthetic note")
assert libc.shmget(key, 4096, 0o1000 | 0o600) >= 0
assert libc.shmget(0, 4096, 0o1000 | 0o600) >= 0
assert libc.msgget(key + 1, 0o1000 | 0o600) >= 0
assert libc.semget(key + 2, 1, 0o1000 | 0o600) >= 0
assert libc.mq_open(f"/wb-{tag}".encode(), os.O_CREAT | os.O_RDWR, 0o600, None) >= 0
"""


def _ipc_of(uid: int) -> list[str]:
    """Everything of ``uid`` in the SysV tables, ``/dev/shm`` and ``/dev/mqueue``, read independently."""
    found = []
    for table in ("shm", "msg", "sem"):
        rows = Path(f"/proc/sysvipc/{table}").read_text().splitlines()
        head = rows[0].split()
        for row in rows[1:]:
            cells = row.split()
            if uid in (int(cells[head.index("uid")]), int(cells[head.index("cuid")])):
                found.append(f"{table}:{cells[1]}")
    for top in ("/dev/shm", "/dev/mqueue"):
        for folder, names, files in os.walk(top):
            found += [f"{folder}/{n}" for n in names + files if os.lstat(f"{folder}/{n}").st_uid == uid]
    return found


def _make_ipc_as(slot: Slot, tag: str, key: int) -> None:
    """Leave SysV objects, POSIX shared memory files and a message queue of the slot's uid, then exit."""

    def drop() -> None:
        """Become the slot."""
        os.setgroups([])
        os.setgid(slot.gid)
        os.setuid(slot.uid)

    subprocess.run([sys.executable, "-I", "-c", MAKE_IPC, tag, str(key)], preexec_fn=drop, check=True, timeout=60)


@pytest.mark.skipif(not LINUX or os.geteuid() != 0, reason="switching uids needs root on Linux")
def test_as_root_a_slot_cleanup_removes_its_ipc_and_nothing_of_others() -> None:
    """Shared memory, queues and semaphores outlive their processes; the cleanup removes the slot's own only."""
    slot, other = Slot(index=6, uid=20007, gid=20007), Slot(index=7, uid=20008, gid=20008)
    _make_ipc_as(slot, "slot", 0x57A0)
    _make_ipc_as(other, "other", 0x57B0)
    assert len(_ipc_of(slot.uid)) == 9  # 4 SysV objects, 4 entries in /dev/shm, 1 queue
    try:
        kill_slot_processes(slot)
        assert _ipc_of(slot.uid) == []
        assert len(_ipc_of(other.uid)) == 9
        assert sandbox._slot_has_ipc(slot.uid) is False
    finally:
        kill_slot_processes(other)
    assert _ipc_of(other.uid) == []
