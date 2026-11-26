"""What the readers share: filling an answer within a budget, and the two ways of paging.

A document is paged in *parts*. A reader offers its parts in order, and a :class:`Collector` takes them
while they fit; the first that does not fit becomes ``next_part``. Block documents (Word, OpenDocument
text) page by paragraph and table (:func:`read_blocks`); sheets page by ranges of rows
(:func:`read_sheets`). All sizes are measured as the JSON the part will be sent in, so a budget in
characters is a budget in bytes of the answer. :func:`measure_blocks` and :func:`measure_sheets` count the same
documents without paging them.
"""

import json
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Final, Protocol

from workbench.executor.readers import CharTally, DocumentText, Measure, NoSuchPart, Part, tick

#: Rows in one range of a sheet.
RANGE_ROWS: Final = 200

#: Longest cell shown when reading; a search matches the whole cell.
CELL_CAP: Final = 300

#: The limits of a spreadsheet: rows, columns (XFD) and, for these readers, sheets.
MAX_ROW: Final = 1_048_576
MAX_COLUMN: Final = 16_384
MAX_SHEETS: Final = 1000

#: The widest unit number a reservation sizes for: a sheet row numbered through every sheet.
MAX_UNIT: Final = MAX_ROW * MAX_SHEETS

#: Longest sheet name read; Excel's own limit is 31. A reference to a sheet carries its name, and the api's answer
#: holds a label of at most 300 characters.
MAX_SHEET_NAME: Final = 100

CUT_MARK: Final = " [cut]"

_TABLE_ROW: Final = re.compile(r"table (\d+), row (\d+)")
_PARAGRAPH_REF: Final = re.compile(r"(?:paragraphs?\s*)?(\d+)")
_TABLE_REF: Final = re.compile(r"table\s*(\d+)(?:\s*,\s*rows?\s*(\d+)(?:\s*-\s*(\d+))?)?")
_SHEET_REF: Final = re.compile(r"[A-Za-z]{0,3}(\d*)(?::[A-Za-z]{0,3}(\d*))?")
_LAST_ROW: Final = 1_048_576


def clip(text: str, cap: int = CELL_CAP) -> str:
    """Shorten a text to ``cap`` characters, marking the cut.

    Args:
        text: The text.
        cap: The most characters to keep.

    Returns:
        ``text``, or its start followed by an ellipsis.
    """
    return text if len(text) <= cap else text[: cap - 1] + "…"


def part_size(label: str, text: str, units: tuple[int, int] | None = None, *, cut: bool = False) -> int:
    """Measure a part as the answer sends it: compact JSON in UTF-8, with the comma that follows it.

    Args:
        label: The part's label.
        text: The part's text.
        units: The units it covers; sent as ``null`` when ``None``, as the runner sends it.
        cut: Whether it is sent as cut short.

    Returns:
        The size in bytes.
    """
    sent = {"label": label, "text": text, "units": units, "cut": cut}
    return len(json.dumps(sent, ensure_ascii=False, separators=(",", ":")).encode()) + 1


class Collector:
    """Fills an answer with whole parts, in order, until the next one would not fit.

    The first part is never left out: if it alone is more than the budget it is cut and marked, so
    paging always moves on. Offer parts until :meth:`add` returns ``False``.

    Attributes:
        parts: The parts taken.
        next_part: The label of the first part that was not taken; ``None`` if every part offered was.
        cut: The label of a part that was cut short, if one was.
    """

    def __init__(self, budget: int) -> None:
        """Start empty.

        Args:
            budget: Bytes of JSON the parts may take together.
        """
        self.parts: list[Part] = []
        self.next_part: str | None = None
        self.cut: str | None = None
        self._left = budget
        self._closed = False

    @property
    def truncated(self) -> bool:
        """Whether text was left out: parts that did not fit, or one cut short."""
        return self.next_part is not None or self.cut is not None

    def add(self, label: str, text: str, units: tuple[int, int] | None = None) -> bool:
        """Offer the next part.

        Args:
            label: The part's label.
            text: The part's text.
            units: The units it covers.

        Returns:
            ``True`` while more parts may be offered; ``False`` once this one was refused, which makes it
            ``next_part``.
        """
        if self._closed:
            self.next_part = label
            return False
        cost = part_size(label, text, units)
        if cost <= self._left:
            self.parts.append(Part(label, text, units))
            self._left -= cost
            return True
        self._closed = True
        if self.parts:
            self.next_part = label
            return False
        fitted = self._fit(label, text, units)
        if fitted is None:
            self.next_part = label
            return False
        self.parts.append(Part(label, fitted, units))
        self.cut = label
        return True

    def grow(self, label: str, text: str, following: str) -> bool:
        """Replace the last part with a longer version of it, if the longer one fits; it keeps its units.

        Args:
            label: The part's new label.
            text: The part's new text.
            following: The label to continue at if it does not fit.

        Returns:
            ``True`` if the part grew. ``False`` if it did not: the part is as it was, nothing more is taken,
            and ``following`` is ``next_part``.
        """
        last = self.parts[-1]
        extra = part_size(label, text, last.units) - part_size(last.label, last.text, last.units)
        if not self._closed and extra <= self._left:
            self.parts[-1] = Part(label, text, last.units)
            self._left -= extra
            return True
        self._closed = True
        self.next_part = following
        return False

    def _fit(self, label: str, text: str, units: tuple[int, int] | None) -> str | None:
        """Find the longest start of ``text``, with the cut mark, that fits what is left, sized as sent cut.

        Args:
            label: The part's label.
            text: The part's text.
            units: The units it covers.

        Returns:
            The cut text, or ``None`` if not even the label and the mark fit.
        """
        low, high = 0, len(text)
        best: str | None = None
        while low <= high:
            middle = (low + high) // 2
            candidate = text[:middle] + CUT_MARK
            if part_size(label, candidate, units, cut=True) <= self._left:
                best, low = candidate, middle + 1
            else:
                high = middle - 1
        return best


@dataclass
class _Start:
    """Where a block read begins.

    Attributes:
        paragraph: The paragraph to start at, or ``None`` if it starts at a table.
        table: The table to start at.
        row: The row of that table to start at.
        last: The last row of that table to read, if the caller named one.
    """

    paragraph: int | None = None
    table: int = 0
    row: int = 1
    last: int | None = None


def _start(part: str | None) -> _Start | None:
    """Turn what the caller asked for into where a block read begins.

    Args:
        part: ``paragraph 12``, ``12``, ``table 2``, ``table 2, row 30`` or ``table 2, rows 30-60``, in any
            case; or ``None``.

    Returns:
        The start, or ``None`` for the start of the document.

    Raises:
        NoSuchPart: It is none of those forms.
    """
    if part is None or not part.strip():
        return None
    wanted = " ".join(part.lower().split())
    paragraph = _PARAGRAPH_REF.fullmatch(wanted)
    if paragraph:
        return _Start(paragraph=int(paragraph[1]))
    table = _TABLE_REF.fullmatch(wanted)
    if table:
        return _Start(table=int(table[1]), row=int(table[2] or 1), last=int(table[3]) if table[3] else None)
    raise NoSuchPart("a part of this document is 'paragraph N', 'table N' or 'table N, rows A-B'")


def _run_label(table: int, first: int, last: int) -> str:
    """Label a run of rows of a table.

    Args:
        table: The table's number.
        first: The first row of the run.
        last: The last row of the run.

    Returns:
        ``table T, row R`` for one row, ``table T, rows A-B`` for more.
    """
    return f"table {table}, row {first}" if first == last else f"table {table}, rows {first}-{last}"


def read_blocks(kind: str, pieces: Iterable[Part], *, part: str | None, max_chars: int) -> DocumentText:
    """Read a document of paragraphs and tables from ``part`` on.

    A paragraph is a part. A table is paged by rows: consecutive rows that fit are one part, labelled
    ``table T, rows A-B``, and the first row that does not fit is where to go on (``table T, row R``), so a
    table of any length can be read to its end.

    The units are blocks, numbered as they come: a paragraph is the next block, and a table is the next block
    at its first row with text, so the blocks are ``total_parts``. A paragraph covers its own block. A table is
    one unit: only a run of its rows that reaches its last row with text covers the table's block; any other
    run covers none, so a table paged with ``next_part`` counts once, on the run that ends it.

    Args:
        kind: The reader family to report.
        pieces: Paragraphs (``paragraph N``) and table rows (``table T, row R``, text ``a | b``) in document order.
        part: The label to start at, or ``None``.
        max_chars: The budget in bytes of JSON.

    Returns:
        The parts that fit, whole, and where to continue.

    Raises:
        NoSuchPart: ``part`` is not a paragraph or table of the document.
    """
    start = _start(part)
    collector = Collector(max_chars)
    paragraphs = 0
    blocks = 0
    tables: dict[int, int] = {}  # a table's number: its block
    last_rows: dict[int, int] = {}  # a table's number: its last row with text
    runs: dict[int, tuple[int, int]] = {}  # a run's place in the parts: its table and its last row
    started = start is None
    offering = True
    open_table = 0
    first_row = 0
    for piece in pieces:
        tick()
        match = _TABLE_ROW.fullmatch(piece.label)
        if match is None:
            paragraphs += 1
            blocks += 1
            open_table = 0
            if not started:
                if start is None or start.paragraph is None or piece.label != f"paragraph {start.paragraph}":
                    continue
                started = True
            if offering:
                offering = collector.add(piece.label, piece.text, (blocks, blocks))
            continue
        table, row = int(match[1]), int(match[2])
        if table not in tables:
            blocks += 1
            tables[table] = blocks
        last_rows[table] = row
        if not started:
            if start is None or start.paragraph is not None or table != start.table or row < start.row:
                continue
            started = True
        if not offering:
            continue
        if start is not None and start.last is not None and table == start.table and row > start.last:
            offering = False
            continue
        line = f"row {row}: {piece.text}"
        if open_table == table and collector.parts:
            last = collector.parts[-1]
            offering = collector.grow(_run_label(table, first_row, row), f"{last.text}\n{line}", piece.label)
        else:
            open_table, first_row = table, row
            offering = collector.add(piece.label, line, (tables[table], tables[table]))
        if offering:
            runs[len(collector.parts) - 1] = (table, row)
    if not started:
        raise NoSuchPart(f"this document has no such part; it has {paragraphs} paragraphs and {len(tables)} tables")
    for place, (table, row) in runs.items():
        if row != last_rows[table]:
            collector.parts[place].units = None
    return DocumentText(
        kind=kind,
        parts=collector.parts,
        pages_without_text=[],
        total_parts=paragraphs + len(tables),
        truncated=collector.truncated,
        next_part=collector.next_part,
        cut=collector.cut,
    )


def measure_blocks(kind: str, pieces: Iterable[Part], *, chars_cap: int) -> Measure:
    """Count the blocks of a document of paragraphs and tables, and the characters a full read shows.

    The total is what :func:`read_blocks` gives as ``total_parts``: the paragraphs and the tables that have a row.
    A paragraph is counted as it is, and each row of a table as ``row R: a | b``, as a read shows it; no break between
    pieces is counted. Counting stops at the cap, the walk goes on to count the blocks.

    Args:
        kind: The reader family to report.
        pieces: Paragraphs (``paragraph N``) and table rows (``table T, row R``, text ``a | b``) in document order.
        chars_cap: The most characters to count.

    Returns:
        The measure.
    """
    tally = CharTally(chars_cap)
    paragraphs = 0
    tables: set[int] = set()
    counting = True
    for piece in pieces:
        tick()
        match = _TABLE_ROW.fullmatch(piece.label)
        if match is None:
            paragraphs += 1
            text = piece.text
        else:
            tables.add(int(match[1]))
            text = f"row {match[2]}: {piece.text}"
        if counting:
            counting = tally.add(text)
    return Measure.of(kind, paragraphs + len(tables), tally)


def numbered_window(part: str | None, total: int, noun: str) -> tuple[int, int]:
    """Understand a part of a document of numbered pages or slides: ``3``, ``page 3``, ``3-5`` or ``pages 3-5``.

    Args:
        part: What the caller asked for, or ``None`` for the whole document.
        total: The number of pages or slides.
        noun: ``page`` or ``slide``.

    Returns:
        The first and last number to read.

    Raises:
        NoSuchPart: The part is not such a reference, or the number is past the end.
    """
    if part is None or not part.strip():
        return 1, total
    match = re.fullmatch(rf"(?:{noun}s?\s*)?(\d+)(?:\s*-\s*(\d+))?", part.strip().lower())
    if match is None:
        raise NoSuchPart(f"a part of this document is a {noun}: '{noun} 3', or a run of them: '{noun}s 3-5'")
    first = int(match[1])
    last = int(match[2]) if match[2] else total
    if not 1 <= first <= total or last < first:
        raise NoSuchPart(f"this document has {total} {noun}s")
    return first, min(last, total)


def column_letters(number: int) -> str:
    """Name a column the way a spreadsheet does.

    Args:
        number: The column's number, from 1.

    Returns:
        ``A`` for 1, ``Z`` for 26, ``AA`` for 27.
    """
    letters = ""
    while number > 0:
        number, rest = divmod(number - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return letters or "A"


def quote_sheet(name: str) -> str:
    """Write a sheet's name as a reference does: in quotes unless it is plain.

    Args:
        name: The sheet's name.

    Returns:
        ``name``, or ``'name'`` with its quotes doubled.
    """
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name):
        return name
    return "'" + name.replace("'", "''") + "'"


class SheetSource(Protocol):
    """A workbook the range reader pages through."""

    @property
    def names(self) -> list[str]:
        """The sheets' names, in order; at most ``MAX_SHEETS``."""
        ...

    def walk(self, sheet: str | None, start: int, *, cap: int | None) -> Iterator[tuple[str, int, list[str]]]:
        """Walk the rows that hold something, in one pass over the workbook.

        Args:
            sheet: The sheet to start at, or ``None`` for the first.
            start: The first row number to give of that sheet; later sheets are given from their first row.
            cap: The most characters of a cell to give; ``None`` for the whole cell.

        Yields:
            The sheet's name, the row's number and its cells as text, from column A, empty cells as empty text.
        """
        ...


def parse_sheet_ref(part: str | None, names: list[str]) -> tuple[str, int, int | None]:
    """Understand a sheet part: ``Sheet1``, ``Sheet1!A201``, ``Sheet1!A201:F400`` or ``'My sheet'!A1:B9``.

    Args:
        part: What the caller asked for, or ``None`` for the first sheet.
        names: The workbook's sheet names.

    Returns:
        The sheet's name, the first row, and the last row if the caller gave one.

    Raises:
        NoSuchPart: The sheet is not in the workbook, or the reference is not understood.
    """
    if not names:
        raise NoSuchPart("the workbook has no sheets")
    if part is None or not part.strip():
        return names[0], 1, None
    text = part.strip()
    sheet, rows = text, ""
    if "!" in text:
        head, _, tail = text.rpartition("!")
        if _SHEET_REF.fullmatch(tail):
            sheet, rows = head, tail
    if len(sheet) >= 2 and sheet[0] == sheet[-1] == "'":
        sheet = sheet[1:-1].replace("''", "'")
    chosen = next((name for name in names if name.casefold() == sheet.casefold()), None)
    if chosen is None:
        raise NoSuchPart("this workbook has no such sheet; its sheets are: " + ", ".join(quote_sheet(n) for n in names))
    first, last = 1, None
    match = _SHEET_REF.fullmatch(rows) if rows else None
    if match is not None:
        first = max(1, min(int(match[1] or 1), _LAST_ROW))
        last = int(match[2]) if match[2] else None
    return chosen, first, last


def _trimmed(row: list[str]) -> list[str]:
    """Drop the empty cells at the end of a row.

    Args:
        row: A row's cells, from column A.

    Returns:
        The cells up to the last one that holds something.
    """
    end = len(row)
    while end and not row[end - 1]:
        end -= 1
    return row[:end]


def row_line(number: int, row: list[str]) -> str:
    """Show a row of a sheet as a read does: ``<row>: a | b | c``, the empty cells at its end left out.

    Args:
        number: The row's number in its sheet.
        row: Its cells, from column A.

    Returns:
        The line.
    """
    return f"{number}: " + " | ".join(_trimmed(row))


def read_sheets(kind: str, source: SheetSource, *, part: str | None, max_chars: int) -> DocumentText:
    """Read one range of rows of a workbook.

    The range starts at the first row with content at or after the one asked for, spans at most
    ``RANGE_ROWS`` rows, and ends sooner if the budget does. Each row is a line ``<row>: a | b | c``
    with the cells from column A; the label gives the range, such as ``Sheet1!A1:F200``. With no part, a
    first sheet that is empty is passed over. The workbook is walked once, from the start of the range to its
    end, however many sheets it has.

    The units are the rows that hold something, numbered through the sheets in their order. A range asked for
    by name gets its first unit from one more walk, :func:`rows_before`, once its own has ended.

    Args:
        kind: The reader family to report.
        source: The workbook.
        part: Where to start; ``None`` for the first sheet's first row.
        max_chars: The budget in bytes of JSON.

    Returns:
        The range, and where to continue: the next range of the sheet, or the next sheet with content.

    Raises:
        NoSuchPart: The sheet is not in the workbook.
    """
    names = source.names
    sheet, start, last = parse_sheet_ref(part, names)
    explicit = part is not None and bool(part.strip())
    lines: list[str] = []
    current = ""
    first = end = 0
    window = RANGE_ROWS
    width = 1
    left = max_chars - part_size(f"{quote_sheet(sheet)}!A{MAX_ROW}:ZZZ{MAX_ROW}", "", (MAX_UNIT, MAX_UNIT))
    next_part: str | None = None
    cut = False
    for name, number, row in source.walk(sheet if explicit else None, start, cap=CELL_CAP):
        tick()
        if not lines:
            if explicit and name != sheet:
                next_part = quote_sheet(name)
                break
            current, first = name, number
            window = RANGE_ROWS if last is None else max(1, min(RANGE_ROWS, last - first + 1))
        elif name != current:
            next_part = quote_sheet(name)
            break
        elif number >= first + window:
            next_part = f"{quote_sheet(current)}!A{number}:{column_letters(width)}{number + RANGE_ROWS - 1}"
            break
        shown = _trimmed(row)
        line = row_line(number, shown)
        cost = len(json.dumps(line, ensure_ascii=False).encode()) + 1
        if cost > left:
            if lines:
                next_part = f"{quote_sheet(current)}!A{number}:{column_letters(width)}{number + RANGE_ROWS - 1}"
                break
            line = clip(line, max(1, left - len(CUT_MARK.encode()) - 8)) + CUT_MARK
            cut = True
            left = 0
            lines.append(line)
            end, width = number, max(width, len(shown))
            continue
        left -= cost
        lines.append(line)
        end, width = number, max(width, len(shown))
    if not lines:
        return DocumentText(
            kind=kind,
            parts=[],
            pages_without_text=[],
            total_parts=len(names),
            truncated=next_part is not None,
            next_part=next_part,
        )
    label = f"{quote_sheet(current)}!A{first}:{column_letters(width)}{end}"
    offset = rows_before(source, current, first) if explicit else 0
    return DocumentText(
        kind=kind,
        parts=[Part(label, "\n".join(lines), (offset + 1, offset + len(lines)))],
        pages_without_text=[],
        total_parts=len(names),
        truncated=next_part is not None or cut,
        next_part=next_part,
        cut=label if cut else None,
    )


def measure_sheets(kind: str, source: SheetSource, *, chars_cap: int) -> Measure:
    """Count the rows of a workbook that hold something, and the characters a full read shows.

    One walk over every sheet with every cell whole, so a cell that a read clips at ``CELL_CAP`` is counted at its
    full length. The total is every row, counted past the cap; the characters are the lines :func:`row_line` makes,
    counted to the cap.

    Args:
        kind: The reader family to report.
        source: The workbook.
        chars_cap: The most characters to count.

    Returns:
        The measure.
    """
    tally = CharTally(chars_cap)
    total = 0
    counting = True
    for _, number, row in source.walk(None, 1, cap=None):
        tick()
        total += 1
        if counting:
            counting = tally.add(row_line(number, row))
    return Measure.of(kind, total, tally)


def rows_before(source: SheetSource, sheet: str, row: int) -> int:
    """Count the rows that hold something in the sheets before ``sheet``, and in ``sheet`` before ``row``.

    One walk from the start of the workbook, which stops at the first row of ``sheet`` numbered ``row`` or more, or
    at the next sheet once ``sheet`` has been reached. Cells are read to one character: only whether a row holds
    something counts.

    Args:
        source: The workbook.
        sheet: A sheet that holds something.
        row: A row number of that sheet.

    Returns:
        How many rows come before it, which is the number of the unit before it.
    """
    count = 0
    reached = False
    for name, number, _ in source.walk(None, 1, cap=1):
        tick()
        if name == sheet:
            if number >= row:
                break
            reached = True
        elif reached:
            break
        count += 1
    return count


def cells(source: SheetSource) -> Iterator[Part]:
    """Walk every cell with content of a workbook, for a search.

    Args:
        source: The workbook.

    Yields:
        Each cell, labelled with its address (``Sheet1!C5``), with its whole text.
    """
    for sheet, number, row in source.walk(None, 1, cap=None):
        tick()
        quoted = quote_sheet(sheet)
        for column, text in enumerate(row, 1):
            if text:
                yield Part(f"{quoted}!{column_letters(column)}{number}", text)
