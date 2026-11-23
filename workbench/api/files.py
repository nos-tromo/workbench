"""Ghostwriter's file routes, held to the request's chat: list, upload, take back, artifacts, prepare, view, delete.

Any path outside the request's chat is ``404``. The calls state a purpose and a request id,
which their events carry, and an upload, a prepare or a view that must state a purpose and does not is
``428``, recorded as denied. ``DELETE /files/upload`` deletes one upload of the chat with its renders, as Ghostwriter
takes back a file its user has not sent (since 0.8.1); ``DELETE /files/chat`` deletes the request's chat.

``POST /files/derived`` keeps one of Nextext's two raw artifacts of a clip (its JSON Lines, its keyframes zip) as a
derived file of the clip. The body is the artifact itself, read whole into a ``.part`` and checked by its first bytes
before the route waits for the clip's lock; under the lock it is recorded, made visible, and replaces the artifact of
its role and what was made from the old one. The api reads no more of it than its first bytes: the executor parses it.

``POST /files/prepare`` renders a picture or a PDF once and answers its manifest (:mod:`workbench.api.renders`), and
with ``strips`` of 2 or 3 also cuts a picture into that many strips. The view of a file answers the file itself; with
``rendition`` it answers one render of it instead, a page or a picture, or one strip of it, made first when it does not
exist yet, on the user's own turn.

Every read or change of content is one audit event, by file id and never by name, and it is
written *before* the content leaves, the file becomes visible or the chat goes: an action that
cannot be recorded does not happen. An upload is recorded by the id it was given before its first
byte was written, before the rename makes it a file of the chat; an artifact is recorded by its id before the rename
makes it a derived file of its clip. An upload or an artifact that could not be stored after its record is recorded
again as ``error``. A prepare is recorded by the renders' own job, before any render is visible; a view of a render
names the source and the render. Listing touches no content and is not recorded (spec section 7).

An error answers short fixed text from :mod:`workbench.api.errors`; no answer here carries a
name, a path or a user id the caller did not just send.
"""

import dataclasses
import json
import os
import posixpath
from collections.abc import AsyncGenerator, Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Annotated, BinaryIO, Final, Literal
from urllib.parse import quote

import anyio
import anyio.from_thread
from fastapi import APIRouter, HTTPException, Query, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from starlette.requests import ClientDisconnect

from workbench.api.audit import AuditLog
from workbench.api.auth import CHAT_PATH, DERIVED_PATH, PREPARE_PATH, Caller
from workbench.api.deps import CallAuditDep, CallerDep, FilePrincipalDep, JobsDep, MaybeJobsDep, SettingsDep, StoreDep
from workbench.api.errors import INTERNAL
from workbench.api.jobs import (
    MCP_QUEUE_WAIT_S,
    PREPARE_QUEUE_WAIT_S,
    SOURCE_LOCK_WAIT_S,
    Busy,
    CallLimits,
    Gate,
    JobQueue,
)
from workbench.api.renders import Noun, RouteJobs, noun_of, prepared, read_render, renders_for
from workbench.api.routing import EncodableRoute
from workbench.api.store import (
    ARTIFACT_EMPTY,
    ARTIFACT_NOT_ITS_ROLE,
    NotFound,
    StagedArtifact,
    Store,
    UnreadableArtifact,
)
from workbench.api.tools_mcp import PREVIEW_RESERVE_S
from workbench.config import Settings
from workbench.models import DerivedRecord, DerivedSpec, FileRecord, Principal
from workbench.renders import ARTIFACTS, JSONL_MAX_BYTES, ZIP_MAX_BYTES, kind_of

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

#: The two roles of a clip's raw artifacts, as the route's ``role`` names them.
ArtifactRole = Literal["nextext_jsonl", "nextext_zip"]

#: What each artifact of a clip is sent as, and the most bytes it may have. The route does not trust the type a caller
#: sends: the first bytes decide (:func:`_artifact_check`).
ROLE_TYPES: Final[Mapping[ArtifactRole, tuple[str, int]]] = {
    "nextext_jsonl": (ARTIFACTS["nextext_jsonl"][0], JSONL_MAX_BYTES),
    "nextext_zip": (ARTIFACTS["nextext_zip"][0], ZIP_MAX_BYTES),
}

#: The answer for a source that is no audio or video upload.
UNSUPPORTED_SOURCE: Final = "unsupported_format: the source is not an audio or video upload"

#: The answer for a client that went before its body was whole: the text FastAPI gives an upload in that case.
BODY_NOT_READ: Final = "There was an error parsing the body"

#: The refusal of ``strips`` above 1 for a view of a clip's frame: they are never cut.
CLIP_NOT_CUT: Final = "a clip's frames are not cut into strips"

#: The first four bytes of a zip, and the most bytes of a jsonl's first line the api parses.
_ZIP_SIGNATURE: Final = b"PK\x03\x04"
_FIRST_LINE_MAX: Final = 64 * 1024


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


class _Body:
    """A request's body as a file a worker thread reads, one chunk of the request in hand at a time.

    The route reads it in a thread (:func:`~fastapi.concurrency.run_in_threadpool`), so that the store may write the
    ``.part`` as the body comes in, as :meth:`~workbench.api.store.Store.add_upload` does for an upload. ``read`` takes
    the next chunk from the request through the event loop (:func:`anyio.from_thread.run`) only when the last is used
    up, so no more than one chunk is held. Whatever the request raises while its body is read (a client that is gone, a
    body past the bound of :class:`~workbench.api.body_limit.BodyLimit`) goes up through ``read`` unchanged.
    """

    def __init__(self, request: Request) -> None:
        """Wrap ``request``; nothing is read yet.

        Args:
            request: The request whose body is read; its stream is taken once.
        """
        self._chunks: AsyncGenerator[bytes, None] = request.stream()
        self._held = b""
        self._ended = False

    async def _pull(self) -> bytes | None:
        """Take the next chunk from the request, on the event loop.

        Returns:
            The next chunk that holds a byte, or ``None`` once the body has ended.
        """
        while True:
            try:
                chunk = await anext(self._chunks)
            except StopAsyncIteration:
                return None
            if chunk:
                return chunk

    def read(self, size: int) -> bytes:
        """Read at most ``size`` bytes of the body, from a worker thread.

        Args:
            size: The most bytes to give, at least 1.

        Returns:
            The next bytes of the body: fewer than ``size`` when the chunk in hand ends first, and ``b""`` at the end.
        """
        if not self._held and not self._ended:
            chunk = anyio.from_thread.run(self._pull)
            if chunk is None:
                self._ended = True
            else:
                self._held = chunk
        taken, self._held = self._held[:size], self._held[size:]
        return taken


def _artifact_check(role: ArtifactRole) -> Callable[[Path], None]:
    """Make the check of an artifact's first bytes, which the store runs on the whole ``.part`` outside its lock.

    A zip must start with its signature. A jsonl must not be empty, and its first line, up to the first line break
    and at most 64 KiB, must be a JSON object: the one thing of it the api parses. The refusal is one of the store's
    two fixed texts, never a word of the artifact. A ``.part`` that has gone (the chat was deleted meanwhile) raises
    ``FileNotFoundError`` out of the check, which the store takes for a clip that is gone.

    Args:
        role: The artifact's role.

    Returns:
        The check: it reads the ``.part`` and raises :class:`~workbench.api.store.UnreadableArtifact` for one that is
        not what its role says.
    """

    def check_zip(part: Path) -> None:
        with part.open("rb") as handle:
            head = handle.read(len(_ZIP_SIGNATURE))
        if not head:
            raise UnreadableArtifact(ARTIFACT_EMPTY)
        if head != _ZIP_SIGNATURE:
            raise UnreadableArtifact(ARTIFACT_NOT_ITS_ROLE)

    def check_jsonl(part: Path) -> None:
        with part.open("rb") as handle:
            head = handle.read(_FIRST_LINE_MAX + 1)
        if not head:
            raise UnreadableArtifact(ARTIFACT_EMPTY)
        line = head.partition(b"\n")[0]
        if len(line) > _FIRST_LINE_MAX:
            raise UnreadableArtifact(ARTIFACT_NOT_ITS_ROLE)
        try:
            first = json.loads(line)
        except (ValueError, RecursionError):  # not JSON, not UTF-8, or nested past the parser's depth
            raise UnreadableArtifact(ARTIFACT_NOT_ITS_ROLE) from None
        if not isinstance(first, dict):
            raise UnreadableArtifact(ARTIFACT_NOT_ITS_ROLE)

    return check_zip if role == "nextext_zip" else check_jsonl


@router.post(DERIVED_PATH)
async def store_derived(
    source: str,
    role: ArtifactRole,
    request: Request,
    p: FilePrincipalDep,
    settings: SettingsDep,
    store: StoreDep,
    audit: CallAuditDep,
    jobs: JobsDep,
) -> dict[str, object]:
    """Keep one of Nextext's raw artifacts of a clip as a derived file of it, replacing the one of its role.

    The request's body is the artifact. It is read whole into a ``.part`` in the clip's folder of derived files first,
    its first bytes are checked (:func:`_artifact_check`) and its size is held to the role's bound, all before the route
    waits for anything: a client whose body is not read meanwhile would hit its write timeout. Then it waits for the
    clip's lock up to :data:`~workbench.api.jobs.SOURCE_LOCK_WAIT_S`, so that a prepare of the clip that is running (an
    artifact posted again after a restart) finishes first. Under the lock the artifact is recorded by its id and the
    ids it replaces, renamed into place and inserted with the deletion of what it replaces: the artifact of its role,
    and the transcript and frames made from the old one. A refusal remembered for the clip is forgotten, as its input
    is new.

    The ``.part`` is removed whenever the call ends before the artifact is placed: by the stage itself when the body
    cannot be read whole (a client that is gone, a body past its bound, a refusal of the check or of the quota), and
    by this route when the clip is busy, the call is cancelled while it waits for the lock, or the store refuses to
    place it. One case is left to retention: a handler cancelled natively in the middle of the transfer (in practice a
    server shutdown) leaves its worker thread running on to the end of the stage, with nobody to receive its result,
    so the ``.part`` stays until the orphan sweep removes it, recorded by its id. A client that goes while its body
    comes is answered ``400`` and :data:`BODY_NOT_READ`, quietly, as an upload's is.

    Args:
        source: The clip's virtual path, in the request's chat.
        role: ``nextext_jsonl`` or ``nextext_zip``.
        request: The request; its body is the artifact, raw.
        p: The caller, user and chat.
        settings: The api's settings; they hold the quota.
        store: The file store.
        audit: The audit log, bound to the call.
        jobs: The job queue, whose lock of the clip the route holds.

    Returns:
        ``id`` of the new row, its ``role`` and ``bytes``, and ``replaced``: the id of the row of that role it replaced,
        or ``None``.

    Raises:
        HTTPException: 422 ``unsupported_format`` for a source that is no audio or video upload; 400 for a client
            that went before its body was whole. The store's refusals
            (a missing clip, a body over its bound, the quota, an artifact that is not what its role says) and a busy
            clip are answered by the app's handlers.
    """
    await run_in_threadpool(audit.check, "derived", p)
    clip = await run_in_threadpool(store.get, p, source)
    if clip.kind != "upload" or kind_of(clip.name) != "media":
        raise HTTPException(status_code=422, detail=UNSUPPORTED_SOURCE)
    mime, max_bytes = ROLE_TYPES[role]
    quota_bytes = settings.workspace_max_mb * _MIB
    spec = DerivedSpec(role, 0, mime, None, None, {})
    body = _Body(request)

    def stage() -> StagedArtifact:
        return store.stage_derived(
            clip, body, spec, max_bytes=max_bytes, quota_bytes=quota_bytes, check=_artifact_check(role)
        )

    try:
        staged = await run_in_threadpool(stage)
    except ClientDisconnect:  # the stage has removed its .part; a client that goes is no fault, as for an upload
        raise HTTPException(status_code=400, detail=BODY_NOT_READ) from None

    def record(file_id: str, size: int, replaced: Sequence[str], outcome: str) -> None:
        audit.record(
            "derived",
            p,
            file_ids=[clip.id, file_id, *replaced],
            outcome=outcome,
            criteria={"role": role, "bytes": size, "replaced": len(replaced)},
        )

    def place() -> tuple[DerivedRecord, list[DerivedRecord]]:
        return store.place_derived(staged, quota_bytes=quota_bytes, record=record)

    try:
        async with jobs.source_lock(clip.id, SOURCE_LOCK_WAIT_S):
            made, replaced = await run_in_threadpool(place)
            store.forget_refusal(clip)
    except BaseException:  # a busy clip, a call that ends, a refusal of the store: the .part goes whatever the reason
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(store.drop_staged, staged)
        raise
    previous = next((each.id for each in replaced if each.role == role), None)
    return {"id": made.id, "role": made.role, "bytes": made.size, "replaced": previous}


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
    strips: Annotated[int, Query(ge=1, le=3)] = 1,
) -> dict[str, object]:
    """Render a picture, a PDF or an Office file once, and answer its manifest.

    The first prepare of a file draws its first pages in one job, recorded before any render is visible; every later
    one answers from the renders kept, with no job and no event. An Office file (``kind: office``, counted by ``unit``)
    is converted once to a PDF first, kept as a derived file of it, and drawn in a second job of the same turn, on the
    caller's conversion gate; each of the two is recorded before what it made is visible. A file that is none of these
    is answered at once as ``kind: none``; one the executor refused for what it is, with that refusal again, at once
    and with no job. The request has no body. It waits at most :data:`~workbench.api.jobs.PREPARE_QUEUE_WAIT_S` for its
    turn, on the caller's prepare gate (a picture's or a PDF's) or conversion gate (an Office file's), and so never
    takes the user's own turn from a chat in progress, nor does a picture's wait behind a conversion. With ``strips``
    of 2 or 3, a picture is also cut into that many strips, in a second job on the same gate (none once they exist),
    so that a model that sees it in strips pays no job on its turn; a strip that cannot be made fails the prepare as a
    view of it would. A PDF's pages are cut when they are viewed, whatever ``strips`` says.

    Args:
        path: The file's virtual path, in the request's chat.
        p: The caller, user and chat.
        caller: The caller, whose wall and slots bound the job.
        settings: The api's settings.
        store: The file store.
        audit: The audit log, bound to the call.
        jobs: The app's job queue.
        strips: Into how many strips a picture is cut as well, 1 to 3; 1 for none.

    Returns:
        The manifest, all its keys, the ones that hold nothing as ``null``: the same whatever ``strips`` is.
    """
    await run_in_threadpool(audit.check, "prepare", p)
    source = await run_in_threadpool(store.get, p, path)
    wall, budgeted = _wall_of(settings, caller)
    gate = _render_jobs(jobs, wall, PREPARE_QUEUE_WAIT_S, caller, "prepare")
    manifest = await prepared(p, source, settings=budgeted, store=store, audit=audit, jobs=gate)
    if strips > 1 and manifest.kind == "image":
        await renders_for(p, source, manifest, [1], strips, settings=budgeted, store=store, audit=audit, jobs=gate)
    return manifest.model_dump(mode="json")


def _rendition_disposition(source: FileRecord, render: DerivedRecord, noun: Noun) -> str:
    """Build the ``Content-Disposition`` of a render: the source's name, the page and the strip, in RFC 5987 form.

    Args:
        source: The file the render is of.
        render: The render.
        noun: What the file's renders show (:func:`~workbench.api.renders.noun_of`): ``slide`` names a deck's.

    Returns:
        ``inline`` and the name ``<stem>-page-3-strip-1-of-2.jpg`` (``<stem>-slide-3.jpg`` for a slide,
        ``<stem>-frame-3.jpg`` for a frame of a clip): a picture shown whole has its stem alone, a page, slide or frame
        its number, a strip its place among the strips of its page.
        Never the render's own name, which is an id.
    """
    stem = posixpath.splitext(posixpath.basename(source.name))[0]
    parts = [stem]
    if render.role == "page":
        parts.append(f"{'slide' if noun == 'slide' else 'page'}-{render.ordinal}")
    elif render.role == "frame":
        parts.append(f"frame-{render.ordinal}")
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

    A clip's frame ``rendition`` is one of the rows its prepare made: it is served whole. One that is lost, its row or
    its file, is not found (404), never an internal error: a frame is not made on its own, and comes back only with the
    whole set when the clip's head (the transcript, else frame 1) is gone and the prepare runs again.

    Args:
        path: The file's virtual path.
        rendition: The page, slide or frame, counted from 1; 1 for a picture.
        strips: Into how many strips the page is cut; a clip's frames are not cut.
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
        NotFound: The file is not shown as pictures, or has no such page, slide or frame (a clip's lost frame too).
        HTTPException: 422 for ``strips`` above 1 of a clip, before any job; 500 ``internal error``: the job made no
            such render without saying why, or what it made is not what its row says (not a clip's frame). Any other
            refusal is what :func:`~workbench.api.renders.prepared` and :func:`~workbench.api.renders.renders_for`
            raise, as the prepare answers it.
    """
    await run_in_threadpool(audit.check, "view", p)
    source = await run_in_threadpool(store.get, p, path)
    if strips > 1 and kind_of(source.name) == "media":  # before any job: a clip's frames are never cut
        raise HTTPException(status_code=422, detail=CLIP_NOT_CUT)
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
        if manifest.kind == "media":  # a frame is not made on its own: one lost (its row, its file) is not found
            raise NotFound("no such rendition")
        raise HTTPException(status_code=500, detail=INTERNAL)
    await run_in_threadpool(audit.record, "view", p, file_ids=[source.id, render.id])
    disposition = _rendition_disposition(source, render, noun_of(manifest))
    return Response(data, media_type=render.mime, headers={"Content-Disposition": disposition})


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
    1, in one piece or, with ``strips`` of 2 or 3, its strip number ``strip``. Of a clip it is frame ``rendition``,
    whole (``strips`` above 1 is 422, with or without a ``strip``), made with its transcript when the clip is prepared
    and never one by one; one that is lost is 404. A page
    or the strips of it that do not exist yet are made first, in one job on the user's own turn, which waits at most
    :data:`~workbench.api.jobs.MCP_QUEUE_WAIT_S`. An Office file with no PDF kept is converted on demand first, and
    drawn, in one turn of the caller's conversion gate, which waits for that gate and then a slot up to
    :data:`~workbench.api.jobs.SOURCE_LOCK_WAIT_S`. While another call makes the file's renders, such as its prepare
    converting it, the view waits for that call up to :data:`~workbench.api.jobs.SOURCE_LOCK_WAIT_S` too, and answers
    what it made. ``strips`` of 1 takes no ``strip`` but 1, a greater ``strips`` needs one, from 1 to ``strips``.

    Args:
        path: The file's virtual path.
        p: The caller, user and chat.
        caller: The caller, whose wall and slots bound a job.
        settings: The api's settings.
        store: The file store.
        audit: The audit log, bound to the call.
        queue: The app's job queue, if it has one.
        rendition: The page or slide, counted from 1; absent for the file itself.
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
    if strips > 1 and kind_of(path) == "media":  # the clip's own refusal comes first, with or without a strip
        raise HTTPException(status_code=422, detail=CLIP_NOT_CUT)
    if (strip is not None and strip > strips) or (strip is None and strips > 1):
        raise HTTPException(status_code=422, detail="strip is 1 to strips")
    if queue is None:
        raise Busy("executor_busy")
    return await _view_rendition(path, rendition, strips, strip or 1, p, caller, settings, store, audit, queue)


@router.delete("/files/upload")
def delete_upload(path: str, p: FilePrincipalDep, store: StoreDep, audit: CallAuditDep) -> dict[str, int]:
    """Delete an upload of the request's chat that Ghostwriter takes back for its user, with its renders.

    Only an upload goes: a tool's output, another chat's file or a render is not found. The deletion is recorded by
    the file's id and its renders' first; one whose event cannot be written deletes nothing. A job still running on the
    file keeps no render (:meth:`~workbench.api.store.Store.remove_upload`). A refusal remembered for the file is
    forgotten with it.

    Args:
        path: The upload's absolute path, ``/workspace/<chat>/<name>``.
        p: The caller, the user and the chat.
        store: The file store.
        audit: The audit log, bound to the call.

    Returns:
        ``deleted``, 1, and how many ``renders`` went with it.

    Raises:
        NotFound: No upload of the chat is at the path.
    """
    audit.check("file_delete", p)
    rec = store.get(p, path)
    if rec.kind != "upload":
        raise NotFound("file not found")

    def record(ids: Sequence[str], outcome: str) -> None:
        audit.record("file_delete", p, file_ids=ids, outcome=outcome, criteria={"renders": len(ids) - 1})

    return {"deleted": 1, "renders": store.remove_upload(rec, record)}


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
