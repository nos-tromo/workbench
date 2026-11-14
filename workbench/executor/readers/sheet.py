"""Excel workbooks (``.xlsx``, ``.xlsm``): sheets paged by ranges of rows, searched by cell.

The workbook is opened read-only and streamed, so a large sheet is not loaded to read its first
rows. Cached results are shown; a formula cell that has none (a file written by a library rather than
saved by Excel) shows its formula text, found by walking a second, formula-reading copy of the sheet in
step with the first. Macros are never run: ``.xlsm`` is read like ``.xlsx``.
"""

import re
from collections.abc import Iterator
from contextlib import closing
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import openpyxl

from workbench.executor.readers import DocumentText, Part
from workbench.executor.readers import paging as pg

_WHITESPACE = re.compile(r"[\r\n\t]+")


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
        """Open the workbook twice, read-only: for the cached values and for the formulas.

        Args:
            path: The file.
        """
        self._values: Any = openpyxl.load_workbook(path, read_only=True, data_only=True)
        self._formulas: Any = openpyxl.load_workbook(path, read_only=True, data_only=False)

    def close(self) -> None:
        """Close both copies."""
        self._values.close()
        self._formulas.close()

    @property
    def names(self) -> list[str]:
        """The sheets' names, in order."""
        return [str(name) for name in self._values.sheetnames]

    def rows(self, sheet: str, start: int, *, cap: int | None) -> Iterator[tuple[int, list[str]]]:
        """Walk the rows of a sheet from ``start`` on that hold something.

        Args:
            sheet: The sheet's name.
            start: The first row number to give.
            cap: The most characters of a cell to give; ``None`` for the whole cell.

        Yields:
            The row's number and its cells as text, from column A.
        """
        values = self._values[sheet]
        formulas = self._formulas[sheet]
        # The sizes a file declares can be wrong; stream what is there.
        values.reset_dimensions()
        formulas.reset_dimensions()
        pairs = zip(
            values.iter_rows(min_row=start, values_only=True),
            formulas.iter_rows(min_row=start, values_only=True),
            strict=False,
        )
        for number, (cached, written) in enumerate(pairs, start):
            width = max(len(cached), len(written))
            row = [
                _text(cached[i] if i < len(cached) else None, written[i] if i < len(written) else None, cap)
                for i in range(width)
            ]
            if any(row):
                yield number, row


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
