"""Where each filled value comes from: the checks of a file's page, sheet and cell, and the sources appendix.

A value's source is one of four kinds: ``file`` (a file of the chat, with a page, a sheet or a cell), ``job`` (the
``run_python`` run that computed it), ``user`` (the user said it) or ``assistant`` (the model wrote it). The api has
checked that a file and a job are the user's and the chat's; here, in the job's copy of the file, that the page,
sheet or cell is there. A page is a PDF's page or a presentation's slide; a sheet and a cell are a workbook's, the
cell in A1 notation within the cells of the sheet that hold something. Nothing checks that the value is really
there: that would take a record of everything read.

The filled file ends with the sources: a last table in a document, a sheet of its own in a workbook, each row a
field, its value, the kind of its source and the reference.
"""

import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Final

from workbench import protocol
from workbench.executor import readers
from workbench.executor.context import JobContext
from workbench.executor.readers import paging as pg
from workbench.executor.templates import Refusal, safe_text, shown
from workbench.executor.templates.xlsx_placeholders import sheet_title, write_text

#: The columns of the appendix.
HEADER: Final = ["field", "value", "source kind", "reference"]

#: The appendix's title: a heading above the table, the name of the sheet.
TITLE: Final = "Sources"

_CELL: Final = re.compile(r"([A-Za-z]{1,3})([1-9]\d{0,6})")
_PAGED: Final = frozenset({".pdf", ".pptx", ".potx"})
_SHEETS: Final = frozenset({".xlsx", ".xlsm", ".ods"})


def _column(letters: str) -> int:
    """Turn a column's letters into its number.

    Args:
        letters: ``A``, ``XFD``, ...

    Returns:
        The number, from 1.
    """
    number = 0
    for char in letters.upper():
        number = number * 26 + ord(char) - ord("A") + 1
    return number


class _Files:
    """The pages and sheets of the files the sources name, each file read once."""

    def __init__(self, ctx: JobContext) -> None:
        """Start with nothing read.

        Args:
            ctx: The job's surroundings.
        """
        self._ctx = ctx
        self._pages: dict[str, int] = {}
        self._sheets: dict[str, dict[str, readers.Extent | None]] = {}

    def _path(self, name: str, source: Mapping[str, Any]) -> Path:
        """Find the job's copy of a source's file.

        Args:
            name: The field.
            source: The source.

        Returns:
            The copy.

        Raises:
            Refusal: It is not in the job's folder.
        """
        path = self._ctx.work_dir / protocol.check_input_path(str(source["path"]))
        if not path.is_file():
            raise Refusal("invalid_source", f"{name}: {source['virtual']} is not in this conversation")
        return path

    def pages(self, name: str, source: Mapping[str, Any]) -> int:
        """Count a source file's pages or slides.

        Args:
            name: The field.
            source: Its source.

        Returns:
            How many there are.

        Raises:
            Refusal: The file has no pages, or cannot be read.
        """
        path = self._path(name, source)
        if path.suffix.lower() not in _PAGED:
            raise Refusal(
                "invalid_source", f"{name}: a page applies to PDF pages and PPTX slides, not to {source['virtual']}"
            )
        key = str(source["path"])
        if key not in self._pages:
            self._pages[key] = self._read(name, source, lambda: readers.count_pages(path, file_mb=self._ctx.file_mb))
        return self._pages[key]

    def sheets(self, name: str, source: Mapping[str, Any]) -> dict[str, readers.Extent | None]:
        """List a source file's sheets and the cells of each that hold something.

        Args:
            name: The field.
            source: Its source.

        Returns:
            The sheets and their extents.

        Raises:
            Refusal: The file is not a workbook, or cannot be read.
        """
        path = self._path(name, source)
        if path.suffix.lower() not in _SHEETS:
            raise Refusal(
                "invalid_source", f"{name}: a sheet and a cell apply to XLSX and ODS, not to {source['virtual']}"
            )
        key = str(source["path"])
        if key not in self._sheets:
            self._sheets[key] = self._read(name, source, lambda: readers.sheet_extents(path, file_mb=self._ctx.file_mb))
        return self._sheets[key]

    @staticmethod
    def _read[T](name: str, source: Mapping[str, Any], read: Callable[[], T]) -> T:
        """Read a source file with a guarded reader; a file that cannot be read is a refusal naming the field.

        Args:
            name: The field.
            source: Its source.
            read: The reading, a function of no arguments.

        Returns:
            What it returns.

        Raises:
            Refusal: The file cannot be read.
        """
        try:
            return read()
        except (readers.UnreadableFile, readers.UnsupportedFormat) as exc:
            raise Refusal("invalid_source", f"{name}: {source['virtual']} cannot be read: {exc}") from None


def _sheet_of(name: str, source: Mapping[str, Any], sheets: dict[str, readers.Extent | None]) -> str:
    """Find a source's sheet, its case ignored as a spreadsheet does.

    Args:
        name: The field.
        source: Its source.
        sheets: The file's sheets.

    Returns:
        The sheet's name as the file writes it.

    Raises:
        Refusal: The file has no such sheet; the message lists its sheets.
    """
    wanted = str(source["sheet"])
    found = next((sheet for sheet in sheets if sheet.casefold() == wanted.casefold()), None)
    if found is None:
        listed = ", ".join(pg.quote_sheet(sheet) for sheet in list(sheets)[:20])
        raise Refusal("invalid_source", f"{name}: {source['virtual']} has no sheet {wanted!r}; its sheets are {listed}")
    return found


def check(values: Mapping[str, Mapping[str, Any]], ctx: JobContext) -> dict[str, str]:
    """Check every file source's page, sheet and cell in the job's copy of its file.

    Args:
        values: The values by field name, each with its ``source``.
        ctx: The job's surroundings.

    Returns:
        The sheet each field's source names, as its file writes the name, by field.

    Raises:
        Refusal: A page, sheet or cell is not there, or does not apply to the file's type; the message names the
            field.
        readers.ReadTimeout: Reading a file ran past the deadline.
    """
    files = _Files(ctx)
    named: dict[str, str] = {}
    for name, entry in values.items():
        source = entry["source"]
        if source.get("kind") != "file":
            continue
        page, sheet, cell = source.get("page"), source.get("sheet"), source.get("cell")
        if page is not None:
            pages = files.pages(name, source)
            if not 1 <= int(page) <= pages:
                noun = "slides" if str(source["path"]).lower().endswith((".pptx", ".potx")) else "pages"
                raise Refusal(
                    "invalid_source", f"{name}: {source['virtual']} has {pages} {noun}; page {page} is not one of them"
                )
        if sheet is None and cell is None:
            continue
        sheets = files.sheets(name, source)
        if sheet is None:
            raise Refusal("invalid_source", f"{name}: a cell needs its sheet")
        found = named[name] = _sheet_of(name, source, sheets)
        if cell is None:
            continue
        match = _CELL.fullmatch(str(cell))
        if match is None:
            raise Refusal("invalid_source", f"{name}: {cell!r} is not a cell in A1 notation, such as B7")
        extent = sheets[found]
        row, column = int(match[2]), _column(match[1])
        if extent is None or not (extent[0] <= row <= extent[1] and extent[2] <= column <= extent[3]):
            used = (
                "is empty"
                if extent is None
                else f"holds something from {pg.column_letters(extent[2])}{extent[0]} to "
                f"{pg.column_letters(extent[3])}{extent[1]}"
            )
            raise Refusal("invalid_source", f"{name}: {cell} lies outside the cells of sheet {found!r}, which {used}")
    return named


def reference(source: Mapping[str, Any], sheet: str | None = None) -> str:
    """Write a source's reference for the appendix.

    Args:
        source: The source.
        sheet: The sheet it names, as its file writes the name; the source's own spelling if ``None``.

    Returns:
        The file's virtual path with its page, sheet or cell; the job's id; the user's note; nothing for the assistant.
    """
    kind = source.get("kind")
    if kind == "file":
        where = str(source["virtual"])
        if source.get("page") is not None:
            where += f", page {source['page']}"
        sheet = sheet if sheet is not None else source.get("sheet")
        cell = source.get("cell")
        if sheet is not None and cell is not None:
            where += f", {pg.quote_sheet(str(sheet))}!{str(cell).upper()}"
        elif sheet is not None:
            where += f", sheet {pg.quote_sheet(str(sheet))}"
        return where
    if kind == "job":
        return str(source.get("job_id", ""))
    if kind == "user":
        return str(source.get("note") or "")
    return ""


def rows(
    values: Mapping[str, Mapping[str, Any]], filled: list[str], sheets: Mapping[str, str], warnings: list[str]
) -> list[list[str]]:
    """Make the appendix's rows: one per filled field, in the order the values were given.

    Args:
        values: The values by field name, each with its ``source``.
        filled: The fields that were filled.
        sheets: The sheets the sources name, as :func:`check` found them.
        warnings: Where a warning goes, should a value hold characters a file cannot.

    Returns:
        The rows, each field, value, source kind and reference, all text.
    """
    done = set(filled)
    made: list[list[str]] = []
    for name, entry in values.items():
        if name in done:
            source = entry["source"]
            cells = [name, shown(entry.get("value")), str(source.get("kind", "")), reference(source, sheets.get(name))]
            made.append([safe_text(name, cell, warnings) for cell in cells])
    return made


def to_document(document: Any, table: list[list[str]]) -> None:
    """End a document with the sources: a heading, then a table with a header row.

    Args:
        document: The python-docx document.
        table: The rows (:func:`rows`).
    """
    heading = document.add_paragraph()
    heading.add_run(TITLE).bold = True
    styles = {style.name for style in document.styles}
    grid = document.add_table(rows=1, cols=len(HEADER), style="Table Grid" if "Table Grid" in styles else None)
    for cell, text in zip(grid.rows[0].cells, HEADER, strict=True):
        cell.text = text
    for row in table:
        for cell, text in zip(grid.add_row().cells, row, strict=True):
            cell.text = text


def to_workbook(book: Any, table: list[list[str]]) -> None:
    """Add the sources to a workbook, as a sheet of its own after the others, every cell text.

    Args:
        book: The openpyxl workbook.
        table: The rows (:func:`rows`).
    """
    sheet = book.create_sheet(sheet_title(book, TITLE))
    for number, row in enumerate([HEADER, *table], 1):
        for column, text in enumerate(row, 1):
            write_text(sheet.cell(row=number, column=column), text)
