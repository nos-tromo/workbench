"""Ghostwriter's file routes: list, upload, prepare and view, held to the request's chat, and the chat delete.

Any path outside the request's chat is ``404``. The calls state a purpose and a request id,
which their events carry, and an upload, a prepare or a view that must state a purpose and does not is
``428``, recorded as denied. ``DELETE /files/chat`` deletes the request's chat.

``POST /files/prepare`` renders a picture or a PDF once and answers its manifest (:mod:`workbench.api.renders`). The
view of a file answers the file itself; with ``rendition`` it answers one render of it instead, a page or a picture, or
one strip of it, made first when it does not exist yet, on the user's own turn.

Every read or change of content is one audit event, by file id and never by name, and it is
written *before* the content leaves, the file becomes visible or the chat goes: an action that
cannot be recorded does not happen. An upload is recorded by the id it was given before its first
byte was written, before the rename makes it a file of the chat. An upload that could not be
stored after its record is recorded again as ``error``. A prepare is recorded by the renders' own job, before any
render is visible; a view of a render names the source and the render. Listing touches no content and is not
recorded (spec section 7).

An error answers short fixed text from :mod:`workbench.api.errors`; no answer here carries a
name, a path or a user id the caller did not just send.
"""

import dataclasses
import os
import posixpath
from collections.abc import Iterator, Sequence
from typing import Annotated, BinaryIO, Final
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from workbench.api.audit import AuditLog
from workbench.api.auth import CHAT_PATH, PREPARE_PATH, Caller
from workbench.api.deps import CallAuditDep, CallerDep, FilePrincipalDep, JobsDep, MaybeJobsDep, SettingsDep, StoreDep
from workbench.api.errors import INTERNAL
from workbench.api.jobs import MCP_QUEUE_WAIT_S, PREPARE_QUEUE_WAIT_S, Busy, CallLimits, Gate, JobQueue
from workbench.api.renders import RouteJobs, prepared, read_render, renders_for
from workbench.api.routing import EncodableRoute
from workbench.api.store import NotFound, Store
from workbench.api.tools_mcp import PREVIEW_RESERVE_S
from workbench.config import Settings
from workbench.models import DerivedRecord, FileRecord, Principal

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


def _view_whole(path: str, p: Principal, store: Store, audit: AuditLog) -> StreamingResponse:
    """Send a file's bytes with its own media type, after recording the view.

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


def _wall_of(settings: Settings, caller: Caller) -> tuple[int, Settings]:
    """Work out how long a call's job may run, as the MCP door does, and the settings its runner budget comes from.

    Args:
        settings: The api's settings.
        caller: The caller.

    Returns:
        The call's wall: the caller's own, at most the job wall; and the settings with the job wall short of it by
        :data:`~workbench.api.tools_mcp.PREVIEW_RESERVE_S`, so that the runner's budget (3/4 of it) ends inside the
        call's wall and the job can say that it timed out.
    """
    wall = min(caller.wall_s or settings.job_wall_s, settings.job_wall_s)
    return wall, dataclasses.replace(settings, job_wall_s=wall - PREVIEW_RESERVE_S)


def _render_jobs(queue: JobQueue, wall: int, wait_s: int, caller: Caller, gate: Gate) -> RouteJobs:
    """The queue as one of these routes sees it.

    Args:
        queue: The app's job queue.
        wall: The call's wall.
        wait_s: How long the call waits for its turn at the most.
        caller: The caller, whose slots bound the jobs of its running at once.
        gate: ``prepare`` for the prepare route, ``user`` for a view, on the user's own turn.

    Returns:
        The queue within the call's limits and on its gate.
    """
    return RouteJobs(queue, CallLimits(wall, wait_s, caller.slots), gate)


@router.post(PREPARE_PATH)
async def prepare_file(
    path: str,
    p: FilePrincipalDep,
    caller: CallerDep,
    settings: SettingsDep,
    store: StoreDep,
    audit: CallAuditDep,
    jobs: JobsDep,
) -> dict[str, object]:
    """Render a picture or a PDF once, and answer its manifest.

    The first prepare of a file draws its first pages in one job, recorded before any render is visible; every later
    one answers from the renders kept, with no job and no event. A file that is neither a picture nor a PDF is
    answered at once as ``kind: none``. The request has no body. It waits at most
    :data:`~workbench.api.jobs.PREPARE_QUEUE_WAIT_S` for its turn, on the caller's prepare gate, and so never takes the
    user's own turn from a chat in progress.

    Args:
        path: The file's virtual path, in the request's chat.
        p: The caller, user and chat.
        caller: The caller, whose wall and slots bound the job.
        settings: The api's settings.
        store: The file store.
        audit: The audit log, bound to the call.
        jobs: The app's job queue.

    Returns:
        The manifest, all its keys, the ones that hold nothing as ``null``.
    """
    await run_in_threadpool(audit.check, "prepare", p)
    source = await run_in_threadpool(store.get, p, path)
    wall, budgeted = _wall_of(settings, caller)
    manifest = await prepared(
        p,
        source,
        settings=budgeted,
        store=store,
        audit=audit,
        jobs=_render_jobs(jobs, wall, PREPARE_QUEUE_WAIT_S, caller, "prepare"),
    )
    return manifest.model_dump(mode="json")


def _rendition_disposition(source: FileRecord, render: DerivedRecord) -> str:
    """Build the ``Content-Disposition`` of a render: the source's name, the page and the strip, in RFC 5987 form.

    Args:
        source: The file the render is of.
        render: The render.

    Returns:
        ``inline`` and the name ``<stem>-page-3-strip-1-of-2.jpg``: a picture shown whole has its stem alone, a page
        its number, a strip its place among the strips of its page. Never the render's own name, which is an id.
    """
    stem = posixpath.splitext(posixpath.basename(source.name))[0]
    parts = [stem]
    if render.role == "page":
        parts.append(f"page-{render.ordinal}")
    if render.strips > 1:
        parts.append(f"strip-{render.strip}-of-{render.strips}")
    name = "-".join(parts) + posixpath.splitext(render.name)[1]
    return f"inline; filename*=UTF-8''{quote(name, safe='')}"


async def _view_rendition(
    path: str,
    rendition: int,
    strips: int,
    strip: int,
    p: Principal,
    caller: Caller,
    settings: Settings,
    store: Store,
    audit: AuditLog,
    queue: JobQueue,
) -> Response:
    """Send one render of a file, making it first when it does not exist yet.

    Args:
        path: The file's virtual path.
        rendition: The page, counted from 1; 1 for a picture.
        strips: Into how many strips the page is cut.
        strip: Which strip to send, counted from 1.
        p: The caller, user and chat.
        caller: The caller, whose wall and slots bound the job.
        settings: The api's settings.
        store: The file store.
        audit: The audit log, bound to the call.
        queue: The app's job queue.

    Returns:
        The render, whole, with its own type and the name of its source.

    Raises:
        NotFound: The file is not shown as pictures, or has no such page.
        HTTPException: 500 ``internal error``: the job made no such render without saying why, or what it made is not
            what its row says. Any other refusal is what :func:`~workbench.api.renders.prepared` and
            :func:`~workbench.api.renders.renders_for` raise, as the prepare answers it.
    """
    await run_in_threadpool(audit.check, "view", p)
    source = await run_in_threadpool(store.get, p, path)
    wall, budgeted = _wall_of(settings, caller)
    jobs = _render_jobs(queue, wall, MCP_QUEUE_WAIT_S, caller, "user")
    manifest = await prepared(p, source, settings=budgeted, store=store, audit=audit, jobs=jobs)
    if manifest.kind == "none" or rendition > manifest.total:
        raise NotFound("no such rendition")
    made = await renders_for(
        p, source, manifest, [rendition], strips, settings=budgeted, store=store, audit=audit, jobs=jobs
    )
    render = next((rec for rec in made if rec.strip == strip), None)
    data = None if render is None else await run_in_threadpool(read_render, store, render)
    if render is None or data is None:
        raise HTTPException(status_code=500, detail=INTERNAL)
    await run_in_threadpool(audit.record, "view", p, file_ids=[source.id, render.id])
    return Response(
        data, media_type=render.mime, headers={"Content-Disposition": _rendition_disposition(source, render)}
    )


@router.get("/files/view")
async def view_file(
    path: str,
    p: FilePrincipalDep,
    caller: CallerDep,
    settings: SettingsDep,
    store: StoreDep,
    audit: CallAuditDep,
    queue: MaybeJobsDep,
    rendition: Annotated[int | None, Query(ge=1)] = None,
    strips: Annotated[int, Query(ge=1, le=3)] = 1,
    strip: Annotated[int | None, Query(ge=1, le=3)] = None,
) -> Response:
    """Send a file's bytes with its own media type, or one render of it.

    A query parameter the route does not name, such as ``preview=true``, is ignored, so a browser
    gets the file itself.

    A file that can run script (HTML, SVG, XML, JavaScript) is sent as plain text and an
    attachment instead, so opening it can never run it at the origin of the page that shows it.
    Every answer of this route, refusals included, carries the sandboxing headers of
    :mod:`workbench.api.hardening`.

    With ``rendition`` the answer is one render of a picture or a PDF instead: page ``rendition``, or the picture for
    1, in one piece or, with ``strips`` of 2 or 3, its strip number ``strip``. A page or the strips of it that do not
    exist yet are made first, in one job on the user's own turn, which waits at most
    :data:`~workbench.api.jobs.MCP_QUEUE_WAIT_S`. ``strips`` of 1 takes no ``strip`` but 1, a greater ``strips`` needs
    one, from 1 to ``strips``.

    Args:
        path: The file's virtual path.
        p: The caller, user and chat.
        caller: The caller, whose wall and slots bound a job.
        settings: The api's settings.
        store: The file store.
        audit: The audit log, bound to the call.
        queue: The app's job queue, if it has one.
        rendition: The page, counted from 1; absent for the file itself.
        strips: Into how many strips the page is cut, 1 to 3.
        strip: Which strip to send, counted from 1.

    Returns:
        The file, streamed, or the render.

    Raises:
        HTTPException: 422 ``strip is 1 to strips`` for a ``strips`` and ``strip`` that do not go together.
        Busy: A render has to be made and the app has no job queue.
    """
    if rendition is None:
        return await run_in_threadpool(_view_whole, path, p, store, audit)
    if (strip is not None and strip > strips) or (strip is None and strips > 1):
        raise HTTPException(status_code=422, detail="strip is 1 to strips")
    if queue is None:
        raise Busy("executor_busy")
    return await _view_rendition(path, rendition, strips, strip or 1, p, caller, settings, store, audit, queue)


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
