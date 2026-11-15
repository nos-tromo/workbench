"""PowerPoint presentations (``.pptx``), by slide: shape text, table cells, then the speaker notes.

A slide without any text (pictures only) is reported in ``pages_without_text`` and never returned empty.
"""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from workbench.executor.readers import DocumentText, Part, check
from workbench.executor.readers import paging as pg

#: Most slides one read looks at.
MAX_SLIDES_LOOKED_AT: Final = 200


def _shape_lines(shape: Any) -> Iterator[str]:
    """Yield the text lines of a shape, a table's rows and a group's shapes included.

    Args:
        shape: A python-pptx shape.

    Yields:
        One line per paragraph or table row, empty ones left out.
    """
    if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.GROUP:
        for inner in shape.shapes:
            yield from _shape_lines(inner)
        return
    if getattr(shape, "has_table", False) and shape.has_table:
        for row in shape.table.rows:
            cells = [" ".join(cell.text.split()) for cell in row.cells]
            if any(cells):
                yield " | ".join(cells)
        return
    if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
        for paragraph in shape.text_frame.paragraphs:
            text = "".join(run.text for run in paragraph.runs).strip() or paragraph.text.strip()
            if text:
                yield text


def _slide_text(slide: Any) -> str:
    """Join the text of a slide and of its notes.

    Args:
        slide: A python-pptx slide.

    Returns:
        The slide's lines, then ``Notes:`` and the speaker notes if there are any; empty if there is no text.
    """
    lines = [line for shape in slide.shapes for line in _shape_lines(shape)]
    if slide.has_notes_slide:
        notes = slide.notes_slide.notes_text_frame
        text = notes.text.strip() if notes is not None else ""
        if text:
            lines.append("Notes: " + text)
    return "\n".join(lines)


def read(path: Path, *, part: str | None, max_chars: int) -> DocumentText:
    """Read slides from ``part`` on.

    Args:
        path: The ``.pptx`` file.
        part: ``None``, ``slide N`` or ``slides N-M``.
        max_chars: The budget in bytes of JSON.

    Returns:
        The slides that have text, whole, the numbers of the slides looked at that have none, and where to
        continue.
    """
    slides = list(Presentation(str(path)).slides)
    total = len(slides)
    first, last = pg.numbered_window(part, total, "slide")
    collector = pg.Collector(max_chars)
    without: list[int] = []
    for number in range(first, last + 1):
        check()
        if number - first >= MAX_SLIDES_LOOKED_AT:
            collector.next_part = f"slide {number}"
            break
        text = _slide_text(slides[number - 1])
        if not text:
            without.append(number)
        elif not collector.add(f"slide {number}", text):
            break
    return DocumentText(
        kind="pptx",
        parts=collector.parts,
        pages_without_text=without,
        total_parts=total,
        truncated=collector.truncated,
        next_part=collector.next_part,
        cut=collector.cut,
    )


def units(path: Path) -> Iterator[Part]:
    """Walk the slides.

    Args:
        path: The ``.pptx`` file.

    Yields:
        Each slide as ``slide N`` with all its text; a slide without text has empty text.
    """
    for number, slide in enumerate(Presentation(str(path)).slides, 1):
        check()
        yield Part(f"slide {number}", _slide_text(slide))
