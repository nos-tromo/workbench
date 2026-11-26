"""The measure of a document: its units and the characters of a full read, counted by the readers in the executor."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook

from tests.executor.test_read_units import gappy_xlsx, reads
from tests.executor.test_readers import hostile_files, read
from tests.executor.test_search import CHAT, context, read_doc
from tests.fixtures import make_fixtures as fx
from workbench.executor import readers
from workbench.executor.readers import UnreadableFile, UnsupportedFormat, pdf
from workbench.executor.runners import RUNNERS
from workbench.executor.runners import documents as docs

#: The rows ``test_readers.test_small_workbook_reads_its_first_sheet_and_points_to_the_next`` asserts, as a read shows
#: them: the six of ``Sheet1`` and the two of ``Summary``.
SHEET1_LINES = [
    "1: id | name | amount | note | region | doubled",
    "2: 2 | item 2 | 3 | synthetic | north | =C2*2",
    "3: 3 | item 3 | 4.5 | synthetic | north | =C3*2",
    "4: 4 | item 4 | 6 | synthetic | north | =C4*2",
    "5: 5 | item 5 | 7.5 | synthetic | north | =C5*2",
    "6: 6 | item 6 | 9 | synthetic | north | =C6*2",
]
SUMMARY_LINES = ["1: total | harbour budget", "2: 42 | approved"]

#: The paragraphs and table rows ``fx.make_docx`` shows, as a full read shows them.
REVIEW_SHOWN = [
    "Quarterly figures review",
    "The harbour project is on schedule.",
    "Budget notes follow in the table below.",
    "row 1: Item | Planned | Actual",
    "row 2: Dredging | 120 | 118",
    "row 3: Piers | 340 | 351",
    "Contact the invented office for questions.",
]


def measure(path: Path, chars_cap: int = 2_000_000) -> readers.Measure:
    """Measure a document the way the runner does, under the default limits."""
    return readers.measure(path, chars_cap=chars_cap)


def write(path: Path, data: bytes) -> Path:
    """Write bytes to a file and give its path back."""
    path.write_bytes(data)
    return path


@pytest.fixture
def work(tmp_path: Path) -> Path:
    """The job's working folder: the chat folder, with the job's own empty ``out/`` in it."""
    folder = tmp_path / "job" / "workspace" / CHAT
    (folder / "out").mkdir(parents=True)
    return folder


# --- what each reader counts -----------------------------------------------------------------------------------


def test_a_pdf_is_measured_in_pages(tmp_path: Path) -> None:
    """A page without text counts in the total and adds no characters."""
    pdf_file = fx.make_pdf(tmp_path / "harbour-report.pdf", ["First page text\nsecond line", None, "Quay list"])
    assert measure(pdf_file) == readers.Measure(
        "pdf", "page", 3, len("First page text\nsecond line") + len("Quay list"), False
    )


def test_a_deck_is_measured_in_slides(tmp_path: Path) -> None:
    """A slide is the text of its shapes and its notes."""
    deck = fx.make_pptx(tmp_path / "harbour-deck.pptx")
    assert measure(deck) == readers.Measure("pptx", "slide", 2, sum(len(p.text) for p in read(deck).parts), False)


def test_a_word_document_counts_a_table_as_one_paragraph(tmp_path: Path) -> None:
    """A table is one unit; its rows are counted as ``row R: a | b``, as a full read shows them."""
    review = measure(fx.make_docx(tmp_path / "review.docx"))
    assert review == readers.Measure("docx", "paragraph", 5, sum(map(len, REVIEW_SHOWN)), False)
    survey = measure(fx.make_odt(tmp_path / "survey.odt"))
    assert (survey.kind, survey.unit, survey.total) == ("odt", "paragraph", 4)


def test_a_workbook_counts_its_rows_through_every_sheet(tmp_path: Path) -> None:
    """Rows are the rows that hold something, of every sheet, each counted as a read shows it."""
    book = measure(fx.make_xlsx(tmp_path / "quarterly-figures.xlsx", rows=5))
    assert (book.kind, book.unit, book.total, book.capped) == ("xlsx", "row", 8, False)
    assert book.chars == sum(map(len, SHEET1_LINES + SUMMARY_LINES))  # the eight lines test_readers asserts
    assert measure(gappy_xlsx(tmp_path / "gappy.xlsx")).total == 7
    assert measure(fx.make_ods(tmp_path / "depths.ods")).total == 2


def test_text_is_measured_in_lines_and_characters(tmp_path: Path) -> None:  # characters, not bytes; \r\n is one break
    """A blank line is a line; a CSV is told from a text by its name."""
    notes = write(tmp_path / "pier-notes.txt", b"alpha\nbeta\n\ngamma\n")
    assert measure(notes) == readers.Measure("text", "line", 4, 14, False)
    assert measure(write(tmp_path / "berths.csv", b"a,b\n1,2\n")) == readers.Measure("csv", "line", 2, 6, False)
    assert measure(write(tmp_path / "greeting.txt", "Grüße\r\n東京".encode())) == readers.Measure(
        "text", "line", 2, 7, False
    )


def test_a_json_file_is_measured_in_the_lines_it_is_read_as(tmp_path: Path) -> None:
    """A minified file is counted in the lines of its indented form."""
    value = {"harbour": "north", "piers": [1, 2]}
    path = write(tmp_path / "berths.json", json.dumps(value, separators=(",", ":")).encode())
    shown = json.dumps(value, indent=2, ensure_ascii=False).splitlines()
    assert measure(path) == readers.Measure("json", "line", 7, sum(map(len, shown)), False)


@pytest.mark.parametrize(
    ("name", "make"), [("harbour.html", fx.make_html), ("pier.eml", fx.make_eml)], ids=["html", "eml"]
)
def test_html_and_mail_are_measured_in_the_lines_they_read_as(
    tmp_path: Path, name: str, make: Callable[[Path], Path]
) -> None:
    """A page and a message are counted in the lines their read shows, blank ones in the message included."""
    path = make(tmp_path / name)
    doc = read(path)
    counted = measure(path)
    assert (counted.unit, counted.total, counted.chars) == (
        "line",
        doc.total_parts,
        sum(len(line) for line in doc.parts[0].text.split("\n")),
    )


def test_the_count_stops_at_the_cap_and_the_total_does_not(tmp_path: Path) -> None:  # Review Focus 4
    """Past the cap the characters are the cap and ``capped`` is true; reaching it exactly is not past it."""
    path = write(tmp_path / "log.txt", "".join(f"{n:04d} " + "x" * 45 + "\n" for n in range(100)).encode())
    assert measure(path, chars_cap=120) == readers.Measure("text", "line", 100, 120, True)
    assert measure(path, chars_cap=5000) == readers.Measure("text", "line", 100, 5000, False)


@pytest.mark.parametrize(
    ("make", "total"),
    [
        (lambda folder: fx.make_xlsx(folder / "quarterly-figures.xlsx", rows=100), 103),
        (lambda folder: gappy_xlsx(folder / "gappy.xlsx"), 7),
        (lambda folder: fx.make_ods(folder / "depths.ods"), 2),
        (lambda folder: fx.make_pptx(folder / "harbour-deck.pptx"), 2),
        (lambda folder: fx.make_docx(folder / "review.docx"), 5),
        (lambda folder: fx.make_odt(folder / "survey.odt"), 4),
    ],
    ids=["xlsx", "gappy-xlsx", "ods", "pptx", "docx", "odt"],
)
def test_a_measure_past_the_cap_still_counts_every_unit(
    tmp_path: Path, make: Callable[[Path], Path], total: int
) -> None:
    """Only the characters stop at the cap: the rows, slides and paragraphs after it are counted all the same."""
    path = make(tmp_path)
    assert measure(path).total == total
    counted = measure(path, chars_cap=10)
    assert (counted.total, counted.chars, counted.capped) == (total, 10, True)


def test_a_line_or_a_cell_a_read_shortens_is_measured_whole(tmp_path: Path, work: Path) -> None:
    """Decisions 5 and 7: the read shows less than the text is, and the measure says how long the text is."""
    path = write(tmp_path / "one-line.txt", b"x" * 50_000)
    assert measure(path) == readers.Measure("text", "line", 1, 50_000, False)
    doc = read(path)
    assert doc.parts[0].text == "x" * 2000 + "… [+48000 characters]"
    assert doc.cut is None
    (work / "one-line.txt").write_bytes(path.read_bytes())
    (part,) = read_doc(work, "one-line.txt")["parts"]
    assert (part["units"], part["cut"]) == ([1, 1], False)

    book = Workbook(write_only=True)
    book.create_sheet("Notes").append(["y" * 1000])
    wide = tmp_path / "wide-cell.xlsx"
    book.save(str(wide))
    assert measure(wide).chars == len("1: ") + 1000
    assert read(wide).parts[0].text == "1: " + "y" * 299 + "…"


def test_a_pdf_stops_extracting_once_past_the_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review Focus 4: the text of the pages after the cap is not extracted, while the total stays whole."""
    long_pdf = fx.make_pdf(tmp_path / "long-report.pdf", ["y" * 50] * 10)
    extracted: list[int] = []
    real = pdf._page_text

    def counting(reader: Any, number: int) -> str:
        extracted.append(number)
        return real(reader, number)

    monkeypatch.setattr(pdf, "_page_text", counting)
    counted = measure(long_pdf, chars_cap=120)
    assert (counted.total, counted.chars, counted.capped) == (10, 120, True)
    assert len(extracted) == 3


# --- what a read refuses ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "which",
    ["encrypted pdf", "truncated docx", "truncated pdf", "encrypted docx", "encrypted xlsx", "not a pdf", "zip bomb"],
)
def test_measure_refuses_what_read_refuses(tmp_path: Path, which: str) -> None:
    """Each fails with ``UnreadableFile``, whose message names neither the folder nor the file."""
    path = hostile_files(tmp_path)[which]
    with pytest.raises(UnreadableFile) as raised:
        readers.measure(path, chars_cap=2_000_000, file_mb=1)
    message = str(raised.value)
    assert message
    assert str(tmp_path) not in message
    assert path.name not in message


def test_measure_refuses_an_old_binary_format(tmp_path: Path) -> None:
    """A type the readers do not handle says what to save it as."""
    with pytest.raises(UnsupportedFormat, match=r"save it as \.xlsx or \.csv"):
        measure(write(tmp_path / "ledger.xls", b"\xd0\xcf\x11\xe0"))


# --- the measure and the parts of a full read ------------------------------------------------------------------


def _scanned_pdf(folder: Path) -> Path:
    return fx.make_pdf(folder / "scan.pdf", ["First page text", None, "Quay list"])


def _lines_txt(folder: Path) -> Path:
    return write(folder / "berths.txt", "".join(f"berth {n}: invented depth {n}\n" for n in range(1, 301)).encode())


def _minified_json(folder: Path) -> Path:
    berths = {"berths": [{"id": n, "depth": n + 8} for n in range(60)]}
    return write(folder / "berths.json", json.dumps(berths, separators=(",", ":")).encode())


@pytest.mark.parametrize(
    "make",
    [
        _scanned_pdf,
        lambda folder: fx.make_pptx(folder / "harbour-deck.pptx"),
        lambda folder: fx.make_big_table_docx(folder / "long-table.docx", rows=300),
        lambda folder: fx.make_odt(folder / "survey.odt"),
        lambda folder: fx.make_xlsx(folder / "quarterly-figures.xlsx", rows=100),
        lambda folder: gappy_xlsx(folder / "gappy.xlsx"),
        lambda folder: fx.make_ods(folder / "depths.ods"),
        _lines_txt,
        _minified_json,
        lambda folder: fx.make_html(folder / "harbour.html"),
        lambda folder: fx.make_eml(folder / "pier.eml"),
    ],
    ids=["pdf", "pptx", "docx", "odt", "xlsx", "gappy-xlsx", "ods", "txt", "json", "html", "eml"],
)
def test_paging_through_a_document_covers_exactly_its_measure(tmp_path: Path, make: Callable[[Path], Path]) -> None:
    """Review Focus 1-3: the units of a full read, with the pages without text, are ``1..total`` of the measure."""
    path = make(tmp_path)
    covered: set[int] = set()
    for doc in reads(path, max_chars=2048):
        assert doc.cut is None
        for part in doc.parts:
            if part.units is not None:
                covered |= set(range(part.units[0], part.units[1] + 1))
        covered |= set(doc.pages_without_text)
    assert covered == set(range(1, measure(path).total + 1))


# --- the runner ------------------------------------------------------------------------------------------------


def test_measure_file_answers_the_counts(work: Path) -> None:
    """The answer holds the virtual path and the counts of the file."""
    fx.make_docx(work / "review.docx")
    answer = RUNNERS["measure_file"]({"path": "review.docx", "budget_s": 30}, context(work))
    assert answer == {
        "path": f"/workspace/{CHAT}/review.docx",
        "kind": "docx",
        "unit": "paragraph",
        "total": 5,
        "chars": sum(map(len, REVIEW_SHOWN)),
        "capped": False,
    }


def test_measure_file_reads_its_cap_when_it_runs(work: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The cap is read at the call, so a test can lower it."""
    fx.make_docx(work / "review.docx")
    monkeypatch.setattr(docs, "MEASURE_CHARS_CAP", 10)
    answer = RUNNERS["measure_file"]({"path": "review.docx", "budget_s": 30}, context(work))
    assert (answer["chars"], answer["capped"], answer["total"]) == (10, True, 5)


def test_measure_file_says_what_went_wrong(work: Path) -> None:
    """A missing file, a type that is not a document and a run past its budget, each in its own words."""
    missing = RUNNERS["measure_file"]({"path": "missing.txt", "budget_s": 30}, context(work))
    assert missing == {
        "error": f"file_not_found: /workspace/{CHAT}/missing.txt is not in this conversation's workspace"
    }

    (work / "quay-photo.jpg").write_bytes(b"\xff\xd8\xff\xe0 synthetic")
    photo = RUNNERS["measure_file"]({"path": "quay-photo.jpg", "budget_s": 30}, context(work))
    assert photo["error"].startswith("unsupported_format:")

    fx.make_big_table_docx(work / "long-table.docx", rows=5000)
    slow = RUNNERS["measure_file"]({"path": "long-table.docx", "budget_s": 0.000001}, context(work))
    assert slow["error"].startswith("read_timeout:")
