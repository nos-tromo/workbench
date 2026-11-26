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
from lxml import etree
from openpyxl.packaging.manifest import Manifest
from openpyxl.packaging.relationship import get_dependents, get_rels_path
from openpyxl.reader.excel import _find_workbook_part
from openpyxl.xml.constants import ARC_CONTENT_TYPES, SHARED_STRINGS, SHEET_MAIN_NS
from openpyxl.xml.functions import fromstring

from workbench.executor.readers import DocumentText, Measure, Part, UnreadableFile, check, tick
from workbench.executor.readers import paging as pg

_WHITESPACE = re.compile(r"[\r\n\t]+")

_CHUNK: Final = 1 << 20
_OVERLAP: Final = 1024

#: A row element with a number of seven digits or more, and a cell whose column has three letters or more or
#: whose row has seven digits or more: the only ones that can be past the end of a sheet. (``XFD`` has three
#: letters; a cell in a column of one or two letters only needs a look when its row has seven digits.) What lies
#: between the name and ``r`` may not hold a ``<``, which no tag of well-formed XML has: so each try ends at the
#: next ``<`` and the scan takes time in proportion to the part, where ``[^>]*?`` ran on to the next ``>`` from
#: every ``<row`` of a run such as ``<row<row<row...`` and took time in proportion to its square.
_BIG_ROW = re.compile(rb'<row\b[^<]*?\sr="(\d{7,})"')
_BIG_CELL = re.compile(rb'<c\b[^<]*?\sr="(?:([A-Za-z]{3,})(\d+)|([A-Za-z]{1,2})(\d{7,}))"')
_TOO_BIG: Final = "the workbook uses rows or columns beyond the limits of a spreadsheet"
_TOO_MANY: Final = "the workbook has too many sheets"
_TOO_MANY_NAMES: Final = "the workbook defines too many names"
_TOO_MANY_STRINGS: Final = "the workbook holds too many strings"

#: Most defined names and shared strings a workbook may have. openpyxl reads both whole before any row is read,
#: in time and memory in proportion to their number, and no timer can stop it before its first row.
MAX_DEFINED_NAMES: Final = 10_000
MAX_SHARED_STRINGS: Final = 1_000_000

_XML_ARGS: Final = {"resolve_entities": False, "no_network": True, "load_dtd": False, "huge_tree": False}

#: Most children the content types may have (``Default`` and ``Override`` entries), and the largest the part may be.
MAX_CONTENT_TYPES: Final = 20_000
MAX_MANIFEST_BYTES: Final = 8 * 1024 * 1024

#: Most relationships the workbook may have; its relationships part may be as large as the content types.
MAX_RELATIONSHIPS: Final = 20_000

#: Largest workbook part, in bytes. openpyxl reads it whole into memory.
MAX_WORKBOOK_BYTES: Final = 16 * 1024 * 1024

#: Most elements of any kind the shared-strings part may have; a string takes a few at the most.
MAX_STRING_ELEMENTS: Final = 10_000_000

_TOO_MANY_PARTS: Final = "the workbook declares too many parts"
_TOO_MANY_ENTRIES: Final = "the workbook lists too many views or references"
_TOO_LARGE: Final = "the workbook's description is too large"
_STRING: Final = f"{{{SHEET_MAIN_NS}}}si"
_CHUNK_BYTES: Final = 1 << 20


def _local(tag: object) -> str:
    """Give an element's name without its namespace.

    Args:
        tag: The tag lxml reports.

    Returns:
        The name after the namespace; empty for a tag that is not a name (a comment).
    """
    return tag.rpartition("}")[2] if isinstance(tag, str) else ""


class _RootChildren:
    """An lxml parser target that counts the children of the root element and builds no tree."""

    def __init__(self, limit: int, refusal: str) -> None:
        """Start the count.

        Args:
            limit: The most children allowed.
            refusal: The message to refuse with.
        """
        self._limit = limit
        self._refusal = refusal
        self._depth = 0
        self._count = 0

    def start(self, tag: str, attrib: object) -> None:
        """Count a child of the root.

        Args:
            tag: The element's tag.
            attrib: Its attributes.

        Raises:
            UnreadableFile: There are more children than the limit.
        """
        self._depth += 1
        if self._depth == 2:
            self._count += 1
            if self._count > self._limit:
                raise UnreadableFile(self._refusal)
            if self._count % 1024 == 0:
                check()

    def end(self, tag: str) -> None:
        """Leave an element.

        Args:
            tag: The element's tag.
        """
        self._depth -= 1

    def close(self) -> None:
        """Finish."""


class _Containers:
    """An lxml parser target that counts the children of each child of the root, as openpyxl will convert them.

    openpyxl turns every child of ``sheets`` (and of the other sequences of the workbook element: views,
    external references, pivot caches) into an object, whatever its tag is called, and every ``definedName``.
    """

    def __init__(self) -> None:
        """Start the count."""
        self._depth = 0
        self._count = 0
        self._limit = pg.MAX_SHEETS
        self._refusal = _TOO_MANY

    def start(self, tag: str, attrib: object) -> None:
        """Count a grandchild of the root, against the limit of its parent.

        Args:
            tag: The element's tag.
            attrib: Its attributes.

        Raises:
            UnreadableFile: A container has more children than the limit for its kind.
        """
        self._depth += 1
        if self._depth == 2:
            self._count = 0
            if _local(tag) == "definedNames":
                self._limit, self._refusal = MAX_DEFINED_NAMES, _TOO_MANY_NAMES
            elif _local(tag) == "sheets":
                self._limit, self._refusal = pg.MAX_SHEETS, _TOO_MANY
            else:
                self._limit, self._refusal = pg.MAX_SHEETS, _TOO_MANY_ENTRIES
        elif self._depth == 3:
            self._count += 1
            if self._count > self._limit:
                raise UnreadableFile(self._refusal)
            if self._count % 1024 == 0:
                check()

    def end(self, tag: str) -> None:
        """Leave an element.

        Args:
            tag: The element's tag.
        """
        self._depth -= 1

    def close(self) -> None:
        """Finish."""


class _Strings:
    """An lxml parser target that counts what openpyxl's string table reader keeps, without building a tree.

    The reader keeps every element called ``si`` in the main namespace, wherever it is; elements of any other
    kind are counted too, because the reader walks them all.
    """

    def __init__(self) -> None:
        """Start the count."""
        self._strings = 0
        self._elements = 0

    def start(self, tag: str, attrib: object) -> None:
        """Count an element.

        Args:
            tag: The element's tag.
            attrib: Its attributes.

        Raises:
            UnreadableFile: There are too many strings, or too many elements.
        """
        self._elements += 1
        if tag == _STRING:
            self._strings += 1
            if self._strings > MAX_SHARED_STRINGS:
                raise UnreadableFile(_TOO_MANY_STRINGS)
        if self._elements > MAX_STRING_ELEMENTS:
            raise UnreadableFile(_TOO_MANY_STRINGS)
        if self._elements % 4096 == 0:
            check()

    def close(self) -> None:
        """Finish."""


def _scan(archive: zipfile.ZipFile, part: str, target: object, *, largest: int | None = None) -> None:
    """Run a counting target over one part, in chunks.

    Args:
        archive: The workbook, open.
        part: The part's name in the zip, exactly as openpyxl will look it up.
        target: An lxml parser target.
        largest: The most bytes the part may be, as the zip declares it; ``None`` for no limit.

    Raises:
        UnreadableFile: The target refused, or the part is larger than ``largest``.
        KeyError: There is no such part.
    """
    if largest is not None and archive.getinfo(part).file_size > largest:
        raise UnreadableFile(_TOO_LARGE)
    parser = etree.XMLParser(target=target, **_XML_ARGS)
    with archive.open(part) as stream:
        while chunk := stream.read(_CHUNK_BYTES):
            tick()
            parser.feed(chunk)
    parser.close()


def check_counts(archive: zipfile.ZipFile) -> str:
    """Refuse a workbook with more sheets, defined names or shared strings than openpyxl can load in time.

    The parts are found as openpyxl finds them, with its own manifest reader and workbook part finder, so that no
    file can name them in a way that this check misses and openpyxl does not (a content type given as the default
    for ``xml``, a part name with one character too many in front, a part moved). openpyxl turns every child of
    the workbook's ``sheets`` into a sheet, whatever it is called, and reads every shared string; the counts are
    made in a streaming pass with no tree, which stops at the limit.

    Args:
        archive: The workbook, open.

    Returns:
        The name of the workbook part in the zip, as openpyxl will open it.

    Raises:
        UnreadableFile: The content types, the workbook description or the shared strings are larger or more
            numerous than the limits.
    """
    _scan(archive, ARC_CONTENT_TYPES, _RootChildren(MAX_CONTENT_TYPES, _TOO_MANY_PARTS), largest=MAX_MANIFEST_BYTES)
    package: Any = Manifest.from_tree(fromstring(archive.read(ARC_CONTENT_TYPES)))
    workbook: str = _find_workbook_part(package).PartName[1:]
    _scan(archive, workbook, _Containers(), largest=MAX_WORKBOOK_BYTES)
    strings = package.find(SHARED_STRINGS)
    if strings is not None:
        _scan(archive, strings.PartName[1:], _Strings())
    return workbook


def related_parts(archive: zipfile.ZipFile, workbook: str) -> list[str]:
    """List the parts the workbook's relationships name, as openpyxl resolves them: where it opens its worksheets.

    openpyxl's ``ExcelReader.read_worksheets`` opens each sheet from the target of its relationship, normalised
    by ``get_dependents``, whatever the part is called and whatever its extension (``xl/ws/s1.bin`` is opened as
    readily as ``xl/worksheets/sheet1.xml``); a target that is not in the zip is passed over. The same functions
    resolve them here. Every target that is in the zip is listed, not only the worksheets: an external target that
    names a member is opened too, and scanning a part that is not a sheet does no harm.

    Args:
        archive: The workbook, open.
        workbook: The workbook part, from :func:`check_counts`.

    Returns:
        The names of those parts in the zip, sorted.

    Raises:
        UnreadableFile: The relationships part is larger or has more entries than the limits.
    """
    rels = get_rels_path(workbook)
    members = set(archive.namelist())
    if rels not in members:
        return []  # openpyxl cannot open the workbook either
    _scan(archive, rels, _RootChildren(MAX_RELATIONSHIPS, _TOO_MANY_PARTS), largest=MAX_MANIFEST_BYTES)
    return sorted({str(rel.target) for rel in get_dependents(archive, rels)} & members)


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
    """Refuse a workbook that names a row or a column no spreadsheet has, or has too much to load in time.

    openpyxl's read-only reader gives an empty row for every row number that is missing, so a file
    that puts one cell at row 2 000 000 000 would keep it busy for hours, and a cell at column ``ZZZ`` would
    make it build a row wider than a sheet. Every part the workbook's relationships name (:func:`related_parts`)
    is scanned for such numbers, in chunks, before openpyxl opens the workbook. The scan reads bytes: a number
    it cannot see (spelled with a character reference, say) is still stopped by the row loop's own bound.

    Args:
        path: The ``.xlsx`` or ``.xlsm`` file, already checked as a zip.

    Raises:
        UnreadableFile: A row is numbered above 1 048 576, a column is past ``XFD``, or the counts of
            :func:`check_counts` are over their limits.
    """
    with zipfile.ZipFile(path) as archive:
        # The parts openpyxl will open as worksheets are the targets of the workbook's relationships, under
        # whatever name and extension they give, not the members called ``xl/worksheets/*.xml``.
        for name in related_parts(archive, check_counts(archive)):
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


def measure(path: Path, *, chars_cap: int) -> Measure:
    """Count the rows that hold something, through every sheet, and the characters of a full read.

    Args:
        path: The ``.xlsx`` or ``.xlsm`` file.
        chars_cap: The most characters to count.

    Returns:
        The measure.
    """
    with closing(Workbook(path)) as book:
        return pg.measure_sheets("xlsx", book, chars_cap=chars_cap)


def units(path: Path) -> Iterator[Part]:
    """Walk every cell with content.

    Args:
        path: The ``.xlsx`` or ``.xlsm`` file.

    Yields:
        Each cell labelled with its address, such as ``Sheet1!C5``.
    """
    with closing(Workbook(path)) as book:
        yield from pg.cells(book)
