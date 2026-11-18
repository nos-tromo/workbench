"""Ghostwriter's file routes: list, upload and view, held to the request's chat, and the chat delete.

Any path outside the request's chat is ``404``. The calls state a purpose and a request id,
which their events carry, and an upload or a view that must state a purpose and does not is
``428``, recorded as denied. ``DELETE /files/chat`` deletes the request's chat.

Every read or change of content is one audit event, by file id and never by name, and it is
written *before* the content leaves, the file becomes visible or the chat goes: an action that
cannot be recorded does not happen. An upload is recorded by the id it was given before its first
byte was written, before the rename makes it a file of the chat. An upload that could not be
stored after its record is recorded again as ``error``. Listing touches no content and is not
recorded (spec section 7).

An error answers short fixed text from :mod:`workbench.api.errors`; no answer here carries a
name, a path or a user id the caller did not just send.
"""

import dataclasses
import os
import posixpath
from collections.abc import Iterator, Sequence
from typing import BinaryIO, Final
from urllib.parse import quote

from fastapi import APIRouter, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from workbench.api.audit import AuditLog
from workbench.api.auth import CHAT_PATH
from workbench.api.deps import CallAuditDep, FilePrincipalDep, SettingsDep, StoreDep
from workbench.api.routing import EncodableRoute
from workbench.api.store import Store
from workbench.config import Settings
from workbench.models import FileRecord, Principal

#: The file routes; none of them is a tool.
router = APIRouter(include_in_schema=False, route_class=EncodableRoute)

_MIB: Final = 1024 * 1024
_CHUNK: Final = _MIB

#: Types that can run script when a browser opens them. They are never served as themselves.
_ACTIVE_TYPES: Final = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
        "text/xml",
        "application/xml",
        "text/javascript",
        "application/javascript",
    }
)
_ACTIVE_SUFFIXES: Final = (".html", ".htm", ".xhtml", ".svg", ".xml", ".js")
_PLAIN: Final = "text/plain"

#: A whole message or web archive: served as itself, but only ever as an attachment, never shown inline.
_ARCHIVE_TYPES: Final = frozenset({"message/rfc822", "multipart/related"})
_ARCHIVE_SUFFIXES: Final = (".eml", ".mht", ".mhtml", ".nws")


def _chunks(src: BinaryIO) -> Iterator[bytes]:
    """Read a file in pieces, then close it.

    Args:
        src: The open file.

    Yields:
        The file's bytes, one read at a time.
    """
    try:
        while chunk := src.read(_CHUNK):
            yield chunk
    finally:
        src.close()


def _is_active(rec: FileRecord) -> bool:
    """Tell whether a file can run script when a browser opens it: HTML, SVG, XML and JavaScript.

    The stored type and the name each decide, so neither a wrong name nor a wrong type lets
    such a file through.

    Args:
        rec: The file's record.

    Returns:
        ``True`` if it must not be served as itself.
    """
    media_type = rec.mime.partition(";")[0].strip().lower()
    return media_type in _ACTIVE_TYPES or rec.name.lower().endswith(_ACTIVE_SUFFIXES)


def _is_archive(rec: FileRecord) -> bool:
    """Tell whether a file is a whole message or web archive (``.eml``, ``.mht``): by its type or by its name.

    Args:
        rec: The file's record.

    Returns:
        ``True`` if it must only ever be downloaded.
    """
    media_type = rec.mime.partition(";")[0].strip().lower()
    return media_type in _ARCHIVE_TYPES or rec.name.lower().endswith(_ARCHIVE_SUFFIXES)


def _disposition(rec: FileRecord) -> str:
    """Build the ``Content-Disposition`` of a file, with its own name in RFC 5987 form.

    Args:
        rec: The file's record.

    Returns:
        The header value: ``inline``, or ``attachment`` for active content and for a message or
        web archive, so a browser saves it instead of rendering it. The name is percent-encoded
        UTF-8, so no character of it can end the header or start another.
    """
    how = "attachment" if _is_active(rec) or _is_archive(rec) else "inline"
    return f"{how}; filename*=UTF-8''{quote(posixpath.basename(rec.name), safe='')}"


def _save_upload(
    store: Store, audit: AuditLog, settings: Settings, p: Principal, directory: str, upload: UploadFile
) -> FileRecord:
    """Store an upload, recording it by its id before it becomes a file of the chat.

    The event's container is the chat folder the store puts the file in: the request's own.

    Args:
        store: The file store.
        audit: The audit log.
        settings: The api's settings; they hold the limits.
        p: The caller, user and chat.
        directory: The folder to put the file in.
        upload: The uploaded part.

    Returns:
        The new record.

    Raises:
        AuditWriteError: If the event cannot be written; nothing is stored.
    """

    def record(chat_id: str, file_id: str, outcome: str) -> None:
        audit.record("upload", dataclasses.replace(p, chat_id=chat_id), file_ids=[file_id], outcome=outcome)

    return store.add_upload(
        p,
        directory,
        upload.filename or "",
        upload.file,
        max_bytes=settings.upload_max_mb * _MIB,
        quota_bytes=settings.workspace_max_mb * _MIB,
        record=record,
    )


@router.get("/files/list")
def list_directory(directory: str, p: FilePrincipalDep, store: StoreDep, _audit: CallAuditDep) -> dict[str, object]:
    """List the request's chat folder, or a folder in it.

    Args:
        directory: The folder: the chat folder or a folder inside it.
        p: The caller, user and chat.
        store: The file store.
        _audit: Not recorded through; it refuses a malformed purpose, as the other file routes do.

    Returns:
        The folder in its plain form, whether it may be written, and its entries.
    """
    entries = store.list_dir(p, directory)
    return {
        "dir": posixpath.normpath(directory),
        "writable": True,
        "entries": [dataclasses.asdict(entry) for entry in entries],
    }


@router.post("/files/upload")
async def upload_file(
    directory: str, file: UploadFile, p: FilePrincipalDep, settings: SettingsDep, store: StoreDep, audit: CallAuditDep
) -> dict[str, object]:
    """Store an uploaded file in the request's chat folder itself.

    Args:
        directory: The folder to put the file in: the chat folder.
        file: The uploaded file, the multipart field ``file``.
        p: The caller, user and chat.
        settings: The api's settings.
        store: The file store.
        audit: The audit log, bound to the call.

    Returns:
        The file's path, which has a number in it if the name was taken, its size and its id.
    """
    await run_in_threadpool(audit.check, "upload", p)
    rec = await run_in_threadpool(_save_upload, store, audit, settings, p, directory, file)
    return {"path": rec.virtual_path, "size": rec.size, "id": rec.id}


@router.get("/files/view")
def view_file(path: str, p: FilePrincipalDep, store: StoreDep, audit: CallAuditDep) -> StreamingResponse:
    """Send a file's bytes with its own media type.

    A query parameter the route does not name, such as ``preview=true``, is ignored, so a browser
    gets the file itself.

    A file that can run script (HTML, SVG, XML, JavaScript) is sent as plain text and an
    attachment instead, so opening it can never run it at the origin of the page that shows it.
    Every answer of this route, refusals included, carries the sandboxing headers of
    :mod:`workbench.api.hardening`.

    Args:
        path: The file's virtual path.
        p: The caller, user and chat.
        store: The file store.
        audit: The audit log, bound to the call.

    Returns:
        The file, streamed.
    """
    audit.check("view", p)
    rec, src = store.open_file(p, path)
    try:
        audit.record("view", p, file_ids=[rec.id])
        size = os.fstat(src.fileno()).st_size
    except BaseException:
        src.close()
        raise
    headers = {
        "Content-Type": f"{_PLAIN}; charset=utf-8" if _is_active(rec) else rec.mime,
        "Content-Length": str(size),
        "Content-Disposition": _disposition(rec),
    }
    return StreamingResponse(_chunks(src), headers=headers)


@router.delete(CHAT_PATH)
def delete_chat(p: FilePrincipalDep, store: StoreDep, audit: CallAuditDep) -> dict[str, int]:
    """Delete the request's chat, after recording it by the ids of its files: its files, rows, job rows and folder.

    A job of the chat still running keeps no output. A chat that holds nothing is deleted and recorded too.

    Args:
        p: The caller, the user and the chat to delete.
        store: The file store.
        audit: The audit log, bound to the call.

    Returns:
        How many files the chat held.
    """
    audit.check("chat_delete", p)

    def record(ids: Sequence[str], outcome: str) -> None:
        audit.record("chat_delete", p, file_ids=ids, outcome=outcome)

    return {"deleted": store.delete_chat(p, record)}
