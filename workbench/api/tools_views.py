"""The ``view_file`` tool: a picture, or pages of a PDF, shown to the model as images.

A picture or a PDF of the chat is prepared once (:func:`~workbench.api.renders.prepared`): its renders are derived
files of it, made in the executor, with the metadata removed. This module chooses which of them one answer shows, reads
them back checked (:func:`~workbench.api.renders.read_render`) and sends them as MCP image blocks for the model alone,
after the answer's text. It never opens an image itself.

One answer shows at most ``MAX_VIEW_IMAGES`` images and ``MAX_SHOWN_BYTES`` raw bytes, and always whole pages: a page
cut into ``k`` strips is ``k`` images, and the first page that does not fit ends the answer, whose ``next_part`` names
the rest. A page that cannot be shown is left out and named in the hint, never an error: a render that is not what its
row says, a page the store could not keep (the user's quota, a full volume, the source's cap of renders). The answer
and its blocks go out together: image block ``i`` is ``shown[i]``, in order, always.

The call is recorded as a ``query`` of the source and of each render shown, before any block is returned; a file that
is not a picture or a PDF is recorded too, with no image. If the event cannot be written the call fails and no image
leaves.
"""

import dataclasses
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Final, Literal

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from pydantic.json_schema import JsonDict

from workbench.api.audit import AuditLog
from workbench.api.renders import (
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
from workbench.api.tools import MAX_HINT, ShownImage, _capped, _join, _within
from workbench.config import Settings
from workbench.models import DerivedRecord, FileRecord, Principal

if TYPE_CHECKING:  # tools_mcp imports this module, through tool_table
    from workbench.api.tools_mcp import CallJobs

#: The last page of the window a call shows when it names no part.
DEFAULT_LAST: Final = 4

_STRING: Final[JsonDict] = {"type": "string"}
_INTEGER: Final[JsonDict] = {"type": "integer"}


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
    source's cap of renders is answered with that said, not as an error.

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
        # No page asked for exists, so the last page that has a render is the one before the window.
        [f"the renders of this file are at their limit; pages after {first - 1} cannot be shown"]
        if at_limit
        else [f"page {n} could not be shown" for n in taken.left_out]
    )
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
