"""Workspace files on disk and their metadata in SQLite.

The ``files`` table is the source of truth for the files the store manages: the
file browser, the tools and the retention sweep all go through it. The bytes live
under the user's own root (``paths.Workspaces``); a file is only ever inside a
chat folder, ``/workspace/<chat-id>/…``. Folders have no record, they are plain
directories. Nothing here follows a symlink.

An upload is streamed to ``<file id>.part`` in its folder and then renamed into
place, so a reader never sees half a file and a failed upload leaves nothing.
Names, quota and the row are settled under one lock, so concurrent uploads of one
name each get their own file.
"""

import dataclasses
import errno
import itertools
import logging
import mimetypes
import os
import posixpath
import re
import sqlite3
import stat
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Final, Literal, TypeGuard

from workbench.models import VIRTUAL_ROOT, DirEntry, FileRecord, JobRecord, Principal
from workbench.paths import DEFAULT_CHAT, PathError, Workspaces, check_filename, check_id
from workbench.utils.audit_event import MAX_IDS

_log = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
_PRIVATE_DIR = 0o700
_PRIVATE_FILE = 0o600
_PART = ".part"
_GENERIC_MIME = "application/octet-stream"
_FULL = frozenset({errno.ENOSPC, errno.EDQUOT})
_BUSY_TIMEOUT_S = 30
_NOT_EMPTY = frozenset({errno.ENOTEMPTY, errno.EEXIST})

#: The name of an upload while it is written: its file id, then ``.part``.
_PART_NAME: Final = re.compile(r"([0-9a-f]{32})\.part")

#: Records orphans of one chat folder: called with the chat id, the file ids their names carry, how many there are, and
#: the outcome, ``ok`` before they go or ``error`` for those that could not be removed.
OrphanRecord = Callable[[str, Sequence[str], int, str], object]

_OFFICE_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".eml": "message/rfc822",
}


def _known_types() -> mimetypes.MimeTypes:
    """Build the name-to-type table: the library's built-in one plus the office formats.

    A private table, so the answer never depends on which ``mime.types`` files the host has
    (a slim image has none, and would call every Office file ``application/octet-stream``).

    Returns:
        The table.
    """
    types = mimetypes.MimeTypes()
    for ext, mime in _OFFICE_TYPES.items():
        types.add_type(mime, ext)
    return types


_TYPES = _known_types()

_FILE_COLUMNS = "id, caller, user_id, chat_id, name, size, mime, kind, created_at, job_id"
_JOB_COLUMNS = "id, caller, user_id, chat_id, tool, created_at, status"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    caller TEXT NOT NULL,
    user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    mime TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('upload', 'output')),
    created_at REAL NOT NULL,
    job_id TEXT,
    UNIQUE (caller, user_id, chat_id, name)
);
CREATE INDEX IF NOT EXISTS files_created ON files (created_at);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    caller TEXT NOT NULL,
    user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    created_at REAL NOT NULL,
    status TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs (created_at);
"""


class StoreError(Exception):
    """Base of the errors the store raises on purpose."""


class NotFound(StoreError):
    """No such file, folder or job for this user."""


class TooLarge(StoreError):
    """An upload is bigger than the per-file limit."""


class QuotaExceeded(StoreError):
    """An upload would take the user over their workspace quota."""


class StorageFull(StoreError):
    """The volume has no room left."""


class Conflict(StoreError):
    """The target already exists, or something that is not a folder is in the way."""


def _write_all(fd: int, data: bytes) -> None:
    """Write all of ``data`` to ``fd``.

    Args:
        fd: An open file descriptor.
        data: The bytes to write.
    """
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _lstat(path: Path) -> os.stat_result | None:
    """Look at a path without following it.

    Args:
        path: Any path.

    Returns:
        Its ``lstat``, or ``None`` if it, or a folder on the way, does not exist.
    """
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _look(path: Path) -> os.stat_result | None:
    """Look at an entry a sweep walks over, without following it; one that cannot be looked at is skipped.

    Args:
        path: The entry.

    Returns:
        Its ``lstat``, or ``None`` if it is gone or cannot be looked at; the latter is logged by the error's type.
    """
    try:
        return _lstat(path)
    except OSError as exc:
        _log.warning("a sweep skipped an entry it could not look at (%s)", type(exc).__name__)
        return None


def _unlisted(exc: OSError) -> None:
    """Note a folder a sweep could not list; the walk goes on without it.

    Args:
        exc: Why it could not be listed.
    """
    _log.warning("a sweep skipped a folder it could not list (%s)", type(exc).__name__)


def _part_ids(paths: Sequence[Path]) -> list[str]:
    """Take the file ids the names of orphans carry.

    Args:
        paths: The orphans.

    Returns:
        The id of each ``<id>.part``; the others carry none.
    """
    return [found.group(1) for path in paths if (found := _PART_NAME.fullmatch(path.name))]


def _unlink(path: Path) -> None:
    """Remove a file if it is there.

    Args:
        path: The file; a symlink is removed itself, never followed.
    """
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _remove_empty(path: Path) -> None:
    """Remove a folder if it is empty; one that is not, or is gone, stays as it is, silently.

    Args:
        path: The folder; ``rmdir`` never follows a link.
    """
    try:
        os.rmdir(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        if exc.errno not in _NOT_EMPTY:
            _log.warning("could not remove an empty folder (%s)", type(exc).__name__)


def _is_folder(st: os.stat_result | None, device: int) -> TypeGuard[os.stat_result]:
    """Tell whether an ``lstat`` is of a folder, not a link, on the data volume.

    Args:
        st: The ``lstat``, or ``None`` for nothing there.
        device: The data volume's device.

    Returns:
        ``True`` for a folder of the volume.
    """
    return st is not None and stat.S_ISDIR(st.st_mode) and st.st_dev == device


def _is_chat_id(name: str) -> bool:
    """Tell whether a folder's name is a chat id, as every chat folder's is.

    Args:
        name: The name.

    Returns:
        ``True`` if :func:`check_id` takes it.
    """
    try:
        check_id(name, what="chat id")
    except PathError:
        return False
    return True


def _is_full(exc: OSError) -> bool:
    """Tell whether an error means the volume or the user's disk quota is used up.

    Args:
        exc: The error.

    Returns:
        ``True`` for ``ENOSPC`` and ``EDQUOT``.
    """
    return exc.errno in _FULL


def _check_new_name(name: str) -> str:
    """Check a name that is about to be created.

    Args:
        name: A file or folder name that already passed ``check_filename``.

    Returns:
        ``name``, unchanged.

    Raises:
        PathError: If it ends in ``.part``, which marks a half-written upload.
    """
    if name.endswith(_PART):
        raise PathError("invalid file name")
    return name


def _numbered(name: str, n: int) -> str:
    """Return ``name`` with `` (n)`` before its extension, shortened to stay a valid name.

    Args:
        name: A valid file name.
        n: The number to add.

    Returns:
        For example ``q (1).xlsx``; the stem is cut if the result would be too long.

    Raises:
        PathError: If no stem is short enough.
    """
    stem, ext = posixpath.splitext(name)
    while True:
        candidate = f"{stem} ({n}){ext}"
        try:
            return check_filename(candidate)
        except PathError:
            if not stem:
                raise
            stem = stem[:-1]


def _mime(name: str) -> str:
    """Guess a media type from a file name.

    Args:
        name: The file name.

    Returns:
        The guessed type, or ``application/octet-stream``.
    """
    return _TYPES.guess_type(name, strict=False)[0] or _GENERIC_MIME


def _file(row: tuple[Any, ...]) -> FileRecord:
    """Build a record from a ``files`` row.

    Args:
        row: Columns in ``_FILE_COLUMNS`` order.

    Returns:
        The record.
    """
    kind: Literal["upload", "output"] = "output" if row[7] == "output" else "upload"
    return FileRecord(
        id=row[0],
        caller=row[1],
        user_id=row[2],
        chat_id=row[3],
        name=row[4],
        size=row[5],
        mime=row[6],
        kind=kind,
        created_at=row[8],
        job_id=row[9],
    )


def _job(row: tuple[Any, ...]) -> JobRecord:
    """Build a record from a ``jobs`` row.

    Args:
        row: Columns in ``_JOB_COLUMNS`` order.

    Returns:
        The record.
    """
    return JobRecord(
        id=row[0], caller=row[1], user_id=row[2], chat_id=row[3], tool=row[4], created_at=row[5], status=row[6]
    )


class Store:
    """Files of every user on the data volume, and the job records, in one SQLite database."""

    def __init__(self, db_path: Path, workspaces: Workspaces, clock: Callable[[], float] = time.time) -> None:
        """Open the database, creating it (mode 0600, WAL) and its tables if needed.

        Args:
            db_path: Where the database lives, on the data volume.
            workspaces: Maps virtual paths to real ones under the user's root.
            clock: Returns the current time in seconds; ``created_at`` comes from it.
        """
        self._db_path = db_path
        self._ws = workspaces
        self._clock = clock
        self._lock = threading.Lock()
        db_path.parent.mkdir(mode=_PRIVATE_DIR, parents=True, exist_ok=True)
        fd = os.open(db_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, _PRIVATE_FILE)
        try:
            os.fchmod(fd, _PRIVATE_FILE)
        finally:
            os.close(fd)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(_SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        """Open a connection for one operation; commit on success, roll back on an error.

        Yields:
            A connection with ``secure_delete`` on, so deleted rows are overwritten.
        """
        conn = sqlite3.connect(self._db_path, timeout=_BUSY_TIMEOUT_S)
        try:
            conn.execute("PRAGMA secure_delete=ON")
            with conn:
                yield conn
        finally:
            conn.close()

    # --- paths ---------------------------------------------------------------

    def _locate(self, p: Principal, virtual: str) -> tuple[Path, list[str]]:
        """Resolve a virtual path and split it into its segments below ``/workspace``.

        Args:
            p: The caller and user.
            virtual: Any virtual path.

        Returns:
            The real path, and ``[chat-id, *names]`` (empty for ``/workspace``).

        Raises:
            PathError: If the path is not inside the user's root.
        """
        real = self._ws.resolve(p, virtual)
        return real, self._ws.to_virtual(p, real).split("/")[2:]

    def _file_target(self, p: Principal, virtual: str) -> tuple[Path, str, str]:
        """Resolve the path of a file, which must be inside a chat folder.

        Args:
            p: The caller and user.
            virtual: A path that should name a file.

        Returns:
            The real path, the chat id, and the file's path below its chat folder.

        Raises:
            PathError: If the path is outside the user's root, or is ``/workspace`` or a chat folder.
        """
        real, segments = self._locate(p, virtual)
        if len(segments) < 2:
            raise PathError("a file is inside a chat folder")
        return real, segments[0], "/".join(segments[1:])

    def _require_folder(self, real: Path, segments: list[str]) -> None:
        """Make sure a folder exists; a chat folder is made on first use, a deeper one must exist.

        Args:
            real: The folder's real path.
            segments: ``[chat-id, *folders]`` of that folder.

        Raises:
            NotFound: If a folder below the chat folder is missing, or the path is not a folder.
            StorageFull: If the chat folder cannot be made for lack of room.
        """
        st = _lstat(real)
        if st is None and len(segments) == 1:
            self._make_dirs(real)
            return
        if st is None or not stat.S_ISDIR(st.st_mode):
            raise NotFound("folder not found")

    def _make_dirs(self, real: Path) -> None:
        """Create a folder and any missing parents, each with mode 0700.

        Args:
            real: A real path inside the user's root, already checked for symlinks.

        Raises:
            Conflict: If something that is not a folder is in the way.
            StorageFull: If the volume has no room.
        """
        missing: list[Path] = []
        current = real
        while True:
            st = _lstat(current)
            if st is not None:
                if not stat.S_ISDIR(st.st_mode):
                    raise Conflict("not a folder")
                break
            missing.append(current)
            current = current.parent
        for folder in reversed(missing):
            try:
                os.mkdir(folder, _PRIVATE_DIR)
            except FileExistsError:
                continue
            except OSError as exc:
                if _is_full(exc):
                    raise StorageFull("storage is full") from exc
                raise
            os.chmod(folder, _PRIVATE_DIR)

    # --- rows ----------------------------------------------------------------

    def _row(self, p: Principal, chat_id: str, name: str) -> FileRecord:
        """Look up one file's record.

        Args:
            p: The owner.
            chat_id: The chat folder.
            name: The file's path below the chat folder.

        Returns:
            The record.

        Raises:
            NotFound: If the user has no such file.
        """
        with self._db() as db:
            row = db.execute(
                f"SELECT {_FILE_COLUMNS} FROM files WHERE caller = ? AND user_id = ? AND chat_id = ? AND name = ?",
                (p.caller, p.user_id, chat_id, name),
            ).fetchone()
        if row is None:
            raise NotFound("file not found")
        return _file(row)

    def _has_row(self, p: Principal, chat_id: str, name: str) -> bool:
        """Tell whether the user has a record for a name.

        Args:
            p: The owner.
            chat_id: The chat folder.
            name: The file's path below the chat folder.

        Returns:
            ``True`` if there is one.
        """
        with self._db() as db:
            row = db.execute(
                "SELECT 1 FROM files WHERE caller = ? AND user_id = ? AND chat_id = ? AND name = ?",
                (p.caller, p.user_id, chat_id, name),
            ).fetchone()
        return row is not None

    def _insert(self, rec: FileRecord) -> None:
        """Store a new record.

        Args:
            rec: The record.

        Raises:
            StorageFull: If the database cannot grow.
        """
        try:
            with self._db() as db:
                db.execute(
                    f"INSERT INTO files ({_FILE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rec.id,
                        rec.caller,
                        rec.user_id,
                        rec.chat_id,
                        rec.name,
                        rec.size,
                        rec.mime,
                        rec.kind,
                        rec.created_at,
                        rec.job_id,
                    ),
                )
        except sqlite3.OperationalError as exc:
            if "full" in str(exc):
                raise StorageFull("storage is full") from exc
            raise

    def _free_name(self, p: Principal, folder: Path, subdirs: list[str], chat_id: str, wanted: str) -> str:
        """Pick a name in a folder that is free on disk and in the table.

        Args:
            p: The owner.
            folder: The real folder.
            subdirs: Folders between the chat folder and ``folder``.
            chat_id: The chat folder's name.
            wanted: The name asked for.

        Returns:
            ``wanted``, or the first ``stem (n).ext`` that is free.
        """
        for n in itertools.count(0):
            candidate = wanted if n == 0 else _numbered(wanted, n)
            if _lstat(folder / candidate) is None and not self._has_row(p, chat_id, "/".join([*subdirs, candidate])):
                return candidate
        raise AssertionError("unreachable")  # pragma: no cover

    # --- files ---------------------------------------------------------------

    def add_upload(
        self,
        p: Principal,
        directory: str,
        filename: str,
        src: BinaryIO,
        *,
        max_bytes: int,
        quota_bytes: int,
    ) -> FileRecord:
        """Stream an upload into a chat folder, or a folder inside one.

        A chat folder is made on first use; a folder below it must exist (``mkdir``).
        On a name clash the new file gets ``name (1).ext``, never replacing the old one.

        Args:
            p: The caller and user; the chat comes from ``directory``.
            directory: ``/workspace/<chat-id>`` or a folder inside it.
            filename: The file's name.
            src: The bytes; read in chunks until empty.
            max_bytes: Largest file allowed.
            quota_bytes: Most bytes the user may hold in all chats together.

        Returns:
            The new record.

        Raises:
            PathError: If the directory is outside the user's root or is ``/workspace`` itself,
                or the name is invalid.
            NotFound: If a folder below the chat folder is missing.
            TooLarge: If the file is over ``max_bytes``.
            QuotaExceeded: If it would take the user over ``quota_bytes``.
            StorageFull: If the volume is full.
        """
        _check_new_name(check_filename(filename))
        folder, segments = self._locate(p, directory)
        if not segments:
            raise PathError("files live inside a chat folder")
        file_id = uuid.uuid4().hex
        part = folder / f"{file_id}{_PART}"
        room = quota_bytes - self.usage(p)
        with self._lock:  # the folder is found or made and the part is in it before retention may look at the folder
            self._require_folder(folder, segments)
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, _PRIVATE_FILE)
        size = self._write_part(fd, part, src, max_bytes=max_bytes, room=room)
        try:
            with self._lock:
                if self.usage(p) + size > quota_bytes:
                    raise QuotaExceeded("workspace quota exceeded")
                name = self._free_name(p, folder, segments[1:], segments[0], filename)
                final = folder / name
                os.replace(part, final)
                rec = FileRecord(
                    id=file_id,
                    caller=p.caller,
                    user_id=p.user_id,
                    chat_id=segments[0],
                    name="/".join([*segments[1:], name]),
                    size=size,
                    mime=_mime(name),
                    kind="upload",
                    created_at=self._clock(),
                    job_id=None,
                )
                try:
                    self._insert(rec)
                except BaseException:
                    _unlink(final)
                    raise
        except BaseException:
            _unlink(part)
            raise
        return rec

    def _write_part(self, fd: int, part: Path, src: BinaryIO, *, max_bytes: int, room: int) -> int:
        """Stream ``src`` into a new private file, stopping at the limits; the file goes on any failure.

        Args:
            fd: The file, just created and open for writing; it is closed here.
            part: Its path.
            src: The bytes.
            max_bytes: Largest size allowed.
            room: Bytes of quota left.

        Returns:
            The number of bytes written.

        Raises:
            TooLarge: If ``src`` holds more than ``max_bytes``.
            QuotaExceeded: If it holds more than ``room``.
            StorageFull: If the volume ran out of room.
        """
        size = 0
        try:
            try:
                while chunk := src.read(_CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise TooLarge("file too large")
                    if size > room:
                        raise QuotaExceeded("workspace quota exceeded")
                    _write_all(fd, chunk)
            finally:
                os.close(fd)
        except BaseException as exc:
            _unlink(part)
            if isinstance(exc, OSError) and _is_full(exc):
                raise StorageFull("storage is full") from exc
            raise
        return size

    def register_output(self, p: Principal, staged: Path, name: str, job_id: str) -> FileRecord:
        """Move a file a job produced into the principal's chat folder.

        ``staged`` must be on the same filesystem as the workspaces (the staging folder of
        the data volume is). Outputs are not counted against the quota when they are made, but
        they use up the room of later uploads. A name clash gets ``name (1).ext``.

        Args:
            p: The caller, user and chat that ran the job.
            staged: A regular file, not a symlink; it is moved, so it is gone afterwards.
            name: The name for the file in the chat folder.
            job_id: The job that made it.

        Returns:
            The new record.

        Raises:
            PathError: If the name is invalid, or ``staged`` is not a regular file.
            NotFound: If ``staged`` does not exist.
            StorageFull: If the volume is full.
        """
        _check_new_name(check_filename(name))
        try:
            fd = os.open(staged, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError as exc:
            raise NotFound("staged file not found") from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise PathError("staged output is not a regular file") from exc
            raise
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise PathError("staged output is not a regular file")
            os.fchmod(fd, _PRIVATE_FILE)
        finally:
            os.close(fd)
        with self._lock:  # the chat folder is found or made and the file is in it before retention may look at it
            folder = self._ws.chat_dir(p)
            final_name = self._free_name(p, folder, [], p.chat_id, name)
            final = folder / final_name
            os.replace(staged, final)
            rec = FileRecord(
                id=uuid.uuid4().hex,
                caller=p.caller,
                user_id=p.user_id,
                chat_id=p.chat_id,
                name=final_name,
                size=st.st_size,
                mime=_mime(final_name),
                kind="output",
                created_at=self._clock(),
                job_id=job_id,
            )
            try:
                self._insert(rec)
            except BaseException:
                _unlink(final)
                raise
        return rec

    def list_dir(self, p: Principal, directory: str) -> list[DirEntry]:
        """List what is on disk in a folder of the user's root.

        ``/workspace`` lists the chat folders. Half-written ``.part`` files and symlinks
        are left out. The root and a chat folder that do not exist yet list as empty.

        Args:
            p: The caller and user.
            directory: ``/workspace``, a chat folder, or a folder inside one.

        Returns:
            The entries, sorted by name.

        Raises:
            PathError: If the directory is outside the user's root.
            NotFound: If a folder below a chat folder does not exist, or the path is not a folder.
        """
        real, segments = self._locate(p, directory)
        st = _lstat(real)
        if st is None and len(segments) <= 1:
            return []
        if st is None or not stat.S_ISDIR(st.st_mode):
            raise NotFound("folder not found")
        entries: list[DirEntry] = []
        with os.scandir(real) as scan:
            for entry in scan:
                if entry.name.endswith(_PART):
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    entries.append(DirEntry(entry.name, "directory", 0, info.st_mtime))
                elif stat.S_ISREG(info.st_mode):
                    entries.append(DirEntry(entry.name, "file", info.st_size, info.st_mtime))
        return sorted(entries, key=lambda e: e.name)

    def get(self, p: Principal, path: str) -> FileRecord:
        """Look up a file of the user's by its virtual path.

        Args:
            p: The caller and user.
            path: ``/workspace/<chat-id>/<name>``.

        Returns:
            The record.

        Raises:
            PathError: If the path is outside the user's root, or is ``/workspace`` or a chat folder.
            NotFound: If the user has no such file, or it is gone from the disk.
        """
        real, chat_id, name = self._file_target(p, path)
        rec = self._row(p, chat_id, name)
        st = _lstat(real)
        if st is None or not stat.S_ISREG(st.st_mode):
            raise NotFound("file not found")
        return rec

    def open_file(self, p: Principal, path: str) -> tuple[FileRecord, BinaryIO]:
        """Open a file of the user's for reading.

        The file is opened without following a link and checked to be a regular file, so
        what is read is the file the record is about.

        Args:
            p: The caller and user.
            path: ``/workspace/<chat-id>/<name>``.

        Returns:
            The record, and the file open in binary mode; the caller closes it.

        Raises:
            PathError: If the path is outside the user's root, is ``/workspace`` or a chat folder,
                or runs through a symlink.
            NotFound: If the user has no such file, or it is gone from the disk.
        """
        real, chat_id, name = self._file_target(p, path)
        rec = self._row(p, chat_id, name)
        try:
            fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise NotFound("file not found") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise NotFound("file not found")
            return rec, os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise

    def files_in_chat(self, p: Principal) -> list[FileRecord]:
        """List the files of the principal's chat, including those in its folders, oldest first.

        Args:
            p: The caller, user and chat.

        Returns:
            Their records.
        """
        with self._db() as db:
            rows = db.execute(
                f"SELECT {_FILE_COLUMNS} FROM files WHERE caller = ? AND user_id = ? AND chat_id = ? "
                "ORDER BY created_at, name",
                (p.caller, p.user_id, p.chat_id),
            ).fetchall()
        return [_file(row) for row in rows]

    def count_files(self) -> int:
        """Count the stored files of every user, for the metrics.

        Returns:
            How many file records there are.
        """
        with self._db() as db:
            row = db.execute("SELECT COUNT(*) FROM files").fetchone()
        return int(row[0])

    def delete(self, p: Principal, path: str) -> FileRecord:
        """Delete a file of the user's: its bytes and its record.

        Args:
            p: The caller and user.
            path: ``/workspace/<chat-id>/<name>``.

        Returns:
            The record that was deleted.

        Raises:
            PathError: If the path is outside the user's root, or is ``/workspace`` or a chat folder.
            NotFound: If the user has no such file.
        """
        real, chat_id, name = self._file_target(p, path)
        with self._lock:
            rec = self._row(p, chat_id, name)
            _unlink(real)
            self._delete_row(rec.id)
        return rec

    def mkdir(self, p: Principal, path: str) -> str:
        """Make a folder, and its parents; one that exists is left as it is.

        Args:
            p: The caller and user.
            path: ``/workspace/<chat-id>`` or a folder inside it.

        Returns:
            The folder's canonical virtual path.

        Raises:
            PathError: If the path is outside the user's root or the name ends in ``.part``.
            Conflict: If a file is in the way.
            StorageFull: If the volume is full.
        """
        real, segments = self._locate(p, path)
        if segments:
            _check_new_name(segments[-1])
        with self._lock:  # retention removes empty folders under the same lock
            self._make_dirs(real)
        return self._ws.to_virtual(p, real)

    def move(self, p: Principal, source: str, destination: str) -> FileRecord:
        """Rename a file, within the user's root.

        The destination is the file's new full path. Its folder must exist, except that a
        chat folder is made on first use. Nothing is overwritten.

        Args:
            p: The caller and user.
            source: The file's current path.
            destination: The path it should have.

        Returns:
            The updated record; the id and the creation time are unchanged.

        Raises:
            PathError: If a path is outside the user's root, is ``/workspace`` or a chat folder,
                or the new name ends in ``.part``.
            NotFound: If the user has no such file, or the destination folder is missing.
            Conflict: If the destination exists.
        """
        src_real, src_chat, src_name = self._file_target(p, source)
        dst_real, dst_chat, dst_name = self._file_target(p, destination)
        _check_new_name(dst_name.rsplit("/", 1)[-1])
        with self._lock:
            rec = self._row(p, src_chat, src_name)
            src_st = _lstat(src_real)
            if src_st is None or not stat.S_ISREG(src_st.st_mode):
                raise NotFound("file not found")
            self._require_folder(dst_real.parent, [dst_chat, *dst_name.split("/")[:-1]])
            if _lstat(dst_real) is not None or self._has_row(p, dst_chat, dst_name):
                raise Conflict("the destination exists")
            os.replace(src_real, dst_real)
            moved = dataclasses.replace(rec, chat_id=dst_chat, name=dst_name, mime=_mime(dst_name))
            try:
                with self._db() as db:
                    db.execute(
                        "UPDATE files SET chat_id = ?, name = ?, mime = ? WHERE id = ?",
                        (moved.chat_id, moved.name, moved.mime, moved.id),
                    )
            except BaseException:
                os.replace(dst_real, src_real)
                raise
        return moved

    def usage(self, p: Principal) -> int:
        """Add up the sizes of all the user's files, in every chat.

        Args:
            p: The caller and user.

        Returns:
            Bytes.
        """
        with self._db() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(size), 0) FROM files WHERE caller = ? AND user_id = ?",
                (p.caller, p.user_id),
            ).fetchone()
        return int(row[0])

    def due(self, cutoff: float, limit: int = 200, *, after: tuple[float, str] | None = None) -> list[FileRecord]:
        """List files created before a time, for the retention sweep.

        Args:
            cutoff: Files with ``created_at`` strictly below this are due.
            limit: Most records to return.
            after: Only files past this ``(created_at, id)``, the last of the page before: a sweep pages on past the
                files it could not delete, which keep their rows.

        Returns:
            The oldest due files, of every user, in ``(created_at, id)`` order.
        """
        where, params = "created_at < ?", [cutoff]
        if after is not None:
            where += " AND (created_at, id) > (?, ?)"
            params += [*after]
        with self._db() as db:
            rows = db.execute(
                f"SELECT {_FILE_COLUMNS} FROM files WHERE {where} ORDER BY created_at, id LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [_file(row) for row in rows]

    def remove(self, record: FileRecord) -> None:
        """Delete a file's bytes and its record; already gone is fine.

        The file is found by ``record.id``, at the path its row has now: a record that
        went stale (the file was moved or deleted meanwhile) never reaches a file that
        now sits at its old path.

        Args:
            record: A record, for example one ``due`` returned.

        Raises:
            PathError: If the file's path now runs through a symlink; nothing is deleted.
            OSError: If the file cannot be deleted; its record stays so a later sweep retries.
        """
        with self._lock:
            with self._db() as db:
                row = db.execute(f"SELECT {_FILE_COLUMNS} FROM files WHERE id = ?", (record.id,)).fetchone()
            if row is None:
                return
            current = _file(row)
            owner = Principal(current.caller, current.user_id, current.chat_id)
            _unlink(self._ws.resolve(owner, current.virtual_path))
            self._delete_row(current.id)

    def _delete_row(self, file_id: str) -> None:
        """Delete one record.

        Args:
            file_id: The file's id.
        """
        with self._db() as db:
            db.execute("DELETE FROM files WHERE id = ?", (file_id,))

    def checkpoint(self) -> bool:
        """Copy the write-ahead log into the database file and empty it.

        ``secure_delete`` zeroes the pages that held deleted rows, but the log can still hold earlier versions of
        them, file names among them. A ``TRUNCATE`` checkpoint copies every frame into the database file, where the
        zeroed pages replace the old ones, and truncates the log. It waits for readers and writers up to the busy
        timeout.

        Returns:
            Whether the log was emptied: ``False`` when a reader or writer kept it busy, and a later checkpoint, or the
            one SQLite makes as the last connection closes, carries the frames on.
        """
        with self._db() as db:
            busy, _, _ = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        return busy == 0

    def sweep_user(
        self, caller: str, user_id: str, *, untouched_before: float, device: int, record: OrphanRecord
    ) -> tuple[int, int]:
        """Remove the files of a user's root that have no record, then the folders left empty, all under the lock.

        An orphan is a regular file in a chat folder that has no row and was last changed before ``untouched_before``:
        the ``<id>.part`` of an upload the api died during, or a file a kill left between its rename into place and
        its row's commit (``add_upload``, ``register_output``, ``move``). The look and the removal hold the store's
        lock, which every rename into place holds until its row is committed, so a file on its way in is never taken
        for one. Each chat folder's orphans are recorded first, by ``record``, still under the lock, at most
        ``MAX_IDS`` to a record so that no id is cut; those that could not be removed are recorded again as an error.
        If ``record`` raises, nothing more is removed and the error goes on.

        Then every folder below the root, deepest first, the chat folders too, and the root itself go if they are
        empty and were last changed before ``untouched_before``. Their ages are taken before anything is removed, so
        an old tree of empty folders goes at once. Nothing is followed: links, anything but folders and regular files,
        and anything of another device are left alone, and so is a folder in the root whose name is no chat id. An
        entry that cannot be looked at, or a folder that cannot be listed, is skipped with a warning.

        Args:
            caller: The caller's folder's name.
            user_id: The user's folder's name.
            untouched_before: A file or folder last changed before this may go.
            device: The data volume's device.
            record: Records one chat folder's orphans before they go.

        Returns:
            How many orphans were removed, and how many could not be.

        Raises:
            PathError: A name is no safe id, or a link is in the root's path.
            Exception: What ``record`` raised.
        """
        p = Principal(caller, user_id, DEFAULT_CHAT)
        with self._lock:
            root = self._ws.resolve(p, VIRTUAL_ROOT)
            root_st = _look(root)
            if not _is_folder(root_st, device):
                return 0, 0
            folders: list[tuple[Path, os.stat_result]] = []
            candidates: dict[str, list[tuple[str, Path]]] = {}
            for here, names, files in os.walk(root, onerror=_unlisted):
                where = Path(here)
                below = where.relative_to(root).parts
                kept: list[str] = []
                for name in sorted(names):
                    st = _look(where / name)
                    if _is_folder(st, device) and (below or _is_chat_id(name)):
                        kept.append(name)
                        folders.append((where / name, st))
                names[:] = kept  # never into a link, another filesystem or a folder that is no chat's
                if not below:
                    continue  # a file in the root is in no chat folder
                for name in files:
                    st = _look(where / name)
                    if st is None or not stat.S_ISREG(st.st_mode) or st.st_dev != device:
                        continue
                    if st.st_mtime < untouched_before:
                        candidates.setdefault(below[0], []).append(("/".join([*below[1:], name]), where / name))
            removed = failed = 0
            if candidates:
                with self._db() as db:
                    rows = db.execute(
                        "SELECT chat_id, name FROM files WHERE caller = ? AND user_id = ?", (caller, user_id)
                    ).fetchall()
                known = {(chat, name) for chat, name in rows}
                for chat in sorted(candidates):
                    orphans = [path for name, path in candidates[chat] if (chat, name) not in known]
                    for start in range(0, len(orphans), MAX_IDS):
                        batch = orphans[start : start + MAX_IDS]
                        record(chat, _part_ids(batch), len(batch), "ok")
                        stayed: list[Path] = []
                        for path in batch:
                            try:
                                _unlink(path)
                            except OSError as exc:
                                _log.warning("could not remove a file that had no record (%s)", type(exc).__name__)
                                stayed.append(path)
                            else:
                                removed += 1
                        if stayed:
                            record(chat, _part_ids(stayed), len(stayed), "error")
                            failed += len(stayed)
            for folder, st in reversed(folders):  # a folder comes after every folder above it
                if st.st_mtime < untouched_before:
                    _remove_empty(folder)
            if root_st is not None and root_st.st_mtime < untouched_before:
                _remove_empty(root)
        return removed, failed

    # --- jobs ----------------------------------------------------------------

    def add_job(self, p: Principal, tool: str) -> JobRecord:
        """Record that a tool run starts.

        Args:
            p: The caller, user and chat.
            tool: The tool's name.

        Returns:
            The job, with status ``running``.

        Raises:
            PathError: If the caller, user or chat id is not a safe id.
        """
        self._ws.user_root(p)
        job = JobRecord(
            id=uuid.uuid4().hex,
            caller=p.caller,
            user_id=p.user_id,
            chat_id=check_id(p.chat_id, what="chat id"),
            tool=tool,
            created_at=self._clock(),
            status="running",
        )
        with self._db() as db:
            db.execute(
                f"INSERT INTO jobs ({_JOB_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (job.id, job.caller, job.user_id, job.chat_id, job.tool, job.created_at, job.status),
            )
        return job

    def finish_job(self, job_id: str, status: str) -> None:
        """Set a job's final status.

        Args:
            job_id: The job's id.
            status: The final status.
        """
        with self._db() as db:
            db.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))

    def get_job(self, p: Principal, job_id: str) -> JobRecord:
        """Look up one of the user's jobs.

        Args:
            p: The caller and user; another user's job is not found.
            job_id: The job's id.

        Returns:
            The job.

        Raises:
            NotFound: If the user has no such job.
        """
        with self._db() as db:
            row = db.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ? AND caller = ? AND user_id = ?",
                (job_id, p.caller, p.user_id),
            ).fetchone()
        if row is None:
            raise NotFound("job not found")
        return _job(row)

    def find_job(self, job_id: str) -> JobRecord | None:
        """Look up a job by its id alone, for retention, which knows a leftover staging folder by its job's id.

        Args:
            job_id: The job's id.

        Returns:
            The job, or ``None`` if there is no such record (any more).
        """
        with self._db() as db:
            row = db.execute(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return None if row is None else _job(row)

    def due_jobs(self, cutoff: float) -> list[str]:
        """List jobs started before a time.

        Args:
            cutoff: Jobs with ``created_at`` strictly below this are due.

        Returns:
            Their ids, oldest first.
        """
        with self._db() as db:
            rows = db.execute("SELECT id FROM jobs WHERE created_at < ? ORDER BY created_at, id", (cutoff,)).fetchall()
        return [row[0] for row in rows]

    def forget_jobs(self, ids: Sequence[str]) -> None:
        """Delete job records.

        Args:
            ids: The jobs' ids; unknown ones are ignored.
        """
        with self._db() as db:
            db.executemany("DELETE FROM jobs WHERE id = ?", [(i,) for i in ids])


@dataclasses.dataclass(frozen=True)
class DueSummary:
    """What retention would delete of one kind of file now, and what it keeps.

    Attributes:
        due: Files created before the cutoff.
        oldest: ``created_at`` of the oldest of them, or ``None`` when none is due.
        newest: ``created_at`` of the newest of them, or ``None`` when none is due.
        kept: Files created at the cutoff or after it.
    """

    due: int
    oldest: float | None
    newest: float | None
    kept: int


def summarize_due(db_path: Path, cutoff: float) -> dict[str, DueSummary]:
    """Count the due and the kept files of each kind, through a connection that cannot write.

    The database is never created, and nothing in it changes. SQLite does make an empty ``-wal`` and ``-shm`` beside a
    database in WAL mode that no other connection holds open, because a reader needs them; the api's next connection
    takes them over.

    Args:
        db_path: The store's database.
        cutoff: Files with ``created_at`` strictly below this are due.

    Returns:
        By kind, ``upload`` or ``output``; a kind of which there is no file is missing.

    Raises:
        sqlite3.Error: The database does not exist or cannot be read.
    """
    conn = sqlite3.connect(f"file:{urllib.parse.quote(str(db_path))}?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_S)
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = conn.execute(
            "SELECT kind, COALESCE(SUM(created_at < :cutoff), 0), "
            "MIN(CASE WHEN created_at < :cutoff THEN created_at END), "
            "MAX(CASE WHEN created_at < :cutoff THEN created_at END), "
            "COALESCE(SUM(created_at >= :cutoff), 0) "
            "FROM files GROUP BY kind",
            {"cutoff": cutoff},
        ).fetchall()
    finally:
        conn.close()
    return {kind: DueSummary(int(due), oldest, newest, int(kept)) for kind, due, oldest, newest, kept in rows}
