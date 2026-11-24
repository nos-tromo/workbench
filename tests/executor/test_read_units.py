"""The units a read covers: every part says which pages, slides, blocks, sheet rows or lines it holds."""

import json
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest
from docx import Document
from openpyxl import Workbook

from tests.executor.test_readers import read
from tests.fixtures import make_fixtures as fx
from workbench.executor.readers import DocumentText, sheet
from workbench.executor.readers import paging as pg

#: The rows of ``Figures`` in :func:`gappy_xlsx` that hold something.
GAPPY_ROWS = (3, 4, 5, 300, 301, 302)


def units_of(doc: DocumentText) -> list[tuple[str, tuple[int, int] | None]]:
    """The label and units of each part a read returned."""
    return [(part.label, part.units) for part in doc.parts]


def reads(path: Path, *, max_chars: int) -> list[DocumentText]:
    """Every read of a whole document, from the start, following ``next_part`` until it is ``None``."""
    done = [read(path, max_chars=max_chars)]
    while (following := done[-1].next_part) is not None:
        assert len(done) < 1000, "paging does not come to an end"
        done.append(read(path, following, max_chars=max_chars))
    return done


def gappy_xlsx(path: Path) -> Path:
    """Write a workbook with an empty first sheet, blank rows before and inside a sheet, and a third sheet.

    ``Empty`` holds nothing; ``Figures`` has two columns of text in rows 3-5 and 300-302; ``Notes`` has ``A1``.

    Args:
        path: Where to write it.

    Returns:
        ``path``.
    """
    book = Workbook(write_only=True)
    book.create_sheet("Empty")
    figures = book.create_sheet("Figures")
    for number in range(1, max(GAPPY_ROWS) + 1):
        figures.append([f"berth {number}", f"invented depth {number}"] if number in GAPPY_ROWS else [])
    book.create_sheet("Notes").append(["a synthetic note"])
    book.save(str(path))
    return path


def test_pdf_pages_and_slides_are_their_own_units(tmp_path: Path) -> None:
    """A page or slide ``n`` covers ``(n, n)``; a page without text is in no part."""
    pdf = fx.make_pdf(tmp_path / "harbour-report.pdf", ["First page text\nsecond line", None, "Quay list"])
    doc = read(pdf)
    assert units_of(doc) == [("page 1", (1, 1)), ("page 3", (3, 3))]
    assert doc.pages_without_text == [2]
    assert units_of(read(fx.make_pptx(tmp_path / "harbour-deck.pptx"))) == [("slide 1", (1, 1)), ("slide 2", (2, 2))]


def test_paragraphs_and_tables_are_numbered_together(tmp_path: Path) -> None:
    """A table is one block, numbered among the paragraphs where it starts."""
    assert units_of(read(fx.make_docx(tmp_path / "review.docx"))) == [
        ("paragraph 1", (1, 1)),
        ("paragraph 2", (2, 2)),
        ("paragraph 3", (3, 3)),
        ("table 1, rows 1-3", (4, 4)),
        ("paragraph 4", (5, 5)),
    ]
    assert units_of(read(fx.make_odt(tmp_path / "survey.odt"))) == [
        ("paragraph 1", (1, 1)),
        ("paragraph 2", (2, 2)),
        ("table 1, rows 1-2", (3, 3)),
        ("paragraph 3", (4, 4)),
    ]


def test_a_table_read_in_runs_counts_once_on_the_run_that_ends_it(tmp_path: Path) -> None:
    """Only the run that reaches the table's last row carries the table; the runs before it carry nothing."""
    path = fx.make_big_table_docx(tmp_path / "long-table.docx", rows=300)
    seen = [pair for doc in reads(path, max_chars=2048) for pair in units_of(doc)]
    runs = [(label, units) for label, units in seen if label.startswith("table 1")]
    assert len(runs) > 2
    assert [units for _, units in runs[:-1]] == [None] * (len(runs) - 1)
    # The last run ends at row 300: ``table 1, rows A-300``, or ``table 1, row 300`` when a page ends at row 299.
    assert runs[-1][0].endswith(("-300", " row 300")) and runs[-1][1] == (2, 2)
    assert (seen[0], seen[-1]) == (("paragraph 1", (1, 1)), ("paragraph 2", (3, 3)))
    assert units_of(read(path, "table 1, rows 30-60")) == [("table 1, rows 30-60", None)]
    assert units_of(read(path, "table 1, row 290"))[0] == ("table 1, rows 290-300", (2, 2))


def test_sheet_rows_are_numbered_through_all_sheets(tmp_path: Path) -> None:
    """The second sheet's rows go on from the first's."""
    path = fx.make_xlsx(tmp_path / "quarterly-figures.xlsx", rows=5)
    assert units_of(read(path)) == [("Sheet1!A1:F6", (1, 6))]
    assert units_of(read(path, "Summary")) == [("Summary!A1:B2", (7, 8))]
    assert units_of(read(path, "Sheet1!A3")) == [("Sheet1!A3:F6", (3, 6))]


def test_only_rows_that_hold_something_are_units(tmp_path: Path) -> None:
    """An empty sheet and blank rows are no units, so a full read covers ``1..total`` with no gap."""
    path = gappy_xlsx(tmp_path / "gappy.xlsx")
    first = read(path)
    assert (units_of(first), first.next_part) == ([("Figures!A3:B5", (1, 3))], "Figures!A300:B499")
    assert units_of(read(path, "Figures!A300:B499")) == [("Figures!A300:B302", (4, 6))]
    assert units_of(read(path, "Notes")) == [("Notes!A1:A1", (7, 7))]
    assert units_of(read(fx.make_ods(tmp_path / "depths.ods"))) == [("Depths!A1:B2", (1, 2))]


def test_lines_are_their_own_units(tmp_path: Path) -> None:
    """A blank line is a line: the units are the line numbers."""
    text = tmp_path / "pier-notes.txt"
    text.write_text("alpha\nbeta\n\ngamma\n", encoding="utf-8")
    assert units_of(read(text)) == [("lines 1-4", (1, 4))]
    assert units_of(read(text, "lines 2-3")) == [("lines 2-3", (2, 3))]


def _csv(folder: Path) -> Path:
    path = folder / "berths.csv"
    path.write_text("berth,depth\n1,12\n2,9\n", encoding="utf-8")
    return path


def _tsv(folder: Path) -> Path:
    path = folder / "berths.tsv"
    path.write_text("berth\tdepth\n1\t12\n2\t9\n", encoding="utf-8")
    return path


def _md(folder: Path) -> Path:
    path = folder / "notes.md"
    path.write_text("# Pier notes\n\nThe invented quay is *open*.\n", encoding="utf-8")
    return path


def _jsonl(folder: Path) -> Path:
    path = folder / "log.jsonl"
    path.write_text('{"berth": 1, "state": "free"}\n{"berth": 2, "state": "taken"}\n', encoding="utf-8")
    return path


def _json(folder: Path) -> Path:
    path = folder / "berths.json"
    berths = [{"berth": n, "depth": {"metres": n + 8, "note": "synthetic"}} for n in range(1, 4)]
    path.write_text(json.dumps(berths, separators=(",", ":")), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "make",
    [
        _csv,
        _tsv,
        _md,
        _jsonl,
        _json,
        lambda folder: fx.make_html(folder / "harbour.html"),
        lambda folder: fx.make_eml(folder / "pier.eml"),
    ],
    ids=["csv", "tsv", "md", "jsonl", "json", "html", "eml"],
)
def test_every_line_reader_covers_its_lines(tmp_path: Path, make: Callable[[Path], Path]) -> None:
    """Every reader that reads by line says which lines its part holds."""
    doc = read(make(tmp_path))
    assert units_of(doc) == [(doc.parts[0].label, (1, doc.total_parts))]


def test_a_part_the_reader_cut_keeps_its_units(tmp_path: Path) -> None:
    """A part cut to fit the budget is still the unit it is."""
    line = tmp_path / "one-line.txt"
    line.write_text("x" * 5000 + "\n", encoding="utf-8")
    doc = read(line, max_chars=512)
    assert units_of(doc) == [("line 1", (1, 1))]
    assert doc.cut == "line 1"
    words = Document()
    words.add_paragraph(" ".join(f"word{n % 10}" for n in range(3000)))
    path = tmp_path / "one-paragraph.docx"
    words.save(str(path))
    doc = read(path)
    assert units_of(doc) == [("paragraph 1", (1, 1))]
    assert doc.cut == "paragraph 1"


def test_a_part_is_sized_as_it_is_sent() -> None:
    """The size is the compact JSON with ``units`` (``null`` when ``None``) and ``cut``, and the comma after it."""
    assert pg.part_size("page 3", "x", (3, 3)) == len('{"label":"page 3","text":"x","units":[3,3],"cut":false}') + 1
    assert pg.part_size("page 3", "x") == len('{"label":"page 3","text":"x","units":null,"cut":false}') + 1
    # ``true`` is one character shorter than ``false``.
    assert pg.part_size("page 3", "x", cut=True) == pg.part_size("page 3", "x") - 1


def test_rows_before_counts_rows_with_content_only(tmp_path: Path) -> None:
    """The rows before a sheet's row are those that hold something, in it and in the sheets before it."""
    with closing(sheet.Workbook(gappy_xlsx(tmp_path / "gappy.xlsx"))) as book:
        assert pg.rows_before(book, "Figures", 3) == 0
        assert pg.rows_before(book, "Figures", 300) == 3
        assert pg.rows_before(book, "Figures", 301) == 4
        assert pg.rows_before(book, "Notes", 1) == 6
