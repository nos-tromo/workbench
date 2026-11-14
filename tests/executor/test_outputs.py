"""The output runners, ``write_table`` and ``write_document``, in a folder laid out as a job's.

Every file a runner writes is read back with the library that would open it: openpyxl for a workbook, the csv module
for a CSV file, python-docx and lxml on its parts for a Word document.
"""

import csv
import socket
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from docx import Document
from lxml import etree

from workbench.executor.runners import RUNNERS, JobContext

JOB_ID = "0123456789abcdef0123456789abcdef"
CHAT = "chat-1"
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


@dataclass(frozen=True)
class Job:
    """A job folder as the supervisor makes it, the runners' working folder its chat folder.

    Attributes:
        ctx: What the job entry point gives the runner.
    """

    ctx: JobContext

    def table(self, name: str, columns: list[str], rows: list[list[Any]], **more: Any) -> dict[str, Any]:
        """Run ``write_table``; the format follows the name's extension."""
        args = {"name": name, "format": name.rsplit(".", 1)[1], "columns": columns, "rows": rows, "budget_s": 30}
        return RUNNERS["write_table"]({**args, **more}, self.ctx)

    def document(self, name: str, markdown: str, **more: Any) -> dict[str, Any]:
        """Run ``write_document``; the format follows the name's extension."""
        args = {"name": name, "format": name.rsplit(".", 1)[1], "markdown": markdown, "budget_s": 30}
        return RUNNERS["write_document"]({**args, **more}, self.ctx)

    def output(self, name: str) -> Path:
        """A file the job wrote into ``out/``."""
        return self.ctx.out_dir / name


@pytest.fixture
def job(tmp_path: Path) -> Job:
    """An empty job folder for ``chat-1``."""
    work = tmp_path / "data" / "jobs" / JOB_ID / "workspace" / CHAT
    (work / "out").mkdir(parents=True)
    return Job(JobContext(work_dir=work, out_dir=work / "out", chat_id=CHAT, max_chars=16 * 1024, file_mb=50))


def document_xml(path: Path) -> etree._Element:
    """The main part of a Word document, parsed."""
    with zipfile.ZipFile(path) as archive:
        return etree.fromstring(archive.read("word/document.xml"))


def read_csv(path: Path) -> list[list[str]]:
    """The rows of a CSV file, read as Excel's dialect after the byte order mark."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.reader(stream))


# --- write_table: a workbook ------------------------------------------------------------------------------------


def test_xlsx_round_trip(job: Job) -> None:
    """The header and the rows read back with their types; the header is bold and the panes are frozen."""
    rows = [["Dredging", 118, 0.5, True], ["Piers", -3, None, False], ["Total"]]
    assert job.table("quarterly-figures.xlsx", ["item", "total", "share", "done"], rows) == {}
    book = openpyxl.load_workbook(job.output("quarterly-figures.xlsx"))
    assert book.sheetnames == ["Sheet1"]
    sheet = book["Sheet1"]
    assert [[cell.value for cell in row] for row in sheet.iter_rows()] == [
        ["item", "total", "share", "done"],
        ["Dredging", 118, 0.5, True],
        ["Piers", -3, None, False],
        ["Total", None, None, None],
    ]
    assert all(cell.font.b for cell in sheet[1])
    assert not sheet["A2"].font.b
    assert sheet.freeze_panes == "A2"


def test_values_are_data_never_formulas(job: Job) -> None:
    """A formula, a number with a leading zero and a web address stay the strings they were."""
    job.table("t.xlsx", ["=SUM(A1:A2)"], [["=1+1"], ["0042"], ["https://example.invalid/x"], ["+1"], ["@x"]])
    sheet = openpyxl.load_workbook(job.output("t.xlsx"))["Sheet1"]
    cells = [sheet[f"A{n}"] for n in range(1, 7)]
    assert [cell.value for cell in cells] == ["=SUM(A1:A2)", "=1+1", "0042", "https://example.invalid/x", "+1", "@x"]
    assert {cell.data_type for cell in cells} == {"s"}
    assert all(cell.hyperlink is None for cell in cells)


def test_the_sheet_is_named_as_asked(job: Job) -> None:
    """The one sheet takes the name given."""
    job.table("t.xlsx", ["a"], [[1]], sheet="Q3 Ümsätze")
    assert openpyxl.load_workbook(job.output("t.xlsx")).sheetnames == ["Q3 Ümsätze"]


def test_characters_a_workbook_cannot_hold_are_replaced_and_the_hint_says_so(job: Job) -> None:
    """A control character becomes U+FFFD, in a cell, a column's name or the sheet's name; tabs and newlines stay."""
    answer = job.table("t.xlsx", ["col\x01"], [["a\x00b"], ["tab\there\nline"]], sheet="S\x02")
    assert "3 texts" in answer["hint"]
    book = openpyxl.load_workbook(job.output("t.xlsx"))
    assert book.sheetnames == ["S\ufffd"]
    sheet = book["S\ufffd"]
    assert [sheet["A1"].value, sheet["A2"].value, sheet["A3"].value] == ["col\ufffd", "a\ufffdb", "tab\there\nline"]


# --- write_table: a CSV file ------------------------------------------------------------------------------------


def test_csv_has_bom_and_umlauts(job: Job) -> None:
    """UTF-8 with a byte order mark, so Excel reads the umlauts; Excel's dialect; bools as TRUE and FALSE."""
    rows = [["Grüße aus Köln", 1.5, True, None], ['Straße "Nord", 3', 2, False]]
    assert job.table("t.csv", ["Ort", "Menge", "ok", "leer"], rows) == {}
    raw = job.output("t.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbfOrt,Menge,ok,leer\r\n")
    assert "Grüße aus Köln".encode() in raw
    assert read_csv(job.output("t.csv")) == [
        ["Ort", "Menge", "ok", "leer"],
        ["Grüße aus Köln", "1.5", "TRUE", ""],
        ['Straße "Nord", 3', "2", "FALSE", ""],
    ]


def test_csv_cells_a_spreadsheet_would_run_are_escaped_and_counted(job: Job) -> None:
    """A text that starts with =, +, -, @, a tab or a carriage return gets a leading '; numbers and bools never do."""
    rows = [["=1+1", "+1", "-2", "@x", "\tt", "\rr", "plain"], [-5, 1.5, True, None, "a=b", "", "x-"]]
    answer = job.table("t.csv", ["=head", "b", "c", "d", "e", "f", "g"], rows)
    assert "7 cells" in answer["hint"]
    assert read_csv(job.output("t.csv")) == [
        ["'=head", "b", "c", "d", "e", "f", "g"],
        ["'=1+1", "'+1", "'-2", "'@x", "'\tt", "'\rr", "plain"],
        ["-5", "1.5", "TRUE", "", "a=b", "", "x-"],
    ]


@pytest.mark.parametrize(("delimiter", "line"), [(";", "'=1+1;1.5;TRUE"), ("\t", "'=1+1\t1.5\tTRUE")])
def test_a_csv_may_be_separated_by_a_semicolon_or_a_tab(job: Job, delimiter: str, line: str) -> None:
    """The byte order mark and the escaping stay whatever separates the cells."""
    assert "1 cell" in job.table("t.csv", ["a", "b", "c"], [["=1+1", 1.5, True]], delimiter=delimiter)["hint"]
    raw = job.output("t.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    with job.output("t.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert list(csv.reader(stream, delimiter=delimiter)) == [["a", "b", "c"], ["'=1+1", "1.5", "TRUE"]]
    assert raw.decode("utf-8-sig").splitlines()[1] == line


def test_a_delimiter_is_for_csv_alone(job: Job) -> None:
    """A workbook has no delimiter: one given is not used. One a CSV does not take is refused."""
    assert job.table("t.xlsx", ["a"], [[1]], delimiter=";") == {}
    assert openpyxl.load_workbook(job.output("t.xlsx"))["Sheet1"]["A2"].value == 1
    assert job.table("u.csv", ["a"], [[1]], delimiter="|")["error"].startswith("invalid_value: delimiter: ")
    assert not job.output("u.csv").exists()


# --- write_table: what the runner refuses -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("columns", "rows", "said"),
    [
        (["a"], [[1, 2]], "row 1 has 2 cells"),
        (["a", "b"], [[1], [1, "x" * 32_768]], "row 2, column 2"),
        (["a"], [[10**400]], "row 1, column 1"),
        (
            ["a"],
            [[1.7e308]],
            "row 1, column 1: a number must be finite and no larger in size than 9.99999999999999E+307",
        ),
        (["a"], [[-1e308]], "row 1, column 1"),
        (["a"], [[[1]]], "row 1, column 1"),
        (["x" * 32_768], [], "column 1"),
        ([], [], "column"),
    ],
    ids=[
        "row too long",
        "cell too long",
        "integer too large",
        "over excel's largest",
        "under excel's smallest",
        "not a value",
        "name too long",
        "no columns",
    ],
)
def test_the_runner_refuses_what_the_api_would_have(job: Job, columns: list[str], rows: list[Any], said: str) -> None:
    """The api checks first; the runner trusts nothing of it and names the place, 1-based, and writes nothing."""
    error = job.table("t.xlsx", columns, rows)["error"]
    assert error.startswith("invalid_value: ")
    assert said in error
    assert list(job.ctx.out_dir.iterdir()) == []


@pytest.mark.parametrize(
    "sheet", ["", "x" * 32, "a[b", "a:b", "a/b", "a\\b", "a?b", "a*b", "a]b", "'a", "a'", "history"]
)
def test_a_sheet_name_excel_refuses_is_refused(job: Job, sheet: str) -> None:
    """From 1 to 31 characters, no bracket, colon, star, question mark or slash, no quote at an end, not History."""
    assert job.table("t.xlsx", ["a"], [], sheet=sheet)["error"].startswith("invalid_value: sheet")


# --- write_document: Word -----------------------------------------------------------------------------------------

MARKDOWN = """# Quarterly report

An intro with **bold**, *italic* and `code`.

## Figures

- Dredging
- Piers
  - north pier
    - its head

1. first
2. second

| Item | Total |
|------|------:|
| Dredging | **118** |
| Piers | 351 |

### Notes

#### Small print

##### Smaller still

```
def total(rows):
    return sum(rows)
```
"""


def test_docx_from_markdown_has_headings_lists_and_tables(job: Job) -> None:
    """Heading styles 1 to 4 (5 and 6 as 4), list paragraphs by level, one table with a bold header, runs formatted."""
    assert job.document("report.docx", MARKDOWN) == {}
    document = Document(str(job.output("report.docx")))
    styled = [(p.style.name if p.style is not None else "", p.text) for p in document.paragraphs]
    assert styled[:3] == [
        ("Heading 1", "Quarterly report"),
        ("Normal", "An intro with bold, italic and code."),
        ("Heading 2", "Figures"),
    ]
    assert styled[3:9] == [
        ("List Bullet", "Dredging"),
        ("List Bullet", "Piers"),
        ("List Bullet 2", "north pier"),
        ("List Bullet 3", "its head"),
        ("List Number", "first"),
        ("List Number", "second"),
    ]
    assert styled[9:12] == [("Heading 3", "Notes"), ("Heading 4", "Small print"), ("Heading 4", "Smaller still")]
    intro = document.paragraphs[1].runs
    assert [(r.text, bool(r.bold), bool(r.italic)) for r in intro if r.text in {"bold", "italic"}] == [
        ("bold", True, False),
        ("italic", False, True),
    ]
    (table,) = document.tables
    assert [[cell.text for cell in row.cells] for row in table.rows] == [
        ["Item", "Total"],
        ["Dredging", "118"],
        ["Piers", "351"],
    ]
    assert all(run.bold for cell in table.rows[0].cells for run in cell.paragraphs[0].runs)
    assert all(run.bold for run in table.rows[1].cells[1].paragraphs[0].runs)
    assert not any(run.bold for run in table.rows[2].cells[0].paragraphs[0].runs)


def test_code_keeps_its_lines_in_a_monospace_font(job: Job) -> None:
    """A code block is one paragraph with its line breaks and indentation; inline code is monospace too."""
    job.document("code.docx", "Run `make test` now.\n\n```\ndef total(rows):\n    return sum(rows)\n```\n")
    document = Document(str(job.output("code.docx")))
    inline = [run for run in document.paragraphs[0].runs if run.text == "make test"]
    assert [run.font.name for run in inline] == ["Consolas"]
    block = document.paragraphs[1]
    assert block.text == "def total(rows):\n    return sum(rows)"
    assert {run.font.name for run in block.runs} == {"Consolas"}
    assert len(block._p.findall(f".//{W}br")) == 1


def test_each_numbered_list_starts_again(job: Job) -> None:
    """Two numbered lists with a paragraph between: each has its own numbering, which starts at its first number."""
    job.document("lists.docx", "1. one\n2. two\n\nBetween.\n\n1. again\n\nAnd:\n\n3. three\n4. four\n")
    root = document_xml(job.output("lists.docx"))
    numbered = [p.find(f"{W}pPr/{W}numPr/{W}numId") for p in root.iter(f"{W}p")]
    ids = [number.get(f"{W}val") for number in numbered if number is not None]
    assert len(ids) == 5
    first, second, third = ids[0], ids[2], ids[3]
    assert ids[1] == first and ids[4] == third
    assert len({first, second, third}) == 3
    with zipfile.ZipFile(job.output("lists.docx")) as archive:
        numbering = etree.fromstring(archive.read("word/numbering.xml"))
    starts = {}
    for num in numbering.iter(f"{W}num"):
        override = num.find(f"{W}lvlOverride/{W}startOverride")
        if override is not None:
            starts[num.get(f"{W}numId")] = override.get(f"{W}val")
    assert [starts[first], starts[second], starts[third]] == ["1", "1", "3"]


def test_a_paragraph_that_goes_on_in_a_list_item_continues_it(job: Job) -> None:
    """The second paragraph of an item is a continuation, not a new bullet."""
    job.document("loose.docx", "- first\n\n  more of the first\n- second\n")
    document = Document(str(job.output("loose.docx")))
    assert [(p.style.name if p.style is not None else "", p.text) for p in document.paragraphs] == [
        ("List Bullet", "first"),
        ("List Continue", "more of the first"),
        ("List Bullet", "second"),
    ]


def _numbered(path: Path) -> list[tuple[str, str, str | None]]:
    """Each paragraph's style, text and numbering id."""
    document = Document(str(path))
    found: list[tuple[str, str, str | None]] = []
    for paragraph in document.paragraphs:
        number = paragraph._p.find(f"{W}pPr/{W}numPr/{W}numId")
        style = (paragraph.style.name or "") if paragraph.style is not None else ""
        found.append((style, paragraph.text, None if number is None else str(number.get(f"{W}val"))))
    return found


def test_an_item_without_a_paragraph_keeps_its_number(job: Job) -> None:
    """An empty item, or one that opens with a code block, a heading or a list, has its number before what it holds.

    So the block reads as the item's, not the one before, and the numbers after it hold.
    """
    assert job.document("items.docx", "1. a\n2.\n3. c\n") == {}
    found = _numbered(job.output("items.docx"))
    assert [(style, text) for style, text, _ in found] == [
        ("List Number", "a"),
        ("List Number", ""),
        ("List Number", "c"),
    ]
    assert len({number for _, _, number in found}) == 1
    assert found[0][2] is not None
    assert job.document("code.docx", "- a\n-     code\n- c\n") == {}
    bullets = [(style, text) for style, text, _ in _numbered(job.output("code.docx"))]
    assert bullets == [("List Bullet", "a"), ("List Bullet", ""), ("Normal", "code"), ("List Bullet", "c")]
    assert job.document("heading.docx", "1. a\n2. # Findings\n3. c\n") == {}
    found = _numbered(job.output("heading.docx"))
    assert [(style, text) for style, text, _ in found] == [
        ("List Number", "a"),
        ("List Number", ""),
        ("Heading 1", "Findings"),
        ("List Number", "c"),
    ]
    assert len({number for style, _, number in found if style == "List Number"}) == 1
    assert job.document("nested.docx", "- - inner\n  - more\n- c\n") == {}
    bullets = [(style, text) for style, text, _ in _numbered(job.output("nested.docx"))]
    assert bullets == [("List Bullet", ""), ("List Bullet 2", "inner"), ("List Bullet 2", "more"), ("List Bullet", "c")]


def test_nested_numbered_lists_take_the_levels_numbering(job: Job) -> None:
    """Three levels: List Number, List Number 2 and List Number 3, each list numbered on its own from its first item."""
    assert job.document("nested.docx", "1. a\n   1. b\n      1. c\n   2. d\n2. e\n") == {}
    found = _numbered(job.output("nested.docx"))
    assert [(style, text) for style, text, _ in found] == [
        ("List Number", "a"),
        ("List Number 2", "b"),
        ("List Number 3", "c"),
        ("List Number 2", "d"),
        ("List Number", "e"),
    ]
    ids = [number for _, _, number in found]
    assert None not in ids
    assert ids[0] == ids[4] and ids[1] == ids[3]
    assert len({ids[0], ids[1], ids[2]}) == 3


def test_a_thematic_break_is_written_as_text(job: Job) -> None:
    """Other syntax is plain text: a line of --- is a paragraph of its own, never dropped."""
    assert job.document("break.docx", "Before.\n\n---\n\nAfter.\n") == {}
    document = Document(str(job.output("break.docx")))
    assert [p.text for p in document.paragraphs] == ["Before.", "———", "After."]


def test_a_long_table_reads_its_grid_once(job: Job, monkeypatch: pytest.MonkeyPatch) -> None:
    """python-docx's ``add_row`` looks the grid up on every row, a search as long as the table: N rows cost N² time."""
    from docx.oxml.table import CT_Tbl

    looked = 0
    grid = CT_Tbl.tblGrid

    def counted(table: CT_Tbl) -> Any:
        nonlocal looked
        looked += 1
        return grid.fget(table)

    monkeypatch.setattr(CT_Tbl, "tblGrid", property(counted))
    assert job.document("long.docx", "|a|b|\n|-|-|\n" + "|x|y|\n" * 500) == {}
    assert looked == 1
    monkeypatch.undo()
    (table,) = Document(str(job.output("long.docx"))).tables
    assert len(table.rows) == 501
    assert [cell.text for cell in table.rows[500].cells] == ["x", "y"]


def test_a_table_of_40000_rows_is_written_well_inside_the_budget(job: Job) -> None:
    """The longest tables a document holds are written in a fraction of the 90 s a job of 120 s gives the writer."""
    markdown = "|a|\n|-|\n" + "|x|\n" * 40_000
    began = time.monotonic()
    assert job.document("tall.docx", markdown, budget_s=90) == {}
    assert time.monotonic() - began < 45
    assert job.output("tall.docx").stat().st_size > 0


def test_the_python_docx_internals_the_writer_uses_are_there() -> None:
    """The writer reaches below python-docx's public api to stay fast; an upgrade that drops one fails here, by name."""
    from docx.oxml.numbering import CT_Num, CT_Numbering
    from docx.oxml.table import CT_Row, CT_Tbl
    from docx.table import _Cell

    document = Document()
    used = {
        "Paragraph._p": hasattr(document.add_paragraph(), "_p"),
        "Document._body": hasattr(document, "_body"),
        "CT_Num.new": callable(getattr(CT_Num, "new", None)),
        "CT_Numbering._insert_num": callable(getattr(CT_Numbering, "_insert_num", None)),
        "CT_Numbering.num_having_numId": callable(getattr(CT_Numbering, "num_having_numId", None)),
        "CT_Tbl.new_tbl": callable(getattr(CT_Tbl, "new_tbl", None)),
        "CT_Tbl.tblGrid": isinstance(getattr(CT_Tbl, "tblGrid", None), property),
        "CT_Tbl.add_tr": callable(getattr(CT_Tbl, "add_tr", None)),
        "CT_Row.add_tc": callable(getattr(CT_Row, "add_tc", None)),
        "_Cell": isinstance(_Cell, type),
    }
    assert [name for name, there in used.items() if not there] == []


def test_links_images_and_raw_html_are_text_and_nothing_is_fetched(job: Job, monkeypatch: pytest.MonkeyPatch) -> None:
    """A link is its text and its address, an image its alt text, raw HTML as written; no socket is ever opened."""

    def no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the runner opened a socket")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    markdown = (
        "See [the plan](https://example.invalid/plan), <https://example.invalid/a> and "
        "![a harbour map](http://example.invalid/map.png). <b>raw</b>\n"
    )
    assert job.document("links.docx", markdown) == {}
    document = Document(str(job.output("links.docx")))
    assert document.paragraphs[0].text == (
        "See the plan (https://example.invalid/plan), https://example.invalid/a and a harbour map. <b>raw</b>"
    )
    with zipfile.ZipFile(job.output("links.docx")) as archive:
        assert not [name for name in archive.namelist() if name.startswith("word/media/")]
        rels = archive.read("word/_rels/document.xml.rels").decode()
    assert "image" not in rels
    assert "example.invalid" not in rels


def test_excels_largest_numbers_are_written(job: Job) -> None:
    """9.99999999999999E+307 either way is within Excel's bound."""
    assert job.table("t.xlsx", ["a"], [[9.99999999999999e307], [-9.99999999999999e307]]) == {}
    sheet = openpyxl.load_workbook(job.output("t.xlsx"))["Sheet1"]
    assert (sheet["A2"].value, sheet["A3"].value) == (9.99999999999999e307, -9.99999999999999e307)


def _markdown_table(width: int) -> str:
    """A Markdown table of ``width`` columns and one row."""
    return "|" + "|".join(f"c{n}" for n in range(width)) + "|\n|" + "---|" * width + "\n|" + "x|" * width + "\n"


def test_a_table_wider_than_word_holds_is_refused_before_anything_is_written(job: Job) -> None:
    """Word's tables have at most 63 columns: the refusal names the table, counted from 1, and points to write_table."""
    markdown = f"# Figures\n\n{_markdown_table(2)}\nText.\n\n{_markdown_table(64)}"
    error = job.document("wide.docx", markdown)["error"]
    assert error.startswith("invalid_value: markdown: table 2 has 64 columns")
    assert "63" in error and "write_table" in error
    assert list(job.ctx.out_dir.iterdir()) == []
    assert job.document("widest.docx", _markdown_table(63)) == {}
    assert len(Document(str(job.output("widest.docx"))).tables[0].columns) == 63
    assert job.document("wide.md", _markdown_table(64), format="md") == {}


def test_characters_a_document_cannot_hold_are_replaced_and_the_hint_says_so(job: Job) -> None:
    """A control character becomes U+FFFD, in text, a table or code."""
    answer = job.document("ctl.docx", "a\x01b\n\n| h\x02 |\n|---|\n| c |\n\n```\nx\x03y\n```\n")
    assert "3 characters" in answer["hint"]
    document = Document(str(job.output("ctl.docx")))
    assert document.paragraphs[0].text == "a\ufffdb"
    assert document.tables[0].rows[0].cells[0].text == "h\ufffd"
    assert document.paragraphs[1].text == "x\ufffdy"


# --- write_document: Markdown -------------------------------------------------------------------------------------


def test_markdown_is_written_as_given(job: Job) -> None:
    """UTF-8 without a byte order mark, line endings and all."""
    text = "# Bericht\r\n\nGrüße, **fett**\n<b>raw</b>\n"
    assert job.document("notes.md", text) == {}
    assert job.output("notes.md").read_bytes() == text.encode()


def test_a_lone_surrogate_in_markdown_is_written_as_a_replacement_character(job: Job) -> None:
    """Half of a pair cannot be UTF-8: it becomes U+FFFD, the hint says so, and a whole pair stays one character."""
    answer = job.document("notes.md", "a\ud800b \U0001f600")
    assert "1 character" in answer["hint"]
    assert job.output("notes.md").read_bytes() == "a�b \U0001f600".encode()


def test_a_document_over_the_bound_is_refused(job: Job) -> None:
    """The api's bound, again in the runner."""
    assert job.document("long.md", "x" * 200_001)["error"].startswith("invalid_value: markdown: ")
    assert list(job.ctx.out_dir.iterdir()) == []


def test_work_past_its_budget_is_a_timeout(job: Job) -> None:
    """The job's time limit, as for the reading tools: an answer the api maps to a 422."""
    error = job.document("slow.docx", "# x\n" * 1000, budget_s=1e-9)["error"]
    assert error.startswith("write_timeout: ")


@pytest.mark.parametrize("name", ["../escape.docx", "a/b.docx", ""])
def test_a_name_that_is_not_a_plain_file_name_is_refused(job: Job, name: str) -> None:
    """The api chose the name; the runner checks it all the same."""
    args = {"name": name, "format": "docx", "markdown": "x", "budget_s": 30}
    assert RUNNERS["write_document"](args, job.ctx)["error"].startswith("invalid_value: ")
    assert list(job.ctx.out_dir.iterdir()) == []


def test_a_format_the_runner_does_not_write_is_refused(job: Job) -> None:
    """The api allows two each; the runner checks again."""
    assert job.table("t.xls", ["a"], [])["error"].startswith("invalid_value: format: ")
    assert job.document("t.pdf", "x")["error"].startswith("invalid_value: format: ")
    assert list(job.ctx.out_dir.iterdir()) == []
