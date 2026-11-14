"""The document readers: every format, paging, caps, and files that are broken, encrypted or hostile."""

import time
import zipfile
from pathlib import Path

import pytest

from tests.fixtures import make_fixtures as fx
from workbench.executor import readers
from workbench.executor.readers import DocumentText, NoSuchPart, UnreadableFile, UnsupportedFormat

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
    """Paragraphs are numbered, empty ones skipped; a table is one part with a line per row."""
    doc = read(fx.make_docx(tmp_path / "review.docx"))
    assert doc.kind == "docx"
    assert labels(doc) == ["paragraph 1", "paragraph 2", "paragraph 3", "table 1", "paragraph 4"]
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
    assert labels(read(docx, "paragraph 3")) == ["paragraph 3", "table 1", "paragraph 4"]
    assert labels(read(docx, "3")) == ["paragraph 3", "table 1", "paragraph 4"]
    assert labels(read(docx, "Table 1")) == ["table 1", "paragraph 4"]
    assert labels(read(docx, "table 1, row 2")) == ["table 1", "paragraph 4"]
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
    assert labels(doc) == ["paragraph 1", "paragraph 2", "table 1", "paragraph 3"]
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


def test_an_xml_bomb_and_an_external_entity_are_not_expanded(tmp_path: Path) -> None:
    """Nothing external is loaded and entities are not resolved: the secret stays out, and it takes no time."""
    secret = tmp_path / "secret.txt"
    secret.write_text("SYNTHETIC-SECRET-CONTENT", encoding="utf-8")
    lol = "".join(f'<!ENTITY l{n} "{f"&l{n - 1};" * 10}">' for n in range(1, 8))
    body = (
        f'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY l0 "ha">{lol}<!ENTITY x SYSTEM "{secret.as_uri()}">]>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
        "<w:p><w:r><w:t>before &x; &l7; after</w:t></w:r></w:p></w:body></w:document>"
    )
    path = tmp_path / "xxe.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", body)
    began = time.monotonic()
    try:
        doc = read(path)
    except UnreadableFile:
        doc = None
    assert time.monotonic() - began < 5
    assert "SYNTHETIC-SECRET-CONTENT" not in (texts(doc) if doc else "")
    assert "hahahaha" not in (texts(doc) if doc else "")


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
