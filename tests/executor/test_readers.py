"""The document readers: every format, paging, caps, and files that are broken, encrypted or hostile."""

import re
import signal
import time
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fixtures import make_fixtures as fx
from workbench.executor import readers
from workbench.executor.readers import DocumentText, NoSuchPart, ReadTimeout, UnreadableFile, UnsupportedFormat, sheet

BUDGET = 16384


def read(path: Path, part: str | None = None, *, max_chars: int = BUDGET, file_mb: int = 50) -> DocumentText:
    """Read a document the way the runner does, with the default limits."""
    return readers.read(path, part=part, max_chars=max_chars, file_mb=file_mb)


def labels(doc: DocumentText) -> list[str]:
    """The labels of the parts a read returned."""
    return [part.label for part in doc.parts]


def texts(doc: DocumentText) -> str:
    """All the text a read returned, one part after the other."""
    return "\n".join(part.text for part in doc.parts)


@pytest.fixture(scope="module")
def big_xlsx(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A workbook with 100 000 data rows and a formula in column F, built once for the module."""
    return fx.make_xlsx(tmp_path_factory.mktemp("big") / "quarterly-figures.xlsx", rows=100_000)


# --- PDF -------------------------------------------------------------------------------------------------------


def test_pdf_reports_pages_without_text(tmp_path: Path) -> None:
    """A page with only a picture is listed, never returned as an empty page."""
    pdf = fx.make_pdf(tmp_path / "scan.pdf", ["First page text\nsecond line", None])
    doc = read(pdf)
    assert doc.kind == "pdf"
    assert labels(doc) == ["page 1"]
    assert doc.parts[0].text == "First page text\nsecond line"
    assert doc.pages_without_text == [2]
    assert doc.total_parts == 2
    assert (doc.truncated, doc.next_part) == (False, None)


def test_pdf_with_no_text_at_all_returns_no_parts(tmp_path: Path) -> None:
    """Every page a picture: nothing to return, every page named."""
    doc = read(fx.make_pdf(tmp_path / "scan.pdf", [None, None, None]))
    assert doc.parts == []
    assert doc.pages_without_text == [1, 2, 3]


@pytest.mark.parametrize(
    ("part", "expected"),
    [
        ("2", ["page 2", "page 3"]),
        ("2-2", ["page 2"]),
        ("page 3", ["page 3"]),
        ("pages 1-2", ["page 1", "page 2"]),
        ("Page 2-3", ["page 2", "page 3"]),
    ],
)
def test_pdf_pages_are_chosen_by_part(tmp_path: Path, part: str, expected: list[str]) -> None:
    """A part is where to start, or a run of pages to read, in the words of the labels or as a bare number."""
    doc = read(fx.make_pdf(tmp_path / "three.pdf", ["one", "two", "three"]), part)
    assert labels(doc) == expected


def test_pdf_pages_stop_at_the_budget_and_say_where_to_go_on(tmp_path: Path) -> None:
    """Whole pages only: the first that does not fit is ``next_part``, and paging from it continues."""
    pages = [f"page text {n} " + "word " * 60 for n in range(1, 8)]
    pdf = fx.make_pdf(tmp_path / "long.pdf", pages)
    first = read(pdf, max_chars=900)
    assert 1 <= len(first.parts) < 7
    assert first.truncated is True
    assert first.next_part == f"page {len(first.parts) + 1}"
    seen = labels(first)
    doc = first
    while doc.next_part is not None:
        doc = read(pdf, doc.next_part, max_chars=900)
        seen += labels(doc)
    assert seen == [f"page {n}" for n in range(1, 8)]


@pytest.mark.parametrize("part", ["page 9", "0", "3-2", "chapter 1", "page x"])
def test_a_page_that_is_not_there_is_no_such_part(tmp_path: Path, part: str) -> None:
    """The error says what a part of a PDF is, and how many pages there are."""
    with pytest.raises(NoSuchPart):
        read(fx.make_pdf(tmp_path / "two.pdf", ["a", "b"]), part)


def test_pdf_units_give_every_page_and_an_empty_text_for_a_page_without_text(tmp_path: Path) -> None:
    """A search can match page by page, and learns which pages it could not look into."""
    pieces = list(readers.units(fx.make_pdf(tmp_path / "scan.pdf", ["alpha", None, "gamma"])))
    assert [(p.label, p.text) for p in pieces] == [("page 1", "alpha"), ("page 2", ""), ("page 3", "gamma")]


# --- Word ------------------------------------------------------------------------------------------------------


def test_docx_reads_paragraphs_and_tables(tmp_path: Path) -> None:
    """Paragraphs are numbered, empty ones skipped; a table is a run of rows, a line per row."""
    doc = read(fx.make_docx(tmp_path / "review.docx"))
    assert doc.kind == "docx"
    assert labels(doc) == ["paragraph 1", "paragraph 2", "paragraph 3", "table 1, rows 1-3", "paragraph 4"]
    assert doc.parts[0].text == "Quarterly figures review"
    assert doc.parts[3].text == "row 1: Item | Planned | Actual\nrow 2: Dredging | 120 | 118\nrow 3: Piers | 340 | 351"
    assert doc.total_parts == 5
    assert (doc.truncated, doc.next_part) == (False, None)


def test_a_word_template_reads_like_a_document(tmp_path: Path) -> None:
    """``.dotx`` carries another content type; the body is the same."""
    docx = fx.make_docx(tmp_path / "review.docx")
    template = fx.as_template(docx, tmp_path / "review.dotx")
    assert [(p.label, p.text) for p in read(template).parts] == [(p.label, p.text) for p in read(docx).parts]


def test_a_shape_that_word_wrote_twice_is_read_once(tmp_path: Path) -> None:
    """The fallback copy of a text box, for old readers, is not a second paragraph of text."""
    body = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"><w:body>'
        "<w:p><w:r><mc:AlternateContent>"
        "<mc:Choice><w:t>boxed text</w:t></mc:Choice><mc:Fallback><w:t>boxed text</w:t></mc:Fallback>"
        "</mc:AlternateContent></w:r></w:p></w:body></w:document>"
    )
    path = tmp_path / "boxed.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", body)
    assert texts(read(path)) == "boxed text"


def test_docx_pages_by_paragraph_and_table(tmp_path: Path) -> None:
    """Paging starts at a paragraph or a table; a bare number is a paragraph."""
    docx = fx.make_docx(tmp_path / "review.docx")
    assert labels(read(docx, "paragraph 3")) == ["paragraph 3", "table 1, rows 1-3", "paragraph 4"]
    assert labels(read(docx, "3")) == ["paragraph 3", "table 1, rows 1-3", "paragraph 4"]
    assert labels(read(docx, "Table 1")) == ["table 1, rows 1-3", "paragraph 4"]
    assert labels(read(docx, "table 1, row 2")) == ["table 1, rows 2-3", "paragraph 4"]
    assert labels(read(docx, "table 1, rows 2-2")) == ["table 1, row 2"]
    with pytest.raises(NoSuchPart):
        read(docx, "paragraph 40")
    with pytest.raises(NoSuchPart):
        read(docx, "page 1")


def test_docx_stops_at_the_budget_with_whole_paragraphs(tmp_path: Path) -> None:
    """The budget counts the JSON the parts are sent in; the next paragraph is where to go on."""
    from docx import Document

    doc = Document()
    for n in range(1, 40):
        doc.add_paragraph(f"Paragraph number {n}: " + "text " * 20)
    path = tmp_path / "many.docx"
    doc.save(str(path))
    first = read(path, max_chars=1500)
    assert first.truncated is True
    assert first.next_part == f"paragraph {len(first.parts) + 1}"
    assert sum(readers.paging.part_size(p.label, p.text) for p in first.parts) <= 1500
    assert first.total_parts == 39


def test_one_paragraph_longer_than_the_budget_is_cut_and_marked(tmp_path: Path) -> None:
    """Paging must move on: the first part is cut to fit rather than refused."""
    from docx import Document

    doc = Document()
    doc.add_paragraph("long " * 2000)
    doc.add_paragraph("short")
    path = tmp_path / "long.docx"
    doc.save(str(path))
    result = read(path, max_chars=1000)
    assert labels(result) == ["paragraph 1"]
    assert result.parts[0].text.endswith(" [cut]")
    assert readers.paging.part_size("paragraph 1", result.parts[0].text) <= 1000
    assert (result.truncated, result.next_part, result.cut) == (True, "paragraph 2", "paragraph 1")


def test_docx_units_name_paragraphs_and_table_rows(tmp_path: Path) -> None:
    """A search matches a paragraph, or one row of a table, with that exact label."""
    pieces = list(readers.units(fx.make_docx(tmp_path / "review.docx")))
    assert [p.label for p in pieces] == [
        "paragraph 1",
        "paragraph 2",
        "paragraph 3",
        "table 1, row 1",
        "table 1, row 2",
        "table 1, row 3",
        "paragraph 4",
    ]
    assert pieces[4].text == "Dredging | 120 | 118"


# --- Excel -----------------------------------------------------------------------------------------------------


def test_small_workbook_reads_its_first_sheet_and_points_to_the_next(tmp_path: Path) -> None:
    """Rows are ``row: a | b``; a formula with no cached value shows its formula; the next sheet is next."""
    doc = read(fx.make_xlsx(tmp_path / "figures.xlsx"))
    assert doc.kind == "xlsx"
    assert labels(doc) == ["Sheet1!A1:F6"]
    assert doc.parts[0].text.splitlines() == [
        "1: id | name | amount | note | region | doubled",
        "2: 2 | item 2 | 3 | synthetic | north | =C2*2",
        "3: 3 | item 3 | 4.5 | synthetic | north | =C3*2",
        "4: 4 | item 4 | 6 | synthetic | north | =C4*2",
        "5: 5 | item 5 | 7.5 | synthetic | north | =C5*2",
        "6: 6 | item 6 | 9 | synthetic | north | =C6*2",
    ]
    assert doc.total_parts == 2
    assert (doc.truncated, doc.next_part) == (True, "Summary")
    summary = read(fx.make_xlsx(tmp_path / "again.xlsx"), "Summary")
    assert labels(summary) == ["Summary!A1:B2"]
    assert summary.parts[0].text == "1: total | harbour budget\n2: 42 | approved"
    assert (summary.truncated, summary.next_part) == (False, None)


def test_a_cached_formula_result_is_shown_instead_of_the_formula(tmp_path: Path) -> None:
    """Excel saves the result beside the formula; that is what the reader shows."""
    path = fx.with_cached_value(fx.make_xlsx(tmp_path / "figures.xlsx"), "F2", "3")
    lines = read(path).parts[0].text.splitlines()
    assert lines[1].endswith("| north | 3")
    assert lines[2].endswith("| north | =C3*2")


def test_large_sheet_pages_and_caps(big_xlsx: Path) -> None:
    """With no part: the first range of 200 rows only, cut, with the range to ask for next."""
    first = read(big_xlsx)
    assert labels(first) == ["Sheet1!A1:F200"]
    assert first.truncated is True
    assert first.next_part == "Sheet1!A201:F400"
    assert first.total_parts == 2
    rows = first.parts[0].text.splitlines()
    assert len(rows) == 200
    assert rows[0] == "1: id | name | amount | note | region | doubled"
    assert rows[1] == "2: 2 | item 2 | 3 | synthetic | north | =C2*2"
    nxt = read(big_xlsx, first.next_part)
    assert labels(nxt) == ["Sheet1!A201:F400"]
    assert nxt.parts[0].text.splitlines()[0].startswith("201: 201 | item 201 |")
    assert nxt.next_part == "Sheet1!A401:F600"


def test_the_last_range_of_a_sheet_points_to_the_next_sheet(big_xlsx: Path) -> None:
    """The sheet has 100 001 rows; the range that ends it is not cut, and the sheet after it is next."""
    last = read(big_xlsx, "Sheet1!A100001")
    assert labels(last) == ["Sheet1!A100001:F100001"]
    assert last.parts[0].text.startswith("100001: 100001 | item 100001 |")
    assert (last.truncated, last.next_part) == (True, "Summary")


def test_a_range_is_cut_at_the_budget_and_continues_at_the_next_row(big_xlsx: Path) -> None:
    """A small budget ends the range early; the next row is where to go on."""
    doc = read(big_xlsx, max_chars=600)
    (part,) = doc.parts
    rows = part.text.splitlines()
    assert 1 < len(rows) < 200
    assert readers.paging.part_size(part.label, part.text) <= 600
    assert part.label == f"Sheet1!A1:F{len(rows)}"
    assert doc.next_part == f"Sheet1!A{len(rows) + 1}:F{len(rows) + 200}"
    assert doc.truncated is True


def test_a_range_can_be_asked_for_by_its_end(big_xlsx: Path) -> None:
    """``Sheet1!A5:F9`` is five rows, the rest of the range is next."""
    doc = read(big_xlsx, "Sheet1!A5:F9")
    assert labels(doc) == ["Sheet1!A5:F9"]
    assert [row.split(":")[0] for row in doc.parts[0].text.splitlines()] == ["5", "6", "7", "8", "9"]
    assert doc.next_part == "Sheet1!A10:F209"


def test_workbook_sheet_names_are_matched_without_case_and_quoted_when_odd(tmp_path: Path) -> None:
    """A sheet name with a space is quoted in labels, and a bad name lists the sheets."""
    from openpyxl import Workbook

    book = Workbook(write_only=True)
    book.create_sheet("Q3 figures").append(["a", "b"])
    book.create_sheet("it's").append(["c"])
    path = tmp_path / "odd.xlsx"
    book.save(str(path))
    assert labels(read(path)) == ["'Q3 figures'!A1:B1"]
    assert read(path).next_part == "'it''s'"
    assert labels(read(path, "q3 FIGURES")) == ["'Q3 figures'!A1:B1"]
    assert labels(read(path, "'it''s'!A1")) == ["'it''s'!A1:A1"]
    with pytest.raises(NoSuchPart, match="Q3 figures"):
        read(path, "Sheet9")


def test_sheet_cells_are_units_with_their_addresses(tmp_path: Path) -> None:
    """A search matches one cell and names it as a spreadsheet does."""
    cells = {p.label: p.text for p in readers.units(fx.make_xlsx(tmp_path / "figures.xlsx", rows=2))}
    assert cells["Sheet1!B2"] == "item 2"
    assert cells["Sheet1!F3"] == "=C3*2"
    assert cells["Summary!B2"] == "approved"
    assert "Sheet1!E1" in cells
    assert all(text for text in cells.values())


def test_a_macro_workbook_reads_like_a_plain_one(tmp_path: Path) -> None:
    """``.xlsm`` is read for its cells; no macro is run."""
    source = fx.make_xlsx(tmp_path / "figures.xlsx", rows=2)
    macro = tmp_path / "figures.xlsm"
    macro.write_bytes(source.read_bytes())
    assert read(macro).parts[0].text == read(source).parts[0].text


# --- PowerPoint ------------------------------------------------------------------------------------------------


def test_pptx_reads_slides_with_tables_and_notes(tmp_path: Path) -> None:
    """Each slide is a part: its shapes' text, its table rows, then the speaker notes."""
    doc = read(fx.make_pptx(tmp_path / "status.pptx"))
    assert doc.kind == "pptx"
    assert labels(doc) == ["slide 1", "slide 2"]
    assert (
        doc.parts[0].text
        == "Harbour project status\nPhase | State\nDredging | done\nNotes: Mention the invented budget."
    )
    assert doc.parts[1].text == "Next steps\nBuild the northern pier"
    assert doc.total_parts == 2
    assert labels(read(tmp_path / "status.pptx", "slide 2")) == ["slide 2"]
    assert labels(read(tmp_path / "status.pptx", "2")) == ["slide 2"]


def test_a_slide_without_text_is_listed_not_returned(tmp_path: Path) -> None:
    """A picture-only slide is a page without text, as in a PDF."""
    from pptx import Presentation

    deck = Presentation()
    deck.slides.add_slide(deck.slide_layouts[6])
    deck.slides.add_slide(deck.slide_layouts[5]).shapes.title.text = "Only this one"  # pyrefly: ignore
    path = tmp_path / "pictures.pptx"
    deck.save(str(path))
    doc = read(path)
    assert labels(doc) == ["slide 2"]
    assert doc.pages_without_text == [1]


# --- OpenDocument ----------------------------------------------------------------------------------------------


def test_odt_reads_paragraphs_and_tables(tmp_path: Path) -> None:
    """Headings and paragraphs are numbered together; a table is one part."""
    doc = read(fx.make_odt(tmp_path / "survey.odt"))
    assert doc.kind == "odt"
    assert labels(doc) == ["paragraph 1", "paragraph 2", "table 1, rows 1-2", "paragraph 3"]
    assert doc.parts[2].text == "row 1: Harbour | Depth\nrow 2: North | 12"
    assert [p.label for p in readers.units(tmp_path / "survey.odt")][2:4] == ["table 1, row 1", "table 1, row 2"]


def test_ods_reads_ranges_and_never_unfolds_blank_rows(tmp_path: Path) -> None:
    """A million repeated empty rows are one element; reading must not take time for them."""
    path = fx.make_ods(tmp_path / "depths.ods")
    began = time.monotonic()
    doc = read(path)
    assert time.monotonic() - began < 2
    assert doc.kind == "ods"
    assert labels(doc) == ["Depths!A1:B2"]
    assert doc.parts[0].text == "1: North | 12\n2: South | 9"
    assert (doc.truncated, doc.next_part) == (False, None)
    assert {p.label: p.text for p in readers.units(path)} == {
        "Depths!A1": "North",
        "Depths!B1": "12",
        "Depths!A2": "South",
        "Depths!B2": "9",
    }


# --- text formats ----------------------------------------------------------------------------------------------


def test_latin1_text_is_decoded(tmp_path: Path) -> None:
    """A file that is not UTF-8 is read as the Western European text it is, accents intact."""
    doc = read(fx.make_latin1_txt(tmp_path / "notes.txt"))
    assert doc.kind == "text"
    assert labels(doc) == ["lines 1-2"]
    assert doc.parts[0].text == "Café crème brûlée, façade und Größe\nzweite Zeile mit Müller"


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("Привет, это тестовый документ для проверки".encode("cp1251"), "Привет, это тестовый документ для проверки"),
        ("日本語のテキストです。これはテストです。".encode("shift_jis"), "日本語のテキストです。これはテストです。"),
        (b"\xef\xbb\xbfplain with a mark", "plain with a mark"),
        ("tabellarische Übersicht".encode("utf-16"), "tabellarische Übersicht"),
        (b"mostly plain \xe9 text\n", "mostly plain é text"),
    ],
)
def test_other_encodings_are_detected(tmp_path: Path, data: bytes, expected: str) -> None:
    """Byte order marks, other scripts and Windows code pages read as text."""
    path = tmp_path / "notes.txt"
    path.write_bytes(data)
    assert read(path).parts[0].text == expected


def test_csv_and_markdown_are_text_by_line(tmp_path: Path) -> None:
    """CSV, TSV and Markdown are read as lines, and paged by line numbers."""
    csv = tmp_path / "rows.csv"
    csv.write_text("a,b\n" + "\n".join(f"{n},x{n}" for n in range(1, 301)), encoding="utf-8")
    first = read(csv, max_chars=400)
    assert first.kind == "csv"
    assert first.parts[0].label.startswith("lines 1-")
    assert first.truncated is True
    assert first.next_part is not None
    again = read(csv, first.next_part, max_chars=400)
    start = int(first.next_part.split()[1])
    assert again.parts[0].label.startswith(f"lines {start}-")
    assert again.parts[0].text.splitlines()[0] == f"{start - 1},x{start - 1}"
    md = tmp_path / "notes.md"
    md.write_text("# Title\n\nSome *text*.\n", encoding="utf-8")
    assert read(md).kind == "text"
    tsv = tmp_path / "rows.tsv"
    tsv.write_text("a\tb\n1\t2\n", encoding="utf-8")
    assert read(tsv).kind == "csv"


def test_lines_are_chosen_by_part_and_a_missing_line_is_no_such_part(tmp_path: Path) -> None:
    """``lines 2-3``, ``line 2`` and ``2`` all work; a line past the end does not."""
    path = tmp_path / "five.txt"
    path.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8")
    assert read(path, "lines 2-3").parts[0].text == "two\nthree"
    assert (read(path, "lines 2-3").truncated, read(path, "lines 2-3").next_part) == (False, None)
    assert (read(path, "line 5").parts[0].label, read(path, "line 5").parts[0].text) == ("line 5", "five")
    assert read(path, "4").parts[0].text == "four\nfive"
    assert read(path).total_parts == 5
    with pytest.raises(NoSuchPart):
        read(path, "line 6")


def test_a_very_long_line_is_cut_and_says_how_much(tmp_path: Path) -> None:
    """One line of a megabyte cannot fill the answer."""
    path = tmp_path / "one-line.txt"
    path.write_text("x" * 1_000_000 + "\nnext line\n", encoding="utf-8")
    doc = read(path)
    assert "[+998000 characters]" in doc.parts[0].text
    assert "next line" in doc.parts[0].text
    assert len(doc.parts[0].text) < 3000


def test_an_empty_text_file_has_no_parts(tmp_path: Path) -> None:
    """Nothing to read is not an error."""
    path = tmp_path / "empty.txt"
    path.write_bytes(b"")
    doc = read(path)
    assert (doc.parts, doc.total_parts, doc.truncated) == ([], 0, False)


def test_html_is_read_as_the_text_it_shows(tmp_path: Path) -> None:
    """No script, no style, no markup; a table row is one line."""
    doc = read(fx.make_html(tmp_path / "harbour.html"))
    assert doc.kind == "html"
    assert doc.parts[0].text.splitlines() == [
        "Harbour page",
        "Harbour notes",
        "The invented quay is open.",
        "Berth | Free",
        "7 | yes",
    ]
    assert "script text" not in texts(doc)
    assert "color" not in texts(doc)


def test_eml_has_headers_body_and_attachment_names(tmp_path: Path) -> None:
    """The attachment is named, not opened."""
    doc = read(fx.make_eml(tmp_path / "pier.eml"))
    assert doc.kind == "eml"
    lines = doc.parts[0].text.splitlines()
    assert lines[:4] == [
        "From: sender@example.invalid",
        "To: reader@example.invalid",
        "Date: Mon, 02 Mar 2026 09:30:00 +0000",
        "Subject: Pier inspection",
    ]
    assert "the inspection of the northern pier is done." in lines
    assert lines[-1] == "Attachments: inspection-report.pdf (application/pdf)"
    assert "synthetic" not in texts(doc)


def test_an_html_only_mail_is_read_as_text(tmp_path: Path) -> None:
    """The body of a message with no plain part is its HTML, as text."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = "Status"
    msg.set_content("<html><body><p>The <b>quay</b> is open.</p></body></html>", subtype="html")
    path = tmp_path / "status.eml"
    path.write_bytes(msg.as_bytes())
    assert "The quay is open." in texts(read(path))


# --- unsupported types -----------------------------------------------------------------------------------------


def test_msg_is_unsupported_with_a_hint(tmp_path: Path) -> None:
    """Outlook's format has no parser with a usable licence: the answer says what to do."""
    path = tmp_path / "mail.msg"
    path.write_bytes(fx.OLE_MAGIC)
    with pytest.raises(UnsupportedFormat, match=r"\.eml or PDF"):
        read(path)


@pytest.mark.parametrize(
    ("name", "hint"),
    [
        ("old.doc", ".docx or PDF"),
        ("old.xls", ".xlsx or .csv"),
        ("picture.png", "readable are PDF"),
        ("noext", "readable"),
    ],
)
def test_other_types_are_unsupported(tmp_path: Path, name: str, hint: str) -> None:
    """Anything else is refused with what can be read, or what to save the file as."""
    path = tmp_path / name
    path.write_bytes(b"x")
    with pytest.raises(UnsupportedFormat, match=hint):
        read(path)
    assert readers.supported(path) is False


def test_the_extension_decides_without_regard_to_case(tmp_path: Path) -> None:
    """``REPORT.TXT`` is a text file."""
    path = tmp_path / "REPORT.TXT"
    path.write_text("hello", encoding="utf-8")
    assert read(path).parts[0].text == "hello"


# --- files that are broken, encrypted or hostile ---------------------------------------------------------------


def hostile_files(tmp_path: Path) -> dict[str, Path]:
    """One file of each way a file can be unreadable."""
    good_docx = fx.make_docx(tmp_path / "good.docx")
    good_pdf = fx.make_pdf(tmp_path / "good.pdf", ["text"])
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"this is not a pdf at all")
    return {
        "encrypted pdf": fx.make_pdf(tmp_path / "locked.pdf", ["secret"], password="synthetic-password"),
        "truncated docx": fx.make_truncated(good_docx, tmp_path / "cut.docx"),
        "truncated pdf": fx.make_truncated(good_pdf, tmp_path / "cut.pdf"),
        "encrypted docx": fx.make_ole(tmp_path / "locked.docx"),
        "encrypted xlsx": fx.make_ole(tmp_path / "locked.xlsx"),
        "not a pdf": junk,
        "zip bomb": fx.make_zip_bomb(tmp_path / "bomb.docx", mb=25),
    }


@pytest.mark.parametrize(
    "which",
    ["encrypted pdf", "truncated docx", "truncated pdf", "encrypted docx", "encrypted xlsx", "not a pdf", "zip bomb"],
)
def test_broken_and_encrypted_files_fail_cleanly(tmp_path: Path, which: str) -> None:
    """Each fails fast with ``UnreadableFile``; the message names no path and no parser."""
    path = hostile_files(tmp_path)[which]
    began = time.monotonic()
    with pytest.raises(UnreadableFile) as raised:
        read(path, file_mb=1)
    assert time.monotonic() - began < 5
    message = str(raised.value)
    assert str(tmp_path) not in message
    assert path.name not in message
    assert message
    with pytest.raises(UnreadableFile):
        list(readers.units(path, file_mb=1))


def test_the_bomb_is_judged_by_its_unpacked_size_and_the_job_limit(tmp_path: Path) -> None:
    """25 MB unpacked is over 20 x 1 MB, but under 20 x 50 MB: the limit is the job's file size."""
    bomb = fx.make_zip_bomb(tmp_path / "bomb.docx", mb=25)
    assert bomb.stat().st_size < 100_000
    with pytest.raises(UnreadableFile, match="more than 20 MB"):
        read(bomb, file_mb=1)
    readers.check_zip(bomb, 2)
    readers.check_zip(bomb, 50)


def test_a_zip_with_too_many_parts_is_refused(tmp_path: Path) -> None:
    """A directory of a million entries is not a document."""
    path = tmp_path / "many.docx"
    with zipfile.ZipFile(path, "w") as archive:
        for n in range(readers.MAX_ZIP_MEMBERS + 1):
            archive.writestr(f"p{n}", b"")
    with pytest.raises(UnreadableFile, match="too many parts"):
        read(path)


def test_a_failing_parser_never_shows_its_own_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever a library raises, the model sees the fixed text: parser messages can hold paths."""
    from workbench.executor.readers import pdf

    def explode(_path: Path) -> object:
        raise ValueError(f"synthetic failure in {tmp_path}/secret-name.pdf")

    monkeypatch.setattr(pdf, "_open", explode)
    path = fx.make_pdf(tmp_path / "plain.pdf", ["text"])
    with pytest.raises(UnreadableFile) as raised:
        read(path)
    assert "synthetic" not in str(raised.value)
    assert str(tmp_path) not in str(raised.value)


# --- tables are paged by rows ----------------------------------------------------------------------------------


@pytest.mark.parametrize("make", [fx.make_big_table_docx, fx.make_big_table_odt])
def test_a_long_table_is_read_to_its_end_by_following_next_part(tmp_path: Path, make: Callable[..., Path]) -> None:
    """A table of 1 000 rows is more than one answer: its rows are paged, each once, to the text after it."""
    path = make(tmp_path / "long.docx" if make is fx.make_big_table_docx else tmp_path / "long.odt", 1000)
    rows: list[int] = []
    seen: list[str] = []
    doc = read(path, max_chars=3000)
    pages = 1
    while True:
        for part in doc.parts:
            assert readers.paging.part_size(part.label, part.text) <= 3000
            seen.append(part.label)
            if part.label.startswith("table 1"):
                rows += [int(m[1]) for m in re.finditer(r"^row (\d+):", part.text, re.M)]
        if doc.next_part is None:
            break
        assert doc.truncated is True
        doc = read(path, doc.next_part, max_chars=3000)
        pages += 1
    assert pages > 5
    assert rows == list(range(1, 1001))
    assert seen[0] == "paragraph 1"
    assert seen[-1] == "paragraph 2"
    assert doc.total_parts == 3


@pytest.mark.parametrize("make", [fx.make_big_table_docx, fx.make_big_table_odt])
def test_the_label_of_a_table_run_is_a_range_and_can_be_asked_for(tmp_path: Path, make: Callable[..., Path]) -> None:
    """``table 1, rows 1-N`` says what is in the part; ``table 1, row N+1`` is where the next one starts."""
    path = make(tmp_path / ("long.docx" if make is fx.make_big_table_docx else "long.odt"), 1000)
    first = read(path, "table 1", max_chars=3000)
    (part,) = first.parts
    last = int(re.findall(r"^row (\d+):", part.text, re.M)[-1])
    assert part.label == f"table 1, rows 1-{last}"
    assert first.next_part == f"table 1, row {last + 1}"
    follow = read(path, first.next_part, max_chars=3000)
    assert follow.parts[0].text.startswith(f"row {last + 1}: item {last + 1}")
    assert labels(read(path, "table 1, rows 10-12")) == ["table 1, rows 10-12"]
    with pytest.raises(NoSuchPart):
        read(path, "table 1, row 5000")
    with pytest.raises(NoSuchPart):
        read(path, "table 2")


def test_a_search_hit_in_a_table_can_be_opened_with_its_location(tmp_path: Path) -> None:
    """``units`` names ``table 1, row 350``; that is a ``part`` that starts there."""
    path = fx.make_big_table_docx(tmp_path / "long.docx", 1000)
    (hit,) = [p for p in readers.units(path) if "item 350" in p.text]
    assert hit.label == "table 1, row 350"
    opened = read(path, hit.label)
    assert opened.parts[0].label.startswith("table 1, rows 350-")
    assert opened.parts[0].text.startswith("row 350: item 350 | synthetic value 350 | 1050")


def test_a_row_longer_than_the_budget_is_cut_and_paging_moves_on(tmp_path: Path) -> None:
    """One cell of 5 000 characters in a table: the row is cut and marked, the next row is next."""
    from docx import Document

    doc = Document()
    table = doc.add_table(rows=3, cols=1)
    for r, value in enumerate(["short", "long " * 1000, "after"]):
        table.cell(r, 0).text = value
    path = tmp_path / "wide.docx"
    doc.save(str(path))
    first = read(path, "table 1, row 2", max_chars=800)
    assert first.parts[0].text.endswith(" [cut]")
    assert first.cut == "table 1, row 2"
    assert first.next_part == "table 1, row 3"


# --- files that would keep a reader busy -----------------------------------------------------------------------


def refused_quickly(path: Path, *, file_mb: int = 50) -> None:
    """Assert that reading and walking a file both raise ``UnreadableFile`` in under five seconds."""
    for work in (lambda: read(path, file_mb=file_mb), lambda: list(readers.units(path, file_mb=file_mb))):
        began = time.monotonic()
        with pytest.raises(UnreadableFile):
            work()
        assert time.monotonic() - began < 5


def test_a_cell_in_a_row_no_spreadsheet_has_is_refused(tmp_path: Path) -> None:
    """Openpyxl would give an empty row for each of two billion numbers; the file is refused instead."""
    refused_quickly(fx.make_sparse_xlsx(tmp_path / "sparse.xlsx", row=2_000_000_000))
    refused_quickly(fx.make_sparse_xlsx(tmp_path / "just-over.xlsx", row=1_048_577))


@pytest.mark.parametrize("column", ["XFE", "ZZZ", "AAAA", "ZZZZZZZZZZ"])
def test_a_cell_in_a_column_no_spreadsheet_has_is_refused(tmp_path: Path, column: str) -> None:
    """Past ``XFD`` there are no columns; a row of billions of cells is never built."""
    refused_quickly(fx.make_sparse_xlsx(tmp_path / "wide.xlsx", row=2, column=column))


def test_the_last_row_and_the_last_column_are_read(tmp_path: Path) -> None:
    """The limits are the spreadsheet's own: row 1 048 576 and column XFD are fine."""
    last_row = read(fx.make_sparse_xlsx(tmp_path / "row.xlsx", row=1_048_576))
    assert last_row.parts[0].label == "Sheet1!A1:B1"
    assert last_row.next_part == "Sheet1!A1048576:B1048775"
    assert read(fx.make_sparse_xlsx(tmp_path / "row.xlsx", row=1_048_576), last_row.next_part).parts[0].text == (
        "1048576: c | d"
    )
    wide = read(fx.make_sparse_xlsx(tmp_path / "col.xlsx", row=2, column="XFD"), max_chars=100_000)
    assert wide.parts[0].label == "Sheet1!A1:XFD2"


def test_a_workbook_with_too_many_sheets_is_refused(tmp_path: Path) -> None:
    """More than 1 000 sheets is not a workbook, and its sheets are not opened one by one."""
    refused_quickly(fx.make_many_sheets_xlsx(tmp_path / "many.xlsx", 1001))
    assert read(fx.make_many_sheets_xlsx(tmp_path / "limit.xlsx", 1000)).total_parts == 1000


def test_html_with_many_blocks_is_read_in_time_in_proportion_to_its_size(tmp_path: Path) -> None:
    """20 000 paragraphs side by side took twenty seconds when every block was inserted into a list of its siblings."""
    for count in (20_000, 40_000):
        path = fx.make_many_blocks_html(tmp_path / f"many-{count}.html", count)
        began = time.monotonic()
        doc = read(path, max_chars=100_000)
        pieces = list(readers.units(path))
        assert time.monotonic() - began < 5
        assert len(pieces) == count
        assert doc.total_parts == count


def test_html_blocks_tables_and_breaks_keep_their_lines(tmp_path: Path) -> None:
    """The one-pass walk gives what the insertions did: a line per block, a table row on one line."""
    path = tmp_path / "page.html"
    path.write_text(
        "<html><head><title>T</title><style>p{}</style></head><body><div>one<br>two</div>"
        "<table><tr><td>a</td><td><table><tr><td>x</td><td>y</td></tr></table></td><td>c</td></tr></table>"
        "<ul><li>first</li><li>second <b>bold</b> end</li></ul><script>hidden()</script><!-- c --></body></html>",
        encoding="utf-8",
    )
    assert read(path).parts[0].text.splitlines() == ["T", "one", "two", "a | x | y | c", "first", "second bold end"]


def test_an_ods_with_many_empty_sheets_is_refused_and_the_limit_reads_fast(tmp_path: Path) -> None:
    """The file was streamed once for every sheet; now it is streamed once, and more than 1 000 sheets is refused."""
    refused_quickly(fx.make_many_sheets_ods(tmp_path / "many.ods", 20_000))
    refused_quickly(fx.make_many_sheets_ods(tmp_path / "over.ods", 1001))
    path = fx.make_many_sheets_ods(tmp_path / "limit.ods", 1000, data_in_last=True)
    began = time.monotonic()
    doc = read(path)
    assert time.monotonic() - began < 5
    assert labels(doc) == ["last!A1:A1"]
    assert doc.parts[0].text == "1: only data"
    assert doc.total_parts == 1000
    assert [p.label for p in readers.units(path)] == ["last!A1"]


def test_a_sheet_with_a_very_long_name_is_refused(tmp_path: Path) -> None:
    """A reference carries the sheet's name; Excel's names have 31 characters at the most, and a label may have 300."""
    from openpyxl import Workbook

    book = Workbook()
    sheet: Any = book.active
    sheet.title = "x" * 31
    path = tmp_path / "long-name.xlsx"
    book.save(str(path))
    with zipfile.ZipFile(path) as src:
        items = [(info, src.read(info.filename)) for info in src.infolist()]
    with zipfile.ZipFile(path, "w") as out:
        for info, data in items:
            out.writestr(info, data.replace(b"x" * 31, b"x" * 500) if info.filename == "xl/workbook.xml" else data)
    refused_quickly(path)
    ods = fx.make_many_sheets_ods(tmp_path / "long-name.ods", 2)
    with zipfile.ZipFile(ods) as src:
        content = src.read("content.xml").replace(b'table:name="s0"', b'table:name="' + b"y" * 500 + b'"')
    with zipfile.ZipFile(ods, "w") as out:
        out.writestr("content.xml", content)
    refused_quickly(ods)


def test_an_ods_sheet_after_empty_ones_is_found_with_no_part_and_by_name(tmp_path: Path) -> None:
    """Empty sheets are passed over; asking for an empty one says where the next content is."""
    path = fx.make_many_sheets_ods(tmp_path / "gaps.ods", 5, data_in_last=True)
    assert labels(read(path)) == ["last!A1:A1"]
    empty = read(path, "s1")
    assert (empty.parts, empty.next_part) == ([], "last")


def test_the_deadline_stops_a_read_that_is_slow(big_xlsx: Path) -> None:
    """The row walk checks the clock: deep in a 100 000-row sheet 0.3 s is not enough, and it is ``ReadTimeout``."""
    began = time.monotonic()
    readers.set_deadline(began + 0.3)
    try:
        with pytest.raises(ReadTimeout):
            read(big_xlsx, "Sheet1!A99000")
        assert time.monotonic() - began < 3
        readers.set_deadline(time.monotonic() + 0.3)
        with pytest.raises(ReadTimeout):
            list(readers.units(big_xlsx))
    finally:
        readers.set_deadline(None)


def test_no_deadline_no_timeout(tmp_path: Path) -> None:
    """With the deadline taken away nothing stops a read."""
    readers.set_deadline(time.monotonic() - 10)
    readers.set_deadline(None)
    assert read(fx.make_docx(tmp_path / "a.docx")).parts


def test_a_deadline_that_has_passed_stops_the_next_page_or_sheet(tmp_path: Path) -> None:
    """Page, slide and sheet loops look at the clock on every step."""
    pdf = fx.make_pdf(tmp_path / "three.pdf", ["a", "b", "c"])
    readers.set_deadline(time.monotonic() - 1)
    try:
        with pytest.raises(ReadTimeout):
            read(pdf)
        with pytest.raises(ReadTimeout):
            list(readers.units(fx.make_pptx(tmp_path / "s.pptx")))
        with pytest.raises(ReadTimeout):
            read(fx.make_xlsx(tmp_path / "s.xlsx"))
    finally:
        readers.set_deadline(None)


def within(seconds: float, work: Callable[[], object]) -> None:
    """Assert that ``work`` raises ``UnreadableFile`` in under ``seconds``."""
    began = time.monotonic()
    with pytest.raises(UnreadableFile):
        work()
    assert time.monotonic() - began < seconds


@pytest.mark.parametrize("moved", [False, True])
@pytest.mark.parametrize("entries", [1001, 20_000, 200_000])
def test_a_workbook_with_many_sheet_entries_is_refused_whatever_parts_they_name(
    tmp_path: Path, entries: int, moved: bool
) -> None:
    """A worksheet is opened per entry, and the entries can all name one part: 20 000 took 84 s to walk."""
    path = fx.make_many_entries_xlsx(tmp_path / "entries.xlsx", entries, moved_workbook=moved)
    within(2, lambda: read(path))
    within(2, lambda: list(readers.units(path)))


def test_a_thousand_sheet_entries_are_allowed_and_a_workbook_with_defined_names_is_counted(tmp_path: Path) -> None:
    """The limits are counted, not guessed: 1 000 sheets read, 10 001 defined names do not, 10 000 do."""
    # 999 entries and the workbook's second sheet.
    assert read(fx.make_many_entries_xlsx(tmp_path / "limit.xlsx", 999)).total_parts == 1000
    within(2, lambda: read(fx.make_defined_names_xlsx(tmp_path / "names.xlsx", 10_001)))
    assert read(fx.make_defined_names_xlsx(tmp_path / "names-ok.xlsx", 10_000)).parts


@pytest.mark.parametrize("strings", [1_000_001, 2_000_000])
def test_a_workbook_with_a_million_shared_strings_is_refused_before_it_is_loaded(tmp_path: Path, strings: int) -> None:
    """2 000 000 tiny strings took 18 s to load and 8 000 000 took 74 s at 890 MB, with nothing able to stop it."""
    path = fx.make_shared_strings_xlsx(tmp_path / "strings.xlsx", strings)
    assert path.stat().st_size < 200_000
    within(2, lambda: read(path))
    within(2, lambda: list(readers.units(path)))


def test_a_million_shared_strings_are_counted_as_the_limit_not_over_it(tmp_path: Path) -> None:
    """The count stops at the limit: exactly 1 000 000 passes the check (loading them is not tried here)."""
    path = fx.make_shared_strings_xlsx(tmp_path / "strings.xlsx", 1_000_000)
    with zipfile.ZipFile(path) as archive:
        sheet.check_counts(archive)


def test_the_timer_interrupts_a_workbook_that_is_slow_to_load(tmp_path: Path) -> None:
    """900 000 shared strings are under the limit and take about nine seconds to load: the deadline cuts that short."""
    path = fx.make_shared_strings_xlsx(tmp_path / "strings.xlsx", 900_000)
    for work in (lambda: read(path), lambda: list(readers.units(path))):
        began = time.monotonic()
        readers.set_deadline(began + 0.5)
        try:
            with pytest.raises(ReadTimeout):
                work()
        finally:
            readers.set_deadline(None)
        assert time.monotonic() - began < 2


def test_the_timer_interrupts_pure_python_in_a_reader_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reader that loops without looking at the clock is stopped at the deadline, for ``read`` and for ``units``."""
    from workbench.executor.readers import text

    def endless(*_args: object, **_kwargs: object) -> DocumentText:
        while True:
            sum(range(1000))

    def endless_units(_path: Path) -> Iterator[readers.Part]:
        while True:
            sum(range(1000))
            yield from ()

    monkeypatch.setattr(text, "read", endless)
    monkeypatch.setattr(text, "units", endless_units)
    path = tmp_path / "a.txt"
    path.write_text("x\n", encoding="utf-8")
    for work in (lambda: read(path), lambda: list(readers.units(path))):
        began = time.monotonic()
        readers.set_deadline(began + 0.3)
        try:
            with pytest.raises(ReadTimeout):
                work()
        finally:
            readers.set_deadline(None)
        assert 0.25 < time.monotonic() - began < 1.0
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_a_timeout_is_not_swallowed_by_a_reader_that_catches_exceptions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ReadTimeout`` is not an ``Exception``: an ``except Exception`` around a library's work lets it through."""
    from workbench.executor.readers import text

    def careless(*_args: object, **_kwargs: object) -> DocumentText:
        while True:
            try:
                sum(range(1000))
            except Exception:
                pass

    monkeypatch.setattr(text, "read", careless)
    path = tmp_path / "a.txt"
    path.write_text("x\n", encoding="utf-8")
    readers.set_deadline(time.monotonic() + 0.3)
    try:
        with pytest.raises(ReadTimeout):
            read(path)
    finally:
        readers.set_deadline(None)
    assert not issubclass(ReadTimeout, Exception)


# --- PDFs that restrict only what a reader may do --------------------------------------------------------------


@pytest.mark.parametrize("algorithm", ["RC4-128", "AES-128", "AES-256"])
def test_a_pdf_with_an_owner_password_and_no_user_password_is_read(tmp_path: Path, algorithm: str) -> None:
    """The password only restricts printing and copying: a reader opens it, and so do we."""
    pdf = fx.make_restricted_pdf(tmp_path / "restricted.pdf", ["Owner restricted page", None], algorithm=algorithm)
    doc = read(pdf)
    assert labels(doc) == ["page 1"]
    assert doc.parts[0].text == "Owner restricted page"
    assert doc.pages_without_text == [2]
    assert [p.label for p in readers.units(pdf)] == ["page 1", "page 2"]


@pytest.mark.parametrize("algorithm", ["RC4-128", "AES-128", "AES-256"])
def test_a_pdf_that_needs_a_user_password_is_refused_as_protected(tmp_path: Path, algorithm: str) -> None:
    """Only a password that really is needed makes a PDF unreadable."""
    pdf = fx.make_restricted_pdf(
        tmp_path / "locked.pdf", ["secret"], user_password="synthetic-user", algorithm=algorithm
    )
    with pytest.raises(UnreadableFile, match=r"password-protected"):
        read(pdf)


def test_a_missing_decryption_library_is_not_reported_as_a_page_without_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the AES library were missing, an unreadable page must not turn into 'needs OCR': the file is refused."""
    from pypdf import PageObject
    from pypdf.errors import DependencyError

    def missing(self: PageObject, *args: object, **kwargs: object) -> str:
        raise DependencyError("cryptography>=3.1 is required for AES algorithm")

    monkeypatch.setattr(PageObject, "extract_text", missing)
    pdf = fx.make_restricted_pdf(tmp_path / "restricted.pdf", ["Owner restricted page"])
    with pytest.raises(UnreadableFile, match="cannot be decrypted here"):
        read(pdf)


# --- entities in XML parts -------------------------------------------------------------------------------------

#: The main XML part of each format, and some text in it.
ENTITY_TARGETS = {
    "docx": (fx.make_docx, "word/document.xml", "The harbour project is on schedule."),
    "odt": (fx.make_odt, "content.xml", "Survey summary"),
    "ods": (fx.make_ods, "content.xml", "North"),
    "pptx": (fx.make_pptx, "ppt/slides/slide1.xml", "Harbour project status"),
    "xlsx": (fx.make_xlsx, "xl/worksheets/sheet1.xml", "item 2"),
}
SECRET = "SYNTHETIC-SECRET-CONTENT"


def entity_file(tmp_path: Path, kind: str, use: str) -> Path:
    """Build a file of ``kind`` whose main part declares entities, and a secret they point to."""
    (tmp_path / "secret.txt").write_text(SECRET, encoding="utf-8")
    make, member, text = ENTITY_TARGETS[kind]
    return fx.with_entities(make(tmp_path / f"entities.{kind}"), member, text, tmp_path / "secret.txt", use=use)


@pytest.mark.parametrize("kind", ["docx", "odt", "ods", "pptx"])
def test_a_document_type_with_entities_that_are_not_used_reads_as_usual(tmp_path: Path, kind: str) -> None:
    """The control: the declarations alone are harmless, and the text is the document's own."""
    plain = read(ENTITY_TARGETS[kind][0](tmp_path / f"plain.{kind}"))
    declared = read(entity_file(tmp_path, kind, "none"))
    assert texts(declared) == texts(plain)


@pytest.mark.parametrize("kind", ["docx", "odt", "ods", "pptx"])
def test_an_external_entity_is_not_loaded(tmp_path: Path, kind: str) -> None:
    """The reference to a local file is left unresolved: the read succeeds, with the text around it and not the file."""
    path = entity_file(tmp_path, kind, "external")
    for text in (texts(read(path)), "\n".join(p.text for p in readers.units(path))):
        assert "before" in text
        assert SECRET not in text


def test_an_xlsx_with_entities_is_refused_because_openpyxl_forbids_them(tmp_path: Path) -> None:
    """Openpyxl reads with defusedxml (which odfpy brings), and defusedxml refuses any entity declaration."""
    import openpyxl.xml

    assert openpyxl.xml.DEFUSEDXML is True
    for use in ("none", "external", "bomb"):
        refused_quickly(entity_file(tmp_path, "xlsx", use))


@pytest.mark.parametrize("kind", ["docx", "odt", "ods", "pptx", "xlsx"])
def test_an_entity_bomb_is_refused_without_being_expanded(tmp_path: Path, kind: str) -> None:
    """A chain of entities that would make twenty megabytes of text is stopped by the parser: the file is unreadable."""
    refused_quickly(entity_file(tmp_path, kind, "bomb"))
