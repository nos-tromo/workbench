"""The executor's supervisor: ``python -m workbench.executor.supervisor``.

It runs as root with ``SETUID``, ``SETGID``, ``CHOWN``, ``FOWNER``, ``DAC_OVERRIDE`` and ``KILL``, in a
container with no network, and takes one job per connection on a Unix socket the api shares. For
each job it copies the requested files of the chat folder into a private job folder, runs the job
there as the uid of a free slot (``workbench.executor.sandbox``) and, once no process of that uid is
left, reads the result and stages the outputs for the api.

The job folder, ``<data_dir>/jobs/<job-id>/``:

- the folder itself: root's, ``0710``, its group the slot's: the job may pass through it and
  nobody else may, and the job can neither add a name to it nor open it to others;
  ``spec.json`` (root's, ``0444``) lies in it;
- ``workspace/<chat-id>/``: root's, ``0755``, the job's working folder, holding root-owned ``0444``
  copies of the inputs at their paths below the chat folder;
- ``workspace/<chat-id>/out/``: the slot's, ``0700``, where the job writes its outputs;
- ``home/``: the slot's, ``0700``, the job's ``HOME``, where it writes ``result.json``; with
  ``home/tmp/``, the slot's, ``0700``, its ``TMPDIR``.

``out/`` and ``home/`` are all the job can write. While it runs, a watchdog measures them, and
the memory and OOM scores of the slot's processes, about twice a second, and stops the job past
its limits. One thread of its own measures every running job (``_Watchdog``); the wall clock is
kept on the event loop, so neither waits on the other. Below a floor of free space on the data
volume, every running job is stopped and no job starts (``below_floor``): a file a job removed
and keeps open is in no folder the watchdog walks, but it takes room on the volume.

The api owns the workspaces and the staging folder, and the job owns ``out/`` and ``home/``, so
neither is trusted: everything below ``data_dir`` is opened relative to a folder already open,
with ``O_NOFOLLOW``, and nothing is read from the job folder before the slot is cleared. Logs name
slots and counts, never a job, a file or anything a request holds.
"""

import asyncio
import contextlib
import json
import logging
import os
import resource
import shutil
import signal
import socket
import stat
import sys
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from workbench.config import UID_POOL_SIZE, ConfigError, Settings, load_settings
from workbench.executor import seccomp
from workbench.executor.fs import (
    CREATE_FLAGS,
    DIR_FLAGS,
    READ_FLAGS,
    closing,
    copy_bytes,
    make_dir,
    measure,
    open_dir,
    open_regular,
    settle,
    write_all,
)
from workbench.executor.prepare_volume import API_UID, Owners
from workbench.executor.sandbox import WATCH_INTERVAL_S, Slot, SlotUse, run_in_slot, slots_use
from workbench.layout import JOBS, STAGING, WORKSPACES
from workbench.paths import PathError, check_filename
from workbench.protocol import (
    JOB_ID,
    KILLED,
    MAX_LINE,
    JobRequest,
    JobResult,
    Limits,
    OutputFile,
    decode_request,
    encodable,
    encode,
)

MB = 1024 * 1024
MAX_OUTPUTS = 20
OUT = "out"
#: Most entries a running job's folders may hold, files and folders together.
MAX_JOB_ENTRIES = 10_000
#: Deepest a running job's folders may nest.
MAX_JOB_DEPTH = 256
#: Seconds between two tries to remove the job folders a failed removal left.
LEFTOVER_RETRY_S = 300
#: Descriptors the supervisor keeps beyond the walks of the job folders: its socket and connections, each job's process
#: and its watch, and the cleanup helpers, with room to spare.
NOFILE_MARGIN = 256
#: The least free space the data volume keeps for the api and the database (R84): 1 GiB, or a share of the volume where
#: that is more. Below it every running job is stopped and none starts. A fixed rule, not a setting.
FREE_FLOOR_MIN = 1 << 30
#: That share: one part in 20, 5 %.
FREE_FLOOR_PARTS = 20
#: The refusal of a job the floor stops or keeps from starting: a runner's ``code: text``, which the api answers 507.
STORAGE_FULL = "storage_full: storage is full"

_WORKSPACE = "workspace"
_HOME = "home"
_TMP = "tmp"
_SPEC = "spec.json"
_RESULT = "result.json"
_RESULT_SLACK = 64 * 1024
_REQUEST_TIMEOUT_S = 30.0
# The protocol's defaults; R19 sizes the container's pids limit from them.
_MAX_NPROC = 64
_MAX_NOFILE = 256
# Slot uids are ordinary users: no system account below, nothing at or past nobody (65534) above.
_FIRST_SLOT_UID = 1000
_LAST_SLOT_UID = 65533
_PASSWD = Path("/etc/passwd")
_MEMORY_MAX = Path("/sys/fs/cgroup/memory.max")
_PIDS_MAX = Path("/sys/fs/cgroup/pids.max")
_SYSCTLS = Path("/proc/sys/fs")
_MOUNTINFO = Path("/proc/self/mountinfo")
_SHM = Path("/dev/shm")
_SHARED_TMP = Path("/tmp")
_STATUS = Path("/proc/self/status")
_DEV_ZERO = Path("/dev/zero")
_QUEUES_MAX = Path("mqueue/queues_max")  # below /proc/sys/fs
_CLOSE_GRACE_S = 5.0  # for the answer to the request that retired the last slot
# Each must read 1. Hard links: a job cannot give out/ a name for a file it does not own, such as
# the api's database, which root would then stage. Symlinks: a link a job leaves in a sticky,
# world-writable folder is not followed by anyone else.
_LINK_PROTECTIONS = (
    ("protected_hardlinks", "so a job cannot link files it does not own"),
    ("protected_symlinks", "so no one follows a job's links in shared folders"),
)

_log = logging.getLogger("workbench.executor")

Status = Literal["ok", "error", "timeout", "killed", "busy"]


class NoUsableSlot(Exception):
    """Every slot is retired; the process ends so that its restart brings fresh slots and IPC."""


class SocketRefused(RuntimeError):
    """The socket's folder or place is not safe; serving does not start. The message is a fixed text."""


class _SetupError(Exception):
    """The job folder could not be made; nothing ran."""


class _OutputsRefused(Exception):
    """The job's outputs break a limit; the message says which, for the job's answer."""


def job_argv(spec: Path, result: Path) -> list[str]:
    """Build the command line of a job.

    Args:
        spec: The job's ``spec.json``.
        result: Where the job writes its ``result.json``: in its own ``home/``.

    Returns:
        ``python -I -m workbench.executor.job <spec> <result>``, with this interpreter.
    """
    return [sys.executable, "-I", "-m", "workbench.executor.job", str(spec), str(result)]


def make_slots(settings: Settings) -> tuple[Slot, ...]:
    """Build the slots: slot ``n`` runs as uid and gid ``slot_uid_base + n``.

    Args:
        settings: The executor's settings.

    Returns:
        One slot per ``executor_slots``.

    Raises:
        ValueError: The slots would pass the pool of ``UID_POOL_SIZE`` uids from ``slot_uid_base``,
            a slot would run as root, a uid lies outside 1000-65533, or the pool holds the api's uid.
    """
    base, count = settings.slot_uid_base, settings.executor_slots
    last = base + count - 1
    if count > UID_POOL_SIZE or last > base + UID_POOL_SIZE - 1:
        raise ValueError(f"at most {UID_POOL_SIZE} slots: the pool is {base} to {base + UID_POOL_SIZE - 1}")
    slots = tuple(Slot(index=n, uid=base + n, gid=base + n) for n in range(count))
    if any(s.uid <= 0 or s.gid <= 0 or not base <= s.uid <= base + UID_POOL_SIZE - 1 for s in slots):
        raise ValueError("a slot would run as root or outside its pool")
    if base < _FIRST_SLOT_UID or last > _LAST_SLOT_UID:
        raise ValueError(f"slot uids must lie between {_FIRST_SLOT_UID} and {_LAST_SLOT_UID}")
    if base <= API_UID <= last:
        raise ValueError(f"the slot uids must not include the api's uid {API_UID}")
    return slots


def free_space_floor(size: int) -> int:
    """Give the free space below which the data volume takes no job.

    Args:
        size: The volume's size in bytes.

    Returns:
        ``FREE_FLOOR_MIN``, or one ``FREE_FLOOR_PARTS``-th of ``size`` where that is more.
    """
    return max(FREE_FLOOR_MIN, size // FREE_FLOOR_PARTS)


def below_floor(data_dir: Path) -> bool:
    """Tell whether the data volume's free space is below its floor (``free_space_floor``).

    The free space is what a process without privilege may still use (``f_bavail``), as the api and the jobs are. It
    counts every file still open, named or not: a job can remove a file and keep writing it, out of every folder the
    watchdog walks, but not out of the volume's free space.

    Args:
        data_dir: The data volume.

    Returns:
        Whether it is below.

    Raises:
        OSError: The volume cannot be asked.
    """
    st = os.statvfs(data_dir)
    return st.f_bavail * st.f_frsize < free_space_floor(st.f_blocks * st.f_frsize)


def _passwd_uids(passwd: Path) -> set[int]:
    """Read the user ids of ``passwd``.

    Args:
        passwd: An ``/etc/passwd`` file.

    Returns:
        Every uid it lists; a line that is not a user entry is skipped.

    Raises:
        OSError: The file cannot be read.
    """
    uids: set[int] = set()
    for line in passwd.read_text(encoding="utf-8", errors="replace").splitlines():
        fields = line.split(":")
        if len(fields) >= 3 and fields[2].isascii() and fields[2].isdigit():
            uids.add(int(fields[2]))
    return uids


def _cgroup_limit(limit: Path) -> int | None:
    """Read one of the container's cgroup v2 limits, such as ``memory.max`` or ``pids.max``.

    Args:
        limit: The cgroup's file.

    Returns:
        The limit, or ``None`` when there is none (``max``) or it cannot be read.
    """
    try:
        raw = limit.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return int(raw) if raw.isdigit() else None


def startup_problem(
    settings: Settings,
    *,
    passwd: Path = _PASSWD,
    memory_max: Path = _MEMORY_MAX,
    sysctls: Path = _SYSCTLS,
    mountinfo: Path = _MOUNTINFO,
    shm: Path = _SHM,
    tmp: Path = _SHARED_TMP,
    status: Path = _STATUS,
    pids_max: Path = _PIDS_MAX,
    sockets: Callable[[], str | None] = seccomp.problem,
    euid: int | None = None,
) -> str | None:
    """Find a reason the supervisor must not start.

    Args:
        settings: The executor's settings.
        passwd: The image's user list.
        memory_max: The container's cgroup memory limit, if it has one.
        sysctls: The kernel's ``fs`` settings, ``/proc/sys/fs``; ``mqueue/queues_max`` below it is
            the IPC namespace's own.
        mountinfo: The process's mount table, ``/proc/self/mountinfo``.
        shm: The folder of POSIX shared memory and semaphores, ``/dev/shm``.
        tmp: The shared temporary folder, ``/tmp``.
        status: The process's status, ``/proc/self/status``.
        pids_max: The container's cgroup process limit, if it has one.
        sockets: Tries the seccomp filter that keeps jobs from sockets; returns why it does not hold.
        euid: The effective uid to judge; the process's own by default.

    Returns:
        A short reason, or ``None`` when root runs it, the slot pool is sound (``make_slots``),
        every slot uid is a user of the image, the memory limit holds every slot's job memory,
        ``fs.protected_hardlinks`` and ``fs.protected_symlinks`` both read 1 (the host's kernel
        settings, which a container cannot change), ``/dev/mqueue`` is a mounted mqueue file
        system (without it a job's queues can be neither listed nor removed), ``shm`` is a folder
        of root's that neither group nor others may write (``_root_only``), and
        ``fs.mqueue.queues_max`` reads 0. Those two keep every slot from leaving anything in
        either folder: a slot uid has no right to add a name to ``/dev/shm``, and without
        ``CAP_SYS_RESOURCE`` it may not make a queue past that limit. The slot cleanup still
        looks in both folders, as defence in depth; on its own it cannot be relied on there, since
        another slot renaming and removing entries while a folder is listed can make the listing
        skip entries nobody touched. ``tmp`` must be root's alone too: every job has a ``TMPDIR`` of
        its own, so ``/tmp`` is the last folder two slots could share. ``status`` must show the
        process under a seccomp filter (``Seccomp: 2``), such as Docker's default profile: it
        refuses ``add_key``, ``keyctl`` and ``request_key``, and a kernel keyring a job filled
        would outlive the job and its slot's cleanup; and ``NoNewPrivs: 1``, which keeps any
        setuid program inert for a job. The process limit, like the memory limit, must hold every
        slot's 64 processes and 64 for the supervisor. Last, the filter that keeps jobs from
        sockets must hold, tried in a child: the machine is x86_64 or aarch64, and a process
        under it cannot open an Internet socket.
    """
    if (os.geteuid() if euid is None else euid) != 0:
        return "the supervisor must run as root"
    try:
        slots = make_slots(settings)
    except ValueError as exc:
        return str(exc)
    try:
        known = _passwd_uids(passwd)
    except OSError:
        return "the image's user list cannot be read"
    missing = sum(1 for slot in slots if slot.uid not in known)
    if missing:
        return f"{missing} of the {len(slots)} slot users is missing from the image"
    limit = _cgroup_limit(memory_max)
    if limit is not None and limit < len(slots) * settings.job_memory_mb * MB:
        return f"the container's memory limit is below {len(slots)} slots of {settings.job_memory_mb} MB"
    processes = _cgroup_limit(pids_max)
    if processes is not None and processes < len(slots) * _MAX_NPROC + _MAX_NPROC:
        return f"the container's process limit is below {len(slots)} slots of {_MAX_NPROC} and {_MAX_NPROC} more"
    for name, why in _LINK_PROTECTIONS:
        try:
            value = (sysctls / name).read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            value = ""
        if value != "1":
            return f"fs.{name} must be 1, {why}"
    if not _mqueue_mounted(mountinfo):
        return "/dev/mqueue must be a mounted mqueue file system, so the slots' queues can be cleared"
    try:
        shm_root_only = _root_only(os.lstat(shm))
    except OSError:
        shm_root_only = False
    if not shm_root_only:
        return "/dev/shm must be a folder of root's that no one else may write, so no job can leave anything there"
    try:
        queues_max = (sysctls / _QUEUES_MAX).read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        queues_max = ""
    if queues_max != "0":
        return "fs.mqueue.queues_max must be 0, so no job can make a POSIX message queue"
    try:
        tmp_root_only = _root_only(os.lstat(tmp))
    except OSError:
        tmp_root_only = False
    if not tmp_root_only:
        return "/tmp must be a folder of root's that no one else may write, so no job can leave anything there"
    if _status_value(status, "Seccomp") != "2":
        return (
            "the supervisor must run under a seccomp filter such as Docker's default profile, "
            "so no job can use the kernel's keyrings"
        )
    if _status_value(status, "NoNewPrivs") != "1":
        return "the supervisor must run with no-new-privileges, so no job can gain rights through a setuid program"
    return sockets()


def _root_only(st: os.stat_result) -> bool:
    """Tell from its ``lstat`` whether a folder is root's and no one else may add or remove a name in it.

    Args:
        st: The folder's ``lstat``; a link is judged as the link, never as its target.

    Returns:
        Whether it is a folder, owned by uid 0, with neither the group nor the others write bit.
    """
    return stat.S_ISDIR(st.st_mode) and st.st_uid == 0 and st.st_mode & 0o022 == 0


def _status_value(status: Path, field: str) -> str | None:
    """Read one line of a process's status, such as ``Seccomp`` (0 none, 1 strict, 2 filter) or ``NoNewPrivs``.

    Args:
        status: A ``/proc/<pid>/status`` file.
        field: The line's name.

    Returns:
        Its value, stripped; ``None`` when there is no such line or the file cannot be read.
    """
    try:
        lines = status.read_text(encoding="ascii", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        name, colon, value = line.partition(":")
        if colon and name == field:
            return value.strip()
    return None


def _mqueue_mounted(mountinfo: Path) -> bool:
    """Tell from a mount table whether ``/dev/mqueue`` is a mounted mqueue file system.

    Args:
        mountinfo: A ``/proc/<pid>/mountinfo`` file: the mount point is the fifth field, the file
            system type the first after the ``-`` separator.

    Returns:
        Whether it is; ``False`` when the table cannot be read.
    """
    try:
        lines = mountinfo.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        fields, _, rest = line.partition(" - ")
        mount = fields.split()
        if len(mount) >= 5 and mount[4] == "/dev/mqueue" and rest.split()[:1] == ["mqueue"]:
            return True
    return False


def _read_result(home: int, limit: int, owner: int) -> dict[str, Any] | None:
    """Read the job's ``result.json`` as a JSON object.

    Args:
        home: The job's ``home/``, opened.
        limit: Most bytes to read.
        owner: The uid the job ran as, which must own the file (``_own_file``).

    Returns:
        The object, or ``None`` if the file is missing, not a regular file of the job's alone,
        larger than ``limit``, not JSON or not an object.
    """
    try:
        fd = os.open(_RESULT, READ_FLAGS, dir_fd=home)
    except OSError:
        return None
    chunks: list[bytes] = []
    size = 0
    try:
        with closing(fd):
            st = os.fstat(fd)
            if not _own_file(st, owner) or st.st_size > limit:
                return None
            while size <= limit and (chunk := os.read(fd, limit + 1 - size)):
                chunks.append(chunk)
                size += len(chunk)
    except OSError:
        return None
    if size > limit:
        return None
    data = b"".join(chunks)
    try:
        value = encodable(json.loads(data))
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _own_file(st: os.stat_result, owner: int) -> bool:
    """Tell whether a file is the job's alone: its uid's, with no other name anywhere.

    A second name means a hard link, which could name a file the job may not read, such as the
    api's database, and root would copy it.

    Args:
        st: The file's status.
        owner: The uid the job ran as.

    Returns:
        Whether it is a regular file of ``owner`` with one link.
    """
    return stat.S_ISREG(st.st_mode) and st.st_uid == owner and st.st_nlink == 1


def _find_outputs(out: int, max_mb: int, owner: int) -> tuple[list[tuple[str, os.stat_result]], int]:
    """List the job's outputs: regular files directly in ``out/`` whose names a workspace accepts.

    Symlinks, folders and anything else are left out, and so are names ``check_filename`` refuses.
    Regular files that are not the job's alone (``_own_file``) are left out and counted.

    Args:
        out: The job's ``out/``, opened.
        max_mb: Most megabytes the outputs may hold in all.
        owner: The uid the job ran as.

    Returns:
        Name and ``lstat`` of each output, by name, and how many files were not the job's alone.

    Raises:
        _OutputsRefused: There are more than ``MAX_OUTPUTS``, or together they hold more than ``max_mb``.
        OSError: ``out/`` cannot be listed.
    """
    found: list[tuple[str, os.stat_result]] = []
    dropped = 0
    with os.scandir(out) as entries:
        for entry in entries:
            try:
                st = entry.stat(follow_symlinks=False)
                check_filename(entry.name)
            except (OSError, PathError):
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            if not _own_file(st, owner):
                dropped += 1
                continue
            found.append((entry.name, st))
            if len(found) > MAX_OUTPUTS:
                raise _OutputsRefused(f"too many outputs: at most {MAX_OUTPUTS}")
    if sum(st.st_size for _, st in found) > max_mb * MB:
        raise _OutputsRefused(f"the outputs are larger than {max_mb} MB in all")
    return sorted(found, key=lambda item: item[0]), dropped


def _dropped(count: int) -> str:
    """Say how many outputs were dropped as not the job's own.

    Args:
        count: How many.

    Returns:
        The note for the answer's message; empty for none.
    """
    if not count:
        return ""
    return f"dropped {count} output{'' if count == 1 else 's'}: not the job's own file"


def _full(job_id: str) -> JobResult:
    """Refuse a job because the data volume is below its free-space floor.

    Args:
        job_id: The request's id.

    Returns:
        An ``error`` whose result is the refusal ``STORAGE_FULL``, as a runner's refusal reads.
    """
    return _answer(job_id, "error", "the data volume is full", {"error": STORAGE_FULL})


def _answer(job_id: str, status: Status, message: str = "", result: dict[str, Any] | None = None) -> JobResult:
    """Build an answer without outputs.

    Args:
        job_id: The request's id.
        status: The status.
        message: The short reason.
        result: The job's result, if it may be passed on.

    Returns:
        The answer.
    """
    return JobResult(job_id=job_id, status=status, result=result or {}, outputs=[], message=message)


def _line(answer: JobResult) -> bytes:
    """Encode an answer, replacing one that cannot be sent with a short error.

    Args:
        answer: The answer.

    Returns:
        One line of at most ``MAX_LINE`` bytes.
    """
    try:
        line = encode(answer)
    except ValueError:
        return encode(_answer(answer.job_id, "error", "the job's result could not be encoded"))
    if len(line) > MAX_LINE:
        return encode(_answer(answer.job_id, "error", "the job's result is too large"))
    return line


@dataclass
class _Job:
    """A job folder the supervisor has made and holds open.

    Attributes:
        name: The job id, the folder's name in ``jobs``.
        jobs: ``jobs``, opened.
        folder: The job folder, opened.
        out: Its ``out/``, opened.
        home: Its ``home/``, opened.
        path: The job folder's path, for the job's command line.
        cwd: The job's working folder.
        runner: The uid the job runs as, which must own its outputs and its result.
    """

    name: str
    jobs: int
    folder: int
    out: int
    home: int
    path: Path
    cwd: Path
    runner: int


#: What the watchdog says when it cannot measure a job: the job is stopped and its slot retired (fail closed).
UNMEASURED = "its use of memory and disk could not be measured"
#: What it says when a process of the job's slot runs below the OOM score jobs get.
LOWERED = "one of its processes lowered its OOM score"
#: What it says when the data volume is below its free-space floor; the job is refused as ``STORAGE_FULL``.
FULL = "the data volume is below its free-space floor"


class _Floor:
    """Whether the data volume is below its free-space floor, as last asked; each crossing is logged, without names."""

    def __init__(self, data_dir: Path) -> None:
        """Start above the floor.

        Args:
            data_dir: The data volume.
        """
        self._data_dir = data_dir
        self._lock = threading.Lock()
        self._below = False

    def below(self) -> bool:
        """Ask the volume (``below_floor``) and log when the answer changed.

        Returns:
            Whether it is below its floor.

        Raises:
            OSError: The volume cannot be asked.
        """
        below = below_floor(self._data_dir)
        with self._lock:
            crossed, self._below = below != self._below, below
        if crossed and below:
            _log.warning("the data volume is below its free-space floor: running jobs stop and no job starts")
        elif crossed:
            _log.info("the data volume is above its free-space floor again: jobs start")
        return below


class _Watch:
    """One running job in the watchdog: descriptors of its ``home/`` and ``out/``, its slot and limits, and its stop.

    It holds descriptors of its own of the job's ``home/`` and ``out/``, and a lock: a measure still running in the
    watchdog's thread when the job is over keeps them until it ends, and ``close`` waits for it, so it never measures a
    descriptor that meanwhile names another folder.

    Attributes:
        index: The slot's index, for the log.
        uid: The slot's uid, whose processes are summed; ``None`` without a slot (tests, dev), whose job shares the
            supervisor's uid.
        stop: Completes with the limit the job broke; ``run_in_slot`` waits on it.
        loop: The loop ``stop`` belongs to.
    """

    def __init__(self, job: _Job, index: int, slot: Slot | None, limits: Limits, disk_mb: int) -> None:
        """Watch ``job`` against its limits.

        Args:
            job: The job's folders.
            index: Its slot's index.
            slot: Its slot; without one the memory is not summed.
            limits: The limits it runs with; ``memory_mb`` bounds its processes together.
            disk_mb: The most its folders may hold, ``WORKBENCH_WORKSPACE_MAX_MB``.

        Raises:
            OSError: The folders' descriptors could not be duplicated.
            RuntimeError: There is no running event loop.
        """
        self._lock = threading.Lock()
        self._folders: tuple[int, ...] = ()
        self.loop = asyncio.get_running_loop()
        home = os.dup(job.home)
        try:
            self._folders = (home, os.dup(job.out))
        except BaseException:
            os.close(home)
            raise
        self.index = index
        self.uid = None if slot is None else slot.uid
        self._memory = limits.memory_mb * MB
        self._memory_mb = limits.memory_mb
        self._disk = disk_mb * MB
        self._disk_mb = disk_mb
        self.stop: asyncio.Future[str] = self.loop.create_future()

    def judge(self, use: SlotUse | None) -> str | None:
        """Judge the job by its slot's use, from the watchdog's look at ``/proc``, and by a measure of its folders.

        Args:
            use: What the slot's processes hold; ``None`` without a slot.

        Returns:
            The limit the job broke, as its answer says it, or ``None``.

        Raises:
            OSError: A folder could not be measured.
        """
        with self._lock:
            if not self._folders:
                return None
            if use is not None and use.lowered:
                return LOWERED
            if use is not None and use.memory > self._memory:
                return f"its processes used more than {self._memory_mb} MB of memory together"
            found = measure(self._folders, max_entries=MAX_JOB_ENTRIES, max_depth=MAX_JOB_DEPTH)
            if found.entries > MAX_JOB_ENTRIES:
                return f"it wrote more than {MAX_JOB_ENTRIES:,} files and folders".replace(",", " ")
            if found.depth > MAX_JOB_DEPTH:
                return f"its folders nest more than {MAX_JOB_DEPTH} deep"
            if found.bytes > self._disk:
                return f"it wrote more than {self._disk_mb} MB"
            return None

    def close(self) -> None:
        """Close its descriptors, once a measure in hand has ended; later measures find nothing."""
        with self._lock:
            for fd in self._folders:
                os.close(fd)
            self._folders = ()


def _settle(stop: asyncio.Future[str], verdict: str) -> None:
    """Complete a job's stop with the watchdog's verdict, on its loop; one already done or cancelled stays as it is.

    Args:
        stop: The job's stop.
        verdict: The limit it broke.
    """
    if not stop.done():
        stop.set_result(verdict)


class _Watchdog:
    """Measures every running job about twice a second, in one thread of its own, and stops those past a limit.

    One pass asks the data volume for its free space: below its floor, every job stops (``FULL``). Then it looks at
    ``/proc`` and the SysV table once for every slot (``slots_use``), and measures each job's folders, one job after
    the other, so the supervisor holds one walk's descriptors at a time. A verdict reaches the
    job's loop as the result of its stop; the wall clock never waits on a pass, nor a pass on the loop's own pool.
    What cannot be measured stops the job: ``UNMEASURED``, logged by its type alone, and the slot is retired. The
    thread runs while there are jobs to watch, and ends when there are none.
    """

    def __init__(self, floor: _Floor, interval_s: float = WATCH_INTERVAL_S) -> None:
        """Watch nothing yet.

        Args:
            floor: The data volume's free-space floor, asked first in every pass.
            interval_s: Seconds between two passes.
        """
        self._floor = floor
        self._interval = interval_s
        self._lock = threading.Lock()
        self._watches: list[_Watch] = []
        self._thread: threading.Thread | None = None

    def add(self, watch: _Watch) -> None:
        """Watch a job until its stop is done: stopped, or cancelled by ``run_in_slot`` when the job ended otherwise.

        Args:
            watch: The job's watch.

        Raises:
            RuntimeError: The watchdog's thread could not be started; the job is not watched, and must not run.
        """
        with self._lock:
            if self._thread is None:
                thread = threading.Thread(target=self._run, name="watchdog", daemon=True)
                thread.start()
                self._thread = thread
            self._watches.append(watch)
        watch.stop.add_done_callback(lambda _: self._discard(watch))

    def _discard(self, watch: _Watch) -> None:
        """Watch a job no more.

        Args:
            watch: The job's watch.
        """
        with self._lock:
            with contextlib.suppress(ValueError):
                self._watches.remove(watch)

    def _run(self) -> None:
        """Pass every ``interval_s`` while there are jobs; end when there are none.

        The thread blocks every signal, so the kernel hands the process's signals to the threads that expect them.
        """
        signal.pthread_sigmask(signal.SIG_BLOCK, signal.valid_signals())
        while True:
            time.sleep(self._interval)
            with self._lock:
                watches = list(self._watches)
                if not watches:
                    self._thread = None
                    return
            for watch, verdict in self.check(watches):
                with contextlib.suppress(RuntimeError):  # the loop is closed: that job is over
                    watch.loop.call_soon_threadsafe(_settle, watch.stop, verdict)

    def check(self, watches: list[_Watch]) -> list[tuple[_Watch, str]]:
        """One pass: judge every job, failing closed.

        Args:
            watches: The jobs to judge.

        Returns:
            Each job that broke a limit, with the limit.
        """
        try:
            full = self._floor.below()
        except OSError as exc:
            _log.error("the watchdog could not ask the data volume for its free space (%s)", type(exc).__name__)
            return [(watch, UNMEASURED) for watch in watches]
        if full:
            count = len(watches)
            _log.warning("stopped %d running job%s: the data volume is below its free-space floor", count, _s(count))
            return [(watch, FULL) for watch in watches]
        uids = {watch.uid for watch in watches if watch.uid is not None}
        try:
            use = slots_use(uids)
        except Exception as exc:
            _log.error("the watchdog could not read the slots' processes (%s)", type(exc).__name__)
            return [(watch, UNMEASURED) for watch in watches]
        verdicts: list[tuple[_Watch, str]] = []
        for watch in watches:
            try:
                verdict = watch.judge(None if watch.uid is None else use[watch.uid])
            except Exception as exc:
                _log.error("the watchdog could not measure the job of slot %d (%s)", watch.index, type(exc).__name__)
                verdict = UNMEASURED
            if verdict is not None:
                verdicts.append((watch, verdict))
        return verdicts


def _s(count: int) -> str:
    """The plural ending of a count.

    Args:
        count: How many.

    Returns:
        ``"s"``, or nothing for one.
    """
    return "" if count == 1 else "s"


def _verdict(stop: asyncio.Future[str] | None) -> str | None:
    """Read the watchdog's verdict on a job that has ended.

    Args:
        stop: The job's stop.

    Returns:
        The limit it was stopped on; ``None`` when the watchdog did not stop it (its stop was cancelled).
    """
    if stop is None or not stop.done() or stop.cancelled():
        return None
    return stop.result()


class Supervisor:
    """Runs jobs in slots, one per slot at a time, and answers them on a Unix socket."""

    def __init__(self, settings: Settings, *, use_slots: bool = True) -> None:
        """Set up the slots.

        Args:
            settings: The executor's settings.
            use_slots: Run jobs as the slot uids, as root. ``False`` keeps the current uid for jobs
                and gives every file to it (tests and development).

        Raises:
            ValueError: With slots, the slot pool is unsound (``make_slots``).
        """
        self._settings = settings
        self._slots = make_slots(settings) if use_slots else None
        me = (os.getuid(), os.getgid())
        self._owners = Owners() if use_slots else Owners(root=me, api=me)
        self._free = list(range(settings.executor_slots))
        self._retired: set[int] = set()
        self._exhausted = asyncio.Event()
        self._cleanup_pool = ThreadPoolExecutor(max_workers=settings.executor_slots, thread_name_prefix="slot-cleanup")
        self._sockets = seccomp.for_this_machine()
        self._floor = _Floor(settings.data_dir)
        self._watchdog = _Watchdog(self._floor)
        # Job folders whose removal failed; serve() tries them again every LEFTOVER_RETRY_S. Removals add to it in
        # worker threads while the retry reads it in another, so both hold the lock.
        self._leftovers: set[str] = set()
        self._leftovers_lock = threading.Lock()
        self._jobs = settings.data_dir / JOBS
        self._staging = settings.data_dir / STAGING
        # The device and inode of the socket this supervisor bound, so that it removes only its own.
        self._bound: tuple[int, int] | None = None

    async def serve(self) -> None:
        """Answer jobs on ``executor_socket`` until cancelled, or until no slot is left.

        Job folders an earlier run left behind are removed first, and a folder this run fails to
        remove is tried again every ``LEFTOVER_RETRY_S`` seconds. The socket gets mode ``0660``
        and the api's group, and is bound before it listens, so nobody else ever connects. It is
        removed again when serving ends, however it ends, if it is still the one this supervisor
        bound: a socket a later start bound in its place stays (a kill leaves it, and the next
        start replaces it).

        Raises:
            NoUsableSlot: Every slot is retired. The request that retired the last one is still
                answered, for up to 5 s; then the process should end, so that its restart brings
                a fresh IPC namespace, ``/dev/shm`` and slots, and the start-up sweep runs.
            SocketRefused: The socket's folder is not root's alone, or something other than a
                socket is where the socket belongs.
            OSError: The socket cannot be bound.
        """
        await asyncio.to_thread(self._sweep_stale_jobs)
        server = await asyncio.start_unix_server(self._handle, sock=self._bind(), limit=MAX_LINE)
        retrying = asyncio.create_task(self._retry_leftovers())
        try:
            await self._exhausted.wait()
            server.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), _CLOSE_GRACE_S)
        finally:
            retrying.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retrying
            server.close()
            self._cleanup_pool.shutdown(wait=False)
            with contextlib.suppress(OSError, RuntimeError):  # what is not our socket stays; the answer goes on
                self._remove_socket(only=self._bound)
        count = self._settings.executor_slots
        raise NoUsableSlot(f"every slot is retired ({count} of {count}); exiting so the container restarts")

    async def run_job(self, req: JobRequest) -> JobResult:
        """Run one job in a free slot and answer it.

        Args:
            req: The job.

        Returns:
            The answer. ``error`` with the refusal ``STORAGE_FULL`` when the data volume is below
            its free-space floor, before the job or while it runs (``below_floor``); ``busy`` when
            no slot is free (``no usable slot`` once every slot is retired); ``timeout`` or
            ``killed`` when the job was stopped; ``error`` when it could not be set up, its slot
            could not be cleared, it failed, its runner raised (the result keeps the runner's
            ``{"error": ...}``), it left no readable result, or its outputs broke a limit; else
            ``ok`` with the staged outputs, its message counting outputs dropped as not the job's
            own file (``_own_file``). A slot whose job folder could not be removed, or whose job
            could not be measured, is retired.

        Raises:
            asyncio.CancelledError: The job was cancelled; its slot is retired.
        """
        try:
            full = self._floor.below()
        except OSError as exc:
            _log.error("the data volume's free space could not be read (%s)", type(exc).__name__)
            return _answer(req.job_id, "error", "the job could not be set up")
        if full:
            return _full(req.job_id)
        if not self._free:
            message = "no usable slot" if not self._slots_left() else "every slot is busy"
            return _answer(req.job_id, "busy", message)
        index = self._free.pop(0)
        slot = None if self._slots is None else self._slots[index]
        limits = self._bounded(req.limits)
        try:
            job = await asyncio.to_thread(self._set_up, req, slot, limits)
        except _SetupError:
            self._free.append(index)
            return _answer(req.job_id, "error", "the job could not be set up")
        except BaseException:
            self._retire(index)  # the folder may still be being made, for the slot's uid
            raise
        watch: _Watch | None = None
        try:
            watch = _Watch(job, index, slot, limits, self._settings.workspace_max_mb)
            self._watchdog.add(watch)
            outcome, _ = await run_in_slot(
                job_argv(job.path / _SPEC, job.path / _HOME / _RESULT),
                job.cwd,
                slot,
                limits,
                tmp_dir=job.path / _HOME / _TMP,
                home=job.path / _HOME,
                cleanup_pool=self._cleanup_pool,
                sockets=self._sockets,
                stop=watch.stop,
            )
        except BaseException as exc:
            if watch is not None and not watch.stop.done():
                watch.stop.cancel()
            self._retire(index)
            # The removal runs in a thread, off the loop; a second cancellation stops only the wait for it.
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(asyncio.get_running_loop().run_in_executor(None, self._remove, job, watch))
            if not isinstance(exc, Exception):
                raise
            return _answer(req.job_id, "error", "the job's processes could not be stopped")
        if not watch.stop.done():  # run_in_slot cancels it; this holds for any run, so no watch outlives its job
            watch.stop.cancel()
        broken = _verdict(watch.stop)
        try:
            answer, removed = await asyncio.to_thread(self._finish, req, job, outcome, limits, watch, broken)
        except BaseException:
            self._retire(index)  # the folder may still be being read
            raise
        if removed and broken != UNMEASURED:
            self._free.append(index)
        else:
            # What is left holds a user's files, under a folder the slot owned; or the job ran unmeasured.
            self._retire(index)
        return answer

    def _slots_left(self) -> int:
        """Count the slots not retired.

        Returns:
            Free and running slots together.
        """
        return self._settings.executor_slots - len(self._retired)

    def _retire(self, index: int) -> None:
        """Take a slot out of use for the life of the process.

        Args:
            index: The slot's index.
        """
        self._retired.add(index)
        _log.warning("slot %d retired", index)
        if not self._slots_left():
            self._exhausted.set()

    def _bounded(self, limits: Limits) -> Limits:
        """Cap a job's limits at the executor's own settings.

        The container's memory and pids limits are sized from these, so a request cannot give a
        job more.

        Args:
            limits: The limits the request asks for.

        Returns:
            The limits the job runs with.
        """
        s = self._settings
        return Limits(
            wall_s=min(limits.wall_s, s.job_wall_s),
            cpu_s=min(limits.cpu_s, s.job_wall_s),
            memory_mb=min(limits.memory_mb, s.job_memory_mb),
            file_mb=min(limits.file_mb, s.upload_max_mb),
            nproc=min(limits.nproc, _MAX_NPROC),
            nofile=min(limits.nofile, _MAX_NOFILE),
        )

    def _open_data(self) -> int:
        """Open the volume root.

        Returns:
            The volume root, opened.

        Raises:
            OSError: It is missing or a symlink.
        """
        return os.open(self._settings.data_dir, DIR_FLAGS)

    def _open_jobs(self) -> int:
        """Open ``jobs`` and check it is root's and closed to others' writes.

        Returns:
            ``jobs``, opened.

        Raises:
            OSError: It is missing or a symlink.
            _SetupError: Someone else owns it, or its group or others may write it.
        """
        with closing(self._open_data()) as data:
            jobs = open_dir(JOBS, data)
        st = os.fstat(jobs)
        if st.st_uid != self._owners.root[0] or st.st_mode & 0o022:
            os.close(jobs)
            raise _SetupError("the jobs folder is not root's alone")
        return jobs

    def _set_up(self, req: JobRequest, slot: Slot | None, limits: Limits) -> _Job:
        """Make the job folder, copy the inputs and write the spec.

        Args:
            req: The job.
            slot: The slot it will run in, or ``None``.
            limits: The limits it will run with.

        Returns:
            The folder, held open.

        Raises:
            _SetupError: The folder could not be made; whatever was made of it is removed.
        """
        if JOB_ID.fullmatch(req.job_id) is None:
            raise _SetupError("the job id is not 32 lowercase hex digits")
        root = self._owners.root
        runner = root if slot is None else (slot.uid, slot.gid)
        try:
            jobs = self._open_jobs()
        except OSError as exc:
            raise _SetupError("the jobs folder cannot be opened") from exc
        try:
            os.mkdir(req.job_id, 0o700, dir_fd=jobs)
        except OSError as exc:
            os.close(jobs)
            raise _SetupError("the job folder cannot be made") from exc
        opened: list[int] = []
        try:
            folder = open_dir(req.job_id, jobs)
            opened.append(folder)
            workspace = make_dir(_WORKSPACE, folder, root, 0o755)
            opened.append(workspace)
            chat = make_dir(req.chat_id, workspace, root, 0o755)
            opened.append(chat)
            self._copy_inputs(req, chat)
            out = make_dir(OUT, chat, runner, 0o700)
            opened.append(out)
            spec = {
                "tool": req.tool,
                "args": req.args,
                "chat_id": req.chat_id,
                "max_chars": req.max_chars,
                "file_mb": limits.file_mb,
            }
            with closing(os.open(_SPEC, CREATE_FLAGS, 0o444, dir_fd=folder)) as fd:
                write_all(fd, json.dumps(spec).encode())
                settle(fd, root, 0o444)
            home = make_dir(_HOME, folder, runner, 0o700)
            opened.append(home)
            os.close(make_dir(_TMP, home, runner, 0o700))
            settle(folder, (root[0], runner[1]), 0o710)
        except BaseException as exc:
            for fd in opened:
                os.close(fd)
            self._rmtree(req.job_id, jobs)
            os.close(jobs)
            if isinstance(exc, OSError):
                raise _SetupError("the job folder cannot be filled") from exc
            raise
        os.close(workspace)
        os.close(chat)
        path = self._jobs / req.job_id
        cwd = path / _WORKSPACE / req.chat_id
        return _Job(name=req.job_id, jobs=jobs, folder=folder, out=out, home=home, path=path, cwd=cwd, runner=runner[0])

    def _open_chat(self, req: JobRequest) -> int | None:
        """Open the chat folder the inputs come from, through real folders only.

        Args:
            req: The job, naming caller, user and chat.

        Returns:
            The chat folder, opened, or ``None`` if a part of its path is missing or a symlink.
        """
        try:
            here = self._open_data()
        except OSError:
            return None
        for name in (WORKSPACES, req.caller, req.user_id, req.chat_id):
            try:
                with closing(here):
                    here = open_dir(name, here)
            except OSError:
                return None
        return here

    def _copy_inputs(self, req: JobRequest, target: int) -> None:
        """Copy the requested inputs, root-owned and ``0444``, into the job's working folder.

        An input that is missing, reached through a symlink, not a regular file, or below a
        folder named ``out`` (the job's own) is left out.

        Args:
            req: The job.
            target: The job's working folder, opened.

        Raises:
            OSError: The job folder could not take a copy.
        """
        if not req.input_files:
            return
        source = self._open_chat(req)
        if source is None:
            return
        with closing(source):
            for path in dict.fromkeys(req.input_files):
                parts = path.split("/")
                if parts[0] == OUT:
                    continue
                try:
                    fd = open_regular(source, parts)
                except OSError:
                    continue
                with closing(fd):
                    self._copy_in(fd, target, parts)

    def _copy_in(self, source: int, target: int, parts: list[str]) -> None:
        """Copy one opened input to its path below the working folder.

        Args:
            source: The input, opened.
            target: The job's working folder, opened.
            parts: The input's path segments.

        Raises:
            OSError: A folder or the copy could not be made.
        """
        root = self._owners.root
        folders: list[int] = []
        try:
            here = target
            for name in parts[:-1]:
                here = make_dir(name, here, root, 0o755, exist_ok=True)
                folders.append(here)
            with closing(os.open(parts[-1], CREATE_FLAGS, 0o444, dir_fd=here)) as fd:
                copy_bytes(source, fd, os.fstat(source).st_size)
                settle(fd, root, 0o444)
        finally:
            for folder in folders:
                os.close(folder)

    def _finish(
        self,
        req: JobRequest,
        job: _Job,
        outcome: str,
        limits: Limits,
        watch: _Watch | None = None,
        broken: str | None = None,
    ) -> tuple[JobResult, bool]:
        """Answer a job whose slot is clear, then remove its folder.

        Args:
            req: The job.
            job: Its folder.
            outcome: How the process ended (``run_in_slot``).
            limits: The limits it ran with.
            watch: Its watch, closed before the folder goes.
            broken: The limit the watchdog stopped it on, if it did.

        Returns:
            The answer, and whether the job folder is gone.
        """
        try:
            answer = self._answer_job(req, job, outcome, limits, broken)
        finally:
            removed = self._remove(job, watch)
        return answer, removed

    def _answer_job(
        self, req: JobRequest, job: _Job, outcome: str, limits: Limits, broken: str | None = None
    ) -> JobResult:
        """Read the result and stage the outputs of a job whose slot is clear.

        Args:
            req: The job.
            job: Its folder.
            outcome: How the process ended (``run_in_slot``).
            limits: The limits it ran with.
            broken: The limit the watchdog stopped it on, if it did: the answer names it.

        Returns:
            The answer.
        """
        if outcome == "timeout":
            return _answer(req.job_id, "timeout", f"the job ran past its wall time of {limits.wall_s} s")
        if outcome == "killed" and broken == FULL:
            return _full(req.job_id)
        if outcome == "killed":
            return _answer(req.job_id, "killed", KILLED if broken is None else f"the job was stopped: {broken}")
        if outcome != "ok":
            return _answer(req.job_id, "error", "the job failed")
        result = _read_result(job.home, min(req.max_chars * 4 + _RESULT_SLACK, MAX_LINE), job.runner)
        if result is None:
            return _answer(req.job_id, "error", "the job left no readable result")
        if set(result) == {"error"}:
            return _answer(req.job_id, "error", "the tool failed", result)
        try:
            found, dropped = _find_outputs(job.out, self._settings.upload_max_mb, job.runner)
        except _OutputsRefused as exc:
            return _answer(req.job_id, "error", str(exc), result)
        except OSError:
            return _answer(req.job_id, "error", "the job's outputs could not be read", result)
        try:
            outputs = self._stage(req.job_id, job.out, found, job.runner)
        except OSError:
            return _answer(req.job_id, "error", "the outputs could not be staged", result)
        dropped += len(found) - len(outputs)
        return JobResult(job_id=req.job_id, status="ok", result=result, outputs=outputs, message=_dropped(dropped))

    def _stage(
        self, job_id: str, out: int, found: Iterable[tuple[str, os.stat_result]], owner: int
    ) -> list[OutputFile]:
        """Copy the outputs to ``staging/<job-id>/``, for the api, which owns them afterwards.

        Args:
            job_id: The job's id, the staging folder's name.
            out: The job's ``out/``, opened.
            found: Name and ``lstat`` of each output (``_find_outputs``).
            owner: The uid the job ran as.

        Returns:
            The staged outputs, without those that, opened, are not the job's alone; none, and no
            folder, when there are none.

        Raises:
            OSError: ``staging`` is missing, a symlink or not the api's; the job's folder in it
                exists already; an output changed since it was looked at; or a copy failed. What
                was staged is removed again.
        """
        found = list(found)
        if not found:
            return []
        api = self._owners.api
        with closing(self._open_data()) as data:
            staging = open_dir(STAGING, data)
        with closing(staging):
            if os.fstat(staging).st_uid != api[0]:
                raise PermissionError("the staging folder is not the api's")
            folder = make_dir(job_id, staging, self._owners.root, 0o700)
            try:
                with closing(folder):
                    staged = [self._stage_one(out, folder, name, st, job_id, owner) for name, st in found]
                    outputs = [output for output in staged if output is not None]
                    settle(folder, api, 0o700)
            except BaseException:
                self._rmtree(job_id, staging)
                raise
            if not outputs:
                self._rmtree(job_id, staging)
        return outputs

    def _stage_one(
        self, out: int, folder: int, name: str, st: os.stat_result, job_id: str, owner: int
    ) -> OutputFile | None:
        """Copy one output into the job's staging folder.

        Args:
            out: The job's ``out/``, opened.
            folder: The job's staging folder, opened.
            name: The output's name.
            st: Its ``lstat`` when it was found.
            job_id: The job's id.
            owner: The uid the job ran as.

        Returns:
            The staged output, or ``None`` if the opened file is not the job's alone (``_own_file``).

        Raises:
            OSError: It is no longer the file that was found, or the copy failed.
        """
        with closing(os.open(name, READ_FLAGS, dir_fd=out)) as source:
            now = os.fstat(source)
            if not stat.S_ISREG(now.st_mode) or (now.st_dev, now.st_ino) != (st.st_dev, st.st_ino):
                raise FileNotFoundError("the output changed")
            if not _own_file(now, owner):
                return None
            with closing(os.open(name, CREATE_FLAGS, 0o600, dir_fd=folder)) as target:
                if copy_bytes(source, target, st.st_size) != st.st_size:
                    raise FileNotFoundError("the output shrank")
                settle(target, self._owners.api, 0o600)
        return OutputFile(name=name, staged_path=str(self._staging / job_id / name), size=st.st_size)

    def _remove(self, job: _Job, watch: _Watch | None = None) -> bool:
        """Remove a job folder, without following any symlink in it.

        The folder is first handed back to root alone, ``0700``, so if removal fails, no later job
        of the slot can enter what is left; the caller retires the slot all the same, and the
        folder is tried again every ``LEFTOVER_RETRY_S`` seconds (``_retry_leftovers``).

        Args:
            job: The folder.
            watch: Its watch, closed first.

        Returns:
            Whether it is gone.
        """
        if watch is not None:
            watch.close()
        try:
            with contextlib.suppress(OSError):
                settle(job.folder, self._owners.root, 0o700)
            os.close(job.out)
            os.close(job.home)
            os.close(job.folder)
            # only ever a folder this supervisor made
            if JOB_ID.fullmatch(job.name) is None:
                return False
            removed = self._rmtree(job.name, job.jobs)
            if not removed:
                with self._leftovers_lock:
                    self._leftovers.add(job.name)
            return removed
        finally:
            os.close(job.jobs)

    def _remove_leftovers(self) -> int:
        """Try once more to remove the job folders whose removal failed.

        Returns:
            How many are gone now.
        """
        with self._leftovers_lock:
            names = sorted(self._leftovers)
        if not names:
            return 0
        try:
            jobs = self._open_jobs()
        except (OSError, _SetupError):
            _log.warning("the jobs folder is missing or not root's alone")
            return 0
        gone = 0
        with closing(jobs):
            for name in names:
                try:
                    os.stat(name, dir_fd=jobs, follow_symlinks=False)
                except FileNotFoundError:
                    present = False
                else:
                    present = True
                if not present or self._rmtree(name, jobs, quiet=True):
                    with self._leftovers_lock:
                        self._leftovers.discard(name)
                    gone += 1
        _log.info(
            "removed %d of %d job folder%s a failed removal left", gone, len(names), "" if len(names) == 1 else "s"
        )
        return gone

    async def _retry_leftovers(self) -> None:
        """Every ``LEFTOVER_RETRY_S`` seconds, try again to remove what a failed removal left, in a thread."""
        while True:
            await asyncio.sleep(LEFTOVER_RETRY_S)
            try:
                await asyncio.to_thread(self._remove_leftovers)
            except Exception as exc:
                _log.warning("the leftover job folders could not be tried again (%s)", type(exc).__name__)

    def _rmtree(self, name: str, parent: int, *, quiet: bool = False) -> bool:
        """Remove a folder below ``parent`` with the walk that never follows a symlink.

        Args:
            name: Its name.
            parent: The folder it is in, opened.
            quiet: Log nothing; the caller counts.

        Returns:
            Whether every part of it was removed. A failure is logged once, without a path.
        """
        failures: list[BaseException] = []

        def note(function: Callable[..., Any], path: Any, error: BaseException) -> None:
            """Keep what failed; its path is never logged.

            Args:
                function: The call that failed.
                path: What it was called on.
                error: What it raised.
            """
            failures.append(error)

        shutil.rmtree(name, dir_fd=parent, onexc=note)
        if failures and not quiet:
            _log.warning("a job folder could not be removed")
        return not failures

    def _sweep_stale_jobs(self) -> None:
        """Remove job folders an earlier run left, before any job of this run starts."""
        try:
            jobs = self._open_jobs()
        except (OSError, _SetupError):
            _log.warning("the jobs folder is missing or not root's alone")
            return
        with closing(jobs):
            stale = [name for name in os.listdir(jobs) if JOB_ID.fullmatch(name)]
            for name in stale:
                self._rmtree(name, jobs)
        if stale:
            _log.info("removed %d job folder%s left from an earlier run", len(stale), "" if len(stale) == 1 else "s")

    def _remove_socket(self, *, only: tuple[int, int] | None = None) -> None:
        """Remove the socket from its place, if a socket is there; anything else is left as it is.

        Args:
            only: The device and inode of the one socket to remove; another socket in the place stays. ``None``
                removes whichever socket is there.

        Raises:
            SocketRefused: The socket's folder is a symlink, not a folder, not root's, or open to others' writes; or
                something other than a socket is in the socket's place.
            OSError: It cannot be removed.
        """
        path = self._settings.executor_socket
        try:
            folder = os.lstat(path.parent)
        except FileNotFoundError:
            folder = None
        if (
            folder is None
            or not stat.S_ISDIR(folder.st_mode)
            or folder.st_uid != self._owners.root[0]
            or folder.st_mode & 0o022
        ):
            raise SocketRefused("the socket's folder must be root's alone")
        try:
            found = os.lstat(path)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(found.st_mode):
            raise SocketRefused("the socket's place holds something that is not a socket")
        if only is not None and (found.st_dev, found.st_ino) != only:
            return
        os.unlink(path)

    def _bind(self) -> socket.socket:
        """Bind the socket, replacing one an earlier run left, and give it its owner and mode.

        The socket's folder must be root's alone, so nobody can swap the socket or its name once
        it is checked. The mode comes from the umask in force while the socket is made, ``0660``,
        never from a later change through its path; only the group is set afterwards, without
        following a link. Its device and inode are kept, so that the end of serving removes this
        socket and no other.

        Returns:
            The bound socket, not yet listening.

        Raises:
            SocketRefused: The socket's folder is a symlink, not a folder, not root's, or open to
                others' writes; or something other than a socket is in the socket's place.
            OSError: It cannot be bound.
        """
        path = self._settings.executor_socket
        self._remove_socket()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            previous = os.umask(0o117)
            try:
                sock.bind(str(path))
            finally:
                os.umask(previous)
            os.chown(path, self._owners.root[0], self._owners.api[1], follow_symlinks=False)
            bound = os.lstat(path)
        except BaseException:
            sock.close()
            raise
        self._bound = (bound.st_dev, bound.st_ino)
        return sock

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Answer the one request of a connection.

        An error the job's own handling did not answer closes the connection without an answer, and is logged by its
        type alone: asyncio would log it with its traceback, whose message can name a path on the volume.

        Args:
            reader: The connection's incoming side.
            writer: The connection's outgoing side.
        """
        try:
            try:
                line = await asyncio.wait_for(reader.readline(), _REQUEST_TIMEOUT_S)
                req = decode_request(line)
            except (ValueError, TimeoutError, OSError):
                _log.warning("refused a malformed request")
                answer = _answer("", "error", "malformed request")
            else:
                answer = await self._run_safely(req)
            writer.write(_line(answer))
            await writer.drain()
        except OSError:
            pass  # the api went away; staged outputs are swept with the staging folder
        except Exception as exc:
            _log.error("a connection failed in the supervisor: %s", type(exc).__name__)
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _run_safely(self, req: JobRequest) -> JobResult:
        """Run a job; an unexpected failure is answered, and logged by its type alone.

        Args:
            req: The job.

        Returns:
            The answer.
        """
        try:
            return await self.run_job(req)
        except Exception as exc:
            _log.error("a job failed in the supervisor: %s", type(exc).__name__)
            return _answer(req.job_id, "error", "the executor failed")


def open_files_needed(slots: int) -> int:
    """Count the descriptors the supervisor may hold at once.

    A job folder is walked one descriptor per level, ``MAX_JOB_DEPTH`` + 2 at most with the walk's own: by the watchdog,
    one job at a time, and by its removal, which may run for every slot at once.

    Args:
        slots: ``WORKBENCH_EXECUTOR_SLOTS``.

    Returns:
        Slots times a walk, and ``NOFILE_MARGIN`` for the rest.
    """
    return slots * (MAX_JOB_DEPTH + 2) + NOFILE_MARGIN


def _open_files_problem(slots: int) -> str | None:
    """Raise the supervisor's soft limit of open files to its hard one, and check it covers what the slots need.

    Raising it needs no capability. A host may start the container with a soft limit of 1024, which four slots' walks
    already pass. The jobs keep their own limit (``preexec``).

    Args:
        slots: ``WORKBENCH_EXECUTOR_SLOTS``.

    Returns:
        A short reason when the limit stays below ``open_files_needed``, else ``None``.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft != hard:
        with contextlib.suppress(ValueError, OSError):
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
        soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    need = open_files_needed(slots)
    if soft != resource.RLIM_INFINITY and soft < need:
        return f"the supervisor may open {soft} files, fewer than the {need} its {slots} slots need"
    return None


def _mount_points(mountinfo: Path) -> set[str] | None:
    """Read the mount points of a mount table.

    Args:
        mountinfo: A ``/proc/<pid>/mountinfo`` file; the mount point is its fifth field.

    Returns:
        The mount points; ``None`` when the table cannot be read.
    """
    try:
        lines = mountinfo.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return {fields[4] for fields in (line.split() for line in lines) if len(fields) >= 5}


def _close_dev_zero(path: Path = _DEV_ZERO, *, root: int = 0, mountinfo: Path = _MOUNTINFO) -> bool:
    """Take the write bits of group and others off ``/dev/zero`` where the node is the container's own; best effort.

    A shared map of ``/dev/zero`` opened for writing is shared memory, which a job can fill, by reading its pages as
    much as by writing them, then leave to a child it forks and unmap itself, out of every resident set the watchdog
    counts (R85); the jobs' seccomp filter refuses the other way there, ``mmap`` with ``MAP_SHARED | MAP_ANONYMOUS``. A
    shared map opened read-only holds no memory, however many of its pages are read (measured, R88). Under Docker's
    usual set-up ``/dev`` is a tmpfs of the container's and root owns the node, so the change needs no capability and
    lives as long as the container. Under a user-namespaced daemon (userns-remap, rootless) the node is the host's,
    bind-mounted and owned by an unmapped uid: it is left as it is, and so is any node that is not root's or is a mount
    of its own. The supervisor starts all the same; the route stays open there (docs/sandbox.md, Residual risks).

    Args:
        path: ``/dev/zero``.
        root: The uid that must own it.
        mountinfo: The process's mount table, to tell a bind-mounted node.

    Returns:
        Whether ``/dev/zero`` is missing or closed to others' writes. When it is not, a warning names why, by a fixed
        text or an error's type.
    """
    open_route = "/dev/zero stays writable by the slots, who may map shared memory through it"
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        _log.warning("%s: it could not be looked at (%s)", open_route, type(exc).__name__)
        return False
    if not st.st_mode & 0o022 and not stat.S_ISLNK(st.st_mode):
        return True
    if stat.S_ISLNK(st.st_mode) or st.st_uid != root:
        _log.warning("%s: it is not root's own node (a user-namespaced daemon?)", open_route)
        return False
    mounts = _mount_points(mountinfo)
    if mounts is None or str(path) in mounts:
        _log.warning("%s: it is a mount of its own, the host's node, which the supervisor leaves alone", open_route)
        return False
    try:
        os.chmod(path, stat.S_IMODE(st.st_mode) & ~0o022)
    except OSError as exc:
        _log.warning("%s: its mode could not be changed (%s)", open_route, type(exc).__name__)
        return False
    return True


def prepare_host(settings: Settings) -> str | None:
    """Make the changes the supervisor needs in its container before it serves, and check the one it cannot do without.

    ``/dev/zero`` is closed to others' writes where it can be (``_close_dev_zero``; best effort, never a reason to
    stop). The soft limit of open files goes up to the hard one, which must hold what the slots need
    (``_open_files_problem``).

    Args:
        settings: The executor's settings.

    Returns:
        A short reason it must not start, or ``None``.
    """
    _close_dev_zero(_DEV_ZERO)
    return _open_files_problem(settings.executor_slots)


def main() -> int:
    """Check the host, then serve until stopped.

    Returns:
        ``1`` after a one-line reason on stderr when it must not start, including a socket it may
        not bind, or once every slot is retired (counts only); ``1`` after one line with its type
        alone for any other error, never its message or a traceback, which can name a path; ``0``
        when serving ends.
    """
    try:
        settings = load_settings(os.environ, role="executor")
    except ConfigError as exc:
        print(f"workbench executor: {exc}", file=sys.stderr)
        return 1
    problem = startup_problem(settings)
    if problem is None:
        logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="workbench executor: %(message)s")
        problem = prepare_host(settings)
    if problem is not None:
        print(f"workbench executor: refusing to start: {problem}", file=sys.stderr)
        return 1
    try:
        asyncio.run(Supervisor(settings).serve())
    except NoUsableSlot as exc:
        print(f"workbench executor: {exc}", file=sys.stderr)
        return 1
    except SocketRefused as exc:  # serve's own refusals, fixed texts: the socket's folder or place
        print(f"workbench executor: refusing to start: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"workbench executor: stopped by an unexpected error ({type(exc).__name__})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
