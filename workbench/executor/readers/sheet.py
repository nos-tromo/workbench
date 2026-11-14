"""Excel workbooks (``.xlsx``, ``.xlsm``): sheets paged by ranges of rows, searched by cell.

The workbook is opened read-only and streamed, so a large sheet is not loaded to read its first
rows. Cached results are shown; a formula cell that has none (a file written by a library rather than
saved by Excel) shows its formula text, found by walking a second, formula-reading copy of the sheet in
step with the first. Macros are never run: ``.xlsm`` is read like ``.xlsx``.
"""

import re
import zipfile
from collections.abc import Iterator
from contextlib import closing
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Final

import openpyxl

from workbench.executor.readers import DocumentText, Part, UnreadableFile, check, tick
from workbench.executor.readers import paging as pg

_WHITESPACE = re.compile(r"[\r\n\t]+")

_SHEET_PART = re.compile(r"xl/worksheets/[^/]+\.xml")
_CHUNK: Final = 1 << 20
_OVERLAP: Final = 64

#: A row element with a number of seven digits or more, and a cell whose column has three letters or more or
#: whose row has seven digits or more: the only ones that can be past the end of a sheet. (``XFD`` has three
#: letters; a cell in a column of one or two letters only needs a look when its row has seven digits.)
_BIG_ROW = re.compile(rb'<row\b[^>]*?\sr="(\d{7,})"')
_BIG_CELL = re.compile(rb'<c\b[^>]*?\sr="(?:([A-Za-z]{3,})(\d+)|([A-Za-z]{1,2})(\d{7,}))"')
_TOO_BIG: Final = "the workbook uses rows or columns beyond the limits of a spreadsheet"
_TOO_MANY: Final = "the workbook has too many sheets"


def _column_number(letters: str) -> int:
    """Turn a column's letters into its number.

    Args:
        letters: ``A``, ``XFD``, ...

    Returns:
        The column's number, from 1.
    """
    number = 0
    for char in letters.upper():
        number = number * 26 + ord(char) - ord("A") + 1
    return number


def check_bounds(path: Path) -> None:
    """Refuse a workbook that names a row or a column no spreadsheet has, or holds too many sheets.

    openpyxl's read-only reader gives an empty row for every row number that is missing, so a file
    that puts one cell at row 2 000 000 000 would keep it busy for hours, and a cell at column ``ZZZZZZ`` would
    make it build a row of billions. The sheet files are scanned for such numbers, in chunks, before openpyxl
    opens the workbook.

    Args:
        path: The ``.xlsx`` or ``.xlsm`` file, already checked as a zip.

    Raises:
        UnreadableFile: A row is numbered above 1 048 576, a column is past ``XFD``, or there are more than
            ``MAX_SHEETS`` sheets.
    """
    with zipfile.ZipFile(path) as archive:
        sheets = [name for name in archive.namelist() if _SHEET_PART.fullmatch(name)]
        if len(sheets) > pg.MAX_SHEETS:
            raise UnreadableFile(_TOO_MANY)
        for name in sheets:
            check()
            with archive.open(name) as member:
                tail = b""
                while chunk := member.read(_CHUNK):
                    tick()
                    window = tail + chunk
                    for match in _BIG_ROW.finditer(window):
                        if int(match[1]) > pg.MAX_ROW:
                            raise UnreadableFile(_TOO_BIG)
                    for cell in _BIG_CELL.finditer(window):
                        letters, digits = (cell[1], cell[2]) if cell[1] else (cell[3], cell[4])
                        if _column_number(letters.decode()) > pg.MAX_COLUMN or int(digits) > pg.MAX_ROW:
                            raise UnreadableFile(_TOO_BIG)
                    tail = chunk[-_OVERLAP:]


def _text(value: object, formula: object, cap: int | None) -> str:
    """Show a cell as text.

    Args:
        value: The cell's cached value, or ``None``.
        formula: What the cell holds when formulas are not evaluated; a formula starts with ``=``.
        cap: The most characters to keep, or ``None`` for all.

    Returns:
        The cached value as text; the formula if there is no cached value; empty for an empty cell.
    """
    if value is None:
        shown = formula if isinstance(formula, str) and formula.startswith("=") else ""
    elif isinstance(value, bool):
        shown = "TRUE" if value else "FALSE"
    elif isinstance(value, datetime):
        shown = value.date().isoformat() if value.time() == time(0) else value.isoformat(sep=" ")
    elif isinstance(value, date | time):
        shown = value.isoformat()
    elif isinstance(value, float):
        shown = f"{value:.15g}"
    else:
        shown = str(value)
    shown = _WHITESPACE.sub(" ", str(shown)).strip()
    return shown if cap is None else pg.clip(shown, cap)


class Workbook:
    """An ``.xlsx`` file opened for reading, as a :class:`~workbench.executor.readers.paging.SheetSource`."""

    def __init__(self, path: Path) -> None:
        """Check the file's bounds, then open the workbook twice, read-only: for the cached values and the formulas.

        Args:
            path: The file.

        Raises:
            UnreadableFile: The file names rows or columns past the limits of a spreadsheet, or has too many sheets.
        """
        check_bounds(path)
        self._books: list[Any] = []
        # The file objects are ours, so that they are closed even if openpyxl fails to open the workbook.
        self._files = [path.open("rb"), path.open("rb")]
        try:
            self._values: Any = openpyxl.load_workbook(self._files[0], read_only=True, data_only=True)
            self._books.append(self._values)
            self._formulas: Any = openpyxl.load_workbook(self._files[1], read_only=True, data_only=False)
            self._books.append(self._formulas)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """Close both copies and the file."""
        for book in self._books:
            book.close()
        for handle in self._files:
            handle.close()

    @property
    def names(self) -> list[str]:
        """The sheets' names, in order."""
        names = [str(name) for name in self._values.sheetnames]
        if any(len(name) > pg.MAX_SHEET_NAME for name in names):
            raise UnreadableFile("a sheet's name is too long")
        return names

    def _rows(self, sheet: str, start: int, cap: int | None) -> Iterator[tuple[int, list[str]]]:
        """Walk the rows of one sheet from ``start`` on that hold something.

        Args:
            sheet: The sheet's name.
            start: The first row number to give.
            cap: The most characters of a cell to give; ``None`` for the whole cell.

        Yields:
            The row's number and its cells as text, from column A.

        Raises:
            UnreadableFile: A row number or a width beyond the limits of a spreadsheet.
        """
        values = self._values[sheet]
        formulas = self._formulas[sheet]
        # The sizes a file declares can be wrong; stream what is there.
        values.reset_dimensions()
        formulas.reset_dimensions()
        # From row 1, not from ``start``: openpyxl skips the rows before ``min_row`` inside one call, in which
        # the deadline cannot be looked at.
        pairs = zip(values.iter_rows(values_only=True), formulas.iter_rows(values_only=True), strict=False)
        for number, (cached, written) in enumerate(pairs, 1):
            tick()
            if number > pg.MAX_ROW:
                raise UnreadableFile(_TOO_BIG)
            if number < start:
                continue
            width = max(len(cached), len(written))
            if width > pg.MAX_COLUMN:
                raise UnreadableFile(_TOO_BIG)
            row = [
                _text(cached[i] if i < len(cached) else None, written[i] if i < len(written) else None, cap)
                for i in range(width)
            ]
            if any(row):
                yield number, row

    def walk(self, sheet: str | None, start: int, *, cap: int | None) -> Iterator[tuple[str, int, list[str]]]:
        """Walk the rows that hold something, sheet after sheet.

        Args:
            sheet: The sheet to start at, or ``None`` for the first.
            start: The first row number to give of that sheet; later sheets are given from their first row.
            cap: The most characters of a cell to give; ``None`` for the whole cell.

        Yields:
            The sheet's name, the row's number and its cells as text.
        """
        names = self.names
        first = 0 if sheet is None else names.index(sheet)
        for index in range(first, len(names)):
            check()
            for number, row in self._rows(names[index], start if index == first else 1, cap):
                yield names[index], number, row


def read(path: Path, *, part: str | None, max_chars: int) -> DocumentText:
    """Read one range of rows of a workbook.

    Args:
        path: The ``.xlsx`` or ``.xlsm`` file.
        part: ``None``, a sheet name, or a range such as ``Sheet1!A201:F400``.
        max_chars: The budget in bytes of JSON.

    Returns:
        The range (at most 200 rows) and where to continue.
    """
    with closing(Workbook(path)) as book:
        return pg.read_sheets("xlsx", book, part=part, max_chars=max_chars)


def units(path: Path) -> Iterator[Part]:
    """Walk every cell with content.

    Args:
        path: The ``.xlsx`` or ``.xlsm`` file.

    Yields:
        Each cell labelled with its address, such as ``Sheet1!C5``.
    """
    with closing(Workbook(path)) as book:
        yield from pg.cells(book)
