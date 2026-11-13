"""The slot sandbox: real child processes under rlimits, a wall clock and a clean environment."""

import json
import os
import resource
import signal
import stat
import subprocess
import sys
import tempfile
import time
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


def _stat_state(stat_line: str) -> str:
    """The state letter of a ``/proc/<pid>/stat`` line; the command name may hold spaces and ``)``."""
    return stat_line.rpartition(")")[2].split()[0]


def _state(pid: int) -> str | None:
    """The process state letter of ``pid`` (``R``, ``S``, ``Z`` for a zombie, ...), or ``None`` once it is gone."""
    try:
        if LINUX:
            return _stat_state(Path(f"/proc/{pid}/stat").read_text())
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
    """
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = dict(line.split(":", 1) for line in (entry / "status").read_text().splitlines() if ":" in line)
            if int(fields["Uid"].split()[0]) != uid:
                continue
            for task in (entry / "task").iterdir():
                if _stat_state((task / "stat").read_text()) not in ("Z", "X"):
                    found.append(f"{entry.name}/{task.name}")
        except (FileNotFoundError, ProcessLookupError):
            continue
    return found


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


def _status(state: str, uid: int, name: str = "python", threads: int = 1) -> str:
    """The relevant lines of a ``/proc/<pid>/status`` file."""
    ids = f"{uid}\t{uid}\t{uid}\t{uid}"
    return f"Name:\t{name}\nState:\t{state}\nTgid:\t4242\nUid:\t{ids}\nGid:\t{ids}\nThreads:\t{threads}\n"


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
    assert sandbox._is_live_process_of(_status("Z (zombie)", 20001, "State: S (sleeping)"), 20001) is False
    assert sandbox._is_live_process_of(_status("S (sleeping)", 20002, "Uid: 20001"), 20001) is False


def test_the_tests_tell_a_zombie_from_a_running_process() -> None:
    """``_stat_state`` reads the state after the last ``)``, so a command name cannot confuse it."""
    assert _stat_state("4242 (sleep) S 1 4242 4242 0 -1 4194304") == "S"
    assert _stat_state("4242 (sleep) Z 1 4242 4242 0 -1 4194304") == "Z"
    assert _stat_state("4242 (x) Z y) S 1 4242 4242 0 -1 4194304") == "S"
    assert _stat_state("4242 (x) S y) Z 1 4242 4242 0 -1 4194304") == "Z"


def test_slot_cleanup_does_nothing_without_a_slot_or_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    """No slot means nothing to clear; off Linux there is no ``/proc`` to check."""
    monkeypatch.setattr(sandbox, "_kill_as_slot", lambda slot: pytest.fail("signalled"))
    kill_slot_processes(None)
    monkeypatch.setattr(sys, "platform", "darwin")
    kill_slot_processes(Slot(index=0, uid=20001, gid=20001))


def test_slot_cleanup_refuses_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """``kill(-1)`` as uid 0 would reach every process, so a slot with uid 0 is refused."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_kill_as_slot", lambda slot: pytest.fail("signalled"))
    with pytest.raises(RuntimeError, match="slot 0 must not run as root"):
        kill_slot_processes(Slot(index=0, uid=0, gid=0))


def test_slot_cleanup_gives_up_after_bounded_retries_and_names_only_the_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Processes that will not die raise ``RuntimeError`` after a bounded number of attempts."""
    kills: list[int] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_slot_has_live_processes", lambda uid: True)
    monkeypatch.setattr(sandbox, "_kill_as_slot", lambda slot: kills.append(slot.uid))
    monkeypatch.setattr(sandbox.time, "sleep", lambda seconds: None)
    with pytest.raises(RuntimeError) as raised:
        kill_slot_processes(Slot(index=3, uid=20004, gid=20004))
    assert str(raised.value) == "processes of slot 3 survived the cleanup"
    assert len(kills) == 50


def test_slot_cleanup_stops_as_soon_as_nothing_is_left(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each pass signals once and then looks; the cleanup returns on the first clean look at ``/proc``."""
    looks = iter([True, True, False])
    kills: list[int] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_slot_has_live_processes", lambda uid: next(looks))
    monkeypatch.setattr(sandbox, "_kill_as_slot", lambda slot: kills.append(slot.uid))
    monkeypatch.setattr(sandbox.time, "sleep", lambda seconds: None)
    kill_slot_processes(Slot(index=0, uid=20001, gid=20001))
    assert kills == [20001, 20001, 20001]


def test_slot_cleanup_signals_before_it_looks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a slot that looks empty gets the signal first, since what ``/proc`` shows can be misleading."""
    events: list[str] = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_slot_has_live_processes", lambda uid: events.append("look") or False)
    monkeypatch.setattr(sandbox, "_kill_as_slot", lambda slot: events.append("kill"))
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
    """A surviving slot process can stop the helper; ``run`` has a timeout, and its expiry is a ``RuntimeError``."""
    seen: dict[str, Any] = {}

    def run(*args: Any, **kwargs: Any) -> None:
        """Time out as a stopped helper does, after noting how it was called."""
        seen.update(kwargs)
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(sandbox.subprocess, "run", run)
    with pytest.raises(RuntimeError) as raised:
        sandbox._kill_as_slot(Slot(index=3, uid=20004, gid=20004))
    assert str(raised.value) == "could not clear the processes of slot 3"
    assert 0 < seen["timeout"] <= 10


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
