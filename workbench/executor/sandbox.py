"""Run one job as a fresh process under a slot uid, with limits, a wall clock and a clean environment."""

import asyncio
import contextlib
import os
import resource
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from workbench.protocol import Limits

MB = 1024 * 1024

CLEAN_ENV: Mapping[str, str] = MappingProxyType(
    {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "MPLBACKEND": "Agg",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
    }
)

_KILLED_BY = frozenset({signal.SIGKILL, signal.SIGXCPU})
_SLOT_KILL_ATTEMPTS = 50
_SLOT_KILL_PAUSE_S = 0.02
_HELPER_TIMEOUT_S = 5.0  # a slot process can SIGSTOP the helper once it has become the slot's uid
_DROP_AND_KILL = """\
import os, signal, sys
uid, gid = int(sys.argv[1]), int(sys.argv[2])
os.setgroups([])
os.setgid(gid)
os.setuid(uid)
if os.getresuid() != (uid, uid, uid) or os.getresgid() != (gid, gid, gid):
    sys.exit(2)
try:
    os.kill(-1, signal.SIGKILL)
except ProcessLookupError:
    pass
"""
_GONE_STATES = frozenset({"Z", "X"})  # zombie (waiting to be reaped) and dead: no longer running


@dataclass(frozen=True)
class Slot:
    """One of the executor's job slots; each has an id pair no other slot and no other service uses.

    Attributes:
        index: Position of the slot, from 0.
        uid: User id jobs in this slot run as.
        gid: Group id jobs in this slot run as.
    """

    index: int
    uid: int
    gid: int


def clean_env(cwd: Path, tmp_dir: Path | None = None) -> dict[str, str]:
    """Build the environment of a job: ``CLEAN_ENV`` and ``HOME`` set to the job directory.

    Args:
        cwd: The job directory.
        tmp_dir: The job's private temporary directory. When given, ``TMPDIR``, ``TEMP`` and
            ``TMP`` point to it, so the job never writes the shared ``/tmp``.

    Returns:
        The only variables the job sees; nothing of the supervisor's own environment.
    """
    env = {**CLEAN_ENV, "HOME": str(cwd)}
    if tmp_dir is not None:
        env.update(TMPDIR=str(tmp_dir), TEMP=str(tmp_dir), TMP=str(tmp_dir))
    return env


def preexec(slot: Slot | None, limits: Limits) -> Callable[[], None]:
    """Build the function that runs in the child between ``fork`` and ``exec``.

    The child leaves the supervisor's session, drops its groups and switches to the slot's
    ids, then lowers its limits. Failing at any step aborts the spawn, so a job never runs
    with more rights than intended.

    Args:
        slot: The slot whose ids the job runs as, or ``None`` to keep the current ids (tests, dev).
        limits: The limits to set in the child.

    Returns:
        The function to pass as ``preexec_fn``.
    """
    ceilings = {
        resource.RLIMIT_CPU: limits.cpu_s,
        resource.RLIMIT_FSIZE: limits.file_mb * MB,
        resource.RLIMIT_NPROC: limits.nproc,
        resource.RLIMIT_NOFILE: limits.nofile,
        resource.RLIMIT_CORE: 0,
    }
    if sys.platform == "linux":
        ceilings[resource.RLIMIT_AS] = limits.memory_mb * MB  # other kernels refuse or ignore it

    def child() -> None:
        """Isolate the freshly forked child."""
        os.setsid()
        if slot is not None:
            os.setgroups([])
            os.setgid(slot.gid)
            os.setuid(slot.uid)
        for which, ceiling in ceilings.items():
            resource.setrlimit(which, (ceiling, ceiling))

    return child


def _kill_group(pgid: int) -> None:
    """Kill every process of the job's process group; one that is already gone is fine.

    Args:
        pgid: The group id, which is the job's own pid since the job leads its session.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


def _is_live_process_of(status: bytes, uid: int) -> bool:
    """Tell from the bytes of ``/proc/<pid>/status`` whether the process is running as ``uid``.

    Only the ASCII ``Uid:``, ``State:`` and ``Threads:`` lines are read, and the text is split
    on newlines alone, which the kernel escapes in the process name. The name itself is whatever
    bytes the process chose, so it is never decoded.

    Args:
        status: The contents of the status file.
        uid: The user id to look for among the process's real uid.

    Returns:
        ``True`` when the real uid is ``uid`` and something of the process still runs. A zombie
        or dead process runs nothing, unless it is a thread-group leader whose other threads
        live on: the file then shows the leader's state, but ``Threads`` is above 1.

    Raises:
        KeyError: The text has no ``Uid``, ``State`` or ``Threads`` line.
        ValueError: A number in it is not a number.
        IndexError: One of those lines holds no value.
    """
    fields: dict[bytes, list[bytes]] = {}
    for line in status.split(b"\n"):
        key, _, value = line.partition(b":")
        if key in (b"Uid", b"State", b"Threads"):
            fields[key] = value.split()
    if int(fields[b"Uid"][0]) != uid:
        return False
    return fields[b"State"][0].decode("ascii") not in _GONE_STATES or int(fields[b"Threads"][0]) > 1


def _slot_has_live_processes(uid: int) -> bool:
    """Look through ``/proc`` for a running process whose real uid is ``uid``.

    Args:
        uid: The slot's user id.

    Returns:
        Whether one exists. A process that ends while it is looked at counts as gone.

    Raises:
        OSError: ``/proc`` cannot be read.
        KeyError: A process's status has an unexpected shape.
        ValueError: A number in it is not a number.
        IndexError: A line in it holds no value.
    """
    with os.scandir("/proc") as entries:
        pids = [entry.name for entry in entries if entry.name.isdigit()]
    for pid in pids:
        try:
            status = Path(f"/proc/{pid}/status").read_bytes()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if _is_live_process_of(status, uid):
            return True
    return False


def _kill_as_slot(slot: Slot) -> None:
    """Send ``SIGKILL`` to every process the slot's uid can signal, from a helper that is the slot.

    The helper is started as root and drops to the slot's ids only inside the program it has
    already exec'd, so everything it does as the slot's uid falls under the timeout, and on
    expiry it is killed and reaped as root. Dropping inside ``preexec_fn`` instead would hold
    ``Popen`` in its start-up read, beyond any timeout, if a slot process stopped the helper.
    ``kill(-1)`` as the slot reaches that uid's processes and nobody else's; from the
    supervisor itself it would reach everything.

    Args:
        slot: The slot whose processes to kill.

    Raises:
        RuntimeError: The helper could not run, did not become the slot's ids, or did not finish
            within ``_HELPER_TIMEOUT_S`` (a surviving slot process can stop it).
    """
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _DROP_AND_KILL, str(slot.uid), str(slot.gid)],
            env=dict(CLEAN_ENV),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_HELPER_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not clear the processes of slot {slot.index}") from exc
    if done.returncode != 0:
        raise RuntimeError(f"could not clear the processes of slot {slot.index}")


def kill_slot_processes(slot: Slot | None) -> None:
    """Kill every process the slot's uid still has, however it left the job's process group.

    A job's descendants can leave the group with ``setsid`` or ``setpgid``, so the group kill
    does not reach them; they keep the slot's uid, and the slot's next job would share a uid
    with them. The signal goes out first, unconditionally: whether something runs is judged
    only after it. Then ``/proc`` is checked for a running task of that uid, and the signal is
    repeated until none is left, to catch a process that forks while it goes out. A pass takes
    tens of milliseconds, and there are at most 50 of them; the signal helper alone is limited to
    5 seconds, after which the cleanup fails. So the call is slow only for a slot that keeps
    getting new processes, and it always ends, in about 50 x 5 s at the very worst. It blocks,
    so call it from a thread. Does nothing without a slot or off Linux.

    Args:
        slot: The slot to clear, or ``None``.

    Raises:
        RuntimeError: The only failure. Processes of the slot remain after the retries, the helper
            failed or timed out, ``/proc`` could not be checked, or the slot runs as root. The
            message names only the slot's index. The slot must not be reused.
    """
    if slot is None:
        return
    if sys.platform != "linux":
        return
    if slot.uid <= 0:
        raise RuntimeError(f"slot {slot.index} must not run as root")
    try:
        for _ in range(_SLOT_KILL_ATTEMPTS):
            _kill_as_slot(slot)
            if not _slot_has_live_processes(slot.uid):
                return
            time.sleep(_SLOT_KILL_PAUSE_S)
    except (OSError, KeyError, ValueError, IndexError) as exc:
        raise RuntimeError(f"processes of slot {slot.index} could not be checked") from exc
    raise RuntimeError(f"processes of slot {slot.index} survived the cleanup")


def _outcome(returncode: int) -> Literal["ok", "error", "killed"]:
    """Classify how a job that ended by itself ended.

    Args:
        returncode: The exit code, negative for death by a signal.

    Returns:
        ``ok`` for exit 0, ``killed`` when the kernel stopped it with ``SIGKILL`` or ``SIGXCPU``,
        else ``error``.
    """
    if returncode == 0:
        return "ok"
    if returncode < 0 and -returncode in _KILLED_BY:
        return "killed"
    return "error"


async def run_in_slot(
    argv: list[str], cwd: Path, slot: Slot | None, limits: Limits, *, tmp_dir: Path | None = None
) -> tuple[Literal["ok", "error", "timeout", "killed"], int]:
    """Run ``argv`` as a fresh process in ``cwd`` under the slot's ids and the limits.

    The process leads its own session and runs with umask ``077``, so what it creates is
    private to its uid. When it ends, by itself, on the wall clock or because this task is
    cancelled, everything left in its process group is killed with it. With a slot, every
    other process of the slot's uid is killed too, including those that left the group.

    Args:
        argv: The command line; ``argv[0]`` should be an absolute path since ``PATH`` is fixed.
        cwd: The job directory; the process starts there and has it as ``HOME``.
        slot: The slot to run as, or ``None`` to keep the current uid (tests and dev).
        limits: Wall time and the kernel limits.
        tmp_dir: The job's private temporary directory, handed over as ``TMPDIR``, ``TEMP`` and ``TMP``.

    Returns:
        The outcome and the exit code (negative for death by a signal). ``timeout`` means the
        wall time ran out and the group was killed.

    Raises:
        RuntimeError: Processes of the slot survived the cleanup; the slot must be retired.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=clean_env(cwd, tmp_dir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=preexec(slot, limits),
        start_new_session=False,
        umask=0o077,
    )
    returncode: int | None
    try:
        returncode = await asyncio.wait_for(process.wait(), timeout=limits.wall_s)
    except TimeoutError:
        returncode = None
    finally:
        _kill_group(process.pid)
        reaped = await process.wait()
        if slot is not None:
            await asyncio.to_thread(kill_slot_processes, slot)
    if returncode is None:
        return "timeout", reaped
    return _outcome(returncode), returncode
