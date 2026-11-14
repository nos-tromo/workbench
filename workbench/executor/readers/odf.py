"""OpenDocument files: text (``.odt``) by paragraph and table, spreadsheets (``.ods``) by range of rows.

Both are read from ``content.xml`` with lxml, which loads nothing external. A spreadsheet is streamed
and its memory released as it goes, because OpenDocument stores a run of identical rows or cells once with
a repeat count (every sheet ends in a million blank rows): an empty row that repeats is stepped over, never
unfolded, and a repeat of something that has content is unfolded only up to a limit.
"""

import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from lxml import etree

from workbench.executor.readers import DocumentText, Part
from workbench.executor.readers import paging as pg

_CONTENT: Final = "content.xml"
_TEXT: Final = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_TABLE: Final = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
_OFFICE: Final = "{urn:oasis:names:tc:opendocument:xmlns:office:1.0}"

_P: Final = f"{_TEXT}p"
_H: Final = f"{_TEXT}h"
_SPACE: Final = f"{_TEXT}s"
_TAB: Final = f"{_TEXT}tab"
_BREAK: Final = f"{_TEXT}line-break"
_TABLE_EL: Final = f"{_TABLE}table"
_ROW: Final = f"{_TABLE}table-row"
_CELL: Final = f"{_TABLE}table-cell"
_COVERED: Final = f"{_TABLE}covered-table-cell"
_SKIPPED: Final = frozenset(
    {f"{_TEXT}tracked-changes", f"{_TEXT}sequence-decls", f"{_TEXT}variable-decls", f"{_OFFICE}forms"}
)
_NOT_TEXT: Final = frozenset({f"{_OFFICE}annotation", f"{_OFFICE}annotation-end"})

#: Widest row read, in columns, and most copies made of one repeated row that has content.
MAX_COLUMNS: Final = 1024
MAX_ROW_COPIES: Final = 1000
_MAX_SPACES: Final = 200


def _parse_args() -> dict[str, bool]:
    """Say how the XML is parsed: nothing external is loaded.

    Returns:
        The keyword arguments for lxml's parsers.
    """
    return {"resolve_entities": False, "no_network": True, "load_dtd": False, "huge_tree": False}


def _text(element: etree._Element) -> str:
    """Join the text of a paragraph, with the spaces, tabs and line breaks OpenDocument encodes as elements.

    Args:
        element: A ``text:p``, ``text:h`` or other text container.

    Returns:
        Its text; comments (annotations) are left out.
    """
    pieces = [element.text or ""]
    for child in element:
        tag = child.tag
        if not isinstance(tag, str):  # a comment or a processing instruction
            pieces.append(child.tail or "")
            continue
        if tag == _SPACE:
            pieces.append(" " * min(int(child.get(f"{_TEXT}c", "1")), _MAX_SPACES))
        elif tag == _TAB:
            pieces.append("\t")
        elif tag == _BREAK:
            pieces.append("\n")
        elif tag not in _NOT_TEXT:
            pieces.append(_text(child))
        pieces.append(child.tail or "")
    return "".join(pieces)


def _cell_text(cell: etree._Element) -> str:
    """Join the paragraphs of a table cell of a text document on one line.

    Args:
        cell: A ``table:table-cell`` element.

    Returns:
        The cell's text.
    """
    parts = (" ".join(_text(p).split()) for p in cell.iter(_P, _H))
    return " ".join(text for text in parts if text)


def _walk(parent: etree._Element, counters: list[int]) -> Iterator[Part]:
    """Walk the body of a text document.

    Args:
        parent: The ``office:text`` element, or a container inside it (a list, a section).
        counters: The paragraphs and tables numbered so far.

    Yields:
        ``paragraph N`` for each non-empty paragraph or heading, ``table T, row R`` for each row of a table.
    """
    for child in parent:
        tag = child.tag
        if not isinstance(tag, str) or tag in _SKIPPED:
            continue
        if tag in {_P, _H}:
            text = _text(child).strip()
            if text:
                counters[0] += 1
                yield Part(f"paragraph {counters[0]}", text)
        elif tag == _TABLE_EL:
            counters[1] += 1
            for number, row in enumerate(child.iter(_ROW), 1):
                cells = " | ".join(_cell_text(cell) for cell in row if cell.tag in {_CELL, _COVERED})
                if cells.replace("|", "").strip():
                    yield Part(f"table {counters[1]}, row {number}", cells)
        else:
            yield from _walk(child, counters)


def _text_units(path: Path) -> Iterator[Part]:
    """Walk the paragraphs and table rows of an ``.odt``.

    Args:
        path: The file.

    Yields:
        ``paragraph N`` and ``table T, row R`` pieces.
    """
    with zipfile.ZipFile(path) as archive, archive.open(_CONTENT) as stream:
        root = etree.parse(stream, etree.XMLParser(**_parse_args())).getroot()
    body = root.find(f"{_OFFICE}body/{_OFFICE}text")
    if body is not None:
        yield from _walk(body, [0, 0])


def _number(raw: str | None) -> str:
    """Show a number as a spreadsheet would, without float noise.

    Args:
        raw: The ``office:value`` attribute.

    Returns:
        The number, or the attribute as it is if it is not one.
    """
    if raw is None:
        return ""
    try:
        return f"{float(raw):.15g}"
    except ValueError:
        return raw


def _value(cell: etree._Element, cap: int | None) -> str:
    """Show a spreadsheet cell as text.

    Args:
        cell: A ``table:table-cell`` element.
        cap: The most characters to keep, or ``None`` for all.

    Returns:
        The cell's value; its formula if it has no value; empty for an empty cell.
    """
    kind = cell.get(f"{_OFFICE}value-type")
    if kind in {"float", "percentage", "currency"}:
        text = _number(cell.get(f"{_OFFICE}value"))
    elif kind == "date":
        text = cell.get(f"{_OFFICE}date-value") or ""
    elif kind == "time":
        text = cell.get(f"{_OFFICE}time-value") or ""
    elif kind == "boolean":
        text = "TRUE" if cell.get(f"{_OFFICE}boolean-value") == "true" else "FALSE"
    else:
        text = cell.get(f"{_OFFICE}string-value") or " ".join(line for p in cell.iter(_P) if (line := _text(p).strip()))
    if not text:
        formula = cell.get(f"{_TABLE}formula")
        text = formula or ""
    text = " ".join(text.split())
    return text if cap is None else pg.clip(text, cap)


def _row_cells(row: etree._Element, cap: int | None) -> list[str]:
    """Read the cells of a spreadsheet row, unfolding repeated cells up to ``MAX_COLUMNS``.

    Args:
        row: A ``table:table-row`` element.
        cap: The most characters of a cell to keep, or ``None`` for all.

    Returns:
        The cells' text from column A, trailing empty cells removed.
    """
    cells: list[str] = []
    for cell in row:
        if cell.tag not in {_CELL, _COVERED}:
            continue
        repeat = max(1, int(cell.get(f"{_TABLE}number-columns-repeated", "1")))
        text = _value(cell, cap) if cell.tag == _CELL else ""
        cells.extend([text] * min(repeat, MAX_COLUMNS - len(cells)))
    while cells and not cells[-1]:
        cells.pop()
    return cells


def _release(element: etree._Element) -> None:
    """Free an element read, and the siblings before it, so a long sheet does not pile up in memory.

    Args:
        element: The element just ended.
    """
    element.clear()
    parent = element.getparent()
    while parent is not None and element.getprevious() is not None:
        del parent[0]


class Sheets:
    """An ``.ods`` file, as a :class:`~workbench.executor.readers.paging.SheetSource`."""

    def __init__(self, path: Path) -> None:
        """Remember the file; nothing is read until a sheet is asked for.

        Args:
            path: The file.
        """
        self._path = path
        self._names: list[str] | None = None

    @property
    def names(self) -> list[str]:
        """The sheets' names, in order, found by one pass over the file."""
        if self._names is None:
            self._names = [name for name, _, _ in self._walk(None, 1, None) if name is not None]
        return self._names

    def rows(self, sheet: str, start: int, *, cap: int | None) -> Iterator[tuple[int, list[str]]]:
        """Walk the rows of a sheet from ``start`` on that hold something.

        Args:
            sheet: The sheet's name.
            start: The first row number to give.
            cap: The most characters of a cell to give; ``None`` for the whole cell.

        Yields:
            The row's number and its cells as text, from column A.
        """
        for _, number, cells in self._walk(sheet, start, cap):
            if number is not None and cells is not None:
                yield number, cells

    def _walk(
        self, sheet: str | None, start: int, cap: int | None
    ) -> Iterator[tuple[str | None, int | None, list[str] | None]]:
        """Stream ``content.xml``, giving each top-level sheet's name and the rows of the chosen sheet.

        Args:
            sheet: The sheet whose rows to give; ``None`` gives only the names.
            start: The first row number to give.
            cap: The most characters of a cell to keep.

        Yields:
            ``(name, None, None)`` at each sheet, and ``(None, row number, cells)`` for each row with content
            of the chosen sheet; the walk ends after that sheet.
        """
        depth = 0
        number = 0
        chosen = False
        with zipfile.ZipFile(self._path) as archive, archive.open(_CONTENT) as stream:
            for event, element in etree.iterparse(
                stream, events=("start", "end"), tag=(_TABLE_EL, _ROW), **_parse_args()
            ):
                if element.tag == _TABLE_EL:
                    if event == "start":
                        depth += 1
                        if depth == 1:
                            name = element.get(f"{_TABLE}name") or ""
                            number, chosen = 0, name == sheet
                            yield name, None, None
                    else:
                        depth -= 1
                        if depth == 0 and chosen:
                            return
                        _release(element)
                    continue
                if event != "end" or depth != 1:
                    continue
                if chosen:
                    repeat = max(1, int(element.get(f"{_TABLE}number-rows-repeated", "1")))
                    cells = _row_cells(element, cap)
                    if any(cells):
                        for copy in range(min(repeat, MAX_ROW_COPIES)):
                            if number + 1 + copy >= start:
                                yield None, number + 1 + copy, cells
                    number += repeat
                _release(element)


def read(path: Path, *, part: str | None, max_chars: int) -> DocumentText:
    """Read an ``.odt`` by paragraph and table, or an ``.ods`` by range of rows.

    Args:
        path: The file.
        part: For a text: ``None``, ``paragraph N`` or ``table N``. For a spreadsheet: ``None``, a sheet's
            name or a range such as ``Depths!A201:F400``.
        max_chars: The budget in bytes of JSON.

    Returns:
        The parts that fit, whole, and where to continue.
    """
    if path.suffix.lower() == ".ods":
        return pg.read_sheets("ods", Sheets(path), part=part, max_chars=max_chars)
    return pg.read_blocks("odt", _text_units(path), part=part, max_chars=max_chars)


def units(path: Path) -> Iterator[Part]:
    """Walk an ``.odt``'s paragraphs and table rows, or an ``.ods``'s cells.

    Args:
        path: The file.

    Yields:
        ``paragraph N``, ``table T, row R``, or a cell's address such as ``Depths!B2``.
    """
    if path.suffix.lower() == ".ods":
        yield from pg.cells(Sheets(path))
    else:
        yield from _text_units(path)
