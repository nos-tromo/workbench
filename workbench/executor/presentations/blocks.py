"""What a slide's places hold: text, native tables, native charts and pictures, and which block goes where.

Block i of a slide fills place i of its layout (:func:`fill`), and a place takes only the kinds :data:`ACCEPTS` gives
its role (:func:`check_places`). Text is Markdown, shrunk when it may not fit its place (:func:`fit`). A table and a
chart take the placeholder's own place: python-pptx's ``insert_table`` and ``insert_chart`` work on the placeholder's
element, so a content place takes them as a table or chart place does, and the ``p:ph idx`` stays, which keeps the
layout's position. A picture in a picture place is cropped to fill it; in a content place it is fitted within it, its
aspect kept and centred, and the placeholder goes.

A chart is PowerPoint's own, its data in an embedded workbook, so it can be edited; no colour is set, so the theme's
accents apply, and it has no title of its own: the slide's title names it. The workbook takes every text as text,
never as a formula, a number or a link.

A picture is a PNG, JPEG or GIF of the chat, known by its content, not by its extension; its size and its header are
read (:func:`check_image`) before any slide is made, and it is placed from its bytes, so its file's name is nowhere in
the deck. Every refusal names the slide and the block, a picture by its virtual path, never a path of the job; a
layout's name may be in one, as the model needs it, and nothing here logs.
"""

import dataclasses
import io
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final, override

from PIL import Image as PILImage
from pptx.chart.data import CategoryChartData
from pptx.chart.xlsx import CategoryWorkbookWriter
from pptx.enum.chart import XL_AXIS_CROSSES, XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.text import MSO_AUTO_SIZE
from pptx.opc.constants import CONTENT_TYPE as CT
from pptx.oxml.ns import qn
from pptx.parts.image import Image
from pptx.shapes.placeholder import ChartPlaceholder, PicturePlaceholder, TablePlaceholder
from pptx.text.text import Font
from pptx.util import Emu, Length, Pt, lazyproperty
from xlsxwriter import Workbook

from workbench import decks, protocol
from workbench.decks import BlockKind
from workbench.executor.context import JobContext
from workbench.executor.presentations import text
from workbench.executor.presentations.master import Layout, Place, Role
from workbench.executor.runners.templates import cut
from workbench.executor.templates import Refusal, shown, xml_safe

#: The roles of the places that take each kind of block.
ACCEPTS: Final[dict[BlockKind, frozenset[Role]]] = {
    "text": frozenset({"text", "content"}),
    "table": frozenset({"table", "content"}),
    "chart": frozenset({"chart", "content"}),
    "image": frozenset({"picture", "content"}),
}

#: Each chart type as PowerPoint draws it: a column chart has vertical bars, a bar chart horizontal ones.
CHART_TYPES: Final = {
    "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
    "bar": XL_CHART_TYPE.BAR_CLUSTERED,
    "line": XL_CHART_TYPE.LINE_MARKERS,
    "pie": XL_CHART_TYPE.PIE,
}

#: The content types a picture may have, read from its header.
IMAGE_CONTENT: Final = frozenset({CT.PNG, CT.JPEG, CT.GIF})

#: The sizes a table's text steps down through, largest first, until its rows fit its place; and a line's height, as
#: a multiple of the size.
TABLE_SIZES: Final = (Pt(18), Pt(16), Pt(14), Pt(12), Pt(10))
LINE_HEIGHT: Final = 1.2

#: ``normAutofit``'s ``fontScale``, in thousandths of a percent: the full size, and the least that text is shrunk to.
FULL_SCALE: Final = 100_000
MIN_SCALE: Final = 60_000

#: How much shrunk text's line spacing is reduced, in thousandths of a percent.
LINE_REDUCTION: Final = "10000"

#: What a warning says when a text had characters a document cannot hold.
REPLACED: Final = "characters a document cannot hold were replaced with �"

#: How the chart's embedded workbook takes a text: as a text, whatever it starts with.
_PLAIN: Final = {"strings_to_formulas": False, "strings_to_numbers": False, "strings_to_urls": False}

_WHAT: Final[dict[BlockKind, str]] = {"text": "text", "table": "a table", "chart": "a chart", "image": "an image"}
_MB: Final = 1024 * 1024
_NOT_AN_IMAGE: Final = "is not a PNG, JPEG or GIF image"


class _TextWorkbook(CategoryWorkbookWriter):
    """python-pptx's workbook of a chart's data, in which a label or a series' name is a text, never a formula."""

    @override
    @contextmanager
    def _open_worksheet(self, xlsx_file: Any) -> Iterator[tuple[Any, Any]]:
        """Open the workbook as python-pptx does, with every text written as a text.

        Args:
            xlsx_file: Where the workbook is written.

        Yields:
            The workbook and its one worksheet.
        """
        workbook = Workbook(xlsx_file, {"in_memory": True, **_PLAIN})
        worksheet = workbook.add_worksheet()
        yield workbook, worksheet
        workbook.close()


class _ChartData(CategoryChartData):
    """A chart's categories and series, written into a :class:`_TextWorkbook`."""

    @override
    @lazyproperty
    def _workbook_writer(self) -> CategoryWorkbookWriter:
        """The workbook the chart's data goes into."""
        return _TextWorkbook(self)


def check_places(layout: Layout, content: Sequence[Any], number: int) -> None:
    """Make sure each block of a slide has a place that takes it: block i goes into place i of the layout.

    Args:
        layout: The slide's layout.
        content: The slide's blocks, checked with :func:`workbench.decks.deck_problem`.
        number: The slide's number.

    Raises:
        Refusal: A block is past the layout's places, or its place takes another kind (``invalid_value``). The message
            names the slide and the block, and lists the layout's places by role.
    """
    for index, block in enumerate(content, 1):
        where = f"slide {number}, block {index}"
        kind = decks.block_kind(block)
        if kind is None:  # deck_problem refuses it first
            raise Refusal("invalid_value", f"{where}: a block is text, a table, a chart or an image")
        if index > len(layout.places) or layout.places[index - 1].role not in ACCEPTS[kind]:
            (own,) = ACCEPTS[kind] - {"content"}
            places = ", ".join(place.role for place in layout.places) or "none"
            name = repr(cut(layout.name, decks.MAX_LAYOUT_NAME)) if layout.name else str(layout.number)
            said = f"{_WHAT[kind]} goes into a {own} or content place; layout {name} has: {places}"
            raise Refusal("invalid_value", f"{where}: {said}")


def find_images(slides: Sequence[Mapping[str, Any]], ctx: JobContext) -> dict[str, Path]:
    """Find every picture of the deck among the job's inputs, and check each, before a slide is made.

    Args:
        slides: The slides; an image block's ``image`` is its path below the chat folder.
        ctx: The job's surroundings.

    Returns:
        The job's copy of each picture, by its block (``slide N, block M``).

    Raises:
        Refusal: A picture is not there or cannot be placed (:func:`check_image`), or all of them together are larger
            than :data:`workbench.decks.MAX_IMAGES_BYTES` (``invalid_value``).
    """
    found: dict[str, Path] = {}
    total = 0
    for number, slide in enumerate(slides, 1):
        for index, block in enumerate(slide.get("content", []), 1):
            if decks.block_kind(block) != "image":
                continue
            label = f"slide {number}, block {index}"
            path, virtual = _image_input(str(block["image"]), ctx, label)
            check_image(path, virtual, label)
            total += path.stat().st_size
            if total > decks.MAX_IMAGES_BYTES:
                most = decks.MAX_IMAGES_BYTES // _MB
                raise Refusal("invalid_value", f"{label}: the deck's images take more than {most} MB together")
            found[label] = path
    return found


def _image_input(name: str, ctx: JobContext, label: str) -> tuple[Path, str]:
    """Find a picture among the job's inputs, as a template is found: a file below the chat folder, not in ``out/``.

    Args:
        name: The picture's path below the chat folder.
        ctx: The job's surroundings.
        label: The block, for the message.

    Returns:
        The job's copy and the virtual path.

    Raises:
        Refusal: It is not a file among the inputs (``invalid_value``).
    """
    virtual = f"/workspace/{ctx.chat_id}/{name}"
    missing = Refusal("invalid_value", f"{label}: {virtual} is not in this conversation")
    try:
        protocol.check_input_path(name)
    except ValueError:
        raise missing from None
    path = ctx.work_dir / name
    if name.split("/")[0] == "out" or not path.is_file():
        raise missing
    return path, virtual


def check_image(path: Path, virtual: str, label: str) -> None:
    """Make sure a picture can be placed: a PNG, JPEG or GIF by its content, within the bytes and pixels of one.

    The size of the file is measured first; then only its header is read, as python-pptx reads it, nothing decoded.

    Args:
        path: The job's copy.
        virtual: Its virtual path, for the message.
        label: Its block, for the message.

    Raises:
        Refusal: It is larger than :data:`workbench.decks.MAX_IMAGE_BYTES`, is not a PNG, JPEG or GIF whatever its
            extension, or has more than :data:`workbench.decks.MAX_IMAGE_PIXELS` (``invalid_value``).
    """
    if path.stat().st_size > decks.MAX_IMAGE_BYTES:
        raise Refusal("invalid_value", f"{label}: {virtual} is larger than {decks.MAX_IMAGE_BYTES // _MB} MB")
    megapixels = decks.MAX_IMAGE_PIXELS // 1_000_000
    too_large = Refusal("invalid_value", f"{label}: {virtual} is larger than {megapixels} megapixels")
    try:
        header = Image.from_blob(path.read_bytes())
        content_type, (width, height) = header.content_type, header.size
    except PILImage.DecompressionBombError:
        raise too_large from None
    except Exception:  # whatever Pillow cannot identify, or python-pptx does not place
        raise Refusal("invalid_value", f"{label}: {virtual} {_NOT_AN_IMAGE}") from None
    if content_type not in IMAGE_CONTENT or width < 1 or height < 1:
        raise Refusal("invalid_value", f"{label}: {virtual} {_NOT_AN_IMAGE}")
    if width * height > decks.MAX_IMAGE_PIXELS:
        raise too_large


def fill(
    slide: Any,
    layout: Layout,
    content: Sequence[Any],
    number: int,
    images: Mapping[str, Path],
    filled: set[int],
    warnings: list[str],
) -> None:
    """Fill a slide's places with its blocks, block i in place i, as :func:`check_places` allowed them.

    Args:
        slide: The slide.
        layout: Its layout.
        content: The slide's blocks.
        number: Its number.
        images: The deck's pictures.
        filled: The ``idx`` of each place that holds something, added to.
        warnings: Where warnings go.
    """
    for index, (block, place) in enumerate(zip(content, layout.places[: len(content)], strict=True), 1):
        where = f"slide {number}, block {index}"
        if isinstance(block, str) and not block.strip():
            continue
        try:
            on_slide = dataclasses.replace(place, shape=slide.placeholders[place.idx])
        except KeyError:
            warnings.append(f"{where}: the layout's place {index} is not on the slide; the block was left out")
            continue
        filled.add(place.idx)
        if not isinstance(block, str):
            if not place_block(on_slide, block, images, where, warnings):
                warnings.append(f"slide {number}: the table may not fit; split it over two slides")
            continue
        frame = on_slide.shape.text_frame
        written = text.write_markdown(frame, block)
        if written.replaced:
            warnings.append(f"{where}: {REPLACED}")
        if not fit(frame, text.estimate_lines(written, on_slide.chars), on_slide):
            warnings.append(
                f"slide {number}: the text may not fit, even at {MIN_SCALE // 1000} % of its size (block {index}); "
                "shorten it or move some of it to another slide"
            )


def fit(frame: Any, needed: int, place: Place) -> bool:
    """Shrink a text frame's text when it needs more lines than its place has, as PowerPoint's autofit would.

    The font is scaled to the place's lines over the lines needed, but not below :data:`MIN_SCALE`, and the line
    spacing is reduced by :data:`LINE_REDUCTION`. A bare ``normAutofit`` would shrink nothing until the text is edited.

    Args:
        frame: The python-pptx text frame, written.
        needed: The lines its text takes at full size (:func:`~workbench.executor.presentations.text.estimate_lines`).
        place: The place it fills.

    Returns:
        Whether the text fits at the scale written; ``False`` when even :data:`MIN_SCALE` is too large.
    """
    if needed <= place.lines:
        return True
    frame.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
    autofit = frame._bodyPr.normAutofit
    autofit.set("fontScale", str(max(MIN_SCALE, FULL_SCALE * place.lines // needed)))
    autofit.set("lnSpcReduction", LINE_REDUCTION)
    return FULL_SCALE * place.lines >= MIN_SCALE * needed


def place_block(
    on_slide: Place, block: Mapping[str, Any], images: Mapping[str, Path], label: str, warnings: list[str]
) -> bool:
    """Put a table, chart or picture block into its place of the slide.

    Args:
        on_slide: The place, its ``shape`` the slide's placeholder; :func:`check_places` allowed the block in it.
        block: The block.
        images: The pictures :func:`find_images` found.
        label: The block (``slide N, block M``), for a warning and the pictures.
        warnings: Where a warning goes, should a text hold characters a document cannot.

    Returns:
        Whether the block fits its place; only a table may not (:func:`place_table`).
    """
    kind = decks.block_kind(block)
    fits = True
    if kind == "table":
        replaced, fits = place_table(on_slide, block["table"])
    elif kind == "chart":
        replaced = place_chart(on_slide, block["chart"])
    else:
        replaced = place_image(on_slide, images[label], block.get("alt"))
    if replaced:
        warnings.append(f"{label}: {REPLACED}")
    return fits


def place_table(place: Place, rows: Sequence[Sequence[Any]]) -> tuple[bool, bool]:
    """Write a native table into a table or content place: its first row the header, the table style's own.

    A row shorter than the widest is filled with empty cells. The text takes the largest of :data:`TABLE_SIZES` at which
    the rows fit the place's height, each row at its natural height: a line of :data:`LINE_HEIGHT` times the size, and
    its cells' top and bottom margins (python-pptx's, or a cell's own). A row is never stretched, so a short table is
    short; one that does not fit even at the smallest size keeps that size.

    Args:
        place: The place on the slide.
        rows: The rows, each a list of strings, numbers, bools and ``None``.

    Returns:
        Whether a cell had characters a document cannot hold, now U+FFFD; and whether the table fits its place.
    """
    width = max(len(row) for row in rows)
    room = int(place.shape.height or 0)
    frame = TablePlaceholder(place.shape._element, place.shape._parent).insert_table(len(rows), width)
    table = frame.table
    margins = [
        max(cell.margin_top + cell.margin_bottom for cell in table.rows[index].cells) for index in range(len(rows))
    ]

    def heights(size: Length) -> list[int]:
        return [int(size * LINE_HEIGHT) + margin for margin in margins]

    size = next((size for size in TABLE_SIZES if sum(heights(size)) <= room), TABLE_SIZES[-1])
    replaced = False
    for index, (values, height) in enumerate(zip(rows, heights(size), strict=True)):
        row = table.rows[index]
        row.height = Emu(height)
        for cell, value in zip(row.cells, [*values, *[None] * (width - len(values))], strict=True):
            written, changed = xml_safe("" if value is None else shown(value))
            replaced |= changed
            cell.text = written
            for paragraph in cell.text_frame.paragraphs:
                for run in paragraph.runs:
                    run.font.size = size
                Font(paragraph._p.get_or_add_endParaRPr()).size = size
    frame.height = Emu(sum(heights(size)))
    return replaced, frame.height <= room


def place_chart(place: Place, chart: Mapping[str, Any]) -> bool:
    """Draw a native chart into a chart or content place, its data in an embedded workbook, in the theme's colours.

    A bar chart lists its first category on top, its value axis below; a pie, or a chart of several series, has a
    legend at the bottom, outside the plot; no chart has a title, as the slide's title names it. Categories that are
    all numbers stay numbers; otherwise each is a text. A value of ``None`` is a gap: no point is written for it.

    Args:
        place: The place on the slide.
        chart: The chart: ``type``, ``categories`` and ``series`` (a name to a value per category).

    Returns:
        Whether a category or a series' name had characters a document cannot hold, now U+FFFD.
    """
    replaced = False

    def as_text(value: object) -> str:
        nonlocal replaced
        safe, changed = xml_safe(shown(value))
        replaced |= changed
        return safe

    categories: list[Any] = list(chart["categories"])
    data = _ChartData()
    if all(isinstance(category, int | float) and not isinstance(category, bool) for category in categories):
        data.categories = categories
    else:
        data.categories = [as_text(category) for category in categories]
    for name, values in chart["series"].items():
        data.add_series(as_text(name), list(values))
    kind = chart["type"]
    drawn = ChartPlaceholder(place.shape._element, place.shape._parent).insert_chart(CHART_TYPES[kind], data).chart
    if kind == "bar":
        drawn.category_axis.reverse_order = True
        drawn.value_axis._element.get_or_add_crosses().val = XL_AXIS_CROSSES.MAXIMUM  # the value axis stays below
    drawn.has_title = False  # no automatic title of a single series' name
    drawn.has_legend = kind == "pie" or len(chart["series"]) > 1
    legend = drawn.legend
    if legend is not None:
        legend.position = XL_LEGEND_POSITION.BOTTOM
        legend.include_in_layout = False
    return replaced


def place_image(place: Place, path: Path, alt: str | None = None) -> bool:
    """Put a picture into a picture place, cropped to fill it, or into a content place, fitted within it.

    In a content place the picture keeps its aspect, centred in the place's box, and takes the placeholder's place in
    the slide's reading order; the placeholder goes. The picture's alternative text is ``alt``, or none: never its
    file's name.

    Args:
        place: The place on the slide.
        path: The job's copy of the picture, checked with :func:`check_image`.
        alt: Its alternative text.

    Returns:
        Whether ``alt`` had characters a document cannot hold, now U+FFFD.
    """
    blob = path.read_bytes()
    shape = place.shape
    if place.role == "picture":
        picture = PicturePlaceholder(shape._element, shape._parent).insert_picture(io.BytesIO(blob))
    else:
        left, top, width, height = (int(value or 0) for value in (shape.left, shape.top, shape.width, shape.height))
        pixels_wide, pixels_high = Image.from_blob(blob).size
        scale = min(width / pixels_wide, height / pixels_high)
        wide, high = round(pixels_wide * scale), round(pixels_high * scale)
        picture = shape.part.slide.shapes.add_picture(
            io.BytesIO(blob), Emu(left + (width - wide) // 2), Emu(top + (height - high) // 2), Emu(wide), Emu(high)
        )
        shape._element.addprevious(picture._element)
        shape._element.getparent().remove(shape._element)
    names = picture._element.find(f"{qn('p:nvPicPr')}/{qn('p:cNvPr')}")
    names.attrib.pop("descr", None)
    if not alt:
        return False
    described, replaced = xml_safe(alt)
    names.set("descr", described)
    return replaced
