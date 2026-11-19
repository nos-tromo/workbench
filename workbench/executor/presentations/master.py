"""A slide master: its layouts, each layout's places in reading order, what fits in each, and the inspection of it.

The layouts are numbered 1..N across the file's slide masters, in their order. A layout's title and subtitle are flags,
not places, and its date, footer and slide number set ``footer``, unless ``p:hf`` turns them all off
(:func:`shown_footers`). A place is a body, object, picture, table or chart placeholder, with a role and a capacity:
about how many lines, and characters a line, fit at the font size it inherits. Other placeholders (SmartArt, media,
headers) are not places.

The file is untrusted. It goes through the zip guard first, and whatever python-pptx fails with while it opens or reads
the file is the file's damage (:func:`~workbench.executor.runners.templates.library`). The inspection names no path,
and nothing here logs: a layout's name is the template's content, which the answer holds and a log line never does.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.oxml.ns import qn

from workbench import decks
from workbench.executor import readers
from workbench.executor.context import JobContext
from workbench.executor.readers.pptx import open_presentation
from workbench.executor.runners.templates import cut, library
from workbench.executor.templates import Refusal

Role = Literal["text", "content", "picture", "table", "chart"]

#: The role of each type of placeholder that is a place.
ROLES: Final[dict[PP_PLACEHOLDER, Role]] = {
    PP_PLACEHOLDER.BODY: "text",
    PP_PLACEHOLDER.OBJECT: "content",
    PP_PLACEHOLDER.PICTURE: "picture",
    PP_PLACEHOLDER.BITMAP: "picture",
    PP_PLACEHOLDER.TABLE: "table",
    PP_PLACEHOLDER.CHART: "chart",
}
_TITLES: Final = frozenset({PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE})
_FOOTERS: Final = frozenset({PP_PLACEHOLDER.DATE, PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER})

#: The attribute of ``p:hf`` that turns each footer kind off with ``0`` or ``false``; one that is left out means on.
_HF: Final = {PP_PLACEHOLDER.DATE: "dt", PP_PLACEHOLDER.FOOTER: "ftr", PP_PLACEHOLDER.SLIDE_NUMBER: "sldNum"}
_OFF: Final = frozenset({"0", "false"})

#: The font size when none is set, in hundredths of a point (``sz``), and the EMU in one of those.
DEFAULT_SIZE: Final = 1800
_EMU_PER_SZ: Final = 127

#: A text frame's insets when none is set, in EMU: 0.1 inch left and right, 0.05 inch top and bottom.
DEFAULT_INSETS: Final = {"lIns": 91440, "rIns": 91440, "tIns": 45720, "bIns": 45720}

#: A character is taken as this many times the font size wide, and a line as this many times it high.
_CHAR_WIDTH: Final = 0.5
_LINE_HEIGHT: Final = 1.2

#: How far apart, in EMU, the tops of places in one row may be (0.1 inch): they are read left to right.
ROW_SPREAD: Final = 91440

_LEVEL_ONE_SIZE: Final = f"{qn('p:txBody')}/{qn('a:lstStyle')}/{qn('a:lvl1pPr')}/{qn('a:defRPr')}"
_BODY_PR: Final = f"{qn('p:txBody')}/{qn('a:bodyPr')}"


@dataclass(frozen=True)
class Place:
    """A place of a layout, which one block of a slide fills.

    Attributes:
        role: What it takes: ``text`` (a body placeholder), ``content`` (an object placeholder, which takes text, a
            table, a chart or a picture), ``picture``, ``table`` or ``chart``.
        lines: About how many lines fit at its inherited font size, at least 1.
        chars: About how many characters fit on a line, at least 1.
        idx: The placeholder's ``idx``, which a slide's placeholder made from it shares.
        shape: The layout's placeholder.
    """

    role: Role
    lines: int
    chars: int
    idx: int
    shape: Any


@dataclass(frozen=True)
class Layout:
    """A layout of the master, as ``inspect_template`` lists it and a slide is made from it.

    Attributes:
        number: Its place among all the layouts of the file, from 1.
        name: Its name, as the file has it (it may be empty, or shared with another layout).
        layout: The python-pptx slide layout.
        title: Whether it has a title or centre title placeholder.
        subtitle: Whether it has a subtitle placeholder.
        places: Its places in reading order: top, then left, places whose tops are at most :data:`ROW_SPREAD` apart
            being one row.
        footer: Whether it has a date, footer or slide number placeholder that ``p:hf`` leaves on.
    """

    number: int
    name: str
    layout: Any
    title: bool
    subtitle: bool
    places: tuple[Place, ...]
    footer: bool


def open_master(path: Path, ctx: JobContext) -> Any:
    """Open a slide master: the zip guard, then the presentation, a ``.potx`` as the presentation it is.

    Args:
        path: The job's copy of the ``.pptx`` or ``.potx``.
        ctx: The job's surroundings.

    Returns:
        The python-pptx presentation.

    Raises:
        readers.UnreadableFile: The zip guard refused it, or it is damaged.
        Refusal: It is a macro-enabled presentation or a slide show (``unsupported_template``).
    """
    readers.check_zip(path, ctx.file_mb)
    try:
        return library(lambda: open_presentation(path))
    except readers.UnsupportedFormat as exc:
        raise Refusal("unsupported_template", f"the master is {exc}") from None


def layouts(prs: Any) -> list[Layout]:
    """Read every layout of a presentation, numbered from 1 over its slide masters, then each master's layouts.

    Args:
        prs: The presentation, open.

    Returns:
        The layouts in order.

    Raises:
        readers.UnreadableFile: The file's layouts cannot be read.
    """

    def read() -> list[Layout]:
        found: list[Layout] = []
        for slide_master in prs.slide_masters:
            for layout in slide_master.slide_layouts:
                found.append(_layout(len(found) + 1, layout, slide_master))
        return found

    return library(read)


def _layout(number: int, layout: Any, slide_master: Any) -> Layout:
    """Read one layout.

    Args:
        number: Its number.
        layout: The python-pptx slide layout.
        slide_master: The master it belongs to.

    Returns:
        The layout, its places in reading order.
    """
    types: set[PP_PLACEHOLDER] = set()
    places: list[Place] = []
    for shape in layout.placeholders:
        placeholder = shape.placeholder_format
        types.add(placeholder.type)
        role = ROLES.get(placeholder.type)
        if role is not None:
            lines, chars = capacity(shape, layout, slide_master)
            places.append(Place(role=role, lines=lines, chars=chars, idx=placeholder.idx, shape=shape))
    return Layout(
        number=number,
        name=layout.name,
        layout=layout,
        title=bool(types & _TITLES),
        subtitle=PP_PLACEHOLDER.SUBTITLE in types,
        places=reading_order(places),
        footer=bool(types & shown_footers(layout, slide_master)),
    )


def reading_order(places: list[Place]) -> tuple[Place, ...]:
    """Put places in reading order: by rows from the top, each row from the left.

    A row is the highest place left and every place whose top is at most :data:`ROW_SPREAD` below it, so places side by
    side, a few EMU apart in height as a drawn layout has them, are read left to right.

    Args:
        places: The places of a layout.

    Returns:
        The places in reading order.
    """
    rows: list[list[Place]] = []
    for place in sorted(places, key=lambda place: place.shape.top or 0):
        if rows and (place.shape.top or 0) - (rows[-1][0].shape.top or 0) <= ROW_SPREAD:
            rows[-1].append(place)
        else:
            rows.append([place])
    return tuple(place for row in rows for place in sorted(row, key=lambda place: place.shape.left or 0))


def shown_footers(layout: Any, slide_master: Any) -> frozenset[PP_PLACEHOLDER]:
    """Tell which footer kinds a layout's slides show: those its ``p:hf`` leaves on, or its master's if it has none.

    A layout's ``p:hf`` stands for the whole of it, as the master's does when the layout has none; an attribute left
    out means on, and with no ``p:hf`` at all every kind is on. A deck copies only these (:mod:`.deck`), and the
    inspection's ``footer`` flag is set only by these, so the two say the same.

    Args:
        layout: The python-pptx slide layout.
        slide_master: Its slide master.

    Returns:
        The kinds of date, footer and slide number that are on.
    """
    flags = layout._element.find(qn("p:hf"))
    if flags is None:
        flags = slide_master._element.find(qn("p:hf"))
    if flags is None:
        return _FOOTERS
    return frozenset(kind for kind, attribute in _HF.items() if flags.get(attribute) not in _OFF)


def capacity(shape: Any, layout: Any, master: Any) -> tuple[int, int]:
    """Tell about how many lines, and characters a line, a placeholder holds at the font size it inherits.

    The size is the first one set of: the level-1 size of the placeholder's own list style; of its layout's placeholder
    of the same ``idx``, when it is a slide's; of the master's placeholder it inherits from (a title's the master's
    title, a date, footer or slide number's the same type, any other's the master's body); the master's title style
    for a title and its body style otherwise; 18 pt. A size of no number or not above 0 is not one. The insets are the
    first set along the same placeholders, else :data:`DEFAULT_INSETS`. A character is half the size wide and a line
    1.2 times the size high.

    Args:
        shape: A placeholder: of the layout, or of a slide made from it.
        layout: The layout.
        master: The layout's slide master.

    Returns:
        ``(lines, chars)``, each at least 1.
    """
    kind = shape.placeholder_format.type
    chain = _inherited(shape, layout, master, kind)
    sizes = [_size_of(element.find(_LEVEL_ONE_SIZE)) for element in chain]
    style = "p:titleStyle" if kind in _TITLES else "p:bodyStyle"
    sizes.append(_size_of(master._element.find(f"{qn('p:txStyles')}/{qn(style)}/{qn('a:lvl1pPr')}/{qn('a:defRPr')}")))
    size = next((found for found in sizes if found is not None), DEFAULT_SIZE) * _EMU_PER_SZ
    insets = {side: _inset(chain, side, default) for side, default in DEFAULT_INSETS.items()}
    width = (shape.width or 0) - insets["lIns"] - insets["rIns"]
    height = (shape.height or 0) - insets["tIns"] - insets["bIns"]
    return max(1, int(height / (_LINE_HEIGHT * size))), max(1, int(width / (_CHAR_WIDTH * size)))


def _inherited(shape: Any, layout: Any, master: Any, kind: PP_PLACEHOLDER) -> list[Any]:
    """List the placeholder elements a placeholder's text takes its style from, its own first.

    Args:
        shape: The placeholder.
        layout: Its layout, or its slide's.
        master: The layout's slide master.
        kind: The placeholder's type.

    Returns:
        The placeholder's element, its layout placeholder's when it is a slide's, then the master placeholder's.
    """
    chain = [shape._element]
    if shape.part is not layout.part:
        on_layout = layout.placeholders.get(idx=shape.placeholder_format.idx)
        if on_layout is not None:
            chain.append(on_layout._element)
    base = PP_PLACEHOLDER.TITLE if kind in _TITLES else kind if kind in _FOOTERS else PP_PLACEHOLDER.BODY
    on_master = master.placeholders.get(base)
    if on_master is not None:
        chain.append(on_master._element)
    return chain


def _size_of(run_properties: Any) -> int | None:
    """Read a font size from an ``a:defRPr``.

    Args:
        run_properties: The element, or ``None``.

    Returns:
        Its ``sz`` in hundredths of a point, or ``None`` when it has none, or one of no number or not above 0.
    """
    if run_properties is None:
        return None
    try:
        size = int(run_properties.get("sz", ""))
    except ValueError:
        return None
    return size if size > 0 else None


def _inset(chain: list[Any], side: str, default: int) -> int:
    """Read one inset of a text frame, the first set along the placeholders.

    Args:
        chain: The placeholder elements, nearest first.
        side: ``lIns``, ``rIns``, ``tIns`` or ``bIns``.
        default: What it is when none sets it.

    Returns:
        The inset in EMU.
    """
    for element in chain:
        body = element.find(_BODY_PR)
        value = None if body is None else body.get(side)
        if value is not None:
            try:
                return int(value)
            except ValueError:
                continue
    return default


def _entry(layout: Layout) -> dict[str, Any]:
    """Write a layout as the inspection lists it.

    Args:
        layout: The layout.

    Returns:
        Its number, name (cut to :data:`decks.MAX_LAYOUT_NAME`), title and subtitle flags, places and footer flag.
    """
    return {
        "number": layout.number,
        "name": cut(layout.name, decks.MAX_LAYOUT_NAME),
        "title": layout.title,
        "subtitle": layout.subtitle,
        "places": [{"role": place.role, "lines": place.lines, "chars": place.chars} for place in layout.places],
        "footer": layout.footer,
    }


def inspect(prs: Any, room: int) -> dict[str, Any]:
    """Answer ``inspect_template`` on a slide master: its layouts, as many as fit, and how many slides it has.

    Args:
        prs: The presentation, open.
        room: The characters of JSON the layouts may take.

    Returns:
        ``template_kind`` (``slide_master``), ``layouts`` (at most :data:`decks.MAX_LAYOUTS`, and those that fit in
        ``room``), ``slides`` (the example slides the file has, which a deck leaves out), ``total`` (the layouts there
        are) and ``truncated``.

    Raises:
        readers.UnreadableFile: The file's layouts or slides cannot be read.
    """
    found = layouts(prs)
    listed: list[dict[str, Any]] = []
    for layout in found[: decks.MAX_LAYOUTS]:
        entry = _entry(layout)
        room -= len(json.dumps(entry)) + 2  # and the ", " before the next
        if room < 0:
            break
        listed.append(entry)
    return {
        "template_kind": "slide_master",
        "layouts": listed,
        "slides": library(lambda: len(prs.slides)),
        "total": len(found),
        "truncated": len(listed) < len(found),
    }
