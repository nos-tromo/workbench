"""Run one job as a fresh process under a slot uid, with limits, a wall clock and a clean environment."""

import asyncio
import contextlib
import os
import resource
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from concurrent.futures import Executor
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from workbench.executor.fs import DIR_FLAGS, closing
from workbench.executor.seccomp import Filter
from workbench.protocol import Limits

MB = 1024 * 1024
# The exit code ``run_in_slot`` reports for a job that never started because the child could not
# become its slot. 126 is the shell's "found but could not be executed".
SPAWN_FAILED = 126
# Jobs run at least this nice, so the supervisor and slot cleanups stay responsive when every slot
# is busy. A job cannot lower it again without CAP_SYS_NICE.
JOB_NICENESS = 10
# The kernel's OOM killer picks a job's process before the supervisor or another service. Raising it
# needs no capability; a job may lower it again, but only as far as the container's own floor.
JOB_OOM_SCORE_ADJ = 1000
# How often the watchdog measures a running job: about twice a second.
WATCH_INTERVAL_S = 0.5

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
# Removes, as the slot, what outlives its processes; argv[3] is the entry limit, argv[4:] the folders.
# SysV objects the uid owns or made get IPC_RMID (0), which only their owner or creator may send
# without CAP_IPC_OWNER; ctypes is loaded only when there are any. Message queues and semaphore sets
# go at once with IPC_RMID, messages and values included. A shared memory segment another slot keeps
# attached is only marked by IPC_RMID, and Linux still lets its owner or creator attach a marked
# segment by id, after giving it back a mode with IPC_SET if need be. So each segment is emptied
# first. IPC_SET (1) is written blind, from the layout of struct ipc_perm on 64-bit little-endian
# Linux (uid at offset 4, gid at 8, mode at 20), and gives the segment the uid and mode 0600; IPC_STAT
# (2) then reads uid and mode back, which shows the write landed where that layout says (on another
# layout they would not match, and the segment is not emptied). The helper attaches the segment and
# punches out its pages with madvise(MADV_REMOVE, 9), which every mapping then reads as zeros and
# which needs no memory. Where the kernel refuses that, only a segment the uid made, and of at most
# 256 MiB, is overwritten with memset; a bigger one, or one another uid made, is left as it is. Then
# it detaches, sets mode 0 and removes the segment. A segment the uid made that was not emptied
# fails the helper (exit 5) if it still exists after IPC_RMID, so is attached elsewhere; one that
# vanished on the way, or that IPC_RMID destroyed, is gone: IPC_STAT then fails with EINVAL (22) or
# EIDRM (43).
# Below the folders, the slot's own tree is cleared. That part is defence in depth (ruling R28): the
# supervisor refuses to start unless /dev/shm is root's and no one else may write it, and
# fs.mqueue.queues_max is 0, so no slot can leave anything in either folder; and in a folder slots
# can write, the walk cannot be relied on, since another uid that renames and removes entries
# while it is listed can make the listing skip entries nobody touched. The slot's own tree is its
# entries at the top, and every folder it owns, entered by descriptor, never by path, so neither a
# mode the job set nor a depth past PATH_MAX stops it. Each such folder gets 0700 through an O_PATH
# handle before it is listed, so nobody else can change it from then on, and the walk climbs back
# through ".." of the folder it is in, which nobody else can move, so it keeps one folder open at a
# time (from a folder of the slot's inside another uid's folder, which that uid can make
# unreadable, it returns to the descriptor it kept of that folder instead). The tree is held to the
# entry limit per folder: the slot's own entries at the top, and the listing of each folder it
# owns. Past it the helper fails (exit 4), which only the slot's own job can bring about.
# Folders of other uids that the slot can list, met at the top or in a folder of the slot's, are
# looked into as they are (no mode change, their parent held open to return to), and only the slot's
# own entries in them are removed or entered; folders of other uids inside them are not entered.
# Their owners can change them at will, renaming and refilling them while they are looked at, so
# that pass is best effort: each folder opened and each entry looked at there is taken from a budget
# of the entry limit per top, and once it is spent no further entry of theirs is looked at. Nothing
# found, missed or changed there fails the helper. Both walks go in listing order, as the
# supervisor's second look does. Other errors are left for that look to find.
_DROP_AND_CLEAR = (
    _DROP
    + """\
limit, tops = int(sys.argv[3]), sys.argv[4:]
doomed = []
for table, column in (("shm", "shmid"), ("msg", "msqid"), ("sem", "semid")):
    try:
        with open("/proc/sysvipc/" + table) as rows:
            head = rows.readline().split()
            at, owner, creator = head.index(column), head.index("uid"), head.index("cuid")
            size = head.index("size") if table == "shm" else at
            for row in rows:
                cells = row.split()
                if uid in (int(cells[owner]), int(cells[creator])):
                    doomed.append((table, int(cells[at]), int(cells[creator]), int(cells[size])))
    except FileNotFoundError:
        pass
unemptied = 0
if doomed:
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    libc.shmat.restype = ctypes.c_void_p
    libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    libc.shmdt.argtypes = [ctypes.c_void_p]
    libc.madvise.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    failed = (None, ctypes.c_void_p(-1).value)
    def perm(mode):
        state = ctypes.create_string_buffer(512)
        state[4:12] = uid.to_bytes(4, "little") + gid.to_bytes(4, "little")
        state[20:24] = mode.to_bytes(4, "little")
        return state
    def gone(ident):
        return libc.shmctl(ident, 2, ctypes.create_string_buffer(512)) != 0 and ctypes.get_errno() in (22, 43)
    for table, ident, creator, size in doomed:
        if table == "shm":
            emptied = False
            state = perm(0o600)
            if (
                libc.shmctl(ident, 1, state) == 0
                and libc.shmctl(ident, 2, state) == 0
                and int.from_bytes(state.raw[4:8], "little") == uid
                and int.from_bytes(state.raw[20:22], "little") & 0o777 == 0o600
            ):
                at = libc.shmat(ident, None, 0)
                if at not in failed:
                    if libc.madvise(at, size, 9) == 0:
                        emptied = True
                    elif creator == uid and size <= 256 << 20:
                        ctypes.memset(at, 0, size)
                        emptied = True
                    libc.shmdt(at)
            libc.shmctl(ident, 1, perm(0))
            libc.shmctl(ident, 0, None)
            if creator == uid and not emptied and not gone(ident):
                unemptied += 1
        elif table == "msg":
            libc.msgctl(ident, 0, None)
        else:
            libc.semctl(ident, 0, 0)
FOLDER = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
for top in tops:
    try:
        here = os.open(top, FOLDER)
    except OSError:
        continue
    names = os.listdir(here)[::-1]
    own, budget, mine, levels = 0, limit, True, []
    while True:
        if names and not mine and budget <= 0:
            names = []
        if names:
            if not mine:
                budget -= 1
            name = names.pop()
            try:
                st = os.stat(name, dir_fd=here, follow_symlinks=False)
            except OSError:
                continue
            folder = st.st_mode & 0o170000 == 0o040000
            if st.st_uid != uid:
                if not folder or not mine or budget <= 0:
                    continue
                budget -= 1
                try:
                    inner = os.open(name, FOLDER, dir_fd=here)
                except OSError:
                    continue
                try:
                    inner_names = os.listdir(inner)[::-1]
                except OSError:
                    os.close(inner)
                    continue
                levels.append((names, name, here, mine, False))
                here, names, mine = inner, inner_names, False
                continue
            if not levels:
                own += 1
                if own > limit:
                    sys.exit(4)
            if not folder:
                try:
                    os.unlink(name, dir_fd=here)
                except OSError:
                    pass
                continue
            try:
                handle = os.open(name, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=here)
            except OSError:
                continue
            try:
                if os.fstat(handle).st_uid != uid:
                    continue
                os.chmod("/proc/self/fd/%d" % handle, 0o700)
                inner = os.open(".", FOLDER, dir_fd=handle)
            except OSError:
                continue
            finally:
                os.close(handle)
            try:
                inner_names = os.listdir(inner)[::-1]
            except OSError:
                os.close(inner)
                continue
            if len(inner_names) > limit:
                sys.exit(4)
            if mine:
                levels.append((names, name, None, True, True))
                os.close(here)
            else:
                levels.append((names, name, here, False, True))
            here, names, mine = inner, inner_names, True
        elif levels:
            names, name, kept, mine, ours = levels.pop()
            if kept is None:
                parent = os.open("..", FOLDER, dir_fd=here)
                os.close(here)
                here = parent
            else:
                os.close(here)
                here = kept
            if ours:
                try:
                    os.rmdir(name, dir_fd=here)
                except OSError:
                    pass
        else:
            os.close(here)
            break
if unemptied:
    sys.exit(5)
"""
)
# Where the kernel keeps IPC that outlives processes, as files: POSIX shared memory and semaphores,
# and POSIX message queues where their file system is mounted. Under R28 no slot can write either:
# the supervisor refuses to start unless /dev/shm is root's alone and fs.mqueue.queues_max is 0. The
# cleanup still looks in both and clears what a slot left there, as defence in depth.
_IPC_FOLDERS = ("/dev/shm", "/dev/mqueue")
# Most entries of a slot's own tree the helper clears per folder: its own entries at the top of one
# of those folders, and the listing of each folder it owns. Past it the slot is not clear. It is
# also the budget, per top, for looking into other uids' folders, which is best effort: their owners
# can rename and refill them while they are looked at (and a listing then meets moved entries
# again), so nothing in them counts against a slot, and once the budget is spent no further entry of
# theirs is looked at.
_IPC_ENTRY_LIMIT = 100_000
_SYSVIPC = Path("/proc/sysvipc")
_SHM_DEST = 0o1000  # in the perms of /proc/sysvipc/shm: removed, but still attached somewhere
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


def clean_env(cwd: Path, tmp_dir: Path | None = None, home: Path | None = None) -> dict[str, str]:
    """Build the environment of a job: ``CLEAN_ENV``, ``HOME`` and its temporary directory.

    Args:
        cwd: The job directory.
        tmp_dir: The job's private temporary directory. When given, ``TMPDIR``, ``TEMP`` and
            ``TMP`` point to it, so the job never writes the shared ``/tmp``.
        home: The job's ``HOME``, a folder it owns; the job directory when not given.

    Returns:
        The only variables the job sees; nothing of the supervisor's own environment.
    """
    env = {**CLEAN_ENV, "HOME": str(cwd if home is None else home)}
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


def preexec(slot: Slot | None, limits: Limits, sockets: Filter | None = None) -> Callable[[], None]:
    """Build the function that runs in the child between ``fork`` and ``exec``.

    The child leaves the supervisor's session, lowers its priority to at least ``JOB_NICENESS``,
    raises its ``oom_score_adj`` to ``JOB_OOM_SCORE_ADJ`` (Linux), installs the seccomp filter that
    keeps it from sockets (``workbench.executor.seccomp``), drops its groups and switches to the
    slot's ids, checks that it holds no other id, then lowers its limits. The score and the filter
    come while the child is still root, before the drop. Failing at any step aborts the spawn, so a
    job never runs with more rights than intended; ``run_in_slot`` reports that as ``SPAWN_FAILED``.

    Args:
        slot: The slot whose ids the job runs as, or ``None`` to keep the current ids (tests, dev).
        limits: The limits to set in the child.
        sockets: The filter to install, built in the parent; ``None`` installs none (off Linux).

    Returns:
        The function to pass as ``preexec_fn``. In the child it raises ``RuntimeError`` for a
        slot with uid or gid 0 (before any id changes) or a drop that left another id, and
        ``OSError`` when the score or the filter cannot be set.
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

    linux = sys.platform == "linux"

    def child() -> None:
        """Isolate the freshly forked child."""
        os.setsid()
        niceness = max(JOB_NICENESS, os.getpriority(os.PRIO_PROCESS, 0))
        os.setpriority(os.PRIO_PROCESS, 0, niceness)
        if linux:
            fd = os.open("/proc/self/oom_score_adj", os.O_WRONLY)
            try:
                os.write(fd, str(JOB_OOM_SCORE_ADJ).encode())
            finally:
                os.close(fd)
        if sockets is not None:
            sockets.install()
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


def _status_fields(status: bytes) -> dict[bytes, list[bytes]]:
    """Read the ``Uid`` and ``VmRSS`` lines of the bytes of ``/proc/<pid>/status``, never decoding the name.

    Args:
        status: The file's contents.

    Returns:
        Each of the two lines that is there, split into its words.
    """
    fields: dict[bytes, list[bytes]] = {}
    for line in status.split(b"\n"):
        key, _, value = line.partition(b":")
        if key in (b"Uid", b"VmRSS"):
            fields[key] = value.split()
    return fields


def slot_memory(uid: int, *, proc: Path = Path("/proc")) -> int:
    """Add up the memory a slot's job holds: the resident memory of every process of the uid, and its SysV segments.

    A process counts by its real uid, which a job cannot change. A SysV shared memory segment the uid made and nobody
    has attached is in no process's resident set, so its resident and swapped bytes are added; an attached one is in
    the resident set of each process that has it. ``memfd_create``, the other way to hold memory outside every
    resident set, the jobs' seccomp filter refuses. A process that ends while it is read counts as gone.

    Args:
        uid: The slot's user id.
        proc: The ``/proc`` to read.

    Returns:
        Bytes.

    Raises:
        OSError: ``/proc`` cannot be listed, or the SysV table cannot be read.
    """
    total = 0
    with os.scandir(proc) as entries:
        pids = [entry.name for entry in entries if entry.name.isdigit()]
    for pid in pids:
        try:
            fields = _status_fields((proc / pid / "status").read_bytes())
        except (FileNotFoundError, ProcessLookupError):
            continue
        try:
            if int(fields[b"Uid"][0]) == uid and b"VmRSS" in fields:
                total += int(fields[b"VmRSS"][0]) * 1024
        except (KeyError, IndexError, ValueError):
            continue
    try:
        table = (proc / "sysvipc" / "shm").read_bytes()
    except FileNotFoundError:
        return total
    for row in _sysv_rows(table):
        if row.get(b"cuid") == uid and row.get(b"nattch") == 0:
            total += row.get(b"rss", row.get(b"size", 0)) + row.get(b"swap", 0)
    return total


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
    extra = [str(_IPC_ENTRY_LIMIT), *folders]
    _run_as_slot(_DROP_AND_CLEAR, slot, extra, f"could not clear the IPC objects of slot {slot.index}")


def _sysv_rows(table: bytes) -> list[dict[bytes, int]]:
    """Read a ``/proc/sysvipc`` table.

    Args:
        table: Its contents: a header naming the columns, then one object per line.

    Returns:
        One mapping per object from column name to value; ``perms`` is read as octal.

    Raises:
        ValueError: A row's length differs from the header's, or a cell is not a number.
    """
    lines = table.split(b"\n")
    head = lines[0].split()
    rows = []
    for line in lines[1:]:
        cells = line.split()
        if cells:
            pairs = zip(head, cells, strict=True)
            rows.append({name: int(cell, 8 if name == b"perms" else 10) for name, cell in pairs})
    return rows


def _sysv_table_holds(table: bytes, uid: int) -> bool:
    """Tell whether a ``/proc/sysvipc`` table holds an object ``uid`` owns or made: the first look.

    Args:
        table: The table's contents.
        uid: The user id to look for.

    Returns:
        Whether a row's ``uid`` or ``cuid`` is ``uid``; either may hold what the slot's job wrote.

    Raises:
        KeyError: The header has no ``uid`` or ``cuid`` column.
        ValueError: The table has an unexpected shape.
    """
    return any(uid in (row[b"uid"], row[b"cuid"]) for row in _sysv_rows(table))


def _sysv_table_left(table: bytes, uid: int, *, shm: bool) -> bool:
    """Tell whether a ``/proc/sysvipc`` table still holds an object ``uid`` made: the second look.

    Only the creator counts. Another slot's job can hand objects it made to any uid with
    ``IPC_SET``, at any moment, so judging by owner would let it fail this slot's cleanup at will;
    the creator never changes, and only the slot's own processes, all gone now, could have made one.
    What another slot made and handed over cannot hold anything of this slot's last job unless
    that job wrote it there while it ran, and the other slot, as its creator, can read it anyway.

    Args:
        table: The table's contents.
        uid: The user id to look for.
        shm: Whether this is the shared memory table, where a segment marked ``SHM_DEST`` is
            already removed and kept only for the other slot that still has it attached.

    Returns:
        Whether an object ``uid`` made is still there.

    Raises:
        KeyError: The header has no ``cuid`` or ``perms`` column.
        ValueError: The table has an unexpected shape.
    """
    return any(row[b"cuid"] == uid and not (shm and row[b"perms"] & _SHM_DEST) for row in _sysv_rows(table))


def _folder_holds(top: str, uid: int) -> bool:
    """Look in ``top``, and one level into the folders of other uids there, for an entry ``uid`` owns.

    Whatever a slot leaves below such a folder starts with an entry of its own at the top, or in a
    folder of another uid the slot could write to, a file or a folder; the helper looks in the same
    places, in the same order. The look is defence in depth (ruling R28): in production no slot can
    write ``/dev/shm`` or make a POSIX queue, so it finds nothing there. Where slots can write the
    folder, it does not see every entry at the top: another uid that renames entries onto existing
    names and removes the one where a read of the listing ends, while the folder is listed, makes
    later reads skip entries nobody touched, the slot's own among them. The look into other uids'
    folders is best effort too, from a budget of ``_IPC_ENTRY_LIMIT`` per top, as the helper's is
    (``_foreign_folder_holds``).

    Args:
        top: A folder, such as ``/dev/shm``, opened without following a link.
        uid: The user id to look for.

    Returns:
        Whether an entry that belongs to ``uid`` was found. Nothing else makes the folder unclear:
        not how many entries it or another uid's folder holds, nor a folder of another uid that
        vanished, changed or could not be read while it was looked into. A missing ``top`` holds
        nothing.

    Raises:
        OSError: ``top`` is a link or cannot be read.
    """
    try:
        fd = os.open(top, DIR_FLAGS)
    except FileNotFoundError:
        return False
    budget = _IPC_ENTRY_LIMIT
    with closing(fd), os.scandir(fd) as entries:
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
            except FileNotFoundError:
                continue
            if st.st_uid == uid:
                return True
            if budget > 0 and stat.S_ISDIR(st.st_mode):
                found, budget = _foreign_folder_holds(fd, entry.name, uid, budget)
                if found:
                    return True
    return False


def _foreign_folder_holds(parent: int, name: str, uid: int, budget: int) -> tuple[bool, int]:
    """Look, best effort, into a folder of another uid for an entry ``uid`` owns.

    The folder's owner can rename it, refill it or remove it while it is looked at, and a listing
    then meets moved entries again; so nothing seen, or not seen, here makes the slot unclear,
    except an entry of the slot's that is actually found. Opening the folder and each entry looked
    at take one from ``budget``; when it is spent, the look stops.

    Args:
        parent: The descriptor of the folder that holds it.
        name: Its name there.
        uid: The user id to look for.
        budget: How many more entries of other uids' folders may be looked at; above 0.

    Returns:
        Whether an entry of ``uid`` was found, and the budget left. A folder that vanished, became a
        link or cannot be read counts as empty from where the look got to.
    """
    budget -= 1
    try:
        fd = os.open(name, DIR_FLAGS, dir_fd=parent)
    except OSError:
        return False, budget
    try:
        with closing(fd), os.scandir(fd) as entries:
            for entry in entries:
                if budget <= 0:
                    break
                budget -= 1
                with contextlib.suppress(OSError):
                    if entry.stat(follow_symlinks=False).st_uid == uid:
                        return True, budget
    except OSError:  # removed or made unreadable while it was scanned
        pass
    return False, budget


def _sysv_holds(uid: int, judge: Callable[[bytes, str], bool]) -> bool:
    """Apply ``judge`` to each ``/proc/sysvipc`` table there is.

    Args:
        uid: The slot's user id; only for the caller's judge.
        judge: Called with a table's contents and its name.

    Returns:
        Whether ``judge`` holds for any table.

    Raises:
        OSError: A table cannot be read.
    """
    for name in ("shm", "msg", "sem"):
        try:
            content = (_SYSVIPC / name).read_bytes()
        except FileNotFoundError:
            continue
        if judge(content, name):
            return True
    return False


def _slot_has_ipc(uid: int) -> bool:
    """The first look: IPC the uid owns or made, or an entry of its below ``_IPC_FOLDERS``.

    Args:
        uid: The slot's user id.

    Returns:
        Whether there is any, so the helper must run.

    Raises:
        OSError: A table or folder could not be read.
        KeyError: A table lacks a column.
        ValueError: A table has an unexpected shape.
    """
    if _sysv_holds(uid, lambda table, name: _sysv_table_holds(table, uid)):
        return True
    return any(_folder_holds(folder, uid) for folder in _IPC_FOLDERS)


def _slot_left_ipc(uid: int) -> bool:
    """The second look: IPC the uid made (``_sysv_table_left``), or an entry of its below ``_IPC_FOLDERS``.

    Args:
        uid: The slot's user id.

    Returns:
        Whether any is left, so the slot is not clear.

    Raises:
        OSError: A table or folder could not be read.
        KeyError: A table lacks a column.
        ValueError: A table has an unexpected shape.
    """
    if _sysv_holds(uid, lambda table, name: _sysv_table_left(table, uid, shm=name == "shm")):
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
    the tables in ``/proc/sysvipc`` and those folders are searched for anything the uid owns or
    made (``_slot_has_ipc``). If there is, a second helper, run like the first, removes it as the
    slot (``_DROP_AND_CLEAR``), and a second look must find nothing the uid made: objects by
    creator, entries by owner (``_slot_left_ipc``). That adds at most one more helper run of 30 s.
    For the two folders this is defence in depth (ruling R28): the supervisor starts only where no
    slot can write ``/dev/shm`` or make a POSIX queue, since a listing of a folder other slots
    write can be made to skip entries. SysV IPC is not affected by that: each object is found in
    ``/proc/sysvipc`` and removed by its id, with no folder listed.

    It blocks, so call it from a thread. Does nothing without a slot or off Linux.

    Args:
        slot: The slot to clear, or ``None``.

    Raises:
        RuntimeError: The only failure. Processes or IPC of the slot remain after the cleanup, a
            helper failed or timed out, ``/proc`` or the IPC could not be checked, or the slot runs
            as root. The message says which, naming only the slot's index. The slot must not be
            reused.
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
    except (OSError, KeyError, ValueError, IndexError) as exc:
        raise RuntimeError(f"processes of slot {slot.index} could not be checked") from exc
    try:
        if _slot_has_ipc(slot.uid):
            _clear_ipc_as_slot(slot)
            if _slot_left_ipc(slot.uid):  # a RuntimeError passes the handler below unchanged
                raise RuntimeError(f"IPC objects of slot {slot.index} survived the cleanup")
    except (OSError, KeyError, ValueError, IndexError) as exc:
        raise RuntimeError(f"IPC objects of slot {slot.index} could not be checked") from exc


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
    home: Path | None = None,
    cleanup_pool: Executor | None = None,
    sockets: Filter | None = None,
    watch: Callable[[], str | None] | None = None,
    watch_interval_s: float = WATCH_INTERVAL_S,
) -> tuple[Literal["ok", "error", "timeout", "killed"], int]:
    """Run ``argv`` as a fresh process in ``cwd`` under the slot's ids and the limits.

    The process leads its own session, runs at niceness ``JOB_NICENESS`` or more and with umask
    ``077``, so what it creates is private to its uid. When it ends, by itself, on the wall
    clock, on the watchdog or because this task is cancelled, everything left in its process group
    is killed with it. With a slot, every other process of the slot's uid is killed too, including
    those that left the group; that also happens when the child could not become the slot.

    The watchdog, when given, runs in a thread every ``watch_interval_s`` while the job runs; when
    it names a limit the job broke, the job's group is killed and the job ends ``killed``.

    Args:
        argv: The command line; ``argv[0]`` should be an absolute path since ``PATH`` is fixed.
        cwd: The job directory; the process starts there.
        slot: The slot to run as, or ``None`` to keep the current uid (tests and dev).
        limits: Wall time and the kernel limits.
        tmp_dir: The job's private temporary directory, handed over as ``TMPDIR``, ``TEMP`` and ``TMP``.
        home: The job's ``HOME``; ``cwd`` when not given.
        cleanup_pool: The pool the slot cleanup runs on. A slot process can hold a cleanup for
            30 s, so the supervisor gives one of its own, a thread per slot; ``None`` is the
            loop's default pool.
        sockets: The seccomp filter the job runs under (``preexec``).
        watch: Measures the running job; returns what limit it broke, or ``None``. An error in it
            counts as nothing broken.
        watch_interval_s: Seconds between two measures.

    Returns:
        The outcome and the exit code (negative for death by a signal). ``timeout`` means the
        wall time ran out and the group was killed; ``killed`` that the kernel or the watchdog
        stopped it. ``("error", SPAWN_FAILED)`` means the job never started: the child failed to
        lower its priority, raise its OOM score, install the filter, become the slot or set a
        limit (``preexec`` raised, which ``subprocess`` reports as ``SubprocessError``). Nothing of
        ``argv`` ran then.

    Raises:
        RuntimeError: Processes of the slot survived the cleanup; the slot must be retired.
        OSError: ``argv[0]`` could not be executed.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            env=clean_env(cwd, tmp_dir, home),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=preexec(slot, limits, sockets),
            start_new_session=False,
            umask=0o077,
        )
    except subprocess.SubprocessError:
        await _clear(slot, cleanup_pool)
        return "error", SPAWN_FAILED
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limits.wall_s
    waiting = asyncio.ensure_future(process.wait())
    returncode: int | None = None
    broken = False
    try:
        while (left := deadline - loop.time()) > 0:
            done, _ = await asyncio.wait({waiting}, timeout=left if watch is None else min(left, watch_interval_s))
            if done:
                returncode = waiting.result()
                break
            if watch is not None and await _measure(watch):
                broken = True
                break
    finally:
        _kill_group(process.pid)
        reaped = await process.wait()
        if not waiting.done():
            waiting.cancel()
        await _clear(slot, cleanup_pool)
    if broken:
        return "killed", reaped
    if returncode is None:
        return "timeout", reaped
    return _outcome(returncode), returncode


async def _measure(watch: Callable[[], str | None]) -> bool:
    """Run the watchdog once, in a thread of the loop's default pool.

    Args:
        watch: The watchdog.

    Returns:
        Whether it named a limit the job broke; an error in it is not one.
    """
    try:
        return await asyncio.to_thread(watch) is not None
    except Exception:
        return False
