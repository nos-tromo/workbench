"""The executor's supervisor: ``python -m workbench.executor.supervisor``.

It runs as root with ``SETUID``, ``SETGID``, ``CHOWN``, ``FOWNER``, ``DAC_OVERRIDE`` and ``KILL``, in a
container with no network, and takes one job per connection on a Unix socket the api shares. For
each job it copies the requested files of the chat folder into a private job folder, runs the job
there as the uid of a free slot (``workbench.executor.sandbox``) and, once no process of that uid is
left, reads the result and stages the outputs for the api.

The job folder, ``<data_dir>/jobs/<job-id>/``:

- the folder itself: the slot's, ``0700``; ``spec.json`` (root's, ``0444``) and the job's
  ``result.json`` lie in it;
- ``workspace/<chat-id>/``: root's, ``0755``, the job's working folder, holding root-owned ``0444``
  copies of the inputs at their paths below the chat folder;
- ``workspace/<chat-id>/out/``: the slot's, ``0700``, where the job writes its outputs;
- ``tmp/``: the slot's, ``0700``, the job's ``TMPDIR``.

The api owns the workspaces and the staging folder, and the job owns its folder, so neither is
trusted: everything below ``data_dir`` is opened relative to a folder already open, with
``O_NOFOLLOW``, and nothing is read from the job folder before the slot is cleared. Logs name
slots and counts, never a job, a file or anything a request holds.
"""

import asyncio
import contextlib
import json
import logging
import os
import shutil
import socket
import stat
import sys
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from workbench.config import UID_POOL_SIZE, ConfigError, Settings, load_settings
from workbench.executor.fs import (
    CREATE_FLAGS,
    DIR_FLAGS,
    READ_FLAGS,
    closing,
    copy_bytes,
    make_dir,
    open_dir,
    open_regular,
    settle,
    write_all,
)
from workbench.executor.prepare_volume import API_UID, JOBS, STAGING, WORKSPACES, Owners
from workbench.executor.sandbox import IPC_ENTRY_LIMIT, Slot, run_in_slot
from workbench.paths import PathError, check_filename
from workbench.protocol import JOB_ID, MAX_LINE, JobRequest, JobResult, Limits, OutputFile, decode_request, encode

MB = 1024 * 1024
MAX_OUTPUTS = 20
OUT = "out"

_WORKSPACE = "workspace"
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
_SYSCTLS = Path("/proc/sys/fs")
_MOUNTINFO = Path("/proc/self/mountinfo")
_SHM = "/dev/shm"
_CLOSE_GRACE_S = 5.0  # for the answer to the request that retired the last slot
# Each must read 1. Hard links: a job cannot give out/ a name for a file it does not own, such as
# the api's database, which root would then stage. Symlinks: a link a job leaves in a sticky,
# world-writable folder (/dev/shm, /dev/mqueue) is not followed by anyone else.
_LINK_PROTECTIONS = (
    ("protected_hardlinks", "so a job cannot link files it does not own"),
    ("protected_symlinks", "so no one follows a job's links in shared folders"),
)

_log = logging.getLogger("workbench.executor")

Status = Literal["ok", "error", "timeout", "killed", "busy"]


class NoUsableSlot(Exception):
    """Every slot is retired; the process ends so that its restart brings fresh slots and IPC."""


class _SetupError(Exception):
    """The job folder could not be made; nothing ran."""


class _OutputsRefused(Exception):
    """The job's outputs break a limit; the message says which, for the job's answer."""


def job_argv(spec: Path) -> list[str]:
    """Build the command line of a job.

    Args:
        spec: The job's ``spec.json``.

    Returns:
        ``python -I -m workbench.executor.job <spec>``, with this interpreter.
    """
    return [sys.executable, "-I", "-m", "workbench.executor.job", str(spec)]


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


def _memory_limit(memory_max: Path) -> int | None:
    """Read the container's cgroup v2 memory limit.

    Args:
        memory_max: The cgroup's ``memory.max``.

    Returns:
        The limit in bytes, or ``None`` when there is none (``max``) or it cannot be read.
    """
    try:
        raw = memory_max.read_text(encoding="ascii").strip()
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
    shm_files: int | None = None,
    euid: int | None = None,
) -> str | None:
    """Find a reason the supervisor must not start.

    Args:
        settings: The executor's settings.
        passwd: The image's user list.
        memory_max: The container's cgroup memory limit, if it has one.
        sysctls: The kernel's ``fs`` settings, ``/proc/sys/fs``.
        mountinfo: The process's mount table, ``/proc/self/mountinfo``.
        shm_files: How many files ``/dev/shm`` can hold (``f_files`` of ``statvfs``, 0 for no
            limit); read from ``/dev/shm`` by default.
        euid: The effective uid to judge; the process's own by default.

    Returns:
        A short reason, or ``None`` when root runs it, the slot pool is sound (``make_slots``),
        every slot uid is a user of the image, the memory limit holds every slot's job memory,
        ``fs.protected_hardlinks`` and ``fs.protected_symlinks`` both read 1 (the host's kernel
        settings, which a container cannot change), ``/dev/mqueue`` is a mounted mqueue file
        system (without it a job's queues can be neither listed nor removed), and ``/dev/shm``
        holds at most ``IPC_ENTRY_LIMIT`` files (so no job can fill it past what the slot
        cleanup looks through, and so fail every other slot's cleanup).
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
    limit = _memory_limit(memory_max)
    if limit is not None and limit < len(slots) * settings.job_memory_mb * MB:
        return f"the container's memory limit is below {len(slots)} slots of {settings.job_memory_mb} MB"
    for name, why in _LINK_PROTECTIONS:
        try:
            value = (sysctls / name).read_text(encoding="ascii").strip()
        except (OSError, UnicodeDecodeError):
            value = ""
        if value != "1":
            return f"fs.{name} must be 1, {why}"
    if not _mqueue_mounted(mountinfo):
        return "/dev/mqueue must be a mounted mqueue file system, so the slots' queues can be cleared"
    if shm_files is None:
        try:
            shm_files = os.statvfs(_SHM).f_files
        except OSError:
            shm_files = 0
    if not 0 < shm_files <= IPC_ENTRY_LIMIT:
        return f"/dev/shm must hold at most {IPC_ENTRY_LIMIT} files (mount it with nr_inodes), so no job can fill it"
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


def _read_result(job: int, limit: int) -> dict[str, Any] | None:
    """Read the job's ``result.json`` as a JSON object.

    Args:
        job: The job folder, opened.
        limit: Most bytes to read.

    Returns:
        The object, or ``None`` if the file is missing, not a regular file, larger than
        ``limit``, not JSON or not an object.
    """
    try:
        fd = os.open(_RESULT, READ_FLAGS, dir_fd=job)
    except OSError:
        return None
    chunks: list[bytes] = []
    size = 0
    try:
        with closing(fd):
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
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
        value = json.loads(data)
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
        path: The job folder's path, for the job's command line.
        cwd: The job's working folder.
        runner: The uid the job runs as, which must own its outputs.
    """

    name: str
    jobs: int
    folder: int
    out: int
    path: Path
    cwd: Path
    runner: int


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
        self._jobs = settings.data_dir / JOBS
        self._staging = settings.data_dir / STAGING

    async def serve(self) -> None:
        """Answer jobs on ``executor_socket`` until cancelled, or until no slot is left.

        Job folders an earlier run left behind are removed first. The socket gets mode ``0660``
        and the api's group, and is bound before it listens, so nobody else ever connects.

        Raises:
            NoUsableSlot: Every slot is retired. The request that retired the last one is still
                answered, for up to 5 s; then the process should end, so that its restart brings
                a fresh IPC namespace, ``/dev/shm`` and slots, and the start-up sweep runs.
            RuntimeError: The socket's folder is not root's alone, or something other than a
                socket is where the socket belongs.
            OSError: The socket cannot be bound.
        """
        await asyncio.to_thread(self._sweep_stale_jobs)
        server = await asyncio.start_unix_server(self._handle, sock=self._bind(), limit=MAX_LINE)
        try:
            await self._exhausted.wait()
            server.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), _CLOSE_GRACE_S)
        finally:
            server.close()
            self._cleanup_pool.shutdown(wait=False)
        count = self._settings.executor_slots
        raise NoUsableSlot(f"every slot is retired ({count} of {count}); exiting so the container restarts")

    async def run_job(self, req: JobRequest) -> JobResult:
        """Run one job in a free slot and answer it.

        Args:
            req: The job.

        Returns:
            The answer. ``busy`` when no slot is free (``no usable slot`` once every slot is
            retired); ``timeout`` or ``killed`` when the job was stopped; ``error`` when it could
            not be set up, its slot could not be cleared, it failed, its runner raised (the
            result keeps the runner's ``{"error": ...}``), it left no readable result, or its
            outputs broke a limit; else ``ok`` with the staged outputs, its message counting
            outputs dropped as not the job's own file (``_own_file``). A slot whose job folder
            could not be removed is retired.

        Raises:
            asyncio.CancelledError: The job was cancelled; its slot is retired.
        """
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
        try:
            outcome, _ = await run_in_slot(
                job_argv(job.path / _SPEC),
                job.cwd,
                slot,
                limits,
                tmp_dir=job.path / _TMP,
                cleanup_pool=self._cleanup_pool,
            )
        except BaseException as exc:
            self._retire(index)
            self._remove(job)
            if not isinstance(exc, Exception):
                raise
            return _answer(req.job_id, "error", "the job's processes could not be stopped")
        try:
            answer, removed = await asyncio.to_thread(self._finish, req, job, outcome, limits)
        except BaseException:
            self._retire(index)  # the folder may still be being read
            raise
        if removed:
            self._free.append(index)
        else:
            self._retire(index)  # what is left holds a user's files, under a folder the slot owned
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
            os.close(make_dir(_TMP, folder, runner, 0o700))
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
            settle(folder, runner, 0o700)
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
        return _Job(name=req.job_id, jobs=jobs, folder=folder, out=out, path=path, cwd=cwd, runner=runner[0])

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

    def _finish(self, req: JobRequest, job: _Job, outcome: str, limits: Limits) -> tuple[JobResult, bool]:
        """Answer a job whose slot is clear, then remove its folder.

        Args:
            req: The job.
            job: Its folder.
            outcome: How the process ended (``run_in_slot``).
            limits: The limits it ran with.

        Returns:
            The answer, and whether the job folder is gone.
        """
        try:
            answer = self._answer_job(req, job, outcome, limits)
        finally:
            removed = self._remove(job)
        return answer, removed

    def _answer_job(self, req: JobRequest, job: _Job, outcome: str, limits: Limits) -> JobResult:
        """Read the result and stage the outputs of a job whose slot is clear.

        Args:
            req: The job.
            job: Its folder.
            outcome: How the process ended (``run_in_slot``).
            limits: The limits it ran with.

        Returns:
            The answer.
        """
        if outcome == "timeout":
            return _answer(req.job_id, "timeout", f"the job ran past its wall time of {limits.wall_s} s")
        if outcome == "killed":
            return _answer(req.job_id, "killed", "the job was stopped on a resource limit")
        if outcome != "ok":
            return _answer(req.job_id, "error", "the job failed")
        result = _read_result(job.folder, min(req.max_chars * 4 + _RESULT_SLACK, MAX_LINE))
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

    def _remove(self, job: _Job) -> bool:
        """Remove a job folder, without following any symlink in it.

        The folder is first handed back to root, ``0700``, so if removal fails, no later job of
        the slot can enter what is left; the caller retires the slot all the same.

        Args:
            job: The folder.

        Returns:
            Whether it is gone.
        """
        try:
            with contextlib.suppress(OSError):
                settle(job.folder, self._owners.root, 0o700)
            os.close(job.out)
            os.close(job.folder)
            # only ever a folder this supervisor made
            return JOB_ID.fullmatch(job.name) is not None and self._rmtree(job.name, job.jobs)
        finally:
            os.close(job.jobs)

    def _rmtree(self, name: str, parent: int) -> bool:
        """Remove a folder below ``parent`` with the walk that never follows a symlink.

        Args:
            name: Its name.
            parent: The folder it is in, opened.

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
        if failures:
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

    def _bind(self) -> socket.socket:
        """Bind the socket, replacing one an earlier run left, and give it its owner and mode.

        The socket's folder must be root's alone, so nobody can swap the socket or its name once
        it is checked. The mode comes from the umask in force while the socket is made, ``0660``,
        never from a later change through its path; only the group is set afterwards, without
        following a link.

        Returns:
            The bound socket, not yet listening.

        Raises:
            RuntimeError: The socket's folder is a symlink, not a folder, not root's, or open to
                others' writes; or something other than a socket is in the socket's place.
            OSError: It cannot be bound.
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
            raise RuntimeError("the socket's folder must be root's alone")
        try:
            found = os.lstat(path).st_mode
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(found):
                raise RuntimeError("the socket's place holds something that is not a socket")
            os.unlink(path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            previous = os.umask(0o117)
            try:
                sock.bind(str(path))
            finally:
                os.umask(previous)
            os.chown(path, self._owners.root[0], self._owners.api[1], follow_symlinks=False)
        except BaseException:
            sock.close()
            raise
        return sock

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Answer the one request of a connection.

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


def main() -> int:
    """Check the host, then serve until stopped.

    Returns:
        ``1`` after a one-line reason on stderr when it must not start, including a socket it may
        not bind, or once every slot is retired (counts only); ``0`` when serving ends.
    """
    try:
        settings = load_settings(os.environ, role="executor")
    except ConfigError as exc:
        print(f"workbench executor: {exc}", file=sys.stderr)
        return 1
    problem = startup_problem(settings)
    if problem is not None:
        print(f"workbench executor: refusing to start: {problem}", file=sys.stderr)
        return 1
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="workbench executor: %(message)s")
    try:
        asyncio.run(Supervisor(settings).serve())
    except NoUsableSlot as exc:
        print(f"workbench executor: {exc}", file=sys.stderr)
        return 1
    except RuntimeError as exc:  # serve's own refusals: the socket's folder or place
        print(f"workbench executor: refusing to start: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
