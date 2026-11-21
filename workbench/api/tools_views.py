"""The tools that show pictures and pages to the model as images: ``view_file`` and ``read_document``.

A picture or a PDF of the chat is prepared once (:func:`~workbench.api.renders.prepared`): its renders are derived
files of it, made in the executor, with the metadata removed. This module chooses which of them one answer shows, reads
them back checked (:func:`~workbench.api.renders.read_render`) and sends them as MCP image blocks for the model alone,
after the answer's text. It never opens an image itself.

``view_file`` shows at most ``MAX_VIEW_IMAGES`` images and ``MAX_SHOWN_BYTES`` raw bytes in one answer, and always whole
pages: a page cut into ``k`` strips is ``k`` images, and the first page that does not fit ends the answer, whose
``next_part`` names the rest. A page that cannot be shown is left out and named in the hint, never an error: a render
that is not what its row says, a page the store could not keep (the user's quota, a full volume, the source's cap of
renders). The hint also names, each in a whole sentence, the pages after the first four of a PDF when the call named
no part, and the frames of a picture past its first: the model is told what it has not seen. The answer and its
blocks go out together: image block ``i`` is ``shown[i]``, in order, always.

``read_document`` reads the text as ever, in the executor, and adds the pictures, so that a scan costs the model no
second round, on a call that takes images (``X-Accept-Images: 1``). A call that takes none is read as workbench 0.6
read it: no image, no render, no note of one, the reader's own hint as 0.6 wrote it, and a picture file refused by
the reader. A PDF read that finds pages without a text layer attaches the renders of those pages inside its window,
under the same rules but with ``MAX_READ_IMAGES`` images; a picture file is read as its image, with no read job. The
pictures never fail a PDF read, whose text was read already: when they cannot be made (the executor has no room, the
render job is refused, the user's quota, a full volume or the source's cap of renders stops them) the text is answered
with ``shown`` empty and a hint. A picture file has no text to fall back on, and fails with them as ``view_file``
does. Pages of a Word or PowerPoint file are not rendered yet, and a read of one attaches nothing.

Every call is recorded as a ``query`` of the source and of each render shown, before any block is returned; a file that
is not a picture or a PDF is recorded too, with no image. If the event cannot be written the call fails and no image
leaves.
"""

import dataclasses
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, Literal

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from pydantic.json_schema import JsonDict

from workbench.api.audit import AuditLog
from workbench.api.jobs import Busy
from workbench.api.renders import (
    MAX_READ_IMAGES,
    MAX_SHOWN_BYTES,
    MAX_VIEW_IMAGES,
    NOT_KEPT,
    WithBlocks,
    image_block,
    page_window,
    prepared,
    read_render,
    renders_for,
    shown_image,
)
from workbench.api.store import QuotaExceeded, RendersFull, StorageFull, Store
from workbench.api.tools import (
    _RUNNER_ERRORS,
    MAX_HINT,
    DocumentRead,
    ReadDocumentRequest,
    ShownImage,
    _capped,
    _fit_read,
    _join,
    _json_size,
    _parse,
    _payload,
    _readable,
    _within,
    record_answer,
)
from workbench.config import Settings
from workbench.models import DerivedRecord, FileRecord, Principal
from workbench.renders import kind_of
from workbench.utils.audit_event import AuditWriteError

if TYPE_CHECKING:  # tools_mcp imports this module, through tool_table
    from workbench.api.tools_mcp import CallJobs

#: The last page of the window a call shows when it names no part.
DEFAULT_LAST: Final = 4

_STRING: Final[JsonDict] = {"type": "string"}
_INTEGER: Final[JsonDict] = {"type": "integer"}

#: What a read says of a picture file, which comes with the answer for the model to see.
PICTURE: Final = "This is a picture: it comes with this answer for you to see."
#: What it says when the picture could not be read back.
NO_PICTURE: Final = "This picture could not be shown."
#: What a read of a PDF says when the pictures of its pages without text could not be made now, and may be later.
UNSHOWN: Final = "The pages without text could not be shown now; view_file shows them."
#: What it says when they cannot be made at all: the cause is named, and view_file is not promised.
AT_LIMIT: Final = "The pages without text cannot be shown: the renders of this file are at their limit."
OVER_QUOTA: Final = "The pages without text cannot be shown: the workspace quota is exceeded."
FULL: Final = "The pages without text cannot be shown: storage is full."
UNDRAWABLE: Final = "The pages without text cannot be shown: the file cannot be drawn."

#: The refusals of a runner that may pass on another try: they get ``UNSHOWN``, not ``UNDRAWABLE``.
_TRANSIENT: Final = frozenset({"read_timeout"})

#: A page as the PDF reader labels it.
_PAGE: Final = re.compile(r"page (\d{1,9})")


class ViewFileRequest(BaseModel):
    """Body of ``view_file``.

    Attributes:
        path: The file's absolute path.
        part: Which pages to show; left out, the picture or pages 1-4.
        strips: Into how many strips each page is cut.
    """

    path: str = Field(
        min_length=1,
        max_length=1024,
        description="The file's absolute path, for example /workspace/<chat>/report.pdf, as list_files gives it.",
    )
    part: str | None = Field(
        default=None,
        description=(
            "Which pages to show: 'page 3' shows page 3, 'pages 2-5' pages 2 to 5; left out: the picture, or pages 1-4."
        ),
        json_schema_extra=_STRING,
    )
    strips: int | None = Field(default=None, ge=1, le=3, description="Leave it out.", json_schema_extra=_INTEGER)


class ViewedFile(BaseModel):
    """What ``view_file`` answers; the images themselves come as the blocks after its text.

    Attributes:
        path: The file's path.
        kind: ``image``, ``pdf``, or ``none`` for a file that is not shown as pictures.
        total: How many pages the file has; 1 for a picture, 0 for ``none``.
        shown: The images that come with this answer, in order: the first is the first image block after the text.
        truncated: Whether pages were left out of this answer for want of room.
        next_part: Where to continue when it was cut.
        hint: Pages that could not be shown, or what to do instead.
    """

    path: str = Field(description="The file's path.")
    kind: Literal["image", "pdf", "none"] = Field(
        description="image, pdf, or none for a file that is not shown as pictures."
    )
    total: int = Field(description="How many pages the file has; 1 for a picture, 0 for none.")
    shown: list[ShownImage] = Field(
        description="The images that come with this answer, in order: the first is the first image after the text."
    )
    truncated: bool = Field(description="True if pages were left out for want of room; then call again with next_part.")
    next_part: str | None = Field(
        default=None, max_length=300, description="Where to continue when truncated, such as 'pages 9-12'."
    )
    hint: str | None = Field(
        default=None, max_length=MAX_HINT, description="Pages that could not be shown, or what to do instead."
    )


def _read_page(store: Store, page: list[DerivedRecord]) -> list[bytes] | None:
    """Read every strip of a page back, checked.

    Args:
        store: The store.
        page: The page's renders, by strip.

    Returns:
        Their bytes in the same order; ``None`` when one of them is not what its row says, so that a page is shown whole
        or not at all.
    """
    data: list[bytes] = []
    for rec in page:
        one = read_render(store, rec)
        if one is None:
            return None
        data.append(one)
    return data


@dataclasses.dataclass
class _Taken:
    """The pages one answer takes, whole, in order.

    Attributes:
        renders: Their renders, in the order of the image blocks.
        blocks: The image blocks, one per render.
        left_out: The pages asked for that could not be shown: no render, or one that is not what its row says.
        cut_at: The first page that did not fit, or that one answer may not take; ``None`` when the window ended first.
    """

    renders: list[DerivedRecord] = dataclasses.field(default_factory=list)
    blocks: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    left_out: list[int] = dataclasses.field(default_factory=list)
    cut_at: int | None = None


async def _take(store: Store, pages: Mapping[int, list[DerivedRecord]], first: int, last: int, through: int) -> _Taken:
    """Take whole pages of the window in order, while the images and their bytes stay within what one answer shows.

    Args:
        store: The store.
        pages: The renders there are, by page, each page's strips in order.
        first: The window's first page.
        last: The window's last page.
        through: The last page one answer may take (:data:`MAX_VIEW_IMAGES` images, whole pages).

    Returns:
        The pages taken. The first page that does not fit ends it; a page that cannot be read back is left out.
    """
    taken = _Taken()
    size = 0
    for n in range(first, last + 1):
        if n > through:
            taken.cut_at = n
            break
        data = await run_in_threadpool(_read_page, store, pages[n]) if n in pages else None
        if data is None:
            taken.left_out.append(n)
            continue
        weight = sum(len(one) for one in data)
        if len(taken.renders) + len(data) > MAX_VIEW_IMAGES or size + weight > MAX_SHOWN_BYTES:
            taken.cut_at = n
            break
        size += weight
        taken.renders.extend(pages[n])
        taken.blocks.extend(image_block(rec.mime, one) for rec, one in zip(pages[n], data, strict=True))
    return taken


def _part(first: int, last: int) -> str:
    """Write the pages ``first`` to ``last`` as a ``part`` the tool takes.

    Args:
        first: The first page.
        last: The last page.

    Returns:
        ``page 9`` or ``pages 9-12``.
    """
    return f"page {first}" if first == last else f"pages {first}-{last}"


def _not_shown_after(last: int, total: int) -> str:
    """Say which pages after the window a call named no part for are not shown, and how to see them.

    Args:
        last: The window's last page.
        total: The file's pages, more than ``last``.

    Returns:
        A whole sentence: ``Pages 5-30 are not shown; call view_file with part "pages 5-8".``
    """
    rest = f"Page {last + 1} is" if last + 1 == total else f"Pages {last + 1}-{total} are"
    window = _part(last + 1, min(total, last + DEFAULT_LAST))
    return f'{rest} not shown; call view_file with part "{window}".'


def _record(
    audit: AuditLog, p: Principal, source: FileRecord, shown: list[DerivedRecord], strips: int, *, paged: bool
) -> None:
    """Record the call, naming the source and each render shown.

    Args:
        audit: The audit log, bound to the call.
        p: The caller, user and chat of the source.
        source: The file.
        shown: The renders that go out with the answer, in order.
        strips: Into how many strips each page was cut.
        paged: Whether the call named a part.

    Raises:
        AuditWriteError: The event could not be written; nothing may go out.
    """
    criteria = {"images": len(shown), "strips": strips, "paged": paged}
    audit.record("view_file", p, file_ids=[source.id, *(rec.id for rec in shown)], criteria=criteria)


async def view_file(
    body: ViewFileRequest,
    p: Principal,
    settings: Settings,
    store: Store,
    audit: AuditLog,
    jobs: "CallJobs",
) -> ViewedFile | WithBlocks:
    """Show a picture, or pages of a PDF, of the chat as images.

    The file is prepared on its first view; the pages asked for are made when they are not there yet. A window that
    gives more than eight images (``8 // strips`` pages) is shown in part, and ``next_part`` names where the rest
    begins. A page whose render could not be read back or kept is named in the hint, and a window wholly past the
    source's cap of renders is answered with that said, not as an error. The hint says too, in whole sentences, which
    pages a call that named no part does not show (those after the first four of a PDF), and that only the first frame
    of a picture with several is shown.

    Args:
        body: The path, the pages, and into how many strips each is cut.
        p: The caller, user and chat.
        settings: The api's settings; ``result_max_kb`` caps the answer's text.
        store: The file store.
        audit: The audit log, bound to the call.
        jobs: The queue, within the call's limits.

    Returns:
        The answer alone for a file that is not shown or a window with no image; otherwise the answer with the image
        blocks that follow its ``shown``.

    Raises:
        HTTPException: The file cannot be rendered (the runner's refusal), or ``part`` is not a page of it
            (``no_such_part``).
        PathError: The path is outside the chat.
        StoreError: The file does not exist, or is not the chat's.
        PurposeRequired: The call had to state a purpose for ``modify`` and did not.
        AuditWriteError: An event could not be written; no image goes out.
        Busy: The lock or the queue did not come free in time, or the executor is busy.
    """
    source = await run_in_threadpool(store.get, p, body.path)
    manifest = await prepared(p, source, settings=settings, store=store, audit=audit, jobs=jobs)
    strips = body.strips or 1
    paged = body.part is not None
    kind = manifest.kind
    limit = settings.result_max_kb * 1024
    if kind == "none":
        await run_in_threadpool(_record, audit, p, source, [], strips, paged=paged)
        nothing = ViewedFile(
            path=source.virtual_path,
            kind="none",
            total=0,
            shown=[],
            truncated=False,
            next_part=None,
            hint=_capped(manifest.hint, MAX_HINT),
        )
        return _within(nothing, limit, keep_none=True)

    first, last = page_window(body.part, manifest.total, default_last=DEFAULT_LAST)
    through = min(last, first + MAX_VIEW_IMAGES // strips - 1)  # the pages one answer may take
    at_limit = False
    rows: list[DerivedRecord] = []
    try:
        rows = await renders_for(
            p,
            source,
            manifest,
            list(range(first, through + 1)),
            strips,
            settings=settings,
            store=store,
            audit=audit,
            jobs=jobs,
        )
    except RendersFull:
        at_limit = True
    except (QuotaExceeded, StorageFull):
        pass  # no page of the window could be kept: each is named below
    except HTTPException as lost:
        if lost.status_code != 500 or lost.detail != NOT_KEPT:
            raise

    pages: dict[int, list[DerivedRecord]] = {}
    for rec in rows:
        pages.setdefault(rec.ordinal, []).append(rec)
    taken = await _take(store, pages, first, last, through)
    await run_in_threadpool(_record, audit, p, source, taken.renders, strips, paged=paged)
    notes = (
        # No page asked for exists, and nothing more can be made.
        [f"The renders of this file are at their limit, so {_part(first, last)} cannot be shown."]
        if at_limit
        else _notes([], taken.left_out, unshown=None)
    )
    if not at_limit and not paged and last < manifest.total:
        notes.append(_not_shown_after(last, manifest.total))
    if kind == "image" and manifest.frames is not None and taken.renders:
        notes.append(f"Only the first of {manifest.frames} frames is shown.")
    # Nothing more can be made at the source's limit, so a rest there is not offered.
    rest = None if at_limit or taken.cut_at is None else _part(taken.cut_at, last)
    answer = ViewedFile(
        path=source.virtual_path,
        kind=kind,
        total=manifest.total,
        shown=[shown_image(kind, rec) for rec in taken.renders],
        truncated=rest is not None,
        next_part=rest,
        hint=_capped(_join(*notes), MAX_HINT),
    )
    answer = _within(answer, limit, keep_none=True)
    return WithBlocks(answer, taken.blocks) if taken.blocks else answer


# --- read_document: the pictures that come with a read ------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Attached:
    """The images one read of a PDF attaches, and what became of the pages it asked for.

    Attributes:
        shown: The renders that go with the answer and their bytes, in the order of the image blocks.
        left_out: The pages asked for whose renders could not be made or read back.
        cut: The first page that did not fit, or that one answer may not take, where the answer ends and ``next_part``
            begins; ``None`` when every page of the window was taken.
        unshown: What to tell the model when the renders could not be made at all, so that the text goes with no
            image; ``None`` when they could.
    """

    shown: list[tuple[DerivedRecord, bytes]] = dataclasses.field(default_factory=list)
    left_out: list[int] = dataclasses.field(default_factory=list)
    cut: int | None = None
    unshown: str | None = None


def _page_of(label: str | None) -> int | None:
    """Read the page a part or a ``next_part`` of a PDF names.

    Args:
        label: ``page 3``, or ``None``.

    Returns:
        The page, or ``None`` when the label is none or is not that of a page.
    """
    found = None if label is None else _PAGE.fullmatch(label)
    return None if found is None else int(found.group(1))


def _pages(numbers: Sequence[int]) -> str:
    """Name pages in a sentence.

    Args:
        numbers: The pages, in order; at least one.

    Returns:
        ``Page 2`` or ``Pages 2, 5``.
    """
    return f"Page {numbers[0]}" if len(numbers) == 1 else f"Pages {', '.join(str(n) for n in numbers)}"


def _cut_note(cut: int) -> str:
    """Say that the images, not the text, ended the answer, and where to go on.

    Args:
        cut: The first page that did not come with this answer.

    Returns:
        A whole sentence, which the text's own cut at the result limit does not use.
    """
    return (
        f"The answer is cut before page {cut} to keep its images within the limit. "
        f'Call read_document again with part="page {cut}" to continue.'
    )


def _unshown_because(lost: Exception) -> str:
    """Tell the model why the pictures of a read could not be made, and whether view_file can show them later.

    Args:
        lost: What stopped the renders: the executor having no room, a refusal of the render job, or a cap of the store.

    Returns:
        A whole sentence. ``UNSHOWN`` for a cause that may pass (no room now, a job or its runner that ran out of time,
        a job that ran out of memory, a render the store could not keep); one that names the cause, and promises
        nothing, for a cause that will not (the source's cap of renders, the user's quota, a full volume, a file the
        renderer refuses).
    """
    if isinstance(lost, RendersFull):
        return AT_LIMIT
    if isinstance(lost, QuotaExceeded):
        return OVER_QUOTA
    if isinstance(lost, StorageFull):
        return FULL
    if isinstance(lost, HTTPException):
        if lost.status_code == 507:
            return FULL
        code = str(lost.detail).partition(": ")[0]
        refused = lost.status_code in (404, 422) and code in _RUNNER_ERRORS and code not in _TRANSIENT
        return UNDRAWABLE if refused else UNSHOWN
    return UNSHOWN


def _notes(shown: Sequence[DerivedRecord], left_out: Sequence[int], *, unshown: str | None) -> list[str]:
    """Say what a read attached, each note a whole sentence.

    Args:
        shown: The renders that come with the answer.
        left_out: The pages asked for that could not be shown.
        unshown: Why the renders could not be made at all (:func:`_unshown_because`), or ``None``.

    Returns:
        The notes, in the order they are told: the pages that come as images, then those that could not be shown.
    """
    notes: list[str] = []
    if shown:
        pages = list(dict.fromkeys(rec.ordinal for rec in shown))
        verb, noun = ("comes", "an image") if len(pages) == 1 else ("come", "images")
        notes.append(f"{_pages(pages)} {verb} with this answer as {noun}.")
    if left_out:
        notes.append(f"{_pages(left_out)} could not be shown.")
    if unshown:
        notes.append(unshown)
    return notes


def _record_read(
    audit: AuditLog,
    p: Principal,
    source: FileRecord,
    shown: Sequence[DerivedRecord],
    *,
    paged: bool,
    outcome: str = "ok",
) -> None:
    """Record a read, naming the source and each render shown.

    Args:
        audit: The audit log, bound to the call.
        p: The caller, user and chat of the source.
        source: The file.
        shown: The renders that go out with the answer, in order.
        paged: Whether the call named a part.
        outcome: ``ok``, or ``error`` for a read that was answered by the job and then failed.

    Raises:
        AuditWriteError: The event could not be written; nothing may go out.
    """
    audit.record(
        "read_document",
        p,
        file_ids=[source.id, *(rec.id for rec in shown)],
        outcome=outcome,
        criteria={"paged": paged, "images": len(shown)},
    )


def _record_aborted(audit: AuditLog, p: Principal, source: FileRecord, failed: BaseException, *, paged: bool) -> None:
    """Record, as an ``error``, a read whose job answered and whose pictures then failed the call.

    It is a plain call, not a thread's: a call that is being cancelled cannot wait for one, and it must not be held up
    by a failure of its own trail either, so a cancellation goes on as one whatever the event does.

    Args:
        audit: The audit log, bound to the call.
        p: The caller, user and chat of the source.
        source: The file.
        failed: What stopped the call; an :class:`AuditWriteError` is the trail's own failure and is not written about.
        paged: Whether the call named a part.

    Raises:
        AuditWriteError: The event could not be written, and ``failed`` is an ordinary exception: the trail is what
            the call fails with.
    """
    if isinstance(failed, AuditWriteError):
        return
    try:
        _record_read(audit, p, source, [], paged=paged, outcome="error")
    except Exception:
        if isinstance(failed, Exception):
            raise


async def _attach_pages(
    answer: DocumentRead,
    source: FileRecord,
    strips: int,
    *,
    p: Principal,
    settings: Settings,
    store: Store,
    audit: AuditLog,
    jobs: "CallJobs",
) -> _Attached:
    """Choose the images that go with a read of a PDF, and make the renders they need.

    The pages are those of ``pages_without_text`` inside the window the text was read for (before the page the text was
    cut at). Whole pages are taken in order while they fit ``MAX_READ_IMAGES`` images and ``MAX_SHOWN_BYTES`` bytes; the
    first page always goes, and one page is at most three images, so paging always moves on. Only as many pages are made
    as can be shown by count. A page that was made but cannot be read back, or could not be made, is left out and
    named; the page after the last one taken is where the answer ends.

    Args:
        answer: What the read answered, validated.
        source: The PDF.
        strips: Into how many strips each page is cut.
        p: The caller, user and chat of the source.
        settings: The api's settings.
        store: The store.
        audit: The audit log, bound to the call.
        jobs: The queue, within the call's limits.

    Returns:
        The images, the pages left out and where the answer ends. When the renders cannot be made (the executor has no
        room, the render job is refused, or the user's quota, the volume or the source's cap of renders stops them), no
        image and ``unshown`` saying why: the read is not failed over its pictures.

    Raises:
        PurposeRequired: The call had to state a purpose for ``modify`` (the prepare) and did not.
        AuditWriteError: A render job's event could not be written.
        HTTPException: A render job failed in a way that is not a refusal or a loss of the renders (500).
    """
    cut_at = _page_of(answer.next_part) if answer.truncated else None
    candidates = [n for n in sorted(set(answer.pages_without_text)) if cut_at is None or n < cut_at]
    if not candidates:
        return _Attached()
    wanted = candidates[: max(1, MAX_READ_IMAGES // strips)]  # no later page can fit by count
    try:
        manifest = await prepared(p, source, settings=settings, store=store, audit=audit, jobs=jobs)
        made = await renders_for(
            p, source, manifest, wanted, strips, settings=settings, store=store, audit=audit, jobs=jobs
        )
    except (Busy, RendersFull, QuotaExceeded, StorageFull) as lost:
        return _Attached(unshown=_unshown_because(lost))
    except HTTPException as lost:
        if lost.status_code == 500 and lost.detail != NOT_KEPT:
            raise  # a refusal of the runner is the pictures' own; a failure of the tool is the call's
        return _Attached(unshown=_unshown_because(lost))
    by_page: dict[int, list[DerivedRecord]] = {}
    for rec in made:
        by_page.setdefault(rec.ordinal, []).append(rec)
    shown: list[tuple[DerivedRecord, bytes]] = []
    left_out: list[int] = []
    used = 0
    for n in candidates:
        page: list[tuple[DerivedRecord, bytes]] | None = None  # not looked at: it did not fit by count
        if n in wanted:
            data = await run_in_threadpool(_read_page, store, by_page[n]) if n in by_page else None
            page = [] if data is None else list(zip(by_page[n], data, strict=True))  # [] when it could not be made
        size = sum(len(one) for _, one in page or [])
        if page is None or (shown and (len(shown) + len(page) > MAX_READ_IMAGES or used + size > MAX_SHOWN_BYTES)):
            return _Attached(shown, left_out, n)
        if page:
            shown.extend(page)
            used += size
        else:
            left_out.append(n)
    return _Attached(shown, left_out)


async def _read_pdf(
    body: ReadDocumentRequest,
    source: FileRecord,
    payload: Mapping[str, Any],
    *,
    p: Principal,
    settings: Settings,
    store: Store,
    audit: AuditLog,
    jobs: "CallJobs",
) -> DocumentRead | WithBlocks:
    """Answer a read of a PDF that was read, with the images of its pages without text.

    The text is cut to the result limit as it is for any file. The images are chosen first for the window the executor
    read; the answer then ends before the first page they did not take (and says it was the images), the cut to the
    result limit may end it earlier, and the images of a page the text no longer reaches wait for the call that reads
    on, as do the pages named in ``pages_without_text`` from where it ends. The notes about the images are added after
    the text is cut, and room is kept for them first, so that they are never cut themselves. The read is recorded once,
    when the images are settled; when the pictures fail the call it is recorded as an ``error`` first.

    Args:
        body: The path, the part and the strips.
        source: The PDF.
        payload: What the read job answered, with the store's path; it reads as a :class:`DocumentRead`.
        p: The caller, user and chat.
        settings: The api's settings; ``result_max_kb`` caps the answer's text.
        store: The file store.
        audit: The audit log, bound to the call.
        jobs: The queue, within the call's limits.

    Returns:
        The answer alone when no image goes with it, otherwise the answer with its image blocks.

    Raises:
        HTTPException: The payload is not a document read (500), or a render job failed in a way that is not a
            refusal or a loss of the renders (500).
        PurposeRequired: The call had to state a purpose for the renders it makes and did not.
        AuditWriteError: An event could not be written; no image goes out.
        BaseException: What stopped the pictures, as it was, after the read is recorded as an ``error``.
    """
    read = _parse(DocumentRead, payload)
    paged = body.part is not None
    try:
        attached = (
            await _attach_pages(
                read, source, body.strips or 1, p=p, settings=settings, store=store, audit=audit, jobs=jobs
            )
            if read.pages_without_text
            else _Attached()
        )
    except BaseException as failed:
        _record_aborted(audit, p, source, failed, paged=paged)  # the job answered: the trail says the call failed
        raise
    cut = attached.cut
    base = read
    if cut is not None:
        kept = [part for part in read.parts if (page := _page_of(part.label)) is None or page < cut]
        # What the executor said of its own cut no longer holds: the text ends before the images do. It is told again.
        base = read.model_copy(update={"parts": kept, "truncated": False, "next_part": None})
    base = base.model_copy(update={"shown": [shown_image("pdf", rec) for rec, _ in attached.shown]})
    # The notes come after the text is cut, whole: room is kept for the longest they can be, which is all of them.
    full = base.model_copy(
        update={
            "truncated": base.truncated or cut is not None,
            "next_part": base.next_part if cut is None else f"page {cut}",
            "hint": _join(
                base.hint,
                None if cut is None else _cut_note(cut),
                *_notes([rec for rec, _ in attached.shown], attached.left_out, unshown=attached.unshown),
            ),
        }
    )
    answer = _fit_read(base, settings.result_max_kb, reserve=_json_size(full) - _json_size(base))
    ended_by_images = cut is not None and (not answer.truncated or answer.next_part is None)
    if ended_by_images:
        answer = answer.model_copy(update={"truncated": True, "next_part": f"page {cut}"})
    stop = _page_of(answer.next_part) if answer.truncated else None
    kept_images = [(rec, one) for rec, one in attached.shown if stop is None or rec.ordinal < stop]
    shown = [rec for rec, _ in kept_images]
    notes = _notes(shown, [n for n in attached.left_out if stop is None or n < stop], unshown=attached.unshown)
    answer = answer.model_copy(
        update={
            "shown": [shown_image("pdf", rec) for rec in shown],
            "pages_without_text": [n for n in answer.pages_without_text if stop is None or n < stop],
            "hint": _join(answer.hint, _cut_note(cut) if ended_by_images and cut is not None else None, *notes),
        }
    )
    answer = _within(answer, settings.result_max_kb * 1024)
    await run_in_threadpool(_record_read, audit, p, source, shown, paged=paged)
    blocks = [image_block(rec.mime, one) for rec, one in kept_images]
    return WithBlocks(answer, blocks) if blocks else answer


async def _read_picture(
    body: ReadDocumentRequest,
    source: FileRecord,
    *,
    p: Principal,
    settings: Settings,
    store: Store,
    audit: AuditLog,
    jobs: "CallJobs",
) -> DocumentRead | WithBlocks:
    """Answer a read of a picture file with the picture, which is the whole of its content.

    Args:
        body: The path and the strips.
        source: The picture.
        p: The caller, user and chat.
        settings: The api's settings.
        store: The file store.
        audit: The audit log, bound to the call.
        jobs: The queue, within the call's limits.

    Returns:
        The answer with its image blocks; the answer alone if the picture could not be read back.

    Raises:
        HTTPException: The picture cannot be rendered (the runner's refusal), or a render job failed (500).
        QuotaExceeded: The render would take the user over their quota (507).
        RendersFull: The render would take the file over its cap of renders (507).
        StorageFull: The volume is full (507).
        Busy: The queue or the executor has no room.
        PurposeRequired: The call had to state a purpose for the render and did not.
        AuditWriteError: An event could not be written; no image goes out.
    """
    strips = body.strips or 1
    manifest = await prepared(p, source, settings=settings, store=store, audit=audit, jobs=jobs)
    made = await renders_for(p, source, manifest, [1], strips, settings=settings, store=store, audit=audit, jobs=jobs)
    data = await run_in_threadpool(_read_page, store, made) if made else None
    pictures: list[tuple[DerivedRecord, bytes]] = [] if data is None else list(zip(made, data, strict=True))
    notes = [PICTURE if pictures else NO_PICTURE]
    if pictures and manifest.frames is not None:
        notes.append(f"Only the first of {manifest.frames} frames is shown.")
    answer = DocumentRead(
        path=source.virtual_path,
        kind="image",
        parts=[],
        total_parts=1,
        pages_without_text=[1],
        shown=[shown_image("image", rec) for rec, _ in pictures],
        truncated=False,
        hint=_join(*notes),
    )
    answer = _within(answer, settings.result_max_kb * 1024)
    await run_in_threadpool(_record_read, audit, p, source, [rec for rec, _ in pictures], paged=body.part is not None)
    blocks = [image_block(rec.mime, one) for rec, one in pictures]
    return WithBlocks(answer, blocks) if blocks else answer


async def read_document(
    body: ReadDocumentRequest,
    p: Principal,
    settings: Settings,
    store: Store,
    audit: AuditLog,
    jobs: "CallJobs",
    images: bool,
) -> DocumentRead | WithBlocks:
    """Read one of the user's files, and show the pictures it holds that have no text.

    A file that is not a picture is read in the executor, in the request's chat folder, with that one file as its input,
    and answered with its text cut to the result limit. A PDF with pages that have no text layer is answered with the
    renders of those pages inside its window as images, in the same answer. A picture file is not read but shown: it is
    prepared as ``view_file`` does it, and answered as its image. On a call that takes no images every file is read in
    the executor and answered with its text alone, ``strips`` ignored, as workbench 0.6 did. The read is recorded by the
    file's id and then each render shown, once the images are settled and before anything of the answer goes out; a
    refusal of the job is an ``error`` with its code. Neither the name, the part nor the text is recorded.

    Args:
        body: The path, where to start, and into how many strips each image is cut.
        p: The caller, user and chat.
        settings: The api's settings; ``result_max_kb`` caps the answer.
        store: The file store.
        audit: The audit log.
        jobs: The queue, within the call's limits.
        images: Whether the call takes images (``X-Accept-Images: 1``).

    Returns:
        The parts read, the pages without text, the images that come with them and how to go on; the answer alone when
        no image goes with it.

    Raises:
        HTTPException: The runner refused the file, or a job failed; a picture file's render was refused.
        PurposeRequired: The call had to state a purpose for the renders it makes and did not.
        AuditWriteError: An event could not be written; nothing goes out.
        Busy: The queue or the executor has no room for the read, or for a picture's render.
    """
    source = await run_in_threadpool(store.get, p, body.path)
    kind = kind_of(source.name) if images else "none"  # a call that takes no images is read as text alone
    if kind == "image":
        return await _read_picture(body, source, p=p, settings=settings, store=store, audit=audit, jobs=jobs)
    budget = max(1, settings.job_wall_s * 3 // 4)
    args = {"path": source.name, "part": body.part, "budget_s": budget, "images": images}
    _, result, _ = await jobs.submit(p, "read_document", args, [source.name])
    payload = {**result.result, "path": source.virtual_path} if isinstance(result.result, dict) else None
    if kind == "pdf" and result.status == "ok" and payload is not None and _readable(DocumentRead, payload):
        return await _read_pdf(body, source, payload, p=p, settings=settings, store=store, audit=audit, jobs=jobs)
    usable = result.status == "ok" and payload is not None and _readable(DocumentRead, payload)
    criteria: dict[str, object] = {"paged": body.part is not None, **({"images": 0} if usable else {})}
    await run_in_threadpool(
        record_answer, audit, "read_document", p, [source.id], criteria, result, DocumentRead, payload
    )
    _payload(result, "file")
    return _fit_read(_parse(DocumentRead, payload), settings.result_max_kb)
