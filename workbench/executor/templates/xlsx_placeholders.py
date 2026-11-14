"""``{{ }}`` placeholders in a workbook: a cell for a value, an anchor cell for a table that grows downwards.

A cell that holds nothing but ``{{ name }}`` takes the value as it is: a number stays a number, a bool a bool. A
placeholder inside longer text is replaced there, and the cell stays text. A cell that holds nothing but
``{{ table:name }}`` is where a table starts: its rows go downwards from it, its cells to the right, and none of them
may hold anything already.

Values are data, never formulas: a string is stored as text even if it starts with ``=``, ``+``, ``-`` or ``@``,
whether it is a field's value, a table's cell or an entry of the sources sheet (:func:`write_text`).

openpyxl loads and saves the workbook; it does not keep everything a workbook can hold (charts, pivot tables,
pictures and other drawings), and the answer warns when the template has any (:func:`unkept_parts`).
"""

import re
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from openpyxl.cell.cell import MergedCell

from workbench.executor.readers import paging as pg
from workbench.executor.templates import Field, Refusal, merge, safe_text

_NAME: Final = r"([^\W\d]\w*)"
_PLACEHOLDER: Final = re.compile(r"\{\{\s*" + _NAME + r"\s*\}\}")
_ALONE: Final = re.compile(r"\s*\{\{\s*" + _NAME + r"\s*\}\}\s*")
_ANCHOR: Final = re.compile(r"\s*\{\{\s*table\s*:\s*" + _NAME + r"\s*\}\}\s*")

#: Parts of a workbook openpyxl may not keep when it saves one.
_UNKEPT: Final = ("xl/charts/", "xl/pivotTables/", "xl/drawings/", "xl/media/")


@dataclass(frozen=True)
class _Spot:
    """One place of a field: a cell of a sheet.

    Attributes:
        sheet: The sheet's title.
        row: The cell's row, from 1.
        column: Its column, from 1.
        kind: ``alone`` (the cell is the placeholder), ``inline`` (it is in the cell's text) or ``table`` (an anchor).
        name: The field's name.
    """

    sheet: str
    row: int
    column: int
    kind: str
    name: str

    @property
    def place(self) -> str:
        """The cell's reference, such as ``Summary!B4``."""
        return f"{pg.quote_sheet(self.sheet)}!{pg.column_letters(self.column)}{self.row}"


def _cells(book: Any) -> Iterator[tuple[Any, Any]]:
    """Walk the cells that hold text, sheet by sheet, row by row.

    Only the cells the workbook has are walked, never the empty ones of the range a sheet declares.

    Args:
        book: The workbook.

    Yields:
        Each sheet and each of its cells that holds a string.
    """
    for sheet in book.worksheets:
        for key in sorted(sheet._cells):
            cell = sheet._cells[key]
            if isinstance(cell.value, str) and "{{" in cell.value:
                yield sheet, cell


def _spots(book: Any) -> list[_Spot]:
    """Find every placeholder and table anchor.

    Args:
        book: The workbook.

    Returns:
        Their places, in order.
    """
    spots: list[_Spot] = []
    for sheet, cell in _cells(book):
        text: str = cell.value
        anchor = _ANCHOR.fullmatch(text)
        alone = _ALONE.fullmatch(text)
        if anchor is not None:
            spots.append(_Spot(sheet.title, cell.row, cell.column, "table", anchor[1]))
        elif alone is not None:
            spots.append(_Spot(sheet.title, cell.row, cell.column, "alone", alone[1]))
        else:
            for name in dict.fromkeys(_PLACEHOLDER.findall(text)):
                spots.append(_Spot(sheet.title, cell.row, cell.column, "inline", name))
    return spots


def fields(book: Any) -> list[Field]:
    """List the workbook's placeholders and table anchors.

    Args:
        book: The workbook.

    Returns:
        Each name once, at its first cell.
    """
    return merge(
        [
            Field(s.name, "table", "table", None, s.place)
            if s.kind == "table"
            else Field(s.name, "placeholder", "text", None, s.place)
            for s in _spots(book)
        ]
    )


def write_text(cell: Any, text: str) -> None:
    """Store a string in a cell as text, whatever it starts with: data, never a formula.

    Args:
        cell: The cell.
        text: The text, already free of characters XML cannot hold.
    """
    cell.value = text
    cell.data_type = "s"


def _scalar(value: object) -> bool:
    """Tell whether a value fits in one cell.

    Args:
        value: The value.

    Returns:
        Whether it is text, a number, a bool or nothing.
    """
    return value is None or isinstance(value, str | int | float | bool)


def _write(cell: Any, name: str, value: object, warnings: list[str]) -> None:
    """Write one value into a cell with its own type.

    Args:
        cell: The cell.
        name: The field.
        value: A scalar.
        warnings: Where a warning goes.
    """
    if isinstance(value, str):
        write_text(cell, safe_text(name, value, warnings))
    else:
        cell.value = value


def _inline(value: object) -> str:
    """Write a value inside a cell's text.

    Args:
        value: A scalar.

    Returns:
        Its text: a bool as ``TRUE`` or ``FALSE``, nothing as empty.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return str(value)


def _rows(name: str, value: object) -> list[list[object]]:
    """Take a table's value: a list of rows, each a list of plain values.

    Args:
        name: The field.
        value: The value.

    Returns:
        The rows.

    Raises:
        Refusal: It is anything else.
    """
    if isinstance(value, list) and all(isinstance(row, list) and all(_scalar(item) for item in row) for row in value):
        return value
    raise Refusal(
        "invalid_value", f"{name} is a table and takes a list of rows of plain values (text, numbers, true/false)"
    )


def _check_room(sheet: Any, spot: _Spot, rows: Sequence[Sequence[object]]) -> None:
    """Refuse a table that would leave the sheet or write over a cell that holds something.

    Args:
        sheet: The anchor's sheet.
        spot: The anchor.
        rows: The table.

    Raises:
        Refusal: A cell it would write is past the sheet's last row or column, holds something, or is merged.
    """
    width = max((len(row) for row in rows), default=0)
    if spot.row + len(rows) - 1 > pg.MAX_ROW or spot.column + width - 1 > pg.MAX_COLUMN:
        raise Refusal("invalid_value", f"{spot.name} has more rows or columns than fit below {spot.place}")
    for down, row in enumerate(rows):
        for right in range(len(row)):
            key = (spot.row + down, spot.column + right)
            if key == (spot.row, spot.column):
                continue
            cell = sheet._cells.get(key)
            if cell is not None and (isinstance(cell, MergedCell) or cell.value not in (None, "")):
                where = f"{pg.column_letters(key[1])}{key[0]}"
                raise Refusal(
                    "invalid_value",
                    f"{spot.name} would write over {where}, which is not empty; the table needs empty cells below "
                    f"{spot.place}",
                )


def _claim(claimed: dict[tuple[str, int, int], str], spot: _Spot, rows: Sequence[Sequence[object]]) -> None:
    """Take the cells a table writes, refusing one that another table or a placeholder writes too.

    Args:
        claimed: The cells taken so far, by sheet, row and column, with the field that takes each.
        spot: The table's anchor.
        rows: The table.

    Raises:
        Refusal: A cell is taken already; the message names both fields and the cell.
    """
    for down, row in enumerate(rows):
        for right in range(len(row)):
            key = (spot.sheet, spot.row + down, spot.column + right)
            other = claimed.get(key)
            if other is not None:
                where = f"{pg.column_letters(key[2])}{key[1]}"
                raise Refusal(
                    "invalid_value",
                    f"{spot.name} would write {where}, which {other} writes too; the tables would overwrite each other",
                )
            claimed[key] = spot.name


def fill(book: Any, values: Mapping[str, object], warnings: list[str]) -> list[str]:
    """Fill every placeholder and table anchor; every value is checked before any cell changes.

    A field that is given no value is emptied, as a ``{{ }}`` placeholder of a document renders empty.

    Args:
        book: The workbook; it is changed.
        values: The values by field name.
        warnings: Where warnings go.

    Returns:
        The names of the fields filled.

    Raises:
        Refusal: A field does not take its value, or a table has no room: a cell it would write holds something,
            or another table or placeholder writes it too.
    """
    spots = _spots(book)
    tables: dict[str, list[list[object]]] = {}
    claimed: dict[tuple[str, int, int], str] = {
        (spot.sheet, spot.row, spot.column): spot.name for spot in spots if spot.kind != "table"
    }
    for spot in spots:
        if spot.name not in values:
            continue
        value = values[spot.name]
        if spot.kind == "table":
            tables[spot.name] = _rows(spot.name, value)
            _check_room(book[spot.sheet], spot, tables[spot.name])
            _claim(claimed, spot, tables[spot.name])
        elif not _scalar(value):
            raise Refusal("invalid_value", f"{spot.name} takes a single value: text, a number or true/false")

    def value_of(match: re.Match[str]) -> str:
        return safe_text(match[1], _inline(values.get(match[1])), warnings)

    filled: dict[str, None] = {}
    texts: dict[tuple[str, int, int], None] = {}
    for spot in spots:
        if spot.name in values:
            filled[spot.name] = None
        sheet = book[spot.sheet]
        cell = sheet.cell(row=spot.row, column=spot.column)
        if spot.kind == "inline":
            texts[(spot.sheet, spot.row, spot.column)] = None
        elif spot.kind == "alone":
            _write(cell, spot.name, values.get(spot.name), warnings)
        else:
            cell.value = None
            for down, row in enumerate(tables.get(spot.name, [])):
                for right, item in enumerate(row):
                    _write(sheet.cell(row=spot.row + down, column=spot.column + right), spot.name, item, warnings)
    for title, row, column in texts:
        cell = book[title].cell(row=row, column=column)
        write_text(cell, _PLACEHOLDER.sub(value_of, str(cell.value)))
    return list(filled)


def unkept_parts(path: Path) -> bool:
    """Tell whether a workbook holds parts openpyxl may not keep: charts, pivot tables, pictures, drawings.

    Args:
        path: The template, already checked as a zip.

    Returns:
        Whether it has any.
    """
    with zipfile.ZipFile(path) as archive:
        return any(name.startswith(_UNKEPT) for name in archive.namelist())


def sheet_title(book: Any, wanted: str) -> str:
    """Find a sheet title the workbook does not use yet: ``wanted``, else ``wanted (2)`` and so on.

    Sheet titles are compared as Excel does, ignoring case.

    Args:
        book: The workbook.
        wanted: The title wanted.

    Returns:
        A free title.
    """
    taken = {title.casefold() for title in book.sheetnames}
    title, number = wanted, 1
    while title.casefold() in taken:
        number += 1
        title = f"{wanted} ({number})"
    return title
