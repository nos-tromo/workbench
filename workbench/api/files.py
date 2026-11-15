"""The file browser Open WebUI shows beside the chat, and the ``display_file`` tool.

Open WebUI's browser calls these routes through its proxy: list, upload, view, read,
delete, mkdir and move, all hidden from the tool document. Only ``GET /files/display`` is in
it, as the ``display_file`` tool.

Every read or change of content is one audit event, by file id and never by name, and it is
written *before* the content leaves, the file becomes visible or the file goes: an action that
cannot be recorded does not happen. An upload is recorded by the id it was given before its first
byte was written, before the rename makes it a file of the chat. An upload that could not be
stored after its record, and a delete that failed after its record, are recorded again as
``error``. Listing, ``mkdir`` and ``move`` touch no content and are not recorded (spec section 7).

An error answers short fixed text from :mod:`workbench.api.errors`; no answer here carries a
name, a path or a user id the caller did not just send.
"""

import dataclasses
import logging
import os
import posixpath
from collections.abc import Iterator
from typing import Annotated, BinaryIO, Final
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from workbench.api.audit import AuditLog
from workbench.api.auth import require_tool
from workbench.api.deps import AuditDep, PrincipalDep, SettingsDep, StoreDep
from workbench.api.errors import NOT_FOUND
from workbench.api.openapi import TOOL_TAG
from workbench.api.routing import EncodableRoute
from workbench.api.store import NotFound, Store
from workbench.config import Settings
from workbench.models import VIRTUAL_ROOT, FileRecord, Principal

_log = logging.getLogger(__name__)

#: The browser's routes; none of them is a tool.
router = APIRouter(include_in_schema=False, route_class=EncodableRoute)

#: Routes that are tools, and so the only ones in the tool document.
tool_router = APIRouter(tags=[TOOL_TAG], route_class=EncodableRoute)

_MIB: Final = 1024 * 1024
_CHUNK: Final = _MIB
_TEXT_SUFFIXES: Final = (".md", ".csv")

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


class MkdirRequest(BaseModel):
    """Body of ``POST /files/mkdir``.

    Attributes:
        path: The folder to make, as a virtual path.
    """

    path: str


class MoveRequest(BaseModel):
    """Body of ``POST /files/move``.

    Attributes:
        source: The file's current path.
        destination: The path it should have.
    """

    source: str
    destination: str


class DisplayedFile(BaseModel):
    """What ``display_file`` answers.

    Attributes:
        path: The file's path.
        exists: Whether the user has such a file.
        name: The file's own name, without its folder; ``None`` if there is no such file.
        mime_type: The media type the file is served as; ``None`` if there is no such file.
    """

    path: str = Field(description="The file's absolute path.")
    exists: bool = Field(description="False if the user has no such file; then there is nothing to show.")
    name: str | None = Field(description="The file's own name, without its folder.")
    mime_type: str | None = Field(description="The media type the file is shown as; active content is plain text.")


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


def _in_folder_of(p: Principal, rec: FileRecord) -> Principal:
    """Return the principal acting in the chat folder a file lives in.

    The browser sets ``X-Session-Id`` freely, so the chat of the request says nothing about the
    file; the audit event names the folder the file is in.

    Args:
        p: The caller, user and the chat of the request.
        rec: The record of the file the action touches.

    Returns:
        The same caller and user, in the file's chat folder.
    """
    return dataclasses.replace(p, chat_id=rec.chat_id)


def _is_text(rec: FileRecord) -> bool:
    """Tell whether a file may be read as text: ``text/*``, Markdown and CSV.

    Args:
        rec: The file's record.

    Returns:
        ``True`` if it may.
    """
    return rec.mime.startswith("text/") or rec.name.lower().endswith(_TEXT_SUFFIXES)


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


def _media_type(rec: FileRecord) -> str:
    """Return the media type a file is shown as: its own, or plain text if it is active content.

    Args:
        rec: The file's record.

    Returns:
        The media type, without parameters.
    """
    return _PLAIN if _is_active(rec) else rec.mime


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

    The event's container is the chat folder the file goes to, never the chat of the request.

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
def list_directory(directory: str, p: PrincipalDep, store: StoreDep) -> dict[str, object]:
    """List a folder of the user's.

    Args:
        directory: The folder: ``/workspace``, a chat folder or a folder inside one.
        p: The caller, user and chat.
        store: The file store.

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
    directory: str, file: UploadFile, p: PrincipalDep, settings: SettingsDep, store: StoreDep, audit: AuditDep
) -> dict[str, object]:
    """Store an uploaded file in a chat folder, or a folder inside one.

    Args:
        directory: The folder to put the file in.
        file: The uploaded file, the multipart field ``file``.
        p: The caller, user and chat.
        settings: The api's settings.
        store: The file store.
        audit: The audit log.

    Returns:
        The file's path, which has a number in it if the name was taken, and its size.
    """
    rec = await run_in_threadpool(_save_upload, store, audit, settings, p, directory, file)
    return {"path": rec.virtual_path, "size": rec.size}


@router.get("/files/view")
def view_file(path: str, p: PrincipalDep, store: StoreDep, audit: AuditDep) -> StreamingResponse:
    """Send a file's bytes with its own media type.

    Open WebUI may add ``preview=true`` to ask for a rendering of an Office file; it is ignored,
    so the browser gets the file itself and renders it.

    A file that can run script (HTML, SVG, XML, JavaScript) is sent as plain text and an
    attachment instead, so opening it can never run it at Open WebUI's origin. Every answer of
    this route, refusals included, carries the sandboxing headers of :mod:`workbench.api.hardening`.

    Args:
        path: The file's virtual path.
        p: The caller, user and chat.
        store: The file store.
        audit: The audit log.

    Returns:
        The file, streamed.
    """
    rec, src = store.open_file(p, path)
    try:
        audit.record("view", _in_folder_of(p, rec), file_ids=[rec.id])
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


@router.get("/files/read")
def read_file(path: str, p: PrincipalDep, settings: SettingsDep, store: StoreDep, audit: AuditDep) -> dict[str, str]:
    """Read a text file whole. Open WebUI also probes ``<home>/AGENTS.md`` here for every chat.

    Files live only inside chat folders, so a file directly under home is absent by definition.

    Args:
        path: The file's virtual path.
        p: The caller, user and chat.
        settings: The api's settings; ``result_max_kb`` is the largest file read.
        store: The file store.
        audit: The audit log.

    Returns:
        The file's text; bytes that are not UTF-8 are replaced.

    Raises:
        HTTPException: 404 if there is no such file, 415 if it is not text, 413 if it is
            larger than the result limit; none of them is recorded.
    """
    if posixpath.dirname(posixpath.normpath(path)) == VIRTUAL_ROOT:
        raise HTTPException(status_code=404, detail=NOT_FOUND)
    rec, src = store.open_file(p, path)
    with src:
        if not _is_text(rec):
            raise HTTPException(status_code=415, detail="not readable as text")
        limit = settings.result_max_kb * 1024
        data = src.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(status_code=413, detail="file too large to read here")
    audit.record("read", _in_folder_of(p, rec), file_ids=[rec.id])
    return {"content": data.decode("utf-8", errors="replace")}


@router.delete("/files/delete")
def delete_file(path: str, p: PrincipalDep, store: StoreDep, audit: AuditDep) -> dict[str, str]:
    """Delete a file, after recording it; a delete that then fails is recorded again as ``error``.

    Args:
        path: The file's virtual path.
        p: The caller, user and chat.
        store: The file store.
        audit: The audit log.

    Returns:
        The path that was deleted.

    Raises:
        Exception: What the removal raised; the file stays, and the error event names it. If that event cannot be
            written either, the removal's error is still what is raised, and the failed record is logged by its type.
    """
    rec = store.get(p, path)
    where = _in_folder_of(p, rec)
    audit.record("delete", where, file_ids=[rec.id])
    try:
        store.remove(rec)  # by id, so the file recorded is the file removed
    except Exception:
        try:
            audit.record("delete", where, file_ids=[rec.id], outcome="error")
        except Exception as exc:  # the removal's error is the answer
            _log.warning("could not record a delete that failed (%s)", type(exc).__name__)
        raise
    return {"path": rec.virtual_path}


@router.post("/files/mkdir")
def make_directory(body: MkdirRequest, p: PrincipalDep, store: StoreDep) -> dict[str, str]:
    """Make a folder, and its parents; one that exists is left as it is.

    Args:
        body: The folder.
        p: The caller, user and chat.
        store: The file store.

    Returns:
        The folder's path in its plain form.
    """
    return {"path": store.mkdir(p, body.path)}


@router.post("/files/move")
def move_file(body: MoveRequest, p: PrincipalDep, store: StoreDep) -> dict[str, str]:
    """Rename a file; nothing is overwritten.

    Args:
        body: The file's path and the path it should have.
        p: The caller, user and chat.
        store: The file store.

    Returns:
        The file's new path.
    """
    return {"path": store.move(p, body.source, body.destination).virtual_path}


@tool_router.get(
    "/files/display",
    operation_id="display_file",
    dependencies=[Depends(require_tool("display_file"))],
    summary="Show a file to the user",
    description=(
        "Show one of the user's files in the chat as a card with a preview and a download. "
        "Use it for a file you made or the user uploaded, once it exists, to hand it over. "
        "It answers exists=false if there is no such file."
    ),
)
def display_file(
    path: Annotated[str, Query(description="The file's absolute path, for example /workspace/<chat>/report.xlsx.")],
    p: PrincipalDep,
    store: StoreDep,
    audit: AuditDep,
) -> DisplayedFile:
    """Say whether a file exists and what it is called, so Open WebUI can show it.

    Args:
        path: The file's virtual path.
        p: The caller, user and chat.
        store: The file store.
        audit: The audit log.

    Returns:
        The file's path, whether it exists, and its name and media type.
    """
    try:
        rec = store.get(p, path)
    except NotFound:
        return DisplayedFile(path=path, exists=False, name=None, mime_type=None)
    audit.record("display", _in_folder_of(p, rec), file_ids=[rec.id])
    return DisplayedFile(
        path=rec.virtual_path, exists=True, name=posixpath.basename(rec.name), mime_type=_media_type(rec)
    )
