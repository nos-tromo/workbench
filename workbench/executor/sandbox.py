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
from concurrent.futures import Executor
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from workbench.protocol import Limits

MB = 1024 * 1024
# The exit code ``run_in_slot`` reports for a job that never started because the child could not
# become its slot. 126 is the shell's "found but could not be executed".
SPAWN_FAILED = 126
# Jobs run at least this nice, so the supervisor and slot cleanups stay responsive when every slot
# is busy. A job cannot lower it again without CAP_SYS_NICE.
JOB_NICENESS = 10

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
_SLOT_KILL_ATTEMPTS = 6
_SLOT_KILL_PAUSE_S = 0.1  # before the second pass, doubled before each later one: 3.1 s in all
# Finite, since a slot process can SIGSTOP the helper once it is the slot's uid. Generous, since
# busy jobs in other slots slow its start-up down (to 26 s measured, on one CPU next to 192 busy tasks).
_HELPER_TIMEOUT_S = 30.0
# The helpers import only os (frozen) and sys (built in). signal would pull in enum, functools and
# collections, which an image without .pyc files compiles from source at every start.
_DROP = """\
import os, sys
uid, gid = int(sys.argv[1]), int(sys.argv[2])
if uid <= 0 or gid <= 0:
    sys.exit(3)
os.setgroups([])
os.setgid(gid)
os.setuid(uid)
if os.getresuid() != (uid, uid, uid) or os.getresgid() != (gid, gid, gid):
    sys.exit(2)
"""
# 9 is SIGKILL, and ProcessLookupError is ESRCH: nothing of the uid is left to signal.
_DROP_AND_KILL = (
    _DROP
    + """\
try:
    os.kill(-1, 9)
except ProcessLookupError:
    pass
"""
)
# Removes, as the slot, what outlives its processes. SysV objects whose owner or creator is the uid
# get IPC_RMID (0), which only their owner or creator may send without CAP_IPC_OWNER; ctypes is
# loaded only when there are any. Files, folders, POSIX semaphores and shared memory below the
# folders named after the ids (/dev/shm, and /dev/mqueue for message queues) are unlinked when
# the uid owns them, deepest first; links are removed, never followed. Errors are left for the
# supervisor's second look to find.
_DROP_AND_CLEAR = (
    _DROP
    + """\
doomed = []
for table, column in (("shm", "shmid"), ("msg", "msqid"), ("sem", "semid")):
    try:
        with open("/proc/sysvipc/" + table) as rows:
            head = rows.readline().split()
            at, owner, creator = head.index(column), head.index("uid"), head.index("cuid")
            for row in rows:
                cells = row.split()
                if uid in (int(cells[owner]), int(cells[creator])):
                    doomed.append((table, int(cells[at])))
    except FileNotFoundError:
        pass
if doomed:
    import ctypes
    libc = ctypes.CDLL(None)
    for table, ident in doomed:
        if table == "shm":
            libc.shmctl(ident, 0, None)
        elif table == "msg":
            libc.msgctl(ident, 0, None)
        else:
            libc.semctl(ident, 0, 0)
for top in sys.argv[3:]:
    folders, mine = [top], []
    while folders:
        folder = folders.pop()
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            path = folder + "/" + name
            try:
                st = os.lstat(path)
            except OSError:
                continue
            if st.st_mode & 0o170000 == 0o040000:
                folders.append(path)
                if st.st_uid == uid:
                    mine.append(path)
            elif st.st_uid == uid:
                try:
                    os.unlink(path)
                except OSError:
                    pass
    for path in reversed(mine):
        try:
            os.rmdir(path)
        except OSError:
            pass
"""
)
# Where the kernel keeps IPC that outlives processes, as files: POSIX shared memory and semaphores,
# and POSIX message queues where their file system is mounted.
_IPC_FOLDERS = ("/dev/shm", "/dev/mqueue")
_SYSVIPC = Path("/proc/sysvipc")
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


def _holds_only(uid: int, gid: int) -> bool:
    """Tell whether the process's ids are all ``uid`` and ``gid``, saved ids included where the kernel has them.

    Args:
        uid: The user id every one of the process's user ids must be.
        gid: The group id every one of the process's group ids must be.

    Returns:
        ``True`` when they are.
    """
    if sys.platform == "linux":
        return os.getresuid() == (uid, uid, uid) and os.getresgid() == (gid, gid, gid)
    return (os.getuid(), os.geteuid(), os.getgid(), os.getegid()) == (uid, uid, gid, gid)


def _drop_to(slot: Slot) -> None:
    """Switch the calling process to the slot's ids, for good, and prove it did.

    Args:
        slot: The slot to become.

    Raises:
        RuntimeError: The slot's uid or gid is 0 or below, which would leave the job root, or
            after the switch any real, effective or saved id is not the slot's.
        OSError: A switch call failed.
    """
    if slot.uid <= 0 or slot.gid <= 0:
        raise RuntimeError(f"slot {slot.index} must not run as root")
    os.setgroups([])
    os.setgid(slot.gid)
    os.setuid(slot.uid)
    if not _holds_only(slot.uid, slot.gid):
        raise RuntimeError(f"the job did not become slot {slot.index}")


def preexec(slot: Slot | None, limits: Limits) -> Callable[[], None]:
    """Build the function that runs in the child between ``fork`` and ``exec``.

    The child leaves the supervisor's session, lowers its priority to at least ``JOB_NICENESS``,
    drops its groups and switches to the slot's ids, checks that it holds no other id, then
    lowers its limits. Failing at any step aborts the spawn, so a job never runs with more
    rights than intended; ``run_in_slot`` reports that as ``SPAWN_FAILED``.

    Args:
        slot: The slot whose ids the job runs as, or ``None`` to keep the current ids (tests, dev).
        limits: The limits to set in the child.

    Returns:
        The function to pass as ``preexec_fn``. In the child it raises ``RuntimeError`` for a
        slot with uid or gid 0 (before any id changes) or a drop that left another id.
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
        niceness = max(JOB_NICENESS, os.getpriority(os.PRIO_PROCESS, 0))
        os.setpriority(os.PRIO_PROCESS, 0, niceness)
        if slot is not None:
            _drop_to(slot)
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


def _run_as_slot(program: str, slot: Slot, extra: list[str], failure: str) -> None:
    """Run a helper program that makes itself the slot's ids, under a timeout.

    The helper is started as root and drops to the slot's ids only inside the program it has
    already exec'd, so everything it does as the slot's uid falls under the timeout, and on
    expiry it is killed and reaped as root. Dropping inside ``preexec_fn`` instead would hold
    ``Popen`` in its start-up read, beyond any timeout, if a slot process stopped the helper.

    Args:
        program: The helper's source; it reads uid and gid from ``argv[1:3]``.
        slot: The slot to act as.
        extra: Further arguments, after the ids.
        failure: The ``RuntimeError`` message, naming only the slot.

    Raises:
        RuntimeError: The helper could not run, did not become the slot's ids, failed, or did not
            finish within ``_HELPER_TIMEOUT_S`` (a surviving slot process can stop it).
    """
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-S", "-c", program, str(slot.uid), str(slot.gid), *extra],
            env=dict(CLEAN_ENV),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=_HELPER_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(failure) from exc
    if done.returncode != 0:
        raise RuntimeError(failure)


def _kill_as_slot(slot: Slot) -> None:
    """Send ``SIGKILL`` to every process the slot's uid can signal, from a helper that is the slot.

    ``kill(-1)`` as the slot reaches that uid's processes and nobody else's; from the supervisor
    itself it would reach everything.

    Args:
        slot: The slot whose processes to kill.

    Raises:
        RuntimeError: The helper failed or timed out (``_run_as_slot``).
    """
    _run_as_slot(_DROP_AND_KILL, slot, [], f"could not clear the processes of slot {slot.index}")


def _clear_ipc_as_slot(slot: Slot) -> None:
    """Remove, from a helper that is the slot, every IPC object and IPC file the slot's uid owns.

    The supervisor has neither ``CAP_IPC_OWNER`` nor ``CAP_SYS_ADMIN``, so only the owner or creator
    itself may remove a SysV object; the files are removed as the slot too.

    Args:
        slot: The slot whose IPC to remove.

    Raises:
        RuntimeError: The helper failed or timed out (``_run_as_slot``).
    """
    folders = [folder for folder in _IPC_FOLDERS if os.path.isdir(folder)]
    _run_as_slot(_DROP_AND_CLEAR, slot, folders, f"could not clear the IPC objects of slot {slot.index}")


def _sysv_table_holds(table: bytes, uid: int) -> bool:
    """Tell from a ``/proc/sysvipc`` table whether ``uid`` owns or created one of its objects.

    Args:
        table: The table's contents: a header naming the columns, then one object per line.
        uid: The user id to look for.

    Returns:
        Whether a row's ``uid`` or ``cuid`` is ``uid``.

    Raises:
        ValueError: The header has no ``uid`` or ``cuid`` column, or a cell is not a number.
        IndexError: A row is shorter than the header.
    """
    lines = table.split(b"\n")
    head = lines[0].split()
    owner, creator = head.index(b"uid"), head.index(b"cuid")
    for line in lines[1:]:
        cells = line.split()
        if cells and uid in (int(cells[owner]), int(cells[creator])):
            return True
    return False


def _folder_holds(top: str, uid: int) -> bool:
    """Look below ``top`` for anything ``uid`` owns, without following links.

    Args:
        top: A folder, such as ``/dev/shm``.
        uid: The user id to look for.

    Returns:
        Whether a file, folder or link below ``top`` belongs to ``uid``. A folder that vanishes
        while it is looked at counts as empty.

    Raises:
        OSError: A folder could not be read for another reason.
    """
    folders = [top]
    while folders:
        try:
            entries = list(os.scandir(folders.pop()))
        except (FileNotFoundError, NotADirectoryError):
            continue
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if st.st_uid == uid:
                return True
            if entry.is_dir(follow_symlinks=False):
                folders.append(entry.path)
    return False


def _slot_has_ipc(uid: int) -> bool:
    """Look for IPC the uid left: SysV objects it owns or made, and files below ``_IPC_FOLDERS``.

    Args:
        uid: The slot's user id.

    Returns:
        Whether there is any.

    Raises:
        OSError: A table or folder could not be read.
        ValueError: A table has an unexpected shape.
        IndexError: A row of a table is short.
    """
    for table in ("shm", "msg", "sem"):
        try:
            content = (_SYSVIPC / table).read_bytes()
        except FileNotFoundError:
            continue
        if _sysv_table_holds(content, uid):
            return True
    return any(_folder_holds(folder, uid) for folder in _IPC_FOLDERS)


def kill_slot_processes(slot: Slot | None) -> None:
    """Kill every process the slot's uid still has, then remove the IPC it left, so the slot is clear.

    A job's descendants can leave the group with ``setsid`` or ``setpgid``, so the group kill
    does not reach them; they keep the slot's uid, and the slot's next job would share a uid
    with them. The signal goes out first, unconditionally: whether something runs is judged
    only after it. Then ``/proc`` is checked for a running task of that uid. ``kill(-1)`` reaches
    all of them in one go (the kernel holds its task list while it signals, and a process with
    ``SIGKILL`` pending cannot fork), so a later pass is for tasks that are still exiting: it
    repeats the signal after a pause of 0.1 s, doubled each time, for at most six passes.

    Measured in ``python:3.12-slim``, which has no ``.pyc`` files, with a read-only root: a helper
    run takes 8-210 ms on an idle host. On one CPU it shares with busy jobs of other slots it takes
    seconds, varying with the host from one run to the next: 1-19 s next to 64 spinning processes,
    2-26 s next to 192. A look at ``/proc`` takes milliseconds on an idle host and under a second
    on such a CPU, given an init that reaps orphans. The helper is killed after 30 s, and the
    cleanup then fails. So the call always ends: at the very worst after six helper runs of 30 s,
    3.1 s of pauses and six looks, a little over three minutes.

    Then the IPC: a SysV shared memory segment, message queue or semaphore set, or a file in
    ``/dev/shm`` or ``/dev/mqueue``, outlives the job, and the slot's next job, perhaps another
    user's, would run as its owner. Once no process of the uid is left, so none can make more,
    the tables in ``/proc/sysvipc`` and those folders are searched for anything of the uid. If
    there is, a second helper, run like the first, removes it as the slot (``_DROP_AND_CLEAR``),
    and a second look must find nothing. That adds at most one more helper run of 30 s.

    It blocks, so call it from a thread. Does nothing without a slot or off Linux.

    Args:
        slot: The slot to clear, or ``None``.

    Raises:
        RuntimeError: The only failure. Processes or IPC of the slot remain after the cleanup, a
            helper failed or timed out, ``/proc`` could not be checked, or the slot runs as root.
            The message names only the slot's index. The slot must not be reused.
    """
    if slot is None:
        return
    if sys.platform != "linux":
        return
    if slot.uid <= 0 or slot.gid <= 0:
        raise RuntimeError(f"slot {slot.index} must not run as root")
    try:
        for attempt in range(_SLOT_KILL_ATTEMPTS):
            if attempt:
                time.sleep(_SLOT_KILL_PAUSE_S * 2 ** (attempt - 1))
            _kill_as_slot(slot)
            if not _slot_has_live_processes(slot.uid):
                break
        else:
            raise RuntimeError(f"processes of slot {slot.index} survived the cleanup")
        if _slot_has_ipc(slot.uid):
            _clear_ipc_as_slot(slot)
            if _slot_has_ipc(slot.uid):
                raise RuntimeError(f"IPC objects of slot {slot.index} survived the cleanup")
    except (OSError, KeyError, ValueError, IndexError) as exc:
        raise RuntimeError(f"processes of slot {slot.index} could not be checked") from exc


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


async def _clear(slot: Slot | None, cleanup_pool: Executor | None) -> None:
    """Run ``kill_slot_processes`` for the slot in a thread of ``cleanup_pool``.

    Args:
        slot: The slot to clear, or ``None`` for nothing to do.
        cleanup_pool: Where the blocking cleanup runs; ``None`` is the loop's default pool.

    Raises:
        RuntimeError: The cleanup failed; the slot must be retired.
    """
    if slot is not None:
        await asyncio.get_running_loop().run_in_executor(cleanup_pool, kill_slot_processes, slot)


async def run_in_slot(
    argv: list[str],
    cwd: Path,
    slot: Slot | None,
    limits: Limits,
    *,
    tmp_dir: Path | None = None,
    cleanup_pool: Executor | None = None,
) -> tuple[Literal["ok", "error", "timeout", "killed"], int]:
    """Run ``argv`` as a fresh process in ``cwd`` under the slot's ids and the limits.

    The process leads its own session, runs at niceness ``JOB_NICENESS`` or more and with umask
    ``077``, so what it creates is private to its uid. When it ends, by itself, on the wall
    clock or because this task is cancelled, everything left in its process group is killed
    with it. With a slot, every other process of the slot's uid is killed too, including those
    that left the group; that also happens when the child could not become the slot.

    Args:
        argv: The command line; ``argv[0]`` should be an absolute path since ``PATH`` is fixed.
        cwd: The job directory; the process starts there and has it as ``HOME``.
        slot: The slot to run as, or ``None`` to keep the current uid (tests and dev).
        limits: Wall time and the kernel limits.
        tmp_dir: The job's private temporary directory, handed over as ``TMPDIR``, ``TEMP`` and ``TMP``.
        cleanup_pool: The pool the slot cleanup runs on. A slot process can hold a cleanup for
            30 s, so the supervisor gives one of its own, a thread per slot; ``None`` is the
            loop's default pool.

    Returns:
        The outcome and the exit code (negative for death by a signal). ``timeout`` means the
        wall time ran out and the group was killed. ``("error", SPAWN_FAILED)`` means the job
        never started: the child failed to lower its priority, become the slot or set a limit
        (``preexec`` raised, which ``subprocess`` reports as ``SubprocessError``). Nothing of
        ``argv`` ran then.

    Raises:
        RuntimeError: Processes of the slot survived the cleanup; the slot must be retired.
        OSError: ``argv[0]`` could not be executed.
    """
    try:
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
    except subprocess.SubprocessError:
        await _clear(slot, cleanup_pool)
        return "error", SPAWN_FAILED
    returncode: int | None
    try:
        returncode = await asyncio.wait_for(process.wait(), timeout=limits.wall_s)
    except TimeoutError:
        returncode = None
    finally:
        _kill_group(process.pid)
        reaped = await process.wait()
        await _clear(slot, cleanup_pool)
    if returncode is None:
        return "timeout", reaped
    return _outcome(returncode), returncode
