"""The template runners, ``inspect_template`` and ``fill_template``, in a folder laid out as a job's.

Every template is built at test time (``tests/fixtures/make_templates.py``) and every filled file is read back with
the library that would open it: python-docx, lxml on its parts, openpyxl.
"""

import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import openpyxl
import pytest
from docx import Document
from lxml import etree

from tests.fixtures import make_fixtures as mk
from tests.fixtures import make_templates as mt
from workbench.executor.runners import RUNNERS, JobContext

JOB_ID = "0123456789abcdef0123456789abcdef"
CHAT = "chat-1"
W = f"{{{mt.W}}}"
W14 = f"{{{mt.W14}}}"
SOURCES_HEADER = ["field", "value", "source kind", "reference"]
ASSISTANT: dict[str, Any] = {"kind": "assistant"}


@dataclass(frozen=True)
class Job:
    """A job folder as the supervisor makes it, the runners' working folder its chat folder.

    Attributes:
        ctx: What the job entry point gives the runner.
    """

    ctx: JobContext

    def path(self, name: str) -> Path:
        """Where an input of that name lies."""
        return self.ctx.work_dir / name

    def inspect(self, name: str) -> dict[str, Any]:
        """Run ``inspect_template`` on an input."""
        return RUNNERS["inspect_template"]({"path": name, "budget_s": 30}, self.ctx)

    def fill(
        self, name: str, values: dict[str, Any], *, output: str | None = None, sources: bool = True
    ) -> dict[str, Any]:
        """Run ``fill_template`` on an input; a value given bare gets the assistant as its source."""
        given = {
            key: value if isinstance(value, dict) and "source" in value else {"value": value, "source": ASSISTANT}
            for key, value in values.items()
        }
        stem, ext = name.rsplit(".", 1)
        args = {
            "path": name,
            "values": given,
            "output_name": output or f"{stem}-filled.{'docx' if ext == 'dotx' else ext}",
            "include_sources": sources,
            "budget_s": 30,
        }
        return RUNNERS["fill_template"](args, self.ctx)

    def output(self, name: str) -> Path:
        """A file the job wrote into ``out/``."""
        return self.ctx.out_dir / name


@pytest.fixture
def job(tmp_path: Path) -> Job:
    """An empty job folder for ``chat-1``."""
    work = tmp_path / "data" / "jobs" / JOB_ID / "workspace" / CHAT
    (work / "out").mkdir(parents=True)
    return Job(JobContext(work_dir=work, out_dir=work / "out", chat_id=CHAT, max_chars=16 * 1024, file_mb=50))


def by_name(answer: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The fields of an ``inspect_template`` answer by name."""
    return {field["name"]: field for field in answer["fields"]}


def part(path: Path, name: str) -> etree._Element:
    """A part of a filled package, parsed."""
    with zipfile.ZipFile(path) as archive:
        return etree.fromstring(archive.read(name))


def body_text(path: Path) -> list[str]:
    """The text of every paragraph of a document's body, content controls included, in order."""
    root = part(path, "word/document.xml")
    return ["".join(t.text or "" for t in p.iter(f"{W}t")) for p in root.iter(f"{W}p")]


def sources_table(path: Path) -> list[list[str]]:
    """The rows of a filled document's last table: its sources appendix."""
    table = Document(str(path)).tables[-1]
    return [[cell.text for cell in row.cells] for row in table.rows]


def sdt(root: etree._Element, tag: str) -> list[etree._Element]:
    """The content controls of a part with this tag."""
    return [
        element for element in root.iter(f"{W}sdt") if element.find(f"{W}sdtPr/{W}tag[@{W}val='{tag}']") is not None
    ]


# --- inspect_template ---------------------------------------------------------------------------------------------


def test_inspect_lists_content_controls_with_types_and_options(job: Job) -> None:
    """Every named control, once, with its type, a drop-down's entries and where it sits; the header's too."""
    mt.make_controls_docx(job.path("form.docx"))
    answer = job.inspect("form.docx")
    assert answer["template_kind"] == "content_controls"
    assert answer["fields"] == [
        {
            "name": "customer",
            "kind": "content_control",
            "type": "text",
            "options": None,
            "location": "body, paragraph 2 (and 1 more)",
        },
        {
            "name": "report_date",
            "kind": "content_control",
            "type": "date",
            "options": None,
            "location": "body, paragraph 3",
        },
        {
            "name": "status",
            "kind": "content_control",
            "type": "dropdown",
            "options": ["open", "closed"],
            "location": "body, table 1, row 2",
        },
        {
            "name": "approved",
            "kind": "content_control",
            "type": "checkbox",
            "options": None,
            "location": "body, paragraph 4",
        },
        {
            "name": "summary",
            "kind": "content_control",
            "type": "rich_text",
            "options": None,
            "location": "body, paragraph 5",
        },
    ]
    assert answer["truncated"] is False


def test_inspect_lists_legacy_form_fields(job: Job) -> None:
    """Text (a date one too), check box and drop-down form fields, named by their bookmark."""
    mt.make_form_fields_docx(job.path("legacy.docx"))
    answer = job.inspect("legacy.docx")
    assert answer["template_kind"] == "form_fields"
    assert [(f["name"], f["kind"], f["type"], f["options"]) for f in answer["fields"]] == [
        ("name", "form_field", "text", None),
        ("agreed", "form_field", "checkbox", None),
        ("region", "form_field", "dropdown", ["north", "south"]),
        ("signed_on", "form_field", "date", None),
    ]
    assert [f["location"] for f in answer["fields"]] == [f"body, paragraph {n}" for n in range(1, 5)]


def test_inspect_none_explains_what_a_template_needs(job: Job) -> None:
    """A document without fields says that version 1 cannot fill it, and what it needs."""
    mk.make_docx(job.path("plain.docx"))
    answer = job.inspect("plain.docx")
    assert (answer["template_kind"], answer["fields"]) == ("none", [])
    hint = answer["hint"]
    for phrase in ("version 1 cannot fill", "content controls", "form fields", "{{ field }}", "{{ table:name }}"):
        assert phrase in hint, phrase


def test_inspect_lists_placeholders_of_a_document_with_their_place(job: Job) -> None:
    """``{{ }}`` names are fields; a loop's list is a table; Jinja's own names are not fields."""
    mt.make_jinja_docx(job.path("letter.docx"), extra="{% for n in range(2) %}{{ n }}{% endfor %}")
    answer = job.inspect("letter.docx")
    assert answer["template_kind"] == "placeholders"
    assert by_name(answer) == {
        "customer": {
            "name": "customer",
            "kind": "placeholder",
            "type": "text",
            "options": None,
            "location": "body, paragraph 1 (and 1 more)",
        },
        "rows": {
            "name": "rows",
            "kind": "placeholder",
            "type": "table",
            "options": None,
            "location": "body, table 1, row 2",
        },
        "approved": {
            "name": "approved",
            "kind": "placeholder",
            "type": "text",
            "options": None,
            "location": "body, paragraph 2",
        },
    }


def test_inspect_lists_placeholders_and_table_anchors_of_a_workbook(job: Job) -> None:
    """A cell of its own and a cell's text are placeholders; ``{{ table:rows }}`` is a table anchor."""
    mt.make_placeholder_xlsx(job.path("summary.xlsx"))
    answer = job.inspect("summary.xlsx")
    assert answer["template_kind"] == "placeholders"
    assert answer["fields"] == [
        {
            "name": "customer",
            "kind": "placeholder",
            "type": "text",
            "options": None,
            "location": "Summary!B1 (and 1 more)",
        },
        {"name": "total", "kind": "placeholder", "type": "text", "options": None, "location": "Summary!B2"},
        {"name": "rows", "kind": "table", "type": "table", "options": None, "location": "Summary!A6"},
    ]


def test_a_document_with_controls_and_placeholders_is_mixed(job: Job) -> None:
    """Both kinds are listed, controls first."""
    mt.make_controls_body(job.path("both.docx"), [mt.text_control("customer"), "<w:r><w:t>{{ total }}</w:t></w:r>"])
    answer = job.inspect("both.docx")
    assert answer["template_kind"] == "mixed"
    assert [(f["name"], f["kind"]) for f in answer["fields"]] == [
        ("customer", "content_control"),
        ("total", "placeholder"),
    ]


def test_only_the_innermost_controls_are_fields(job: Job) -> None:
    """A control that holds other controls is not listed; the ones inside are. One without a name is counted."""
    group = (
        '<w:sdt><w:sdtPr><w:tag w:val="group"/><w:group/></w:sdtPr><w:sdtContent><w:p>'
        + mt.text_control("inner")
        + "</w:p></w:sdtContent></w:sdt>"
    )
    mt.make_controls_body(job.path("nested.docx"), [group, mt.text_control("")])
    answer = job.inspect("nested.docx")
    assert [f["name"] for f in answer["fields"]] == ["inner"]
    assert "1 content control has neither a tag nor a title" in answer["hint"]


@pytest.mark.parametrize("name", ["macro.docm", "macro.xlsm", "model.xltx", "notes.txt"])
def test_other_files_are_not_templates(job: Job, name: str) -> None:
    """Only ``.docx``, ``.dotx`` and ``.xlsx`` are templates; the answer says so."""
    job.path(name).write_bytes(b"PK")
    error = job.inspect(name)["error"]
    assert error.startswith("unsupported_template: ")
    for ext in (".docx", ".dotx", ".xlsx"):
        assert ext in error


def test_a_damaged_template_is_unreadable(job: Job) -> None:
    """Not a zip, or a zip that is not a Word document, is told as unreadable."""
    job.path("broken.docx").write_bytes(b"PK\x03\x04 not really")
    assert job.inspect("broken.docx")["error"].startswith("unreadable_file: ")


def test_a_zip_bomb_template_is_refused_by_the_zip_guard(job: Job) -> None:
    """The template goes through the same guard as read_document."""
    mk.make_zip_bomb(job.path("bomb.docx"), mb=25)
    small = Job(JobContext(job.ctx.work_dir, job.ctx.out_dir, CHAT, job.ctx.max_chars, file_mb=1))
    assert "decompression bomb" in small.inspect("bomb.docx")["error"]


def test_an_inspection_with_more_fields_than_fit_is_cut(job: Job) -> None:
    """The fields that fit in three times the result limit, then ``truncated`` and how many there are in all."""
    mt.make_controls_body(job.path("many.docx"), [mt.text_control(f"field_{n:04d}_" + "x" * 80) for n in range(400)])
    answer = job.inspect("many.docx")
    assert (answer["truncated"], answer["total"]) == (True, 400)
    assert 0 < len(answer["fields"]) < 400
    assert len(json.dumps(answer["fields"])) <= 3 * job.ctx.max_chars
    assert answer["fields"][0]["name"].startswith("field_0000_")


# --- fill_template: Word content controls ------------------------------------------------------------------------


def test_fill_content_controls_keeps_styles_and_writes_sources(job: Job) -> None:
    """Every control takes its value in its type; styles, header and protection stay; a Sources table ends it."""
    mt.make_controls_docx(job.path("form.docx"))
    answer = job.fill(
        "form.docx",
        {
            "customer": "Invented Harbour Ltd",
            "report_date": "2026-03-07",
            "status": "closed",
            "approved": True,
            "summary": "First line\nSecond line",
        },
    )
    assert answer == {
        "filled": ["customer", "report_date", "status", "approved", "summary"],
        "unfilled": [],
        "warnings": [],
    }
    out = job.output("form-filled.docx")
    texts = body_text(out)
    assert "Customer: Invented Harbour Ltd" in texts
    assert "Report date: 07.03.2026" in texts
    assert "closed" in texts
    assert "Approved: ☒" in texts
    document = part(out, "word/document.xml")
    (summary,) = sdt(document, "summary")
    paragraphs = summary.findall(f"{W}sdtContent/{W}p")
    assert ["".join(t.text or "" for t in p.iter(f"{W}t")) for p in paragraphs] == ["First line", "Second line"]
    assert all(p.find(f"{W}pPr/{W}pStyle") is not None for p in paragraphs)
    assert document.find(f".//{W}showingPlcHdr") is None
    assert document.find(f".//{W}rStyle[@{W}val='{mt.PLACEHOLDER_STYLE}']") is None
    (date,) = sdt(document, "report_date")
    assert date.find(f"{W}sdtPr/{W}date").get(f"{W}fullDate") == "2026-03-07T00:00:00Z"
    (box,) = sdt(document, "approved")
    assert box.find(f"{W}sdtPr/{W14}checkbox/{W14}checked").get(f"{W14}val") == "1"
    (status,) = sdt(document, "status")
    assert status.find(f"{W}sdtPr/{W}dropDownList").get(f"{W}lastValue") == "closed-value"
    filled = Document(str(out))
    assert mt.BODY_STYLE in [style.name for style in filled.styles]
    style = filled.paragraphs[0].style
    assert style is not None
    assert style.name == mt.BODY_STYLE
    header = "".join(t.text or "" for t in part(out, "word/header1.xml").iter(f"{W}t"))
    assert header == mt.HEADER_TEXT + ", customer: Invented Harbour Ltd"
    protection = part(out, "word/settings.xml").find(f"{W}documentProtection")
    assert (protection.get(f"{W}edit"), protection.get(f"{W}enforcement")) == ("forms", "1")
    rows = sources_table(out)
    assert rows[0] == SOURCES_HEADER
    assert rows[1:] == [
        ["customer", "Invented Harbour Ltd", "assistant", ""],
        ["report_date", "2026-03-07", "assistant", ""],
        ["status", "closed", "assistant", ""],
        ["approved", "true", "assistant", ""],
        ["summary", "First line\nSecond line", "assistant", ""],
    ]


def test_dropdown_refuses_values_outside_its_list(job: Job) -> None:
    """The error names the field and lists its entries; nothing is written."""
    mt.make_controls_docx(job.path("form.docx"))
    error = job.fill("form.docx", {"status": "pending"})["error"]
    assert error.startswith("invalid_value: status ")
    assert '"open"' in error
    assert '"closed"' in error
    assert list(job.ctx.out_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("name", "value", "said"),
    [
        ("approved", "yes", "true or false"),
        ("approved", 1, "true or false"),
        ("report_date", "07.03.2026", "YYYY-MM-DD"),
        ("report_date", "2026-02-30", "YYYY-MM-DD"),
        ("customer", True, "text or a number"),
        ("customer", ["a"], "text or a number"),
        ("summary", None, "text or a number"),
    ],
)
def test_each_type_takes_only_its_own_values(job: Job, name: str, value: object, said: str) -> None:
    """A check box a bool, a date an ISO date, text a string or a number; anything else names the field."""
    mt.make_controls_docx(job.path("form.docx"))
    error = job.fill("form.docx", {name: value})["error"]
    assert error.startswith(f"invalid_value: {name} ")
    assert said in error


def test_a_number_in_a_text_control_is_written_as_python_writes_it(job: Job) -> None:
    """``str()`` of the number."""
    mt.make_controls_docx(job.path("form.docx"))
    job.fill("form.docx", {"customer": 1250.5})
    assert "Customer: 1250.5" in body_text(job.output("form-filled.docx"))


def test_a_name_used_twice_is_filled_everywhere(job: Job) -> None:
    """The header's control and the body's take the same value."""
    mt.make_controls_docx(job.path("form.docx"))
    job.fill("form.docx", {"customer": "Invented Harbour Ltd"})
    out = job.output("form-filled.docx")
    assert "Customer: Invented Harbour Ltd" in body_text(out)
    assert "".join(t.text or "" for t in part(out, "word/header1.xml").iter(f"{W}t")).endswith("Invented Harbour Ltd")


@pytest.mark.parametrize(
    ("pattern", "shown", "warned"),
    [
        ("d/M/yy", "7/3/26", False),
        ("yyyy-MM-dd", "2026-03-07", False),
        ("dd.MM.yyyy", "07.03.2026", False),
        ("dddd, MMMM d, yyyy", "2026-03-07", True),
    ],
)
def test_a_date_follows_its_controls_format_or_shows_the_iso_date(
    job: Job, pattern: str, shown: str, warned: bool
) -> None:
    """The tokens d, dd, M, MM, yy and yyyy are written; any other format shows the ISO date, with a warning."""
    mt.make_controls_docx(job.path("form.docx"), date_pattern=pattern)
    answer = job.fill("form.docx", {"report_date": "2026-03-07"})
    assert f"Report date: {shown}" in body_text(job.output("form-filled.docx"))
    assert bool([w for w in answer["warnings"] if "report_date" in w and "format" in w]) is warned


def test_a_bound_control_loses_its_binding_with_a_warning(job: Job) -> None:
    """Word would show the bound value instead of the filled one."""
    mt.make_controls_docx(job.path("form.docx"), bound=True)
    answer = job.fill("form.docx", {"report_date": "2026-03-07"})
    (date,) = sdt(part(job.output("form-filled.docx"), "word/document.xml"), "report_date")
    assert date.find(f"{W}sdtPr/{W}dataBinding") is None
    assert any("report_date" in warning and "bound" in warning for warning in answer["warnings"])


def test_characters_a_document_cannot_hold_are_replaced_with_a_warning(job: Job) -> None:
    """A control character becomes U+FFFD, and the warning names the field."""
    mt.make_controls_docx(job.path("form.docx"))
    answer = job.fill("form.docx", {"customer": "Invented\x07Harbour"})
    assert "Customer: Invented�Harbour" in body_text(job.output("form-filled.docx"))
    assert [w for w in answer["warnings"] if w.startswith("customer:")]


def test_a_newline_in_plain_text_is_a_line_break(job: Job) -> None:
    """A plain-text control stays one paragraph, its lines split by breaks."""
    mt.make_controls_docx(job.path("form.docx"))
    job.fill("form.docx", {"customer": "Invented Harbour\nNorth Quay"})
    (control,) = sdt(part(job.output("form-filled.docx"), "word/document.xml"), "customer")
    assert control.find(f".//{W}p") is None
    assert len(control.findall(f".//{W}br")) == 1
    assert [t.text for t in control.iter(f"{W}t")] == ["Invented Harbour", "North Quay"]


def test_values_for_names_that_are_no_field_are_warned_and_unfilled_fields_listed(job: Job) -> None:
    """The model hears which values went nowhere and which fields stayed empty."""
    mt.make_controls_docx(job.path("form.docx"))
    answer = job.fill("form.docx", {"customer": "Invented Harbour Ltd", "colour": "blue"})
    assert answer["filled"] == ["customer"]
    assert answer["unfilled"] == ["report_date", "status", "approved", "summary"]
    assert any("colour" in warning for warning in answer["warnings"])


def test_without_sources_no_appendix_is_added(job: Job) -> None:
    """``include_sources`` false leaves the document's own tables only."""
    mt.make_controls_docx(job.path("form.docx"))
    job.fill("form.docx", {"customer": "Invented Harbour Ltd"}, sources=False)
    assert len(Document(str(job.output("form-filled.docx"))).tables) == 1


def test_a_control_that_holds_only_a_bookmark_is_filled_where_it_sits(job: Job) -> None:
    """Among paragraphs, an empty-looking control gets a paragraph, never a run where paragraphs go."""
    empty = (
        '<w:sdt><w:sdtPr><w:tag w:val="note"/><w:id w:val="9"/></w:sdtPr><w:sdtContent>'
        '<w:bookmarkStart w:id="0" w:name="mark"/><w:bookmarkEnd w:id="0"/></w:sdtContent></w:sdt>'
    )
    mt.make_controls_body(job.path("note.docx"), [], blocks=(empty,))
    job.fill("note.docx", {"note": "Written by the invented office"})
    (control,) = sdt(part(job.output("note-filled.docx"), "word/document.xml"), "note")
    (paragraph,) = control.find(f"{W}sdtContent")
    assert paragraph.tag == f"{W}p"
    assert "".join(t.text or "" for t in paragraph.iter(f"{W}t")) == "Written by the invented office"


def test_a_template_without_fields_cannot_be_filled(job: Job) -> None:
    """Nothing to fill is a refusal, not a copy of the template."""
    mk.make_docx(job.path("plain.docx"))
    error = job.fill("plain.docx", {"customer": "x"})["error"]
    assert error.startswith("unsupported_template: the template has no content controls")
    assert list(job.ctx.out_dir.iterdir()) == []


def test_dotx_becomes_docx(job: Job) -> None:
    """The output is a document: its main part's content type says so, and python-docx opens it."""
    mt.as_dotx(mt.make_controls_docx(job.path("form.docx")), job.path("form.dotx"))
    answer = job.fill("form.dotx", {"customer": "Invented Harbour Ltd"})
    assert "error" not in answer
    out = job.output("form-filled.docx")
    types = part(out, "[Content_Types].xml")
    overrides = {
        o.get("PartName"): o.get("ContentType")
        for o in types.iter("{http://schemas.openxmlformats.org/package/2006/content-types}Override")
    }
    assert overrides["/word/document.xml"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
    )
    assert Document(str(out)).paragraphs[0].text == "Quarterly report"
    assert "Customer: Invented Harbour Ltd" in body_text(out)


def test_a_dotx_can_be_inspected(job: Job) -> None:
    """A template is read as readily as a document."""
    mt.as_dotx(mt.make_controls_docx(job.path("form.docx")), job.path("form.dotx"))
    assert job.inspect("form.dotx")["template_kind"] == "content_controls"


# --- fill_template: legacy form fields ---------------------------------------------------------------------------


def test_fill_legacy_form_fields(job: Job) -> None:
    """The text field's result is replaced; the check box and the drop-down are set in their form data."""
    mt.make_form_fields_docx(job.path("legacy.docx"))
    answer = job.fill(
        "legacy.docx", {"name": "Invented Person", "agreed": True, "region": "south", "signed_on": "2026-03-07"}
    )
    assert (answer["filled"], answer["warnings"]) == (["name", "agreed", "region", "signed_on"], [])
    out = job.output("legacy-filled.docx")
    texts = body_text(out)
    assert "Name: Invented Person" in texts
    assert "Signed on: 07.03.2026" in texts
    document = part(out, "word/document.xml")
    data = {f.find(f"{W}name").get(f"{W}val"): f for f in document.iter(f"{W}ffData")}
    assert data["agreed"].find(f"{W}checkBox/{W}checked").get(f"{W}val") == "1"
    assert data["region"].find(f"{W}ddList/{W}result").get(f"{W}val") == "1"


def test_a_legacy_text_field_takes_no_more_than_its_maximum(job: Job) -> None:
    """Its maximum length is part of its type."""
    mt.make_form_fields_docx(job.path("legacy.docx"))
    error = job.fill("legacy.docx", {"name": "x" * 41})["error"]
    assert error.startswith("invalid_value: name ")
    assert "40" in error


# --- fill_template: {{ }} placeholders in a document -------------------------------------------------------------


def test_jinja_loop_fills_rows(job: Job) -> None:
    """A row per item, the name in body and header, the condition shown; a name without a value is unfilled."""
    mt.make_jinja_docx(job.path("letter.docx"))
    rows = [{"item": "Dredging", "total": 118}, {"item": "Piers", "total": 351}]
    answer = job.fill("letter.docx", {"customer": "Invented Harbour Ltd", "rows": rows})
    assert answer["filled"] == ["customer", "rows"]
    assert answer["unfilled"] == ["approved"]
    out = Document(str(job.output("letter-filled.docx")))
    assert [[c.text for c in row.cells] for row in out.tables[0].rows] == [
        ["Item", "Total"],
        ["Dredging", "118"],
        ["Piers", "351"],
    ]
    assert out.paragraphs[0].text == "Customer: Invented Harbour Ltd"
    assert "Approved" not in " ".join(p.text for p in out.paragraphs)
    assert out.sections[0].header.paragraphs[0].text == "Report for Invented Harbour Ltd"
    assert sources_table(job.output("letter-filled.docx"))[2] == [
        "rows",
        '[{"item":"Dredging","total":118},{"item":"Piers","total":351}]',
        "assistant",
        "",
    ]


def test_a_value_is_written_as_text_never_as_markup(job: Job) -> None:
    """``<``, ``&`` and ``</w:t>`` in a value appear literally; escaping keeps them out of the WordprocessingML."""
    mt.make_jinja_docx(job.path("letter.docx"))
    value = 'a<b & c</w:t></w:r><w:r><w:t>"injected"'
    job.fill("letter.docx", {"customer": value})
    assert Document(str(job.output("letter-filled.docx"))).paragraphs[0].text == f"Customer: {value}"


def test_jinja_that_does_not_compile_names_the_line(job: Job) -> None:
    """A 422, never a 500: the error says where and what."""
    mt.make_jinja_docx(job.path("broken.docx"), extra="{% if approved %}never closed")
    for answer in (job.inspect("broken.docx"), job.fill("broken.docx", {"customer": "x"})):
        error = answer["error"]
        assert error.startswith("invalid_template: ")
        assert re.search(r"line \d+", error)
        assert "body" in error


def test_the_sandbox_keeps_a_template_from_python(job: Job) -> None:
    """Python's internals are not there for a template: one shows nothing, calling one is refused."""
    mt.make_jinja_docx(job.path("peek.docx"), extra="internals:{{ customer.__class__.__mro__ }}")
    assert "error" not in job.fill("peek.docx", {"customer": "x"})
    assert "internals:" in [p.text for p in Document(str(job.output("peek-filled.docx"))).paragraphs]
    mt.make_jinja_docx(job.path("call.docx"), extra="{{ customer.__class__.__base__.__subclasses__() }}")
    assert job.fill("call.docx", {"customer": "x"})["error"].startswith("invalid_template: ")


# --- fill_template: {{ }} placeholders in a workbook --------------------------------------------------------------


def test_xlsx_table_anchor_writes_rows_and_refuses_overwrite(job: Job) -> None:
    """Rows go down from the anchor with their types; a non-empty cell in their way is a 422 naming the field."""
    mt.make_placeholder_xlsx(job.path("summary.xlsx"))
    rows = [["Dredging", 118], ["Piers", 351.5], ["Total", None]]
    answer = job.fill("summary.xlsx", {"customer": "Invented Harbour Ltd", "total": 469.5, "rows": rows})
    assert answer == {"filled": ["customer", "total", "rows"], "unfilled": [], "warnings": []}
    sheet = openpyxl.load_workbook(job.output("summary-filled.xlsx"))["Summary"]
    assert [sheet[f"A{n}"].value for n in range(6, 9)] == ["Dredging", "Piers", "Total"]
    assert [sheet[f"B{n}"].value for n in range(6, 9)] == [118, 351.5, None]
    assert (sheet["B1"].value, sheet["B2"].value) == ("Invented Harbour Ltd", 469.5)
    assert sheet["A3"].value == "Checked for Invented Harbour Ltd by the invented office"

    mt.make_placeholder_xlsx(job.path("blocked.xlsx"), blocker="B7")
    error = job.fill("blocked.xlsx", {"rows": rows})["error"]
    assert error.startswith("invalid_value: rows ")
    assert "B7" in error
    assert not job.output("blocked-filled.xlsx").exists()


def test_values_are_data_never_formulas(job: Job) -> None:
    """A string starting with ``=``, ``+``, ``-`` or ``@`` is text, in a field, a table and the Sources sheet."""
    mt.make_placeholder_xlsx(job.path("summary.xlsx"))
    job.fill("summary.xlsx", {"customer": "=1+1", "total": "@SUM(A1)", "rows": [["+1", "-2"]]})
    book = openpyxl.load_workbook(job.output("summary-filled.xlsx"))
    sheet = book["Summary"]
    for cell in ("B1", "B2", "A6", "B6"):
        assert sheet[cell].data_type == "s", cell
    assert (sheet["B1"].value, sheet["B2"].value, sheet["A6"].value, sheet["B6"].value) == (
        "=1+1",
        "@SUM(A1)",
        "+1",
        "-2",
    )
    sources = book["Sources"]
    assert sources["B2"].value == "=1+1"
    assert sources["B2"].data_type == "s"


def test_a_workbook_field_given_no_value_is_emptied(job: Job) -> None:
    """As a document's placeholder renders empty: no ``{{ }}`` is left in the filled workbook."""
    mt.make_placeholder_xlsx(job.path("summary.xlsx"))
    answer = job.fill("summary.xlsx", {"total": 3})
    assert answer["unfilled"] == ["customer", "rows"]
    sheet = openpyxl.load_workbook(job.output("summary-filled.xlsx"))["Summary"]
    assert (sheet["B1"].value, sheet["A6"].value) == (None, None)
    assert sheet["A3"].value == "Checked for  by the invented office"


def test_a_placeholder_alone_keeps_its_values_type(job: Job) -> None:
    """A number stays a number and a bool a bool; inside longer text the value is written as text."""
    mt.make_placeholder_xlsx(job.path("summary.xlsx"))
    job.fill("summary.xlsx", {"customer": True, "total": 3})
    sheet = openpyxl.load_workbook(job.output("summary-filled.xlsx"))["Summary"]
    assert (sheet["B1"].value, sheet["B2"].value) == (True, 3)
    assert sheet["A3"].value == "Checked for TRUE by the invented office"


@pytest.mark.parametrize(
    ("name", "value", "said"),
    [
        ("rows", [[["deep"]]], "rows of plain values"),
        ("rows", "Dredging", "rows of plain values"),
        ("rows", [{"item": "x"}], "rows of plain values"),
        ("total", [1, 2], "a single value"),
    ],
)
def test_a_workbook_field_refuses_a_value_of_the_wrong_shape(job: Job, name: str, value: object, said: str) -> None:
    """A table takes a list of rows of scalars; a placeholder one value."""
    mt.make_placeholder_xlsx(job.path("summary.xlsx"))
    error = job.fill("summary.xlsx", {name: value})["error"]
    assert error.startswith(f"invalid_value: {name} ")
    assert said in error


def test_the_sources_sheet_never_replaces_one_of_the_template(job: Job) -> None:
    """A template that has a sheet called Sources keeps it; the appendix is Sources (2)."""
    path = mt.make_placeholder_xlsx(job.path("summary.xlsx"))
    book = openpyxl.load_workbook(path)
    book.create_sheet("Sources")["A1"] = "the template's own"
    book.save(path)
    job.fill("summary.xlsx", {"total": 1})
    filled = openpyxl.load_workbook(job.output("summary-filled.xlsx"))
    assert filled.sheetnames == ["Summary", "Sources", "Sources (2)"]
    assert filled["Sources"]["A1"].value == "the template's own"
    assert [c.value for c in filled["Sources (2)"][1]] == SOURCES_HEADER


def test_what_openpyxl_does_not_keep_is_warned(job: Job) -> None:
    """A chart, picture or other drawing in the template may not survive the fill: the answer says so."""
    path = mt.make_placeholder_xlsx(job.path("summary.xlsx"))
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("xl/drawings/drawing1.xml", "<xdr:wsDr xmlns:xdr='urn:x'/>")
    answer = job.fill("summary.xlsx", {"total": 1})
    assert any("drawings" in warning for warning in answer["warnings"])


def _workbook(path: Path, cells: dict[str, str]) -> Path:
    """Write a workbook whose sheet ``S`` holds ``cells``."""
    book = openpyxl.Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "S"
    for where, text in cells.items():
        sheet[where] = text
    book.save(path)
    return path


def test_tables_whose_cells_cross_are_refused(job: Job) -> None:
    """Two tables that would both write a cell: a 422 naming the later one and the cell, and no file.

    ``down`` runs from B1 over B3, ``across`` from A3 over B3; the second used to overwrite the first without a word.
    """
    _workbook(job.path("cross.xlsx"), {"B1": "{{ table:down }}", "A3": "{{ table:across }}"})
    values = {"down": [["d1"], ["d2"], ["d3"]], "across": [["a1", "a2", "a3"]]}
    error = job.fill("cross.xlsx", values)["error"]
    assert error.startswith("invalid_value: across ")
    assert "B3" in error
    assert "down" in error
    assert list(job.ctx.out_dir.iterdir()) == []
    assert "error" not in job.fill("cross.xlsx", {"down": [["d1"], ["d2"]], "across": [["a1", "a2", "a3"]]})


# --- what a template or a value may hold, and never a 500 ---------------------------------------------------------


@pytest.mark.parametrize("code", ["0001", "D800", "FFFE"])
def test_a_check_box_state_xml_cannot_hold_shows_the_usual_glyph(job: Job, code: str) -> None:
    """A checked state that names a control character, a surrogate or a non-character: the box shows the usual one."""
    box = mt.checkbox_control("ok").replace('w14:checkedState w14:val="2612"', f'w14:checkedState w14:val="{code}"')
    mt.make_controls_body(job.path("box.docx"), [box])
    assert "error" not in job.fill("box.docx", {"ok": True})
    (control,) = sdt(part(job.output("box-filled.docx"), "word/document.xml"), "ok")
    assert [t.text for t in control.iter(f"{W}t")] == ["☒"]


def test_jinja_nested_deeper_than_the_parser_goes_is_an_invalid_template(job: Job) -> None:
    """Code nested three thousand deep is a 422 that names the template, never a RecursionError."""
    mt.make_jinja_docx(job.path("deep.docx"), extra="{{ " + "(" * 3000 + "customer" + ")" * 3000 + " }}")
    for answer in (job.inspect("deep.docx"), job.fill("deep.docx", {"customer": "x"})):
        assert answer["error"].startswith("invalid_template: "), answer


def test_a_field_whose_name_is_too_long_to_be_given_a_value_is_left_out(job: Job) -> None:
    """A value's name may have 255 characters: a longer field is not listed, its neighbour is, and the hint says so."""
    _workbook(job.path("long.xlsx"), {"A1": "{{ " + "n" * 320 + " }}", "A2": "{{ short }}"})
    answer = job.inspect("long.xlsx")
    assert [f["name"] for f in answer["fields"]] == ["short"]
    assert "1 field has a name longer than 255 characters" in answer["hint"]
    filled = job.fill("long.xlsx", {"short": 1})
    assert (filled["filled"], filled["unfilled"]) == (["short"], [])
    assert any("longer than 255 characters" in warning for warning in filled["warnings"])


def test_long_warnings_are_cut_and_the_filled_file_stays(job: Job) -> None:
    """A date format of 2100 characters and eight unknown names of 255: every warning fits the api's bound."""
    pattern = "dd.MM.yyyy " + "q" * 2100
    mt.make_controls_body(job.path("date.docx"), [mt.date_control("when", pattern)])
    answer = job.fill("date.docx", {"when": "2026-03-07"})
    assert answer["warnings"]
    assert all(len(warning) <= 2000 for warning in answer["warnings"])
    assert job.output("date-filled.docx").exists()
    mt.make_controls_body(job.path("one.docx"), [mt.text_control("customer")])
    unknown = {f"{n}" + "u" * 254: "y" for n in range(8)}
    answer = job.fill("one.docx", {"customer": "x", **unknown})
    (warning,) = answer["warnings"]
    assert len(warning) <= 2000
    assert "more" in warning
    assert job.output("one-filled.docx").exists()


# --- sources ------------------------------------------------------------------------------------------------------


def test_assistant_and_user_sources_are_labelled(job: Job) -> None:
    """The appendix's kind column says who stated each value; a user's note is its reference."""
    mt.make_controls_docx(job.path("form.docx"))
    mk.make_pdf(job.path("q.pdf"), ["page one", "page two"])
    mk.make_xlsx(job.path("quarterly-figures.xlsx"))
    job.fill(
        "form.docx",
        {
            "customer": {"value": "Invented Harbour Ltd", "source": {"kind": "user", "note": "said in the chat"}},
            "summary": {"value": "Written by the model", "source": {"kind": "assistant"}},
            "status": {"value": "open", "source": {"kind": "job", "job_id": "f" * 32}},
            "report_date": {
                "value": "2026-03-07",
                "source": {"kind": "file", "path": "q.pdf", "virtual": f"/workspace/{CHAT}/q.pdf", "page": 2},
            },
            "approved": {
                "value": True,
                "source": {
                    "kind": "file",
                    "path": "quarterly-figures.xlsx",
                    "virtual": f"/workspace/{CHAT}/quarterly-figures.xlsx",
                    "sheet": "Sheet1",
                    "cell": "B2",
                },
            },
        },
    )
    assert sources_table(job.output("form-filled.docx"))[1:] == [
        ["customer", "Invented Harbour Ltd", "user", "said in the chat"],
        ["summary", "Written by the model", "assistant", ""],
        ["status", "open", "job", "f" * 32],
        ["report_date", "2026-03-07", "file", f"/workspace/{CHAT}/q.pdf, page 2"],
        ["approved", "true", "file", f"/workspace/{CHAT}/quarterly-figures.xlsx, Sheet1!B2"],
    ]


def _file_source(job: Job, name: str, **where: object) -> dict[str, Any]:
    """A value whose source is a file of the job, with a page, sheet or cell."""
    return {"value": "x", "source": {"kind": "file", "path": name, "virtual": f"/workspace/{CHAT}/{name}", **where}}


@pytest.mark.parametrize(
    ("name", "where", "said"),
    [
        ("q.pdf", {"page": 99}, "page 99"),
        ("q.pdf", {"page": 0}, "page 0"),
        ("q.pdf", {"sheet": "Sheet1"}, "XLSX and ODS"),
        ("quarterly-figures.xlsx", {"page": 1}, "PDF pages and PPTX slides"),
        ("quarterly-figures.xlsx", {"sheet": "Missing"}, "no sheet"),
        ("quarterly-figures.xlsx", {"cell": "B2"}, "needs its sheet"),
        ("quarterly-figures.xlsx", {"sheet": "Sheet1", "cell": "Z99"}, "outside"),
        ("quarterly-figures.xlsx", {"sheet": "Sheet1", "cell": "not a cell"}, "A1"),
        ("notes.txt", {"page": 1}, "PDF pages and PPTX slides"),
    ],
)
def test_a_reference_must_exist_in_the_copied_file(job: Job, name: str, where: dict[str, object], said: str) -> None:
    """Page, sheet and cell are checked in the job's copy; a failed check names the field."""
    mt.make_controls_docx(job.path("form.docx"))
    mk.make_pdf(job.path("q.pdf"), ["page one", "page two"])
    mk.make_xlsx(job.path("quarterly-figures.xlsx"))
    job.path("notes.txt").write_text("invented notes")
    error = job.fill("form.docx", {"customer": _file_source(job, name, **where)})["error"]
    assert error.startswith("invalid_source: customer: "), error
    assert said in error
    assert list(job.ctx.out_dir.iterdir()) == []


def test_slides_count_as_pages_and_a_sheet_name_is_found_whatever_its_case(job: Job) -> None:
    """``page`` 2 of a two-slide deck, ``sheet1`` of a workbook and a cell of an ODS pass.

    The reference writes the sheet as the file names it.
    """
    mt.make_controls_docx(job.path("form.docx"))
    mk.make_pptx(job.path("deck.pptx"))
    mk.make_xlsx(job.path("quarterly-figures.xlsx"))
    mk.make_ods(job.path("depths.ods"))
    answer = job.fill(
        "form.docx",
        {
            "customer": _file_source(job, "deck.pptx", page=2),
            "summary": _file_source(job, "quarterly-figures.xlsx", sheet="sheet1", cell="a1"),
            "status": {**_file_source(job, "depths.ods", sheet="Depths", cell="B2"), "value": "open"},
        },
    )
    assert "error" not in answer, answer
    assert [row[3] for row in sources_table(job.output("form-filled.docx"))[1:]] == [
        f"/workspace/{CHAT}/deck.pptx, page 2",
        f"/workspace/{CHAT}/quarterly-figures.xlsx, Sheet1!A1",
        f"/workspace/{CHAT}/depths.ods, Depths!B2",
    ]
    error = job.fill("form.docx", {"customer": _file_source(job, "depths.ods", sheet="Depths", cell="C3")})["error"]
    assert error.startswith("invalid_source: customer: C3 lies outside the cells of sheet 'Depths'")
    assert "from A1 to B2" in error
