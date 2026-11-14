"""What the readers share: filling an answer within a budget, and the two ways of paging.

A document is paged in *parts*. A reader offers its parts in order, and a :class:`Collector` takes them
while they fit; the first that does not fit becomes ``next_part``. Block documents (Word, OpenDocument
text) page by paragraph and table (:func:`read_blocks`); sheets page by ranges of rows
(:func:`read_sheets`). All sizes are measured as the JSON the part will be sent in, so a budget in
characters is a budget in bytes of the answer.
"""

import json
import re
from collections.abc import Iterable, Iterator
from typing import Final, Protocol

from workbench.executor.readers import DocumentText, NoSuchPart, Part

#: Rows in one range of a sheet.
RANGE_ROWS: Final = 200

#: Longest cell shown when reading; a search matches the whole cell.
CELL_CAP: Final = 300

CUT_MARK: Final = " [cut]"

_TABLE_ROW: Final = re.compile(r"table (\d+), row (\d+)")
_PARAGRAPH_REF: Final = re.compile(r"(?:paragraphs?\s*)?(\d+)")
_TABLE_REF: Final = re.compile(r"table\s*(\d+)(?:,\s*row\s*\d+)?")
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


def part_size(label: str, text: str) -> int:
    """Measure a part as the answer sends it: compact JSON in UTF-8, with the comma that follows it.

    Args:
        label: The part's label.
        text: The part's text.

    Returns:
        The size in bytes.
    """
    return len(json.dumps({"label": label, "text": text}, ensure_ascii=False, separators=(",", ":")).encode()) + 1


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

    def add(self, label: str, text: str) -> bool:
        """Offer the next part.

        Args:
            label: The part's label.
            text: The part's text.

        Returns:
            ``True`` while more parts may be offered; ``False`` once this one was refused, which makes it
            ``next_part``.
        """
        if self._closed:
            self.next_part = label
            return False
        cost = part_size(label, text)
        if cost <= self._left:
            self.parts.append(Part(label, text))
            self._left -= cost
            return True
        self._closed = True
        if self.parts:
            self.next_part = label
            return False
        fitted = self._fit(label, text)
        if fitted is None:
            self.next_part = label
            return False
        self.parts.append(Part(label, fitted))
        self.cut = label
        return True

    def _fit(self, label: str, text: str) -> str | None:
        """Find the longest start of ``text``, with the cut mark, that fits what is left.

        Args:
            label: The part's label.
            text: The part's text.

        Returns:
            The cut text, or ``None`` if not even the label and the mark fit.
        """
        low, high = 0, len(text)
        best: str | None = None
        while low <= high:
            middle = (low + high) // 2
            candidate = text[:middle] + CUT_MARK
            if part_size(label, candidate) <= self._left:
                best, low = candidate, middle + 1
            else:
                high = middle - 1
        return best


def blocks(pieces: Iterable[Part]) -> Iterator[Part]:
    """Group the rows of each table into one block, leaving paragraphs as they are.

    Args:
        pieces: Paragraphs (``paragraph N``) and table rows (``table T, row R``) in document order.

    Yields:
        The paragraphs, and each table as one part labelled ``table T`` with a ``row R: a | b`` line per row.
    """
    label = ""
    rows: list[str] = []
    for piece in pieces:
        match = _TABLE_ROW.fullmatch(piece.label)
        if match is None:
            if rows:
                yield Part(label, "\n".join(rows))
                rows = []
            yield piece
            continue
        name = f"table {match[1]}"
        if rows and name != label:
            yield Part(label, "\n".join(rows))
            rows = []
        label = name
        rows.append(f"row {match[2]}: {piece.text}")
    if rows:
        yield Part(label, "\n".join(rows))


def _block_label(part: str | None) -> str | None:
    """Turn what the caller asked for into a block's label.

    Args:
        part: ``paragraph 12``, ``12``, ``table 2`` or ``table 2, row 3``, in any case; or ``None``.

    Returns:
        The label of the block to start at, or ``None`` for the start of the document.

    Raises:
        NoSuchPart: It is none of those forms.
    """
    if part is None or not part.strip():
        return None
    wanted = " ".join(part.lower().split())
    paragraph = _PARAGRAPH_REF.fullmatch(wanted)
    if paragraph:
        return f"paragraph {int(paragraph[1])}"
    table = _TABLE_REF.fullmatch(wanted)
    if table:
        return f"table {int(table[1])}"
    raise NoSuchPart("a part of this document is 'paragraph N' or 'table N'")


def read_blocks(kind: str, pieces: Iterable[Part], *, part: str | None, max_chars: int) -> DocumentText:
    """Read a document of paragraphs and tables from ``part`` on.

    Args:
        kind: The reader family to report.
        pieces: Paragraphs and table rows in document order (see :func:`blocks`).
        part: The label to start at, or ``None``.
        max_chars: The budget in bytes of JSON.

    Returns:
        The blocks that fit, whole, and where to continue.

    Raises:
        NoSuchPart: ``part`` is not a paragraph or table of the document.
    """
    wanted = _block_label(part)
    collector = Collector(max_chars)
    total = 0
    started = wanted is None
    offering = True
    for block in blocks(pieces):
        total += 1
        if not started:
            if block.label != wanted:
                continue
            started = True
        if offering:
            offering = collector.add(block.label, block.text)
    if not started:
        raise NoSuchPart(f"this document has no {wanted}; it has {total} paragraphs and tables")
    return DocumentText(
        kind=kind,
        parts=collector.parts,
        pages_without_text=[],
        total_parts=total,
        truncated=collector.truncated,
        next_part=collector.next_part,
        cut=collector.cut,
    )


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
        """The sheets' names, in order."""
        ...

    def rows(self, sheet: str, start: int, *, cap: int | None) -> Iterator[tuple[int, list[str]]]:
        """Walk the rows of a sheet from ``start`` on that hold something.

        Args:
            sheet: The sheet's name.
            start: The first row number to give.
            cap: The most characters of a cell to give; ``None`` for the whole cell.

        Yields:
            The row's number and its cells as text, from column A, with empty cells as empty text.
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


def read_sheets(kind: str, source: SheetSource, *, part: str | None, max_chars: int) -> DocumentText:
    """Read one range of rows of a workbook.

    The range starts at the first row with content at or after the one asked for, spans at most
    ``RANGE_ROWS`` rows, and ends sooner if the budget does. Each row is a line ``<row>: a | b | c``
    with the cells from column A; the label gives the range, such as ``Sheet1!A1:F200``.

    Args:
        kind: The reader family to report.
        source: The workbook.
        part: Where to start; ``None`` for the first sheet's first row.
        max_chars: The budget in bytes of JSON.

    Returns:
        The range, and where to continue: the next range of the sheet, or the next sheet.

    Raises:
        NoSuchPart: The sheet is not in the workbook.
    """
    names = source.names
    sheet, start, last = parse_sheet_ref(part, names)
    order = names.index(sheet)
    while True:
        result = _read_range(kind, source, sheet, start, last, max_chars, total=len(names))
        if result.parts or part is not None or order + 1 >= len(names):
            break
        order += 1
        sheet, start = names[order], 1
    if result.next_part is None and order + 1 < len(names):
        result.next_part = quote_sheet(names[order + 1])
        result.truncated = True
    return result


def _read_range(
    kind: str, source: SheetSource, sheet: str, start: int, last: int | None, max_chars: int, *, total: int
) -> DocumentText:
    """Read one range of one sheet.

    Args:
        kind: The reader family to report.
        source: The workbook.
        sheet: The sheet's name.
        start: The first row to look at.
        last: The last row the caller asked for, if any.
        max_chars: The budget in bytes of JSON.
        total: The number of sheets.

    Returns:
        The range; its ``next_part`` is the next range of this sheet if there is one.
    """
    quoted = quote_sheet(sheet)
    left = max_chars - part_size(f"{quoted}!A{_LAST_ROW}:ZZZ{_LAST_ROW}", "")
    lines: list[str] = []
    first = end = 0
    window = RANGE_ROWS
    width = 1
    next_row: int | None = None
    cut = False
    for number, row in source.rows(sheet, start, cap=CELL_CAP):
        if not lines:
            first = number
            window = RANGE_ROWS if last is None else max(1, min(RANGE_ROWS, last - first + 1))
        elif number >= first + window:
            next_row = number
            break
        shown = list(row)
        while shown and not shown[-1]:
            shown.pop()
        line = f"{number}: " + " | ".join(shown)
        cost = len(json.dumps(line, ensure_ascii=False).encode()) + 1
        if cost > left:
            if lines:
                next_row = number
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
        return DocumentText(kind=kind, parts=[], pages_without_text=[], total_parts=total)
    label = f"{quoted}!A{first}:{column_letters(width)}{end}"
    next_part = None
    if next_row is not None:
        next_part = f"{quoted}!A{next_row}:{column_letters(width)}{next_row + RANGE_ROWS - 1}"
    return DocumentText(
        kind=kind,
        parts=[Part(label, "\n".join(lines))],
        pages_without_text=[],
        total_parts=total,
        truncated=next_part is not None or cut,
        next_part=next_part,
        cut=label if cut else None,
    )


def cells(source: SheetSource) -> Iterator[Part]:
    """Walk every cell with content of a workbook, for a search.

    Args:
        source: The workbook.

    Yields:
        Each cell, labelled with its address (``Sheet1!C5``), with its whole text.
    """
    for sheet in source.names:
        quoted = quote_sheet(sheet)
        for number, row in source.rows(sheet, 1, cap=None):
            for column, text in enumerate(row, 1):
                if text:
                    yield Part(f"{quoted}!{column_letters(column)}{number}", text)
