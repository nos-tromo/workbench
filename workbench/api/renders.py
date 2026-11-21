"""The api's view of the renders of a picture or a PDF: prepared once, made on demand, read back checked.

A render is a derived file of its source (:meth:`~workbench.api.store.Store.register_derived`), made by the executor's
``prepare_file`` job through the queue (:meth:`~workbench.api.jobs.JobQueue.submit_renders`). This module decides which
jobs run and reads their renders back. It decodes nothing and imports no imaging library: a render is checked by its
signature and its size alone (:mod:`workbench.renders`).

- :func:`prepared` answers a file's :class:`Manifest`. The first time, one job draws the first pages and finds the
  facts (kind, page count, pages without text), which the whole render of page 1 keeps; every later call answers from
  the rows, with no job, no event and no wait for the source's lock.
- :func:`renders_for` answers the renders of some pages at a strip count, and makes those that are missing, unless
  the source's renders leave no room for one more.
- :func:`read_render` reads one back, or nothing when it is not what its row says.
- :func:`image_block`, :func:`label`, :func:`shown_image` and :func:`page_window` are what the tools that show images
  share, with the caps of what one answer shows.

Every job is recorded before any render it made is visible (``modify`` / ``file.prepare``, by the source's id and
then each render's), and one call at a time makes the renders of a source (:meth:`RenderJobs.source_lock`), so two
prepares of one file run one job and keep one set of renders. The rows are read again after every job: a render whose
row is not there is never answered, whatever the job said it made.
"""

import base64
import dataclasses
import logging
import re
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Final, Literal, Protocol

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ValidationError

from workbench.api.audit import AuditLog
from workbench.api.jobs import CallLimits, Gate, JobQueue, Keeper
from workbench.api.store import NotFound, QuotaExceeded, RendersFull, StorageFull, Store, StoreError
from workbench.api.tools import OutputKeeper, ShownImage, _parse, _payload, _readable, judged
from workbench.config import Settings
from workbench.models import DerivedRecord, FileRecord, JobRecord, Principal
from workbench.paths import PathError
from workbench.protocol import JobResult
from workbench.renders import (
    DERIVED_MAX_BYTES,
    MAX_STRIPS,
    RENDER_MAX_BYTES,
    RENDERS_PER_JOB,
    SIGNATURES,
    RenderAnswer,
    RenderFacts,
    kind_of,
    signature_ok,
)

_log = logging.getLogger(__name__)

#: Most images one ``view_file`` answer shows.
MAX_VIEW_IMAGES: Final = 8
#: Most images one ``read_document`` answer shows.
MAX_READ_IMAGES: Final = 4
#: Most raw bytes of the images of one answer (2.5 MiB): as base64 they stay under Ghostwriter's 4 MiB answer cap.
MAX_SHOWN_BYTES: Final = 2_621_440

#: The hint of a file that is neither a picture nor a PDF.
NOT_SHOWN: Final = "This kind of file is not shown as pictures; read it with read_document."

#: The text of the 500 for a render the store lost for a reason it cannot name (:func:`_lost`).
NOT_KEPT: Final = "the renders of this file could not be kept"

#: The roles of the derived files that are renders.
_RENDER_ROLES: Final = frozenset({"image", "page"})

#: The note the queue adds to a job's message for the outputs it could not keep, their reasons in the order they first
#: came (:meth:`~workbench.api.jobs.JobQueue._keep`). A prepare draws whole pages only, so the whole render of page 1
#: is the first of its outputs, which are in name order: when it was lost, its reason is the first.
_LOST: Final = re.compile(r"\d+ outputs? could not be kept \(([a-z ,]+)\)\Z")

#: What a page or a window of pages may be written as: ``page 3``, ``3``, ``pages 2-5`` or ``2-5``, in any case, with
#: ``page`` and ``pages`` taken alike. Nine digits at most, so no number is too long to read.
_PART: Final = re.compile(r"(?:pages?\s*)?(\d{1,9})(?:\s*-\s*(\d{1,9}))?", re.IGNORECASE)
_GRAMMAR: Final = "no_such_part: give a page as 'page 3', or pages as 'pages 2-5'"


class Rendition(BaseModel):
    """One whole render of a file that exists now: the picture, or a page.

    Attributes:
        n: The page, counted from 1; 1 for a picture.
        width: Its width in pixels.
        height: Its height in pixels.
        mime: Its type.
        bytes: Its size in bytes.
    """

    n: int
    width: int
    height: int
    mime: Literal["image/jpeg", "image/png"]
    bytes: int


class Manifest(BaseModel):
    """What a file is to the tools that show it, and which of its renders exist; never its content.

    Attributes:
        path: The file's path.
        kind: ``image``, ``pdf``, or ``none`` for a file that is not shown as pictures.
        total: Pages of a PDF; 1 for a picture, 0 for ``none``.
        pages_without_text: The pages of a PDF with no text of their own (scans, pictures).
        renditions: The whole renders that exist now, by page.
        frames: The frame count of a picture with several, of which only the first is shown; else ``None``.
        hint: A short note, such as which pages were not checked for text; ``None`` for none.
    """

    path: str
    kind: Literal["image", "pdf", "none"]
    total: int
    pages_without_text: list[int]
    renditions: list[Rendition]
    frames: int | None
    hint: str | None


@dataclasses.dataclass(frozen=True)
class WithBlocks:
    """A tool's answer and the image blocks that go with it, in the order its ``shown`` names them.

    Attributes:
        answer: The answer.
        blocks: The MCP image blocks (:func:`image_block`).
    """

    answer: BaseModel
    blocks: list[dict[str, Any]]


class RenderJobs(Protocol):
    """The queue as a call that renders sees it, within that call's limits and on its gate.

    The MCP door's :class:`~workbench.api.tools_mcp.CallJobs` is one, on the user's own turn; :class:`RouteJobs` is the
    file routes'.
    """

    async def submit_renders(
        self, p: Principal, source: FileRecord, args: dict[str, Any], keeper: Keeper
    ) -> tuple[JobRecord, JobResult, list[DerivedRecord]]:
        """Render a picture or pages of a PDF, as :meth:`~workbench.api.jobs.JobQueue.submit_renders` does.

        Args:
            p: The caller, user and chat the job runs for: the source's own.
            source: The file to render.
            args: The runner's other arguments.
            keeper: Records the job before any render becomes visible, and picks the renders to keep.

        Returns:
            The job record, the executor's answer and the renders kept.
        """
        ...

    def source_lock(self, source_id: str) -> AbstractAsyncContextManager[None]:
        """Hold the lock of one source's renders, as :meth:`~workbench.api.jobs.JobQueue.source_lock` does.

        Args:
            source_id: The source's id.

        Returns:
            The lock, held while the context runs; entering it raises ``Busy("wait_timeout")`` after the call's wait.
        """
        ...


@dataclasses.dataclass(frozen=True)
class RouteJobs:
    """The queue as a file route sees it: its call's limits, and the gate its jobs wait for.

    Attributes:
        queue: The app's job queue.
        call: The call's wall, wait and caller slots.
        gate: ``prepare`` for the prepare route, ``user`` for a view on the user's own turn.
    """

    queue: JobQueue
    call: CallLimits
    gate: Gate

    async def submit_renders(
        self, p: Principal, source: FileRecord, args: dict[str, Any], keeper: Keeper
    ) -> tuple[JobRecord, JobResult, list[DerivedRecord]]:
        """Render within the call's limits, on the route's gate.

        Args:
            p: The caller, user and chat the job runs for: the source's own.
            source: The file to render.
            args: The runner's other arguments.
            keeper: Records the job before any render becomes visible, and picks the renders to keep.

        Returns:
            What :meth:`~workbench.api.jobs.JobQueue.submit_renders` returns.
        """
        return await self.queue.submit_renders(p, source, args, keeper, call=self.call, gate=self.gate)

    def source_lock(self, source_id: str) -> AbstractAsyncContextManager[None]:
        """Hold the lock of one source's renders, waiting at most the call's wait.

        Args:
            source_id: The source's id.

        Returns:
            The lock, held while the context runs.
        """
        return self.queue.source_lock(source_id, self.call.wait_s)


def _is_render(rec: DerivedRecord) -> bool:
    """Tell whether a derived file is a render: a picture or a page, of a type a render has, with its size.

    Args:
        rec: The derived file.

    Returns:
        Whether it is.
    """
    return rec.role in _RENDER_ROLES and rec.mime in SIGNATURES and rec.width is not None and rec.height is not None


def _facts(head: DerivedRecord) -> RenderFacts | None:
    """Read the facts the whole render of page 1 keeps, leniently: a fact this version does not know is left out.

    A later version may keep more facts (and gives each it adds a default, so that it reads this version's); a head
    it prepared is still read here, after a rollback, rather than taken for one with no facts, which no job could mend.

    Args:
        head: The whole render of page 1.

    Returns:
        The facts, or ``None`` when it keeps none that can be read: a render made by a job that did not look for them.
    """
    stored = head.meta.get("facts")
    if isinstance(stored, Mapping):
        stored = {name: value for name, value in stored.items() if name in RenderFacts.model_fields}
    try:
        return RenderFacts.model_validate(stored)
    except ValidationError:
        return None


def manifest_of(source: FileRecord, rows: Sequence[DerivedRecord]) -> Manifest | None:
    """Build a file's manifest from its rows alone.

    Args:
        source: The file.
        rows: Its derived files, as :meth:`~workbench.api.store.Store.derived_of` lists them.

    Returns:
        The manifest: the facts of the whole render of page 1 and every whole render of its role, by page. ``None``
        when there is no whole render of page 1 that carries its facts: the file is not prepared yet.
    """
    whole = [rec for rec in rows if _is_render(rec) and rec.strips == 1]
    head = next((rec for rec in whole if rec.ordinal == 1), None)
    facts = None if head is None else _facts(head)
    if head is None or facts is None:
        return None
    renditions = [
        Rendition.model_validate(
            {"n": rec.ordinal, "width": rec.width, "height": rec.height, "mime": rec.mime, "bytes": rec.size}
        )
        for rec in sorted(whole, key=lambda rec: rec.ordinal)
        if rec.role == head.role
    ]
    return Manifest(
        path=source.virtual_path,
        kind=facts.kind,
        total=facts.total,
        pages_without_text=facts.pages_without_text,
        renditions=renditions,
        frames=facts.frames,
        hint=facts.hint,
    )


def _budget(settings: Settings) -> int:
    """The seconds a render job's runner has, 3/4 of the job's wall, so it can say it timed out.

    Args:
        settings: The api's settings; ``job_wall_s`` is the wall.

    Returns:
        At least 1.
    """
    return max(1, settings.job_wall_s * 3 // 4)


def _keeper(audit: AuditLog, p: Principal, source: FileRecord, *, strips: int, on_demand: bool) -> OutputKeeper:
    """The keeper of a render job: it records the job by the source and the renders it keeps, before any is visible.

    An ``ok`` answer that reads as a render answer keeps every render the job staged; any other keeps none. The queue
    keeps each only as the render the answer names (:meth:`~workbench.api.jobs.JobQueue.submit_renders`).

    Args:
        audit: The audit log.
        p: The caller, user and chat of the source.
        source: The file rendered.
        strips: Into how many strips each page is cut.
        on_demand: Whether the job makes pages a call asked for after the prepare.

    Returns:
        The keeper.
    """

    def decide(result: JobResult, planned: Sequence[str]) -> tuple[list[str], str, dict[str, object]]:
        usable = result.status == "ok" and _readable(RenderAnswer, result.result)
        keep: list[str] = list(planned) if usable else []
        outcome, more = judged(result, usable)
        criteria: dict[str, object] = {
            "renders": len(planned),
            "strips": strips,
            "on_demand": on_demand,
            "outputs": len(keep),
        }
        return keep, outcome, {**criteria, **more}

    return OutputKeeper(audit, p, "prepare", [source.id], decide)


def _lost(message: str) -> Exception:
    """Name why the whole render of page 1 was not kept, from the note the queue adds to the job's message.

    Args:
        message: The job's message.

    Returns:
        The store's error of that loss: a 507 for the user's quota, the source's cap of renders or a full volume, and
        ``NotFound`` (404) for a source that went while the job ran, with its chat or by retention. Any other loss,
        such as a render that exists already (or a reserved folder that is not one) or one the store did not foresee,
        or none, is a 500. The first reason names the loss, except a ``duplicate`` that comes before another: a job of
        strips draws again the strips of a page that exist, which the store refuses as duplicates before it refuses
        the one that was lost.
    """
    found = _LOST.search(message)
    reasons: list[str] = found.group(1).split(", ") if found is not None else []
    reason = next((why for why in reasons if why != "duplicate"), reasons[0] if reasons else None)
    if reason == "gone":
        return NotFound("file not found")
    if reason == "quota":
        return QuotaExceeded("workspace quota exceeded")
    if reason == "full":
        return RendersFull("the renders of this file are at their limit")
    if reason == "storage full":
        return StorageFull("storage is full")
    return HTTPException(status_code=500, detail=NOT_KEPT)


async def prepared(
    p: Principal, source: FileRecord, *, settings: Settings, store: Store, audit: AuditLog, jobs: RenderJobs
) -> Manifest:
    """Answer a file's manifest, rendering its first pages the first time.

    A file that is neither a picture nor a PDF is answered at once, ``kind: none``, with no job. A file whose rows hold
    the manifest is answered from them, with no job, no event and no wait for the source's lock, which a job of later
    pages may hold. One that does not takes the lock, reads the rows again inside it, and has one job, which draws the
    first pages (but those whole pages it has already), finds the facts and is recorded before any render is visible.
    A second call for the same file waits for the lock and then finds the rows.

    Args:
        p: The caller, user and chat of the source.
        source: The file.
        settings: The api's settings; ``job_wall_s`` gives the runner's time.
        store: The store.
        audit: The audit log, bound to the call.
        jobs: The queue, within the call's limits and on its gate.

    Returns:
        The manifest.

    Raises:
        PurposeRequired: The call had to state a purpose for ``modify`` and did not; no job runs.
        AuditWriteError: The job's event could not be written; no render is kept.
        Busy: The lock or the queue did not come free in time, or the executor is busy.
        HTTPException: The runner refused the file (its code's status, such as 422 ``unreadable_file``), the job ran
            out of time (422), or failed or answered what cannot be read (500); or the whole render of page 1, which
            holds the facts, was not kept for a reason that is not one of the three below (500).
        QuotaExceeded: That render would take the user over their quota (507).
        RendersFull: It would take the source over its cap of renders (507).
        StorageFull: The volume is full (507).
        NotFound: The file went while the job ran, with its chat (404).
    """
    if kind_of(source.name) == "none":
        return Manifest(
            path=source.virtual_path,
            kind="none",
            total=0,
            pages_without_text=[],
            renditions=[],
            frames=None,
            hint=NOT_SHOWN,
        )
    manifest = manifest_of(source, await run_in_threadpool(store.derived_of, source))
    if manifest is not None:
        return manifest
    async with jobs.source_lock(source.id):
        rows = await run_in_threadpool(store.derived_of, source)
        manifest = manifest_of(source, rows)
        if manifest is not None:
            return manifest
        await run_in_threadpool(audit.check, "prepare", p)
        have = sorted({rec.ordinal for rec in rows if _is_render(rec) and rec.strips == 1})
        args = {"pages": None, "strips": 1, "have": have, "budget_s": _budget(settings), "facts": True}
        keeper = _keeper(audit, p, source, strips=1, on_demand=False)
        _, result, _ = await jobs.submit_renders(p, source, args, keeper)
        _parse(RenderAnswer, _payload(result, "file"))
        manifest = manifest_of(source, await run_in_threadpool(store.derived_of, source))
        if manifest is None:
            raise _lost(result.message)
    return manifest


def _no_room(rows: Sequence[DerivedRecord], strips: int) -> bool:
    """Tell whether a source's renders leave no room for one more at a strip count, so that a job would keep none.

    The store refuses a render that would take its source past ``DERIVED_MAX_BYTES``. The renders of one file at one
    strip count are of like size, so when the room left is less than the smallest of them the source holds, the job is
    not run: it would be thrown away, with its event, on every view of a page past the cap. With none held at that
    strip count, the job runs and the store decides.

    Args:
        rows: The source's derived files, every one counted.
        strips: Into how many strips each page would be cut.

    Returns:
        Whether there is no room.
    """
    alike = [rec.size for rec in rows if _is_render(rec) and rec.strips == strips]
    return bool(alike) and DERIVED_MAX_BYTES - sum(rec.size for rec in rows) < min(alike)


def _complete(rows: Sequence[DerivedRecord], role: str, strips: int) -> dict[int, list[DerivedRecord]]:
    """Find the pages whose renders at a strip count are all there.

    Args:
        rows: The source's derived files.
        role: ``image`` or ``page``.
        strips: Into how many strips each page is cut.

    Returns:
        Each page that has every one of its strips, to those strips in order; a page that lacks one is not in it.
    """
    cut: dict[int, dict[int, DerivedRecord]] = {}
    for rec in rows:
        if _is_render(rec) and rec.role == role and rec.strips == strips:
            cut.setdefault(rec.ordinal, {})[rec.strip] = rec
    every = range(1, strips + 1)
    return {n: [held[i] for i in every] for n, held in cut.items() if all(i in held for i in every)}


async def renders_for(
    p: Principal,
    source: FileRecord,
    manifest: Manifest,
    pages: Sequence[int],
    strips: int,
    *,
    settings: Settings,
    store: Store,
    audit: AuditLog,
    jobs: RenderJobs,
) -> list[DerivedRecord]:
    """Answer the renders of some pages at a strip count, making those that are missing.

    The rows are read first; when a page lacks a render, the missing pages are made under the source's lock, the rows
    read again inside it, by one job per at most ``RENDERS_PER_JOB // strips`` pages. Each job is recorded before any
    render it made is visible, and does not look for the facts again: the manifest holds them (ruling R4). No job runs
    when the source's renders leave no room for one more (:func:`_no_room`): the missing pages are left out as the cap
    would leave them. A page the job did not make, or whose renders the store could not keep (the user's quota, the
    source's cap, a full volume), is left out: the answer is only rows that exist. When none of the pages asked for
    exists once the jobs are through, and a job reported that renders could not be kept, the first such loss is raised,
    as :func:`prepared` raises the loss of the head (:func:`_lost`), or the cap when no job ran for want of room; a page
    left out beside others that exist is not an error.

    Args:
        p: The caller, user and chat of the source.
        source: The file.
        manifest: Its manifest, from :func:`prepared`.
        pages: The pages, counted from 1, in any order; a page outside the file is left out.
        strips: Into how many strips each page is cut, 1 to ``MAX_STRIPS``.
        settings: The api's settings; ``job_wall_s`` gives the runner's time.
        store: The store.
        audit: The audit log, bound to the call.
        jobs: The queue, within the call's limits and on its gate.

    Returns:
        The renders, by page and then by strip; every strip of each page, or none of it.

    Raises:
        ValueError: ``strips`` is not 1 to ``MAX_STRIPS``; the caller checks it first, so this is a programming error.
        PurposeRequired: The call had to state a purpose for ``modify`` and did not; no job runs.
        AuditWriteError: A job's event could not be written; none of its renders is kept.
        Busy: The lock or the queue did not come free in time, or the executor is busy.
        HTTPException: The runner refused the file (its code's status), the job ran out of time (422), or failed or
            answered what cannot be read (500); or no page asked for exists and the loss a job reported is not one of
            the three below (500).
        QuotaExceeded: No page asked for exists, and the first loss was the user's quota (507).
        RendersFull: No page asked for exists, and the first loss was the source's cap of renders, or the renders left
            no room for one more (507).
        StorageFull: No page asked for exists, and the first loss was a full volume (507).
        NotFound: No page asked for exists, and the file went while a job ran, with its chat (404).
    """
    if not 1 <= strips <= MAX_STRIPS:
        raise ValueError(f"strips is 1 to {MAX_STRIPS}")
    if manifest.kind == "none":
        return []
    role = "image" if manifest.kind == "image" else "page"
    wanted = sorted({n for n in pages if 1 <= n <= manifest.total})
    complete = _complete(await run_in_threadpool(store.derived_of, source), role, strips)
    losses: list[str] = []
    full = False
    if any(n not in complete for n in wanted):
        async with jobs.source_lock(source.id):
            rows = await run_in_threadpool(store.derived_of, source)
            complete = _complete(rows, role, strips)
            missing = [n for n in wanted if n not in complete]
            full = bool(missing) and _no_room(rows, strips)
            if missing and not full:
                await run_in_threadpool(audit.check, "prepare", p)
                per_job = RENDERS_PER_JOB // strips
                for start in range(0, len(missing), per_job):
                    args = {
                        "pages": missing[start : start + per_job],
                        "strips": strips,
                        "budget_s": _budget(settings),
                        "facts": False,
                    }
                    keeper = _keeper(audit, p, source, strips=strips, on_demand=True)
                    _, result, _ = await jobs.submit_renders(p, source, args, keeper)
                    _parse(RenderAnswer, _payload(result, "file"))
                    if _LOST.search(result.message) is not None:
                        losses.append(result.message)
                complete = _complete(await run_in_threadpool(store.derived_of, source), role, strips)
    found = [rec for n in wanted if n in complete for rec in complete[n]]
    if wanted and not found and full:
        raise RendersFull("the renders of this file are at their limit")
    if wanted and not found and losses:
        raise _lost(losses[0])
    return found


def read_render(store: Store, rec: DerivedRecord) -> bytes | None:
    """Read a render back, whole, if it is what its row says it is.

    Args:
        store: The store.
        rec: The render, as :func:`renders_for` answered it.

    Returns:
        Its bytes, when its row is still there, its file is a regular one under ``RENDER_MAX_BYTES`` and it begins with
        the signature of its type; ``None`` otherwise, logged by the error's type or the check that failed alone,
        never by a path, a name or a byte.
    """
    try:
        with store.open_derived(rec) as src:
            data = src.read(RENDER_MAX_BYTES)
    except (StoreError, PathError, OSError) as exc:
        _log.warning("a render could not be read (%s)", type(exc).__name__)
        return None
    if len(data) >= RENDER_MAX_BYTES:
        _log.warning("a render is not what its row says (size)")
        return None
    if not signature_ok(data, rec.mime):
        _log.warning("a render is not what its row says (signature)")
        return None
    return data


def image_block(mime: str, data: bytes) -> dict[str, Any]:
    """Build the MCP image block of a render, for the model alone.

    Args:
        mime: Its type.
        data: Its bytes.

    Returns:
        The block: its bytes as base64, its type, and the assistant as its only audience.
    """
    return {
        "type": "image",
        "data": base64.b64encode(data).decode("ascii"),
        "mimeType": mime,
        "annotations": {"audience": ["assistant"]},
    }


def label(kind: Literal["image", "pdf"], n: int, strip: int, strips: int) -> str:
    """Say what a render shows.

    Args:
        kind: ``image`` or ``pdf``.
        n: The page, counted from 1.
        strip: Which strip, counted from 1.
        strips: Into how many strips the page is cut.

    Returns:
        ``image``, ``image, strip 1 of 2``, ``page 3`` or ``page 3, strip 2 of 3``.
    """
    whole = "image" if kind == "image" else f"page {n}"
    return whole if strips == 1 else f"{whole}, strip {strip} of {strips}"


def shown_image(kind: Literal["image", "pdf"], rec: DerivedRecord) -> ShownImage:
    """Describe a render that comes with an answer.

    Args:
        kind: ``image`` or ``pdf``, the file's.
        rec: The render.

    Returns:
        Its label, page, size and type.
    """
    return ShownImage.model_validate(
        {
            "label": label(kind, rec.ordinal, rec.strip, rec.strips),
            "n": rec.ordinal,
            "width": rec.width,
            "height": rec.height,
            "mime": rec.mime,
        }
    )


def page_window(part: str | None, total: int, *, default_last: int) -> tuple[int, int]:
    """Read which pages a call asks to see.

    Args:
        part: ``page N`` or ``N`` for one page; ``pages N-M`` or ``N-M`` for N to M; ``None`` for the first pages.
        total: How many pages the file has.
        default_last: The last page of the window when ``part`` is ``None``.

    Returns:
        The first and the last page: ``(1, min(default_last, total))`` for ``None``; otherwise ``(N, N)``, or
        ``(N, min(M, total))``.

    Raises:
        HTTPException: 422 ``no_such_part: …``: ``part`` is not of that grammar, its end comes before its start, or N
            is not a page of the file.
    """
    if part is None:
        return 1, min(default_last, total)
    found = _PART.fullmatch(part.strip())
    if found is None:
        raise HTTPException(status_code=422, detail=_GRAMMAR)
    first = int(found.group(1))
    last = first if found.group(2) is None else int(found.group(2))
    if last < first:
        raise HTTPException(status_code=422, detail=_GRAMMAR)
    if not 1 <= first <= total:
        noun = "page" if total == 1 else "pages"
        raise HTTPException(status_code=422, detail=f"no_such_part: this document has {total} {noun}")
    return first, min(last, total)
