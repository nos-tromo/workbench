"""The runner of ``prepare_file``: a picture or the pages of a PDF, drawn into the files the api serves.

The job's working folder is the chat folder, as for ``read_document``. A picture is brought to the bounds of a render
(:mod:`workbench.executor.render.images`) and a PDF page is drawn at ``PAGE_LONG_SIDE``
(:mod:`workbench.executor.render.pages`); either is encoded without metadata and, when strips are asked for, cut
along its long side from the picture as it is, so no render is decoded twice and no JPEG generation is lost. Every
render is one file in ``out/`` named as :func:`workbench.renders.render_name` names it, and the answer is a
:class:`workbench.renders.RenderAnswer` as JSON: no path, no file name, and nothing of the source's own metadata.

Nothing is written until every render is made, so a failure leaves ``out/`` as it was. The imaging libraries load
on first use, so a job that reads a spreadsheet does not import them.
"""

import io
import json
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from workbench import protocol
from workbench.executor import readers
from workbench.executor.context import JobContext
from workbench.executor.runners.documents import error
from workbench.executor.timer import limit_until
from workbench.renders import (
    FIRST_PAGES,
    IMAGE_QUALITY,
    MAX_STRIPS,
    PAGE_QUALITY,
    PAGES_CHECKED_MAX,
    RENDERS_PER_JOB,
    RenderAnswer,
    RenderEntry,
    kind_of,
    render_name,
)

_BUDGET_S: Final = 60.0

#: Pillow's JPEG chroma subsampling: 0 is 4:4:4 (pictures), 2 is 4:2:0 (pages, which are mostly text and lines).
_PICTURE_SUBSAMPLING: Final = 0
_PAGE_SUBSAMPLING: Final = 2


@dataclass(frozen=True)
class _Request:
    """What the caller asked for, checked.

    Attributes:
        pages: The pages to draw, or ``None`` for the first ones.
        strips: How many strips each is cut into, 1 to ``MAX_STRIPS``.
        have: The whole pages the caller already has.
        facts: Whether to look for the pages of a PDF with no text; ``False`` skips the check and its cost.
    """

    pages: list[int] | None
    strips: int
    have: frozenset[int]
    facts: bool

    @classmethod
    def of(cls, args: dict[str, Any]) -> "_Request":
        """Read the arguments of a job.

        Args:
            args: ``pages``, ``strips``, ``have`` and ``facts`` of the job; each may be missing.

        Returns:
            The request.

        Raises:
            ValueError: ``strips`` is not 1 to ``MAX_STRIPS``, or ``facts`` is not a bool; that is the caller's
                mistake, never the file's.
        """
        pages = args.get("pages")
        strips = int(args.get("strips", 1))
        if not 1 <= strips <= MAX_STRIPS:
            raise ValueError(f"strips is 1 to {MAX_STRIPS}")
        facts = args.get("facts", True)
        if not isinstance(facts, bool):
            raise ValueError("facts is true or false")
        return cls(
            pages=None if pages is None else [int(n) for n in pages],
            strips=strips,
            have=frozenset(int(n) for n in args.get("have") or []),
            facts=facts,
        )

    def wanted(self, total: int) -> list[int]:
        """Pick the pages to draw.

        Args:
            total: How many pages the document has.

        Returns:
            Those asked for, or the first ``FIRST_PAGES``, in order and once each; less those the caller has when
            whole pages are asked for (a strip is not a whole page, so the pages it has do not count then).

        Raises:
            readers.NoSuchPart: A page asked for is not in the document.
            ValueError: More renders than one job may make; that is the caller's mistake, never the file's.
        """
        if self.pages is None:
            numbers = list(range(1, min(total, FIRST_PAGES) + 1))
        else:
            numbers = sorted(set(self.pages))
            if numbers and (numbers[0] < 1 or numbers[-1] > total):
                raise readers.NoSuchPart(f"this document has {total} pages")
        if self.strips == 1:
            numbers = [n for n in numbers if n not in self.have]
        if len(numbers) * self.strips > RENDERS_PER_JOB:
            raise ValueError(f"too many renders for one job: at most {RENDERS_PER_JOB}")
        return numbers


@dataclass(frozen=True)
class _Made:
    """One render, encoded, not yet written.

    Attributes:
        n: The page, counted from 1; 1 for a picture.
        strip: Which strip, counted from 1.
        strips: Out of how many.
        mime: The type of ``data``.
        data: The encoded picture.
        width: Its width, read from its header.
        height: Its height.
    """

    n: int
    strip: int
    strips: int
    mime: str
    data: bytes
    width: int
    height: int


@dataclass(frozen=True)
class _Facts:
    """What a file turned out to be, besides its renders.

    Attributes:
        kind: ``image`` or ``pdf``.
        total: Pictures are 1; for a PDF, its pages.
        blank: The pages of a PDF with no text.
        unchecked: Whether pages of a PDF past ``PAGES_CHECKED_MAX`` were left unchecked.
        frames: The frame count of a picture with several, else ``None``.
        made: The renders.
    """

    kind: str
    total: int
    blank: list[int]
    unchecked: bool
    frames: int | None
    made: list[_Made]


def _encoded(n: int, strip: int, strips: int, data: bytes, mime: str) -> _Made:
    """Note an encoded render with the size its header gives, which is the size it was written at.

    Args:
        n: The page.
        strip: Which strip.
        strips: Out of how many.
        data: The encoded picture.
        mime: Its type.

    Returns:
        The render.
    """
    from PIL import Image

    with Image.open(io.BytesIO(data)) as header:
        width, height = header.size
    return _Made(n=n, strip=strip, strips=strips, mime=mime, data=data, width=width, height=height)


def _picture(path: Path, request: _Request) -> _Facts:
    """Make the renders of a picture.

    Args:
        path: The file.
        request: What was asked for.

    Returns:
        The facts and the renders: none when the caller has the whole picture and asks for no strips.

    Raises:
        readers.NoSuchPart: A page other than 1 was asked for.
        readers.UnreadableFile: The picture cannot be decoded or made small enough.
        readers.ReadTimeout: The deadline passed.
    """
    from workbench.executor.render import encode, images

    if request.pages is not None and request.pages != [1]:
        raise readers.NoSuchPart("a picture has one page")
    normalised = images.normalise(path)
    made: list[_Made] = []
    if request.wanted(1):
        parts = [normalised.image] if request.strips == 1 else encode.cut(normalised.image, request.strips)
        for strip, part in enumerate(parts, 1):
            readers.check()
            data, mime = encode.encode(
                part, lossless=normalised.lossless, quality=IMAGE_QUALITY, subsampling=_PICTURE_SUBSAMPLING
            )
            made.append(_encoded(1, strip, request.strips, data, mime))
    frames = normalised.frames if normalised.frames > 1 else None
    return _Facts(kind="image", total=1, blank=[], unchecked=False, frames=frames, made=made)


def _pdf(path: Path, request: _Request) -> _Facts:
    """Make the renders of the pages of a PDF, and find the pages with no text.

    Args:
        path: The file.
        request: What was asked for.

    Returns:
        The facts and the renders.

    Raises:
        readers.NoSuchPart: A page asked for is not in the document.
        readers.UnreadableFile: The PDF is encrypted or damaged, or a page cannot be drawn or encoded.
        readers.ReadTimeout: The deadline passed.
    """
    from workbench.executor.render import encode, pages

    pdf = pages.open_pdf(path)
    try:
        total = len(pdf)
        wanted = request.wanted(total)
        blank: list[int] = []
        unchecked = False
        if request.facts:
            blank, unchecked = pages.pages_without_text(pdf, PAGES_CHECKED_MAX)
        made: list[_Made] = []
        for n in wanted:
            readers.check()
            page = pages.render_page(pdf, n)
            parts = [page] if request.strips == 1 else encode.cut(page, request.strips)
            for strip, part in enumerate(parts, 1):
                readers.check()
                data, mime = encode.encode(part, lossless=False, quality=PAGE_QUALITY, subsampling=_PAGE_SUBSAMPLING)
                made.append(_encoded(n, strip, request.strips, data, mime))
    finally:
        pdf.close()
    return _Facts(kind="pdf", total=total, blank=blank, unchecked=unchecked, frames=None, made=made)


def _hint(notes: list[str]) -> str | None:
    """Join what the model should be told.

    Args:
        notes: The sentences, without a closing full stop.

    Returns:
        Them joined, or ``None`` if there are none.
    """
    return "; ".join(notes) or None


def _fit(answer: dict[str, Any], blank: list[int], notes: list[str], max_chars: int) -> dict[str, Any]:
    """Cut the list of pages without text so that the answer, as the job writes it, stays within ``max_chars``.

    The renders and the other facts are kept whole; only the list gives way, and the hint says where it stops.

    Args:
        answer: The answer with the whole list and the hint of ``notes``.
        blank: The whole list.
        notes: The hint's sentences, which a cut adds to.
        max_chars: The most characters the answer may take.

    Returns:
        ``answer`` when it fits; otherwise it with the longest list that fits, which may be empty.
    """

    def with_first(count: int) -> dict[str, Any]:
        last = blank[count - 1] if count else 0
        cut = [*notes, f"pages without text are listed up to page {last}"]
        return {**answer, "pages_without_text": blank[:count], "hint": _hint(cut)}

    if not blank or len(json.dumps(answer)) <= max_chars:
        return answer
    low, high = 0, len(blank) - 1  # the largest count whose answer fits; the whole list does not
    while low < high:
        middle = (low + high + 1) // 2
        if len(json.dumps(with_first(middle))) <= max_chars:
            low = middle
        else:
            high = middle - 1
    return with_first(low)


def prepare_file(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Draw a picture or the pages of a PDF into ``out/``, for a model to look at.

    Args:
        args: ``path``, relative to the chat folder; ``pages``, the pages to draw or ``None`` for the first
            ``FIRST_PAGES`` (a picture accepts only ``None`` or ``[1]``); ``strips``, 1 to ``MAX_STRIPS``, how many
            strips along the long side each is cut into; ``have``, the whole pages already rendered, which are not
            drawn again when ``strips`` is 1; ``facts``, a bool, default true: ``False`` leaves out the look at every
            page of a PDF for text (the caller has that already), so ``pages_without_text`` is ``[]`` with no hint
            about it; and ``budget_s``, the seconds after which the job stops.
        ctx: The job's surroundings.

    Returns:
        A :class:`workbench.renders.RenderAnswer` as a dict; every render in it is a file in ``ctx.out_dir`` under
        its ``name``. Or an ``error`` result: ``file_not_found``, ``unsupported_format`` (neither a picture nor a
        PDF), ``unreadable_file``, ``no_such_part`` or ``read_timeout``, none of which names a path.

    Raises:
        ValueError: ``strips`` is out of range, ``facts`` is not a bool, or more renders are asked for than one job may
            make.
    """
    name = protocol.check_input_path(str(args["path"]))
    real = ctx.work_dir / name
    if name.split("/")[0] == "out" or not real.is_file():
        return error("file_not_found", "there is no such file in this conversation's workspace")
    kind = kind_of(name)
    if kind == "none":
        return error("unsupported_format", "only pictures and PDFs can be shown")
    request = _Request.of(args)
    budget = float(args.get("budget_s", _BUDGET_S))
    deadline = time.monotonic() + budget
    readers.set_deadline(deadline)
    try:
        with limit_until(deadline, readers.ReadTimeout):
            facts = _picture(real, request) if kind == "image" else _pdf(real, request)
    except readers.UnreadableFile as exc:
        return error("unreadable_file", str(exc))
    except readers.NoSuchPart as exc:
        return error("no_such_part", str(exc))
    except readers.ReadTimeout:
        return error("read_timeout", f"drawing the file took longer than {budget:g} s; it is too large or too complex")
    finally:
        readers.set_deadline(None)
    role = "image" if facts.kind == "image" else "page"
    entries = [
        RenderEntry.model_validate(
            {
                "name": render_name(m.n, m.strip, m.strips, m.mime),
                "role": role,
                "n": m.n,
                "strip": m.strip,
                "strips": m.strips,
                "width": m.width,
                "height": m.height,
                "mime": m.mime,
            }
        )
        for m in facts.made
    ]
    notes: list[str] = []
    if facts.frames is not None:
        notes.append(f"only the first of {facts.frames} frames is shown")
    if facts.unchecked:
        notes.append(f"pages after {PAGES_CHECKED_MAX} were not checked for text")
    whole = RenderAnswer.model_validate(
        {
            "kind": facts.kind,
            "total": facts.total,
            "pages_without_text": facts.blank,
            "frames": facts.frames,
            "hint": _hint(notes),
            "renders": [entry.model_dump() for entry in entries],
        }
    ).model_dump()
    answer = RenderAnswer.model_validate(_fit(whole, facts.blank, notes, ctx.max_chars)).model_dump()
    written: list[Path] = []
    try:
        for made, entry in zip(facts.made, entries, strict=True):
            written.append(ctx.out_dir / entry.name)  # noted first: a file that was begun is removed too
            written[-1].write_bytes(made.data)
    except OSError:  # its text holds the path of the file
        for path in written:
            with suppress(OSError):
                path.unlink(missing_ok=True)
        return error("unreadable_file", "the renders could not be written")
    return answer
