"""The table preview of a job's csv and xlsx outputs, read in the job once its runner has answered.

A caller that shows the tables a call wrote gets, for each csv or xlsx output, its header and at most
``PREVIEW_ROWS`` rows of at most ``PREVIEW_COLUMNS`` cells, each cut plainly to ``PREVIEW_CELL`` characters, so that
it never parses an office file itself. It is read here, in the job's own process, under the job's limits and with no
network. A workbook passes the readers' guards first (the zip check, the sheet reader's bounds and counts); a formula
shows its cached value or its text and is never run, and nothing a file links to is fetched. All of a job's previews
share one deadline, ``PREVIEW_TOTAL_S`` after the runner has answered: a file that cannot be read, or whose header and
rows are not read by then, has no preview.

The job's own code could have written the preview of its own file, so the supervisor trusts only its shape and caps.
"""

import csv
import os
import time
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING, Final

from workbench.executor import readers
from workbench.executor.timer import TimeLimit, limit_until
from workbench.protocol import PREVIEW_CELL, PREVIEW_COLUMNS, PREVIEW_ROWS, TablePreview
from workbench.tables import DELIMITERS

if TYPE_CHECKING:
    from workbench.executor.readers.sheet import Workbook

#: The outputs that get a preview, by suffix in any case.
PREVIEW_SUFFIXES: Final = (".csv", ".xlsx")

#: Seconds all of a job's previews may take together.
PREVIEW_TOTAL_S: Final = 2.0

#: Most rows below the header that are counted; a table with more has no count.
PREVIEW_COUNT_MAX: Final = 100_000

#: The file beside the job's result that holds the previews, by output name.
PREVIEWS_FILE: Final = "previews.json"

#: Bytes at the start of a csv its delimiter is sniffed in.
_SNIFF_BYTES: Final = 8 * 1024

#: The zip guard's base for a workbook, in megabytes (it may unpack to 20 times this): the readers' default.
_ZIP_MB: Final = 50


class PreviewTimeout(TimeLimit):
    """The previews' deadline passed."""


def _delimiter(sample: str) -> str:
    """Tell what separates a csv's cells: a comma, a semicolon or a tab.

    Args:
        sample: The start of the file, decoded.

    Returns:
        The delimiter the sample's whole lines agree on, or a comma.
    """
    lines = sample.rpartition("\n")[0] or sample
    try:
        found = csv.Sniffer().sniff(lines, delimiters="".join(DELIMITERS)).delimiter
    except csv.Error:
        return ","
    return found if found in DELIMITERS else ","


@contextmanager
def _csv_records(path: Path) -> Iterator[Iterator[list[str]]]:
    """Open a csv as one stream of its records with content: UTF-8, the BOM dropped, bad bytes replaced.

    Args:
        path: The file.

    Yields:
        Its records that hold something.
    """
    with path.open("rb") as raw:
        head = raw.read(_SNIFF_BYTES)
    delimiter = _delimiter(head.decode("utf-8-sig", errors="replace"))
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as stream:
        yield (record for record in csv.reader(stream, delimiter=delimiter) if any(record))


def _first_sheet(book: "Workbook") -> Iterator[list[str]]:
    """Walk the rows with content of a workbook's first sheet, and no further: ``walk`` runs on into the next sheets.

    Args:
        book: The workbook, open.

    Yields:
        The rows' cells as the sheet reader shows them, each at most one character longer than a preview's.
    """
    names = book.names
    if not names:
        return
    for name, _number, row in book.walk(None, 1, cap=PREVIEW_CELL + 1):
        if name != names[0]:
            return
        yield row


@contextmanager
def _xlsx_records(path: Path, deadline: float) -> Iterator[Iterator[list[str]]]:
    """Open a workbook through the readers' guards, its warnings silenced.

    Args:
        path: The file.
        deadline: When the readers' own loops stop too.

    Yields:
        The rows with content of its first sheet.
    """
    from workbench.executor.readers.sheet import Workbook  # openpyxl only for a job that wrote a workbook

    readers.check_zip(path, _ZIP_MB)
    readers.set_deadline(deadline)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            book = Workbook(path)
            try:
                yield _first_sheet(book)
            finally:
                book.close()
    finally:
        readers.set_deadline(None)


def _cut(record: list[str]) -> tuple[list[str], bool]:
    """Cut a record to a preview's columns and each cell to its characters, plainly.

    Args:
        record: The cells.

    Returns:
        The cells kept, and whether anything was cut.
    """
    kept = record[:PREVIEW_COLUMNS]
    cut = len(record) > len(kept) or any(len(cell) > PREVIEW_CELL for cell in kept)
    return [cell[:PREVIEW_CELL] for cell in kept], cut


def _head(records: Iterator[list[str]]) -> tuple[list[str], list[list[str]], bool]:
    """Take the header and the rows a preview shows.

    Args:
        records: The table's records with content.

    Returns:
        The header, at most ``PREVIEW_ROWS`` rows, and whether a column or a cell was cut.
    """
    header = next(records, None)
    if header is None:
        return [], [], False
    columns, cut = _cut(header)
    rows: list[list[str]] = []
    for record in islice(records, PREVIEW_ROWS):
        cells, cut_here = _cut(record)
        rows.append(cells)
        cut = cut or cut_here
    return columns, rows, cut


def _count(records: Iterator[list[str]], shown: int) -> int | None:
    """Count the rows below the header to the end.

    Args:
        records: The records left after the rows shown.
        shown: How many rows were shown.

    Returns:
        How many rows lie below the header, or ``None`` past ``PREVIEW_COUNT_MAX``.
    """
    count = shown
    for _ in records:
        count += 1
        if count > PREVIEW_COUNT_MAX:
            return None
    return count


def table_preview(path: Path, deadline: float) -> TablePreview | None:
    """Read the preview of one csv or xlsx file.

    Args:
        path: The file; its suffix, in any case, picks the reader.
        deadline: A :func:`time.monotonic` time; the header and the rows must be read by then, and the rest is
            counted only until then.

    Returns:
        The header and at most ``PREVIEW_ROWS`` rows, with the rows below the header counted when the end is reached
        within ``PREVIEW_COUNT_MAX`` rows and before the deadline. ``None`` for another suffix, a file that cannot be
        read, or one whose header and rows were not read by the deadline.
    """
    suffix = path.suffix.lower()
    if suffix not in PREVIEW_SUFFIXES:
        return None
    head: tuple[list[str], list[list[str]], bool] | None = None
    counted: int | None = None
    try:
        with (
            limit_until(deadline, PreviewTimeout),
            _csv_records(path) if suffix == ".csv" else _xlsx_records(path, deadline) as records,
        ):
            head = _head(records)
            counted = _count(records, len(head[1]))
    except (PreviewTimeout, readers.ReadTimeout, Exception):  # out of time, or unreadable: what was read stands
        pass
    if head is None:
        return None
    columns, rows, cut = head
    truncated = cut or counted is None or counted > len(rows)
    return TablePreview(columns=columns, rows=rows, row_count=counted, truncated=truncated)


def previews_of(out_dir: Path, deadline: float) -> dict[str, TablePreview]:
    """Read the previews of the csv and xlsx files directly in a job's ``out/``, in name order, while time is left.

    Args:
        out_dir: The job's ``out/``.
        deadline: A :func:`time.monotonic` time all the previews share.

    Returns:
        Each regular file's preview by its name; a file with none is left out.
    """
    try:
        with os.scandir(out_dir) as entries:
            names = sorted(
                entry.name
                for entry in entries
                if Path(entry.name).suffix.lower() in PREVIEW_SUFFIXES and entry.is_file(follow_symlinks=False)
            )
    except OSError:
        return {}
    found: dict[str, TablePreview] = {}
    for name in names:
        if time.monotonic() >= deadline:
            break
        preview = table_preview(out_dir / name, deadline)
        if preview is not None:
            found[name] = preview
    return found
