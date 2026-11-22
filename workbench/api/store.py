"""Workspace files on disk and their metadata in SQLite.

The ``files`` table is the source of truth for the files the store manages: the
file routes, the tools and the retention sweep all go through it. The bytes live
under the user's own root (``paths.Workspaces``); a file is only ever inside a
chat folder, ``/workspace/<chat-id>/…``. Folders have no record, they are plain
directories. Nothing here follows a symlink.

An upload is streamed to ``<file id>.part`` in its folder and then renamed into
place, so a reader never sees half a file and a failed upload leaves nothing.
Names, quota and the row are settled under one lock, so concurrent uploads of one
name each get their own file. The file id is chosen before any byte is written, so
the upload is recorded by it before the rename makes the file visible: no file is
ever stored without its record. A job's output is registered under an id the
caller chose and recorded before it called, for the same reason.

The store never deletes a file of its own accord; :meth:`Store.remove` is called
only once its deletion is recorded, and :meth:`Store.delete_chat` and
:meth:`Store.remove_upload` record a chat's or an upload's deletion themselves before anything of it goes.

A derived file, the render of a picture or of a page of a PDF, the PDF an Office file was converted to, the transcript
and the frames of a clip, or the two raw artifacts of a clip Nextext made (its JSON Lines and its keyframes zip), is
kept beside its source, in the reserved folder ``<chat>/.derived/<source-id>/``, under its own table. It is bound to its
source: it is as old as it, counts in the user's quota, is deleted with it, and is reachable by no path: ``.derived``
is found by no path of any principal, listed in no folder, and no upload, output or folder is named so. The caller
records a render before it is registered, as it records an output. An artifact is streamed in, not staged by a job:
:meth:`Store.stage_derived` reads its body into ``<id>.part`` in that folder before the caller waits for the source's
lock, and :meth:`Store.place_derived` records it and makes it visible under the store's lock.

The store also remembers, in this process alone, how the runner refused a source that cannot be converted or drawn
(:meth:`Store.remember_refusal`), so that no later call runs that job again; the refusal goes with its source, and with
a restart.
"""

import dataclasses
import errno
import itertools
import json
import logging
import mimetypes
import os
import posixpath
import re
import shutil
import sqlite3
import stat
import threading
import time
import urllib.parse
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Final, Literal, TypeGuard

from workbench.layout import DERIVED
from workbench.models import VIRTUAL_ROOT, DerivedRecord, DerivedSpec, DirEntry, FileRecord, JobRecord, Principal
from workbench.paths import DEFAULT_CHAT, PathError, Workspaces, check_filename, check_id
from workbench.renders import CONVERTED_MIME, DERIVED_MAX_BYTES, MEDIA_TYPES
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

#: Seconds a deleted chat takes no new job and no upload (:meth:`Store.delete_chat`): longer than any call waits for its
#: turn or any upload takes to arrive. A deleted chat's id never comes back, so the hold costs nothing.
DELETED_CHAT_HOLD_S: Final = 3600

#: Most refusals the store remembers or counts, one per source; past it, the one asked for least recently is forgotten.
#: Each is an id, three numbers and a short fixed text, so a few thousand cost the api well under a megabyte.
REFUSALS_MAX: Final = 4096
#: How many refusals in a row of one source make one that may pass remembered (:meth:`Store.remember_refusal`).
REFUSALS_IN_A_ROW: Final = 2

#: A file id: 32 lowercase hex digits (``uuid4().hex``).
_FILE_ID: Final = re.compile(r"[0-9a-f]{32}")

#: The name of an upload while it is written: its file id, then ``.part``.
_PART_NAME: Final = re.compile(r"([0-9a-f]{32})\.part")

#: The name of a derived file: its file id and its extension, a render's, the converted PDF's, a transcript's or an
#: artifact's.
_RENDER_NAME: Final = re.compile(r"([0-9a-f]{32})\.(?:jpg|png|pdf|txt|jsonl|zip)")

#: The extension of each type of derived file the store keeps: the renders, the PDF an Office file is converted to, the
#: transcript of a clip and the two artifacts Nextext made of it.
_DERIVED_EXTENSIONS: Final = {
    "image/jpeg": "jpg",
    "image/png": "png",
    CONVERTED_MIME: "pdf",
    "text/plain": "txt",
    "application/x-ndjson": "jsonl",
    "application/zip": "zip",
}

#: The types each role of derived file takes: a role takes its own alone.
_ROLE_TYPES: Final[Mapping[str, frozenset[str]]] = {
    "image": frozenset({"image/jpeg", "image/png"}),
    "page": frozenset({"image/jpeg", "image/png"}),
    "frame": frozenset({"image/jpeg"}),
    "pdf": frozenset({CONVERTED_MIME}),
    "transcript": frozenset({"text/plain"}),
    "nextext_jsonl": frozenset({"application/x-ndjson"}),
    "nextext_zip": frozenset({"application/zip"}),
}

#: The roles that are pictures: the only ones with a size in pixels.
_PICTURE_ROLES: Final = frozenset({"image", "page", "frame"})

#: The two raw artifacts of a clip, which are streamed in; one row of each role per source.
_ARTIFACT_ROLES: Final = frozenset({"nextext_jsonl", "nextext_zip"})

#: What is made from an artifact in the executor, and goes when an artifact is replaced.
_MADE_FROM_ARTIFACTS: Final = frozenset({"transcript", "frame"})

#: Most source ids one query of ``derived`` names, under the oldest SQLite's limit of 999 variables.
_IN_CHUNK: Final = 500


def new_file_id() -> str:
    """Choose the id of a new file, before anything of it is stored, so that its event can name it first.

    Returns:
        32 lowercase hex digits.
    """
    return uuid.uuid4().hex


#: Records orphans of one chat folder: called with the chat id, the file ids their names carry, how many there are, and
#: the outcome, ``ok`` before they go or ``error`` for those that could not be removed.
OrphanRecord = Callable[[str, Sequence[str], int, str], object]

#: Records an upload: called with the chat id of its folder, its file id and the outcome, ``ok`` before the file
#: becomes visible or ``error`` when it could not be stored after all. It raises when the record cannot be written.
UploadRecord = Callable[[str, str, str], object]

#: Records a chat's deletion: called with the file ids of the chat's files and the outcome, ``ok`` before anything goes
#: or ``error`` when the deletion failed after that. It raises when the record cannot be written.
ChatRecord = Callable[[Sequence[str], str], object]

#: Records an artifact of a clip: called with its new id, its size, the ids of the rows it replaces and the outcome,
#: ``ok`` before it becomes visible or ``error`` when it could not be kept after all. It raises when the record cannot
#: be written.
ArtifactRecord = Callable[[str, int, Sequence[str], str], object]

_EXTRA_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".potx": "application/vnd.openxmlformats-officedocument.presentationml.template",
    ".dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".eml": "message/rfc822",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
    **MEDIA_TYPES,
}


def _known_types() -> mimetypes.MimeTypes:
    """Build the name-to-type table: the library's built-in one plus the office formats and JSON Lines.

    A private table, so the answer never depends on which ``mime.types`` files the host has
    (a slim image has none, and would call every Office file ``application/octet-stream``).

    Returns:
        The table.
    """
    types = mimetypes.MimeTypes()
    for ext, mime in _EXTRA_TYPES.items():
        types.add_type(mime, ext)
    return types


_TYPES = _known_types()

_FILE_COLUMNS = "id, caller, user_id, chat_id, name, size, mime, kind, created_at, job_id"
_JOB_COLUMNS = "id, caller, user_id, chat_id, tool, created_at, status"
_DERIVED_COLUMNS = (
    "id, source_id, caller, user_id, chat_id, role, ordinal, name, size, mime, width, height, meta, created_at"
)

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
CREATE TABLE IF NOT EXISTS derived (
    id TEXT PRIMARY KEY, source_id TEXT NOT NULL, caller TEXT NOT NULL, user_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('image','page','pdf','frame','transcript','nextext_jsonl','nextext_zip')),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0), name TEXT NOT NULL, size INTEGER NOT NULL, mime TEXT NOT NULL,
    width INTEGER, height INTEGER, meta TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS derived_source ON derived (source_id);
CREATE INDEX IF NOT EXISTS derived_created ON derived (created_at);
CREATE INDEX IF NOT EXISTS derived_owner ON derived (caller, user_id, chat_id);
"""


class StoreError(Exception):
    """Base of the errors the store raises on purpose."""


class NotFound(StoreError):
    """No such file, folder or job for this user."""


class Gone(NotFound):
    """The file, job or chat a new file was for went while it was made: its chat was deleted, or retention took it."""


class TooLarge(StoreError):
    """An upload is bigger than the per-file limit."""


class QuotaExceeded(StoreError):
    """An upload would take the user over their workspace quota."""


class StorageFull(StoreError):
    """The volume has no room left."""


class Conflict(StoreError):
    """The target already exists, or something that is not a folder is in the way."""


class RendersFull(StoreError):
    """A source file already holds as many bytes of renders as it may."""


#: The two reasons an artifact is refused, as the contract words them: fixed literals, the whole of what a caller is
#: told. No text of a caller, no name and no byte of an artifact is ever part of one.
ARTIFACT_EMPTY: Final = "unreadable_file: the artifact is empty"
ARTIFACT_NOT_ITS_ROLE: Final = "unreadable_file: the artifact is not what its role says"
_ARTIFACT_REASONS: Final = (ARTIFACT_EMPTY, ARTIFACT_NOT_ITS_ROLE)


class UnreadableArtifact(StoreError):
    """The first bytes of an artifact are not what its role says; the message is the caller's answer.

    The message is :data:`ARTIFACT_EMPTY` or :data:`ARTIFACT_NOT_ITS_ROLE`, and nothing else can be made of it.
    """

    def __init__(self, reason: str = ARTIFACT_NOT_ITS_ROLE) -> None:
        """Say that the artifact cannot be read.

        Args:
            reason: :data:`ARTIFACT_EMPTY` or :data:`ARTIFACT_NOT_ITS_ROLE`; any other text is replaced by the second,
                so that no word of a caller and no byte of an artifact reaches an answer.
        """
        super().__init__(reason if reason in _ARTIFACT_REASONS else ARTIFACT_NOT_ITS_ROLE)


@dataclasses.dataclass(frozen=True)
class StagedArtifact:
    """An artifact of a clip read in whole into its ``.part``, checked, and not yet visible.

    Attributes:
        source: The clip it is an artifact of.
        spec: What it is: role ``nextext_jsonl`` or ``nextext_zip``, with that role's type.
        id: Its file id, chosen at the stage and named by the event that records it.
        part: Its ``.part`` in the clip's folder of derived files.
        size: Its size in bytes.
    """

    source: FileRecord
    spec: DerivedSpec
    id: str
    part: Path
    size: int


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


def _orphan_ids(paths: Sequence[Path]) -> list[str]:
    """Take the file ids the names of orphans carry.

    Args:
        paths: The orphans.

    Returns:
        The id of each ``<id>.part``, and of each ``<id>.jpg``, ``<id>.png``, ``<id>.pdf``, ``<id>.txt``,
        ``<id>.jsonl`` or ``<id>.zip`` in a folder ``.derived/<source-id>``; the others carry none.
    """
    ids: list[str] = []
    for path in paths:
        found = _PART_NAME.fullmatch(path.name)
        if found is None and path.parent.parent.name == DERIVED and _FILE_ID.fullmatch(path.parent.name):
            found = _RENDER_NAME.fullmatch(path.name)
        if found is not None:
            ids.append(found.group(1))
    return ids


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


def _names_chat(virtual: str, chat_id: str) -> bool:
    """Tell from a virtual path's text alone whether it is a chat's folder or below it, looking nothing up.

    Args:
        virtual: Any virtual path.
        chat_id: The chat.

    Returns:
        ``True`` if, normalised as :meth:`~workbench.paths.Workspaces.resolve` normalises it, its first segment below
        ``/workspace`` is ``chat_id``, as a whole segment.
    """
    norm = posixpath.normpath(virtual)
    below = VIRTUAL_ROOT + "/"
    return norm.startswith(below) and norm[len(below) :].split("/", 1)[0] == chat_id


def _is_full(exc: OSError) -> bool:
    """Tell whether an error means the volume or the user's disk quota is used up.

    Args:
        exc: The error.

    Returns:
        ``True`` for ``ENOSPC`` and ``EDQUOT``.
    """
    return exc.errno in _FULL


def _is_reserved(name: str) -> bool:
    """Tell whether a name is the reserved ``.derived``, in any case: a case-blind volume would be the same folder.

    Args:
        name: One name.

    Returns:
        ``True`` for ``.derived``, ``.Derived`` and the like.
    """
    return name.casefold() == DERIVED


def _check_new_name(name: str) -> str:
    """Check a name that is about to be created.

    Args:
        name: A file or folder name that already passed ``check_filename``.

    Returns:
        ``name``, unchanged.

    Raises:
        PathError: If it ends in ``.part``, which marks a half-written upload, or is the reserved ``.derived``.
    """
    if name.endswith(_PART) or _is_reserved(name):
        raise PathError("invalid file name")
    return name


def _take_staged(staged: Path) -> os.stat_result:
    """Check a file a job staged and make it private, before it is moved.

    Args:
        staged: The staged file.

    Returns:
        Its ``fstat``.

    Raises:
        NotFound: If it does not exist.
        PathError: If it is a link or not a regular file.
    """
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
    return st


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


def _derived(row: tuple[Any, ...]) -> DerivedRecord:
    """Build a record from a ``derived`` row.

    Args:
        row: Columns in ``_DERIVED_COLUMNS`` order.

    Returns:
        The record.
    """
    meta = json.loads(row[12])
    return DerivedRecord(
        id=row[0],
        source_id=row[1],
        caller=row[2],
        user_id=row[3],
        chat_id=row[4],
        role=row[5],
        ordinal=row[6],
        name=row[7],
        size=row[8],
        mime=row[9],
        width=row[10],
        height=row[11],
        meta=meta if isinstance(meta, dict) else {},
        created_at=row[13],
    )


def _order(rec: DerivedRecord) -> tuple[str, int, int, int, str]:
    """Order renders as a source lists them: by role, ordinal, strips and strip.

    Args:
        rec: A render.

    Returns:
        Its sort key.
    """
    return rec.role, rec.ordinal, rec.strips, rec.strip, rec.id


def _replaced_by(role: str, held: Iterable[DerivedRecord]) -> list[DerivedRecord]:
    """Pick the rows a new artifact replaces: those of its own role, and the transcript and frames made from the old.

    Args:
        role: The new artifact's role.
        held: The renders of the clip, in the order they are listed.

    Returns:
        The rows that go, in that order.
    """
    return [rec for rec in held if rec.role == role or rec.role in _MADE_FROM_ARTIFACTS]


def _extension_of(spec: DerivedSpec) -> str:
    """Check that a derived file's role and type go together, and say what its file is called.

    Args:
        spec: What it is.

    Returns:
        The extension of its file: ``jpg``, ``png``, ``pdf``, ``txt``, ``jsonl`` or ``zip``.

    Raises:
        PathError: If the type is no type of derived file, the role does not take it, or a file that is no picture
            has a width or height.
    """
    extension = _DERIVED_EXTENSIONS.get(spec.mime)
    if extension is None:
        raise PathError("not a type of render")
    if spec.mime not in _ROLE_TYPES.get(spec.role, frozenset()):
        raise PathError("a derived file's role and type do not go together")
    if spec.role not in _PICTURE_ROLES and (spec.width, spec.height) != (None, None):
        raise PathError("only a picture has a size in pixels")
    return extension


def _new_derived(source: FileRecord, spec: DerivedSpec, file_id: str, size: int, extension: str) -> DerivedRecord:
    """Build the record of a new derived file of ``source``: as old as it, of its owner and chat.

    Args:
        source: The file it is made from.
        spec: What it is; its facts are stored as JSON, so what is not JSON raises.
        file_id: Its id.
        size: Its size in bytes, taken from the file.
        extension: Its file's extension, from :func:`_extension_of`.

    Returns:
        The record.
    """
    meta: Mapping[str, object] = json.loads(json.dumps(spec.meta, sort_keys=True, separators=(",", ":")))
    return DerivedRecord(
        id=file_id,
        source_id=source.id,
        caller=source.caller,
        user_id=source.user_id,
        chat_id=source.chat_id,
        role=spec.role,
        ordinal=spec.ordinal,
        name=f"{file_id}.{extension}",
        size=size,
        mime=spec.mime,
        width=spec.width,
        height=spec.height,
        meta=meta,
        created_at=source.created_at,
    )


def _open_part(part: Path) -> int:
    """Create a ``.part`` new and private, never through a link.

    Args:
        part: Its path.

    Returns:
        The file, open for writing.

    Raises:
        StorageFull: If the volume or the user's disk quota is used up.
    """
    try:
        return os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, _PRIVATE_FILE)
    except OSError as exc:
        if _is_full(exc):
            raise StorageFull("storage is full") from exc
        raise


def _owner(rec: DerivedRecord) -> tuple[str, str, str, str]:
    """Name a render's source and the owner of both.

    Args:
        rec: A render.

    Returns:
        Its ``(source_id, caller, user_id, chat_id)``.
    """
    return rec.source_id, rec.caller, rec.user_id, rec.chat_id


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
        self._monotonic: Callable[[], float] = time.monotonic
        self._deleted: dict[tuple[str, str, str], float] = {}
        self._refusals_lock = threading.Lock()
        # By source id: the source's creation time and size, the runner's last refusal of it and how many came in a row
        # (``REFUSALS_IN_A_ROW`` or more once it is remembered), oldest asked for first.
        self._refusals: OrderedDict[str, tuple[float, int, str, int]] = OrderedDict()
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

        A confined principal finds only its own chat folder and what is below it; every other path, and every path
        :meth:`~workbench.paths.Workspaces.resolve` refuses, is not found for it, so nothing tells it what is there.
        Its chat is read from the path's text before anything is looked up on disk, so a path into another chat
        touches nothing of that chat's folder; the resolved path is checked again.

        For every principal, confined or not, a path whose segments below the chat id include the reserved ``.derived``
        is not found: the folder of the renders is reached through no path, whether to read, list, make or upload into.

        Args:
            p: The caller and user.
            virtual: Any virtual path.

        Returns:
            The real path, and ``[chat-id, *names]`` (empty for ``/workspace``).

        Raises:
            PathError: If the path is not inside the user's root.
            NotFound: For a confined principal, if the path is not its chat folder or below it, or is refused; for
                every principal, if the path runs through ``.derived``.
        """
        if p.confined and not _names_chat(virtual, p.chat_id):
            raise NotFound("not in the chat")
        try:
            real = self._ws.resolve(p, virtual)
        except PathError:
            if p.confined:
                raise NotFound("not in the chat") from None
            raise
        segments = self._ws.to_virtual(p, real).split("/")[2:]
        if p.confined and segments[:1] != [p.chat_id]:
            raise NotFound("not in the chat")
        if any(_is_reserved(segment) for segment in segments[1:]):
            raise NotFound("not found")
        return real, segments

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

    def _refuse_deleted(self, caller: str, user_id: str, chat_id: str) -> None:
        """Refuse new work in a chat :meth:`delete_chat` deleted less than :data:`DELETED_CHAT_HOLD_S` ago.

        Call it under the store's lock, which the chat delete holds as it adds the chat.

        Args:
            caller: The caller's name.
            user_id: The user's id.
            chat_id: The chat's id.

        Raises:
            Gone: If the chat is held.
        """
        deadline = self._deleted.get((caller, user_id, chat_id))
        if deadline is not None and deadline > self._monotonic():
            raise Gone("chat deleted")

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
        record: UploadRecord,
    ) -> FileRecord:
        """Stream an upload into a chat folder, or a folder inside one, recording it before it becomes visible.

        A chat folder is made on first use; a folder below it must exist (``mkdir``).
        On a name clash the new file gets ``name (1).ext``, never replacing the old one.

        The id is chosen first and names the ``.part`` file the bytes go to. Once the quota and the name are settled,
        ``record`` writes the upload's event by that id, and only then is the file renamed into place and its row
        committed. A record that cannot be written leaves nothing stored. A rename or commit that fails after the record
        takes the file back out and records the upload again as ``error``; the store's error is what is raised, even if
        that second record fails too (it is logged by its type). A ``.part`` that cannot be removed after any failure
        never replaces that failure either: it is logged by its type, and retention removes it later. A process that
        ends between the two leaves a recorded ``<id>.part``, which retention removes and records by that id.

        Args:
            p: The caller and user; the chat comes from ``directory``.
            directory: ``/workspace/<chat-id>`` or a folder inside it; for a confined principal, its chat folder alone.
            filename: The file's name.
            src: The bytes; read in chunks until empty.
            max_bytes: Largest file allowed.
            quota_bytes: Most bytes the user may hold in all chats together.
            record: Writes the upload's event (:data:`UploadRecord`), under the store's lock.

        Returns:
            The new record.

        Raises:
            PathError: If the directory is outside the user's root or is ``/workspace`` itself,
                or the name is invalid.
            NotFound: If a folder below the chat folder is missing, or the chat was deleted within
                :data:`DELETED_CHAT_HOLD_S`; for a confined principal, if the directory is not its chat folder.
            TooLarge: If the file is over ``max_bytes``.
            QuotaExceeded: If it would take the user over ``quota_bytes``.
            StorageFull: If the volume is full.
            Exception: Whatever ``record`` raises when the event cannot be written; nothing is stored.
        """
        _check_new_name(check_filename(filename))
        folder, segments = self._locate(p, directory)
        if not segments:
            raise PathError("files live inside a chat folder")
        if p.confined and len(segments) > 1:
            raise NotFound("not in the chat")
        file_id = uuid.uuid4().hex
        part = folder / f"{file_id}{_PART}"
        room = quota_bytes - self.usage(p)
        with self._lock:  # the folder is found or made and the part is in it before retention may look at the folder
            self._refuse_deleted(p.caller, p.user_id, segments[0])
            self._require_folder(folder, segments)
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, _PRIVATE_FILE)
        size = self._write_part(fd, part, src, max_bytes=max_bytes, room=room)
        try:
            with self._lock:
                if self.usage(p) + size > quota_bytes:
                    raise QuotaExceeded("workspace quota exceeded")
                name = self._free_name(p, folder, segments[1:], segments[0], filename)
                record(segments[0], file_id, "ok")
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
                self._place(
                    part, folder / name, lambda: self._insert(rec), again=lambda: record(segments[0], file_id, "error")
                )
        except BaseException:
            try:
                _unlink(part)
            except OSError as exc:  # the first error is the answer; this one is only logged
                _log.warning("could not remove an upload that was not stored (%s)", type(exc).__name__)
            raise
        return rec

    def _place(
        self, source: Path, final: Path, insert: Callable[[], object], *, again: Callable[[], object] | None = None
    ) -> None:
        """Rename a recorded file into place and commit its row; on a failure take it back out and record that.

        Call it under the store's lock, once the file's event is written.

        Args:
            source: The file, already complete.
            final: Where it goes; a free name.
            insert: Commits its row.
            again: Records the file again as ``error``; a failure of it is logged by its type and never raised. ``None``
                when the caller records the failure itself.

        Raises:
            Exception: Whatever the rename or the commit raised; the file is not left at ``final``.
        """
        try:
            os.replace(source, final)
            try:
                insert()
            except BaseException:
                _unlink(final)
                raise
        except BaseException:
            if again is not None:
                try:
                    again()
                except Exception as exc:  # the store's error is the answer; this one is only logged
                    _log.warning("could not record a file that was not stored (%s)", type(exc).__name__)
            raise

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

    def register_output(
        self, p: Principal, staged: Path, name: str, job_id: str, *, file_id: str, quota_bytes: int
    ) -> FileRecord:
        """Move a file a job produced into the principal's chat folder, under an id its event already names.

        ``staged`` must be on the same filesystem as the workspaces (the staging folder of
        the data volume is). Outputs count against the user's quota like uploads: one that would
        take the user past it is refused, under the same lock as the name and the row. A name
        clash gets ``name (1).ext``.

        The caller records the output by ``file_id`` before it calls, so that the file is never visible without its
        event, and records it again as ``error`` if this raises.

        An output whose job row is gone, because :meth:`delete_chat` deleted the job's chat while it ran, is refused
        before the chat folder is made, under the lock the chat delete holds too.

        Args:
            p: The caller, user and chat that ran the job.
            staged: A regular file, not a symlink; it is moved, so it is gone afterwards.
            name: The name for the file in the chat folder.
            job_id: The job that made it.
            file_id: The file's id, chosen and recorded by the caller: 32 lowercase hex digits.
            quota_bytes: Most bytes the user may hold in all chats together.

        Returns:
            The new record.

        Raises:
            PathError: If the name is invalid, or ``staged`` is not a regular file.
            NotFound: If ``staged`` does not exist; ``staged`` stays.
            Gone: If the job's row is gone, with its chat; ``staged`` stays.
            QuotaExceeded: If it would take the user over ``quota_bytes``; ``staged`` stays.
            StorageFull: If the volume is full.
        """
        _check_new_name(check_filename(name))
        st = _take_staged(staged)
        if _FILE_ID.fullmatch(file_id) is None:
            raise ValueError("a file id is 32 lowercase hex digits")
        with self._lock:  # the chat folder is found or made and the file is in it before retention may look at it
            if self.find_job(job_id) is None:
                raise Gone("job gone")
            if self.usage(p) + st.st_size > quota_bytes:
                raise QuotaExceeded("workspace quota exceeded")
            folder = self._ws.chat_dir(p)
            final_name = self._free_name(p, folder, [], p.chat_id, name)
            rec = FileRecord(
                id=file_id,
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
            self._place(staged, folder / final_name, lambda: self._insert(rec))
        return rec

    def list_dir(self, p: Principal, directory: str) -> list[DirEntry]:
        """List what is on disk in a folder of the user's root.

        ``/workspace`` lists the chat folders. Half-written ``.part`` files, symlinks and the reserved ``.derived`` are
        left out. The root and a chat folder that do not exist yet list as empty.

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
                if entry.name.endswith(_PART) or _is_reserved(entry.name):
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

    def mkdir(self, p: Principal, path: str) -> str:
        """Make a folder, and its parents; one that exists is left as it is.

        No route or tool makes a folder: the tests lay out folders below a chat with it.

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

    def usage(self, p: Principal) -> int:
        """Add up the sizes of all the user's files, in every chat, and of their renders.

        Args:
            p: The caller and user.

        Returns:
            Bytes.
        """
        with self._db() as db:
            row = db.execute(
                "SELECT (SELECT COALESCE(SUM(size), 0) FROM files WHERE caller = ? AND user_id = ?) "
                "+ (SELECT COALESCE(SUM(size), 0) FROM derived WHERE caller = ? AND user_id = ?)",
                (p.caller, p.user_id, p.caller, p.user_id),
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

    def remove(self, record: FileRecord, derived: Sequence[DerivedRecord] = ()) -> None:
        """Delete a file's bytes and its record, and those of the renders it names; already gone is fine.

        It records nothing: call it only once the deletion is recorded (the user's delete, retention), or for a file
        whose own call's event covers its removal within the same request.

        The file is found by ``record.id``, at the path its row has now: a record that
        went stale (the file was deleted meanwhile) never reaches a file that now sits
        at its old path. The renders are found by their ids, as their rows are now, and only those that are this file's
        own, of its owner, are taken; a render named that is another's stays. Under the lock, each named render's file
        is deleted, then the file, and then the rows of both in one transaction; the folders of the renders go once
        they are empty. A render not named stays, its row and its file: with its source gone it is a stray, which
        retention's pass over renders (:meth:`due_derived`) takes, recorded. A source that is already gone still has
        the renders it names taken.

        Args:
            record: A record, for example one ``due`` returned.
            derived: The renders to delete with it, for example ``derived_of(record)``, which the caller has recorded.

        Raises:
            PathError: If the file's path, or a render's, now runs through a symlink; nothing is deleted.
            OSError: If the file cannot be deleted; its record stays so a later sweep retries.
        """
        with self._lock:
            self._remove_held(record, derived)

    def _remove_held(self, record: FileRecord, derived: Sequence[DerivedRecord]) -> None:
        """Delete a file and the renders it names, as :meth:`remove` does, with the store's lock already held.

        Args:
            record: The file's record.
            derived: The renders to delete with it.
        """
        with self._db() as db:
            row = db.execute(f"SELECT {_FILE_COLUMNS} FROM files WHERE id = ?", (record.id,)).fetchone()
        current = None if row is None else _file(row)
        held = current or record  # the row's owner counts; a source already gone has only the record's to go by
        owner = (held.caller, held.user_id, held.chat_id)
        named: dict[str, DerivedRecord] = {}
        for each in derived:
            found = self._current(each)
            if found is not None and _owner(found) == (record.id, *owner):
                named[found.id] = found
        if current is None and not named:
            return
        principal = Principal(*owner)
        doomed = [self._derived_path(found) for found in named.values()]
        file_path = None if current is None else self._ws.resolve(principal, current.virtual_path)
        folders = [self._derived_folder(principal, record.id)] if named else []
        for path in doomed:
            _unlink(path)
        if file_path is not None:
            _unlink(file_path)
        with self._db() as db:
            db.executemany("DELETE FROM derived WHERE id = ?", [(rec_id,) for rec_id in named])
            if current is not None:
                db.execute("DELETE FROM files WHERE id = ?", (current.id,))
        self._forget_refusals([record.id])
        for folder in folders:
            _remove_empty(folder)
            _remove_empty(folder.parent)

    def remove_upload(self, record: FileRecord, event: ChatRecord) -> int:
        """Delete an upload Ghostwriter takes back for its user, once its deletion is recorded: renders, bytes, rows.

        The renders are read and the file and they are deleted under one hold of the store's lock, which a render takes
        on its way in (:meth:`register_derived`): none made meanwhile is left behind. A job still running on the file
        works on its own copy and keeps no render, as its source is gone. ``event`` writes the deletion by the file's id
        and its renders' first; a record that cannot be written deletes nothing, and a failure after it is recorded
        again as ``error`` and raised, as :meth:`delete_chat` does.

        Args:
            record: The upload's record, as :meth:`get` found it for its owner.
            event: Writes the deletion's event (:data:`ChatRecord`), under the store's lock.

        Returns:
            How many renders went with it.

        Raises:
            NotFound: If the file's row went since it was found (taken back twice at once, its chat deleted, retention);
                nothing is recorded.
            PathError: If the file's path, or a render's, now runs through a symlink.
            Exception: What ``event`` raised, with nothing deleted; or what the deletion raised after the record.
        """
        with self._lock:
            if not self._has_file(record.id, record.caller, record.user_id, record.chat_id):
                raise NotFound("file not found")  # taken back, or its chat deleted, since it was found: no second event
            renders = self.derived_of(record)
            ids = [record.id, *(rec.id for rec in renders)]
            event(ids, "ok")
            try:
                self._remove_held(record, renders)
            except BaseException:
                try:
                    event(ids, "error")
                except Exception as exc:  # the deletion's error is the answer; this one is only logged
                    _log.warning("could not record a file delete that failed (%s)", type(exc).__name__)
                raise
        return len(renders)

    def delete_chat(self, p: Principal, record: ChatRecord) -> int:
        """Delete a chat, once its deletion is recorded: its files' and renders' rows, its job rows, and its folder.

        All of it holds the store's lock, which every file holds on its way into a chat folder. The ids of the chat's
        files are read, and then those of its renders (every one of the chat, a stray's too); ``record`` writes the
        deletion by them, the file rows, the render rows and the job rows go in one transaction, and then the chat
        folder, the renders' folder in it; a folder that is not there is fine. A job of the chat that is still running
        finds its row gone, so none of its outputs lands in the chat (:meth:`register_output`). Work that reaches the
        store later, a call that waited for its turn or an upload whose body was still coming in, finds the chat held
        for :data:`DELETED_CHAT_HOLD_S` and is refused (:meth:`add_job`, :meth:`add_upload`). The hold lives in this
        process, as the queue and the uploads in transfer do, and goes with them on a restart.

        A record that cannot be written deletes nothing. A failure after the record is recorded again as ``error``, by
        the same ids, and raised; a failure of that second record is logged by its type, never raised. What a failed
        removal leaves on disk has no rows, so retention's orphan sweep records and removes it.

        Args:
            p: The caller, user and chat to delete.
            record: Writes the deletion's event (:data:`ChatRecord`), under the store's lock.

        Returns:
            How many files the chat held, its renders not counted.

        Raises:
            PathError: If an id is unsafe, or the chat folder is a link; the rows are gone by then.
            Exception: What ``record`` raised, with nothing deleted; or what the deletion raised after the record.
        """
        owner = (p.caller, p.user_id, p.chat_id)
        with self._lock:
            with self._db() as db:
                rows = db.execute(
                    "SELECT id FROM files WHERE caller = ? AND user_id = ? AND chat_id = ? ORDER BY created_at, id",
                    owner,
                ).fetchall()
                renders = db.execute(
                    "SELECT id FROM derived WHERE caller = ? AND user_id = ? AND chat_id = ? ORDER BY created_at, id",
                    owner,
                ).fetchall()
            files = [row[0] for row in rows]
            ids = [*files, *(row[0] for row in renders)]
            record(ids, "ok")
            now = self._monotonic()
            self._deleted = {chat: deadline for chat, deadline in self._deleted.items() if deadline > now}
            self._deleted[owner] = now + DELETED_CHAT_HOLD_S
            try:
                with self._db() as db:
                    db.execute("DELETE FROM files WHERE caller = ? AND user_id = ? AND chat_id = ?", owner)
                    db.execute("DELETE FROM derived WHERE caller = ? AND user_id = ? AND chat_id = ?", owner)
                    db.execute("DELETE FROM jobs WHERE caller = ? AND user_id = ? AND chat_id = ?", owner)
                self._forget_refusals(files)
                folder = self._ws.resolve(p, f"{VIRTUAL_ROOT}/{p.chat_id}")
                try:
                    shutil.rmtree(folder)
                except FileNotFoundError:
                    pass
            except BaseException:
                try:
                    record(ids, "error")
                except Exception as exc:  # the deletion's error is the answer; this one is only logged
                    _log.warning("could not record a chat delete that failed (%s)", type(exc).__name__)
                raise
        return len(files)

    def remember_refusal(self, source: FileRecord, detail: str, *, at_once: bool = True) -> bool:
        """Remember how the runner refused a source, so that later calls answer it without a job.

        A refusal the file alone decides is remembered at once; one that may pass (the caller says which) only when
        it comes ``REFUSALS_IN_A_ROW`` times in a row for the source, with no success between (:meth:`forget_refusal`).
        It lives in this process, never in the database: a restart forgets it, and so does the source's removal
        (:meth:`remove`, :meth:`remove_upload`, :meth:`delete_chat`); a source of a chat deleted while its job ran is
        not remembered at all. A source removed by retention or taken back while its job runs can be, until it is
        pushed out: its id never comes back. It is
        kept by the source's id with its creation time and size, so no other file inherits it: an upload under the same
        name is a new record with a new id. Past ``REFUSALS_MAX`` the refusal asked for least recently is forgotten,
        and that file is only tried again.

        Args:
            source: The file refused.
            detail: The runner's refusal, its code first, as the caller is told it; it names no file.
            at_once: Whether the file alone decides the refusal; otherwise it is counted.

        Returns:
            Whether the refusal is remembered now.
        """
        deadline = self._deleted.get((source.caller, source.user_id, source.chat_id))
        deleted = deadline is not None and deadline > self._monotonic()
        with self._refusals_lock:
            if deleted:
                self._refusals.pop(source.id, None)
                return False
            held = self._refusals.get(source.id)
            before = held[3] if held is not None and held[:2] == (source.created_at, source.size) else 0
            count = REFUSALS_IN_A_ROW if at_once else min(before + 1, REFUSALS_IN_A_ROW)
            self._refusals[source.id] = (source.created_at, source.size, detail, count)
            self._refusals.move_to_end(source.id)
            while len(self._refusals) > REFUSALS_MAX:
                self._refusals.popitem(last=False)
            return count >= REFUSALS_IN_A_ROW

    def forget_refusal(self, source: FileRecord) -> None:
        """Forget what is counted or remembered of a source's refusals: a job of it just went well.

        Args:
            source: The file.
        """
        self._forget_refusals([source.id])

    def refusal_of(self, source: FileRecord) -> str | None:
        """The refusal remembered of a source (:meth:`remember_refusal`), if this very file was refused.

        Args:
            source: The file.

        Returns:
            The refusal as the caller was told it; ``None`` when none is remembered (one that may pass counted once is
            not), or the one under its id is of a record of another time or size, which is then forgotten.
        """
        with self._refusals_lock:
            held = self._refusals.get(source.id)
            if held is None:
                return None
            created_at, size, detail, count = held
            if (created_at, size) != (source.created_at, source.size):
                del self._refusals[source.id]
                return None
            self._refusals.move_to_end(source.id)
            return detail if count >= REFUSALS_IN_A_ROW else None

    def _forget_refusals(self, ids: Iterable[str]) -> None:
        """Forget the refusals of files that went.

        Args:
            ids: Their file ids; an id with no refusal is fine.
        """
        with self._refusals_lock:
            for file_id in ids:
                self._refusals.pop(file_id, None)

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
        self,
        caller: str,
        user_id: str,
        *,
        untouched_before: float,
        device: int,
        record: OrphanRecord,
        stop: threading.Event | None = None,
    ) -> tuple[int, int]:
        """Remove the files of a user's root that have no record, then the folders left empty, all under the lock.

        An orphan is a regular file in a chat folder that has no row and was last changed before ``untouched_before``:
        the ``<id>.part`` of an upload the api died during, or a file a kill left between its rename into place and its
        row's commit (``add_upload``, ``register_output``, ``register_derived``). A render with its row in ``derived``
        is no orphan; one without is recorded by the id its name carries, ``.derived/<source-id>/<id>.<ext>``. The look
        and the removal hold the store's lock, which every rename into place holds until its row is committed, so a
        file on its way in is never taken for one. Each chat folder's orphans are recorded first, by ``record``, still
        under the lock, at most ``MAX_IDS`` to a record so that no id is cut; those that could not be removed are
        recorded again as an error. If ``record`` raises, nothing more is removed and the error goes on. Once ``stop``
        is set, the sweep ends before its next batch: nothing more is recorded or removed, empty folders included, and
        the next sweep finds the rest.

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
            stop: When set, ends the sweep before its next batch of orphans.

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
                    renders = db.execute(
                        "SELECT chat_id, source_id, name FROM derived WHERE caller = ? AND user_id = ?",
                        (caller, user_id),
                    ).fetchall()
                known = {(chat, name) for chat, name in rows}
                known |= {(chat, f"{DERIVED}/{source}/{name}") for chat, source, name in renders}
                for chat in sorted(candidates):
                    orphans = [path for name, path in candidates[chat] if (chat, name) not in known]
                    for start in range(0, len(orphans), MAX_IDS):
                        if stop is not None and stop.is_set():
                            return removed, failed
                        batch = orphans[start : start + MAX_IDS]
                        record(chat, _orphan_ids(batch), len(batch), "ok")
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
                            record(chat, _orphan_ids(stayed), len(stayed), "error")
                            failed += len(stayed)
            for folder, st in reversed(folders):  # a folder comes after every folder above it
                if st.st_mtime < untouched_before:
                    _remove_empty(folder)
            if root_st is not None and root_st.st_mtime < untouched_before:
                _remove_empty(root)
        return removed, failed

    # --- derived files -------------------------------------------------------

    def _derived_row(self, derived_id: str) -> DerivedRecord | None:
        """Look up one render's record by its id alone.

        Args:
            derived_id: The render's id.

        Returns:
            The record, or ``None`` if there is none.
        """
        with self._db() as db:
            row = db.execute(f"SELECT {_DERIVED_COLUMNS} FROM derived WHERE id = ?", (derived_id,)).fetchone()
        return None if row is None else _derived(row)

    def _current(self, rec: DerivedRecord) -> DerivedRecord | None:
        """Read a render's record as its row is now, if the row is still the record's own.

        What a caller holds may be stale or forged: the row is what counts, and only when it names the same source and
        owner as the record does.

        Args:
            rec: A record the caller holds.

        Returns:
            The row's record, or ``None`` if the row is gone or is not of that source and owner.
        """
        found = self._derived_row(rec.id)
        return found if found is not None and _owner(found) == _owner(rec) else None

    def _has_file(self, file_id: str, caller: str, user_id: str, chat_id: str) -> bool:
        """Tell whether a file's row is there, for its own owner and chat.

        Args:
            file_id: The file's id.
            caller: The owner's caller.
            user_id: The owner's user id.
            chat_id: The chat folder.

        Returns:
            ``True`` if it is.
        """
        with self._db() as db:
            row = db.execute(
                "SELECT 1 FROM files WHERE id = ? AND caller = ? AND user_id = ? AND chat_id = ?",
                (file_id, caller, user_id, chat_id),
            ).fetchone()
        return row is not None

    def _derived_folder(self, p: Principal, source_id: str) -> Path:
        """Resolve the folder of a source's renders, ``<chat>/.derived/<source-id>``; it need not exist.

        Args:
            p: The owner, its chat the source's.
            source_id: The source's id.

        Returns:
            The real path.

        Raises:
            PathError: If a part of it is a link.
        """
        return self._ws.resolve(p, f"{VIRTUAL_ROOT}/{p.chat_id}/{DERIVED}/{source_id}")

    def _derived_path(self, rec: DerivedRecord) -> Path:
        """Resolve a render's file, never through a link.

        Args:
            rec: The render.

        Returns:
            The real path; the file need not exist.

        Raises:
            PathError: If a part of it is a link.
        """
        return self._ws.resolve(
            Principal(rec.caller, rec.user_id, rec.chat_id), f"{VIRTUAL_ROOT}/{rec.chat_id}/{rec.relative_path}"
        )

    def _insert_derived(self, rec: DerivedRecord, drop: Sequence[str] = ()) -> None:
        """Store a new render's record, and in the same transaction delete the rows of those it replaces.

        Args:
            rec: The record.
            drop: The ids of the rows it replaces; the caller holds the store's lock and found them for ``rec``'s own
                source and owner.

        Raises:
            StorageFull: If the database cannot grow.
        """
        meta = json.dumps(rec.meta, sort_keys=True, separators=(",", ":"))
        try:
            with self._db() as db:
                db.executemany("DELETE FROM derived WHERE id = ?", [(each,) for each in drop])
                db.execute(
                    f"INSERT INTO derived ({_DERIVED_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rec.id,
                        rec.source_id,
                        rec.caller,
                        rec.user_id,
                        rec.chat_id,
                        rec.role,
                        rec.ordinal,
                        rec.name,
                        rec.size,
                        rec.mime,
                        rec.width,
                        rec.height,
                        meta,
                        rec.created_at,
                    ),
                )
        except sqlite3.OperationalError as exc:
            if "full" in str(exc):
                raise StorageFull("storage is full") from exc
            raise

    def register_derived(
        self,
        source: FileRecord,
        staged: Path,
        spec: DerivedSpec,
        *,
        file_id: str,
        quota_bytes: int,
        cap_bytes: int = DERIVED_MAX_BYTES,
    ) -> DerivedRecord:
        """Move a render a job produced into the reserved folder of its source, under an id its event already names.

        The render is kept as ``<chat>/.derived/<source-id>/<file-id>.jpg`` (``.png`` for a PNG, ``.pdf`` for the PDF
        an Office file was converted to, which is kept as its renders are): the folders are private, the file is
        private, and it is renamed into place and then its row inserted, as :meth:`_place` does for every file. It is
        as old as its source (``created_at``) and counts in the user's quota. All the checks and the placing hold the
        store's lock, which :meth:`remove`, :meth:`remove_upload` and :meth:`delete_chat` hold: a render is never added
        to a source that has
        gone, or to a chat that was deleted.

        The caller records the render by ``file_id`` before it calls, so that it is never visible without its event,
        and records it again as ``error`` if this raises. It checks the render's signature and size; the store takes
        its size from the file.

        Args:
            source: The file it is a render of; its row must still be there, for its owner and chat.
            staged: A regular file, not a symlink; it is moved, so it is gone afterwards.
            spec: What it is. Each role takes its own types alone: ``image/jpeg`` or ``image/png`` for ``image`` and
                ``page``, ``image/jpeg`` for ``frame``, ``application/pdf`` for ``pdf`` and ``text/plain`` for
                ``transcript``; only a picture has a width and height. Never a clip's raw artifact, which
                :meth:`stage_derived` and :meth:`place_derived` keep.
            file_id: The render's id, chosen and recorded by the caller: 32 lowercase hex digits.
            quota_bytes: Most bytes the user may hold in all chats together, renders included.
            cap_bytes: Most bytes of renders the source may have.

        Returns:
            The new record.

        Raises:
            PathError: If ``spec.mime`` is no type of derived file, its role does not take it, the role is a clip's raw
                artifact, a file that is no picture has a width or height, or ``staged`` is not a regular file.
            ValueError: If ``file_id`` is not 32 lowercase hex digits.
            NotFound: If ``staged`` does not exist; ``staged`` stays.
            Gone: If the source's row is gone, or its chat was deleted within :data:`DELETED_CHAT_HOLD_S`; ``staged``
                stays.
            Conflict: If the source has a render of this role, ordinal, strip and strips, or ``file_id`` is taken;
                ``staged`` stays.
            QuotaExceeded: If it would take the user over ``quota_bytes``; ``staged`` stays.
            RendersFull: If it would take the source over ``cap_bytes``; ``staged`` stays.
            StorageFull: If the volume is full.
        """
        extension = _extension_of(spec)
        if spec.role in _ARTIFACT_ROLES:
            raise PathError("a clip's raw artifacts are streamed in, never registered")
        st = _take_staged(staged)
        if _FILE_ID.fullmatch(file_id) is None:
            raise ValueError("a file id is 32 lowercase hex digits")
        p = Principal(source.caller, source.user_id, source.chat_id)
        with self._lock:  # the source, the room and the folder are settled before retention or a delete may look
            if not self._has_file(source.id, source.caller, source.user_id, source.chat_id):
                raise Gone("file not found")
            self._refuse_deleted(p.caller, p.user_id, p.chat_id)
            rec = _new_derived(source, spec, file_id, st.st_size, extension)
            held = self.derived_of(source)
            if self._derived_row(file_id) is not None or any(
                (each.role, each.ordinal, each.strips, each.strip) == (rec.role, rec.ordinal, rec.strips, rec.strip)
                for each in held
            ):
                raise Conflict("render exists")
            if self.usage(p) + st.st_size > quota_bytes:
                raise QuotaExceeded("workspace quota exceeded")
            if sum(each.size for each in held) + st.st_size > cap_bytes:
                raise RendersFull("the renders of this file are at their limit")
            folder = self._derived_folder(p, source.id)
            self._ws.chat_dir(p)
            self._make_dirs(folder)
            self._place(staged, folder / rec.name, lambda: self._insert_derived(rec))
        return rec

    def _require_source(self, source: FileRecord) -> None:
        """Check that a source is still there, for its own owner and chat, and its chat not held as deleted.

        Call it under the store's lock.

        Args:
            source: The file.

        Raises:
            Gone: If its row is gone, or its chat was deleted within :data:`DELETED_CHAT_HOLD_S`.
        """
        if not self._has_file(source.id, source.caller, source.user_id, source.chat_id):
            raise Gone("file not found")
        self._refuse_deleted(source.caller, source.user_id, source.chat_id)

    def _drop_part(self, part: Path) -> None:
        """Remove a ``.part`` that will not be placed, and the folders of its source's renders if it left them empty.

        Call it under the store's lock, which a stage holds between making its folder and opening its file. A removal
        that fails is logged by its type and never raised: the failure that brought the part here is the answer, and
        retention removes a ``.part`` that stayed, recorded by its id.

        Args:
            part: The ``.part``, ``.derived/<source-id>/<id>.part`` below a chat folder; gone is fine.
        """
        try:
            _unlink(part)
            _remove_empty(part.parent)
            _remove_empty(part.parent.parent)
        except OSError as exc:
            _log.warning("could not remove an artifact that was not kept (%s)", type(exc).__name__)

    def _discard_staged(self, staged: StagedArtifact) -> None:
        """Remove the ``.part`` of a staged artifact that is not kept; it never raises, as it runs as an error goes up.

        Call it under the store's lock. The path is made again from the source and the id, never taken from ``staged``.
        A path that now runs through a link is not followed and is logged by its type: the error that brought the
        artifact here stays the answer, and retention removes the ``.part``.

        Args:
            staged: What :meth:`stage_derived` returned; its ``.part``, its folders, or both gone is fine.
        """
        source = staged.source
        try:
            folder = self._derived_folder(Principal(source.caller, source.user_id, source.chat_id), source.id)
        except PathError as exc:
            _log.warning("could not find an artifact that was not kept (%s)", type(exc).__name__)
            return
        self._drop_part(folder / f"{staged.id}{_PART}")

    def stage_derived(
        self,
        source: FileRecord,
        src: BinaryIO,
        spec: DerivedSpec,
        *,
        max_bytes: int,
        quota_bytes: int,
        check: Callable[[Path], None],
    ) -> StagedArtifact:
        """Stream an artifact of a clip into ``<id>.part`` in the clip's folder of derived files, and check it.

        This is the first of two calls, so that a body is read before the caller waits for the source's lock: a client
        whose body is not read meanwhile would hit its write timeout. Nothing of it is visible and nothing is recorded
        until :meth:`place_derived`. Under the store's lock the source's row is found (for its own owner and chat, its
        chat not held as deleted) and the folders and the ``.part`` are made, private, the file opened new
        (``O_EXCL``, never through a link); outside it the bytes are streamed in and ``check`` reads the whole
        ``.part``. The ``.part`` is not counted in the quota until it is placed, which checks the room again.

        Args:
            source: The clip, as :meth:`get` found it for its owner.
            src: The artifact's bytes; read in chunks until empty.
            spec: What it is: role ``nextext_jsonl`` with type ``application/x-ndjson``, or ``nextext_zip`` with
                ``application/zip``, with no width or height.
            max_bytes: Largest artifact allowed.
            quota_bytes: Most bytes the user may hold in all chats together; what an artifact of this role replaces
                (its row, the transcript, the frames) is counted as free.
            check: Reads the ``.part`` and raises :class:`UnreadableArtifact` for one that is not what its role says;
                it is called outside the store's lock.

        Returns:
            The staged artifact, to pass to :meth:`place_derived` or :meth:`drop_staged`.

        Raises:
            PathError: If ``spec`` is not an artifact's.
            Conflict: If something that is no folder is in the way of the clip's folder.
            Gone: If the clip's row is gone or its chat was deleted, before the stage or while ``check`` read the
                ``.part`` (a ``FileNotFoundError`` of ``check`` is taken for the second).
            TooLarge: If the body is over ``max_bytes``.
            QuotaExceeded: If it is over the room.
            StorageFull: If the volume is full.
            UnreadableArtifact: What ``check`` raises for an artifact that is not what its role says.
            Exception: Any other error of ``check`` or of the disk, unchanged. Whatever the error, the ``.part`` is
                removed, and the folders of the clip's derived files if they were left empty.
        """
        _extension_of(spec)
        if spec.role not in _ARTIFACT_ROLES:
            raise PathError("only a clip's raw artifacts are streamed in")
        file_id = new_file_id()
        p = Principal(source.caller, source.user_id, source.chat_id)
        with self._lock:  # the source, the room and the folders are settled before retention or a delete may look
            self._require_source(source)
            folder = self._derived_folder(p, source.id)
            part = folder / f"{file_id}{_PART}"
            try:
                freed = sum(each.size for each in _replaced_by(spec.role, self.derived_of(source)))
                room = quota_bytes - self.usage(p) + freed
                self._ws.chat_dir(p)
                self._make_dirs(folder)
                fd = _open_part(part)
            except BaseException:
                self._drop_part(part)  # the folders just made, if they are empty
                raise
        try:
            size = self._write_part(fd, part, src, max_bytes=max_bytes, room=room)
            try:
                check(part)
            except FileNotFoundError as exc:  # the chat was deleted, and its folder with it, while the check read
                raise Gone("file not found") from exc
        except BaseException:
            with self._lock:
                self._drop_part(part)
            raise
        return StagedArtifact(source=source, spec=spec, id=file_id, part=part, size=size)

    def place_derived(
        self,
        staged: StagedArtifact,
        *,
        quota_bytes: int,
        record: ArtifactRecord,
        cap_bytes: int = DERIVED_MAX_BYTES,
    ) -> tuple[DerivedRecord, list[DerivedRecord]]:
        """Record a staged artifact, make it visible, and replace what it makes out of date, all under the store's lock.

        The second of two calls. The source and its chat are checked again, as they may have gone while the body came
        in (taken back, deleted). The artifact replaces the source's row of its role and every ``transcript`` and
        ``frame`` row, which were made from the old input; the room is what the user and the source hold, less those
        rows, plus the new size. ``record`` writes the event by the new id and the replaced ids, and only then is the
        ``.part`` renamed to ``<id>.<ext>`` and one transaction inserts the new row and deletes the replaced ones; their
        files are deleted after the commit. A record that cannot be written keeps nothing; a rename or commit that
        fails after it takes the file back out and records ``error``, and the replaced rows and files stay. A replaced
        file that cannot be deleted after the commit is logged by its type: it has no row, and retention removes and
        records it. Whatever fails, the ``.part`` or the file is removed.

        Args:
            staged: What :meth:`stage_derived` returned.
            quota_bytes: Most bytes the user may hold in all chats together, renders included.
            record: Writes the event (:data:`ArtifactRecord`), under the store's lock.
            cap_bytes: Most bytes of derived files the source may have.

        Returns:
            The new record, and the records it replaced, by role and ordinal.

        Raises:
            Gone: If the source's row is gone, or its chat was deleted within :data:`DELETED_CHAT_HOLD_S`.
            NotFound: If the ``.part`` is gone, as after it was placed.
            PathError: If ``staged``'s spec is not an artifact's, the ``.part`` is not a regular file, or a path now
                runs through a symlink.
            QuotaExceeded: If it would take the user over ``quota_bytes``.
            RendersFull: If it would take the source over ``cap_bytes``.
            StorageFull: If the volume is full.
            Exception: What ``record`` raised, with nothing kept; or what the rename or commit raised after it.
        """
        source, spec = staged.source, staged.spec
        p = Principal(source.caller, source.user_id, source.chat_id)
        with self._lock:
            try:  # nothing is done before it: whatever fails, the .part is removed
                extension = _extension_of(spec)
                part = self._derived_folder(p, source.id) / f"{staged.id}{_PART}"
                self._require_source(source)
                st = _take_staged(part)
                held = self.derived_of(source)
                replaced = _replaced_by(spec.role, held)
                freed = sum(each.size for each in replaced)
                if self.usage(p) - freed + st.st_size > quota_bytes:
                    raise QuotaExceeded("workspace quota exceeded")
                if sum(each.size for each in held) - freed + st.st_size > cap_bytes:
                    raise RendersFull("the renders of this file are at their limit")
                rec = _new_derived(source, spec, staged.id, st.st_size, extension)
                ids = [each.id for each in replaced]
                doomed = [self._derived_path(each) for each in replaced]
                record(rec.id, rec.size, list(ids), "ok")
                self._place(
                    part,
                    part.with_name(rec.name),
                    lambda: self._insert_derived(rec, ids),
                    again=lambda: record(rec.id, rec.size, list(ids), "error"),
                )
            except BaseException:
                self._discard_staged(staged)
                raise
            for path in doomed:
                try:
                    _unlink(path)
                except OSError as exc:  # the row is gone, so nothing serves it; retention removes the file
                    _log.warning("could not remove a replaced artifact file (%s)", type(exc).__name__)
        return rec, replaced

    def drop_staged(self, staged: StagedArtifact) -> None:
        """Remove the ``.part`` of an artifact that is not placed, and the clip's empty folders of derived files.

        It never raises for a ``.part`` or a folder that is gone, or for a path that now runs through a link (logged by
        its type), so a caller may use it where an error is going up.

        Args:
            staged: What :meth:`stage_derived` returned; gone, or already placed, is fine.
        """
        with self._lock:
            self._discard_staged(staged)

    def derived_for(self, sources: Sequence[FileRecord]) -> list[DerivedRecord]:
        """List every render of some files, each by its own owner and chat.

        Args:
            sources: The files.

        Returns:
            Their renders: source by source, in the order given, each source's by role, ordinal, strips and strip. A
            record that names another owner or chat than its row has finds none.
        """
        keys = {(source.id, source.caller, source.user_id, source.chat_id) for source in sources}
        position: dict[str, int] = {}
        for source in sources:
            position.setdefault(source.id, len(position))
        ids = list(position)
        found: list[DerivedRecord] = []
        with self._db() as db:
            for start in range(0, len(ids), _IN_CHUNK):
                chunk = ids[start : start + _IN_CHUNK]
                rows = db.execute(
                    f"SELECT {_DERIVED_COLUMNS} FROM derived WHERE source_id IN ({','.join('?' * len(chunk))})", chunk
                ).fetchall()
                found += [rec for rec in map(_derived, rows) if _owner(rec) in keys]
        return sorted(found, key=lambda rec: (position[rec.source_id], *_order(rec)))

    def derived_of(self, source: FileRecord) -> list[DerivedRecord]:
        """List the renders of one file, for its own owner and chat.

        Args:
            source: The file.

        Returns:
            Its renders, by role, ordinal, strips and strip.
        """
        return self.derived_for([source])

    def open_derived(self, rec: DerivedRecord) -> BinaryIO:
        """Open a render for reading, as :meth:`open_file` opens a file: not through a link, and only a regular file.

        The row is read as it is now, and must be the record's own, with its source still there: a render whose source
        went, a stray, is served to no one.

        Args:
            rec: A record of a render, for example one :meth:`derived_of` returned.

        Returns:
            The file, open in binary mode; the caller closes it.

        Raises:
            PathError: If the render's path runs through a symlink.
            NotFound: If the row is gone, is not the record's, has no source, or the file is gone from the disk.
        """
        current = self._current(rec)
        if current is None or not self._has_file(current.source_id, current.caller, current.user_id, current.chat_id):
            raise NotFound("file not found")
        real = self._derived_path(current)
        try:
            fd = os.open(real, os.O_RDONLY | os.O_NOFOLLOW)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise NotFound("file not found") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise NotFound("file not found")
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise

    def remove_derived(self, rec: DerivedRecord) -> None:
        """Delete one render's file and row; already gone is fine.

        It records nothing: call it only once the deletion is recorded. The folders of the renders go once empty.

        Args:
            rec: A record of a render, for example one :meth:`due_derived` returned; one whose row is not its own is
                left alone.

        Raises:
            PathError: If the render's path now runs through a symlink; nothing is deleted.
            OSError: If the file cannot be deleted; its record stays so a later sweep retries.
        """
        with self._lock:
            current = self._current(rec)
            if current is None:
                return
            real = self._derived_path(current)
            _unlink(real)
            with self._db() as db:
                db.execute("DELETE FROM derived WHERE id = ?", (current.id,))
            _remove_empty(real.parent)
            _remove_empty(real.parent.parent)

    def due_derived(
        self, cutoff: float, limit: int = 200, *, after: tuple[float, str] | None = None
    ) -> list[DerivedRecord]:
        """List renders for the retention sweep: those made before a time, and those whose source is gone.

        Args:
            cutoff: Renders with ``created_at`` strictly below this are due, as their sources are.
            limit: Most records to return.
            after: Only renders past this ``(created_at, id)``, the last of the page before.

        Returns:
            The oldest due renders, of every user, in ``(created_at, id)`` order.
        """
        where = "(created_at < ? OR NOT EXISTS (SELECT 1 FROM files WHERE files.id = derived.source_id))"
        params: list[object] = [cutoff]
        if after is not None:
            where += " AND (created_at, id) > (?, ?)"
            params += [*after]
        with self._db() as db:
            rows = db.execute(
                f"SELECT {_DERIVED_COLUMNS} FROM derived WHERE {where} ORDER BY created_at, id LIMIT ?",
                (*params, limit),
            ).fetchall()
        return [_derived(row) for row in rows]

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
            NotFound: If the chat was deleted within :data:`DELETED_CHAT_HOLD_S`: a call that waited for its turn
                while its chat went never runs. The check and the row hold the lock the chat delete holds.
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
        with self._lock:
            self._refuse_deleted(job.caller, job.user_id, job.chat_id)
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
        By kind, ``upload``, ``output`` or ``derived``; a kind of which there is no file is missing. A render is due
        when its source is, and also when its source is gone.

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
        renders = _summarize_derived(conn, cutoff)
    finally:
        conn.close()
    summary = {kind: DueSummary(int(due), oldest, newest, int(kept)) for kind, due, oldest, newest, kept in rows}
    if renders is not None:
        summary["derived"] = renders
    return summary


def _summarize_derived(conn: sqlite3.Connection, cutoff: float) -> DueSummary | None:
    """Count the due and the kept renders.

    Args:
        conn: A connection to the store's database.
        cutoff: Renders with ``created_at`` strictly below this are due.

    Returns:
        The counts, or ``None`` when there is no render, or the database has no table of them yet (it was made before
        renders, and the api adds the table when it next opens it).
    """
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'derived'").fetchone() is None:
        return None
    due, oldest, newest, kept, total = conn.execute(
        "SELECT COALESCE(SUM(due), 0), MIN(CASE WHEN due THEN created_at END), MAX(CASE WHEN due THEN created_at END), "
        "COALESCE(SUM(NOT due), 0), COUNT(*) FROM ("
        "SELECT created_at, (created_at < :cutoff "
        "OR NOT EXISTS (SELECT 1 FROM files WHERE files.id = derived.source_id)) AS due FROM derived)",
        {"cutoff": cutoff},
    ).fetchone()
    return DueSummary(int(due), oldest, newest, int(kept)) if total else None
