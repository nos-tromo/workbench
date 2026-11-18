"""Slide masters in a folder laid out as a job's: their layouts, ``inspect_template`` and ``write_presentation``.

A master's layouts, places and capacity are read, ``inspect_template`` lists them, and ``write_presentation`` builds
decks in it. Every master is built at test time from python-pptx's default presentation
(``tests/fixtures/make_templates.py``), all its text invented.
"""

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from lxml import etree
from pptx import Presentation
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.oxml import parse_xml
from pptx.oxml.ns import nsdecls, qn

from tests.fixtures import make_fixtures as mk
from tests.fixtures import make_templates as mt
from workbench import decks
from workbench.executor.presentations import deck, master, text
from workbench.executor.runners import RUNNERS, JobContext
from workbench.executor.runners.templates import LISTED
from workbench.executor.templates import Refusal

JOB_ID = "0123456789abcdef0123456789abcdef"
CHAT = "chat-1"

#: EMU in a hundredth of a point, the unit of ``sz``; and the insets python-pptx's master gives its placeholders.
EMU_PER_SZ = 127
SIDE_INSET = 91440
EDGE_INSET = 45720

COMPARISON = 5
TITLE_AND_CONTENT = 2
PICTURE_WITH_CAPTION = 9


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

    def with_max_chars(self, max_chars: int) -> "Job":
        """The same folder with another result limit."""
        return Job(JobContext(self.ctx.work_dir, self.ctx.out_dir, CHAT, max_chars, self.ctx.file_mb))

    def write(
        self, slides: list[dict[str, Any]], *, path: str = "master.pptx", name: str = "deck.pptx", **extra: Any
    ) -> dict[str, Any]:
        """Run ``write_presentation`` on a master of the job."""
        args = {"path": path, "name": name, "slides": slides, "budget_s": 30, **extra}
        return RUNNERS["write_presentation"](args, self.ctx)

    def deck(self, name: str = "deck.pptx") -> Any:
        """Open a deck the job wrote."""
        return Presentation(str(self.ctx.out_dir / name))


@pytest.fixture
def job(tmp_path: Path) -> Job:
    """An empty job folder for ``chat-1``."""
    work = tmp_path / "data" / "jobs" / JOB_ID / "workspace" / CHAT
    (work / "out").mkdir(parents=True)
    return Job(JobContext(work_dir=work, out_dir=work / "out", chat_id=CHAT, max_chars=16 * 1024, file_mb=50))


def expected(shape: Any, sz: int) -> tuple[float, float]:
    """The capacity formula, worked out here: lines and characters of a placeholder at a size of ``sz``."""
    size = sz * EMU_PER_SZ
    return (shape.height - 2 * EDGE_INSET) / (1.2 * size), (shape.width - 2 * SIDE_INSET) / (0.5 * size)


def near(found: tuple[int, int], wanted: tuple[float, float]) -> bool:
    """Whether a capacity is within one line and one character of the formula's."""
    return abs(found[0] - wanted[0]) <= 1 and abs(found[1] - wanted[1]) <= 1


def level_one(element: Any, path: str) -> Any:
    """The ``a:defRPr`` of level 1 below ``path`` (tags given as ``p:a/a:b``) of an element."""
    return element.find("/".join(qn(tag) for tag in [*path.split("/"), "a:lvl1pPr", "a:defRPr"]))


def set_list_size(placeholder: Any, sz: int) -> None:
    """Give a placeholder's own list style a level-1 font size."""
    styles = placeholder._element.find(f"{qn('p:txBody')}/{qn('a:lstStyle')}")
    styles.append(parse_xml(f'<a:lvl1pPr {nsdecls("a")}><a:defRPr sz="{sz}"/></a:lvl1pPr>'))


# --- inspect_template ---------------------------------------------------------------------------------------------


def test_inspect_lists_the_layouts_of_a_master(job: Job) -> None:
    """Every layout by number, its title and subtitle as flags, its places by role, its footer; and the slides."""
    mt.make_master(job.path("master.pptx"))
    answer = job.inspect("master.pptx")
    assert answer["template_kind"] == "slide_master"
    assert len(answer["layouts"]) == 11
    assert (answer["slides"], answer["total"], answer["truncated"]) == (2, 11, False)
    assert [layout["number"] for layout in answer["layouts"]] == list(range(1, 12))
    first, second = answer["layouts"][:2]
    assert first == {"number": 1, "name": "Title Slide", "title": True, "subtitle": True, "places": [], "footer": True}
    assert second["name"] == "Titel und Inhalt"
    assert (second["title"], second["subtitle"], second["footer"]) == (True, False, True)
    assert [place["role"] for place in second["places"]] == ["content"]
    assert set(second["places"][0]) == {"role", "lines", "chars"}
    picture = answer["layouts"][PICTURE_WITH_CAPTION - 1]
    assert [place["role"] for place in picture["places"]] == ["picture", "text"]
    blank = answer["layouts"][6]
    assert (blank["name"], blank["title"], blank["places"], blank["footer"]) == ("Blank", False, [], True)


def test_the_inspection_names_no_path(job: Job) -> None:
    """The answer holds the layouts' names, never a path of the job."""
    mt.make_master(job.path("master.pptx"))
    said = json.dumps(job.inspect("master.pptx"))
    assert "/workspace" not in said
    assert str(job.ctx.work_dir) not in said
    assert "master.pptx" not in said


def test_places_are_in_reading_order(job: Job) -> None:
    """Comparison's two headings sit above its two contents: top, then left, not placeholder-number order."""
    mt.make_master(job.path("master.pptx"))
    roles = [place["role"] for place in job.inspect("master.pptx")["layouts"][COMPARISON - 1]["places"]]
    assert roles == ["text", "text", "content", "content"]
    comparison = master.layouts(master.open_master(job.path("master.pptx"), job.ctx))[COMPARISON - 1]
    assert comparison.number == COMPARISON
    assert comparison.name == comparison.layout.name == "Comparison"
    assert [place.idx for place in comparison.places] == [1, 3, 2, 4]
    assert [place.shape.placeholder_format.idx for place in comparison.places] == [1, 3, 2, 4]
    tops = [(place.shape.top, place.shape.left) for place in comparison.places]
    assert tops == sorted(tops)


def test_layouts_sharing_a_name_are_both_listed(job: Job) -> None:
    """Two layouts of one name are listed apart, by number; a master without slides has none."""
    mt.make_master(job.path("master.pptx"), duplicate_name=True, examples=0)
    answer = job.inspect("master.pptx")
    assert [layout["name"] for layout in answer["layouts"][2:4]] == ["Zwei Inhalte", "Zwei Inhalte"]
    assert [layout["number"] for layout in answer["layouts"][2:4]] == [3, 4]
    assert answer["slides"] == 0


def test_capacity_follows_the_inherited_font_size(job: Job) -> None:
    """The nearest size wins: the layout's list style, the master's body placeholder, its body style; else 18 pt."""
    prs = master.open_master(mt.make_master(job.path("master.pptx")), job.ctx)
    slide_master = prs.slide_master
    body_style = level_one(slide_master._element, "p:txStyles/p:bodyStyle")
    body_size = int(body_style.get("sz"))
    layout = master.layouts(prs)[TITLE_AND_CONTENT - 1]
    place = layout.places[0]
    assert near((place.lines, place.chars), expected(place.shape, body_size))

    set_list_size(slide_master.placeholders.get(PP_PLACEHOLDER.BODY), 2400)
    place = master.layouts(prs)[TITLE_AND_CONTENT - 1].places[0]
    assert near((place.lines, place.chars), expected(place.shape, 2400))

    set_list_size(place.shape, 2000)
    place = master.layouts(prs)[TITLE_AND_CONTENT - 1].places[0]
    assert near((place.lines, place.chars), expected(place.shape, 2000))
    assert not near((place.lines, place.chars), expected(place.shape, body_size))

    for element in (place.shape._element, slide_master.placeholders.get(PP_PLACEHOLDER.BODY)._element):
        level = element.find(f"{qn('p:txBody')}/{qn('a:lstStyle')}/{qn('a:lvl1pPr')}")
        level.getparent().remove(level)
    del body_style.attrib["sz"]
    assert near(master.capacity(place.shape, layout.layout, slide_master), expected(place.shape, 1800))


def test_a_title_takes_the_title_style(job: Job) -> None:
    """A title placeholder is measured in the master's title style, not its body style."""
    prs = master.open_master(mt.make_master(job.path("master.pptx")), job.ctx)
    slide_master = prs.slide_master
    title_size = int(level_one(slide_master._element, "p:txStyles/p:titleStyle").get("sz"))
    layout = master.layouts(prs)[TITLE_AND_CONTENT - 1]
    title = layout.layout.placeholders.get(idx=0)
    assert near(master.capacity(title, layout.layout, slide_master), expected(title, title_size))


def test_the_insets_of_the_placeholder_count(job: Job) -> None:
    """Insets the layout's placeholder sets take their room from the line."""
    prs = master.open_master(mt.make_master(job.path("master.pptx")), job.ctx)
    layout = master.layouts(prs)[TITLE_AND_CONTENT - 1]
    shape = layout.places[0].shape
    before = layout.places[0].chars
    body = shape._element.find(f"{qn('p:txBody')}/{qn('a:bodyPr')}")
    body.set("lIns", str(SIDE_INSET + 914400))
    body.set("rIns", str(SIDE_INSET + 914400))
    _, chars = master.capacity(shape, layout.layout, prs.slide_master)
    size = int(level_one(prs.slide_master._element, "p:txStyles/p:bodyStyle").get("sz")) * EMU_PER_SZ
    assert abs(before - chars - 2 * 914400 / (0.5 * size)) <= 1


def test_a_slides_placeholder_is_measured_by_its_layouts(job: Job) -> None:
    """A slide's placeholder takes the list style of its layout's placeholder of the same idx."""
    prs = master.open_master(mt.make_master(job.path("master.pptx")), job.ctx)
    layout = master.layouts(prs)[TITLE_AND_CONTENT - 1]
    place = layout.places[0]
    on_slide = prs.slides[1].placeholders[place.idx]
    assert master.capacity(on_slide, layout.layout, prs.slide_master) == (place.lines, place.chars)
    set_list_size(place.shape, 2000)
    assert near(master.capacity(on_slide, layout.layout, prs.slide_master), expected(place.shape, 2000))


def test_a_size_that_is_not_one_is_passed_over(job: Job) -> None:
    """A size of 0 or of no number in the list style is not a size: the next one down the line is used."""
    prs = master.open_master(mt.make_master(job.path("master.pptx")), job.ctx)
    layout = master.layouts(prs)[TITLE_AND_CONTENT - 1]
    place = layout.places[0]
    for bad in ("0", "large"):
        styles = place.shape._element.find(f"{qn('p:txBody')}/{qn('a:lstStyle')}")
        for level in list(styles):
            styles.remove(level)
        styles.append(parse_xml(f'<a:lvl1pPr {nsdecls("a")}><a:defRPr sz="{bad}"/></a:lvl1pPr>'))
        assert master.capacity(place.shape, layout.layout, prs.slide_master) == (place.lines, place.chars)


def test_a_potx_is_inspected_like_a_pptx(job: Job) -> None:
    """A ``.potx`` is the same master under the template content type: the same answer."""
    pptx = mt.make_master(job.path("master.pptx"))
    mt.as_potx(pptx, job.path("master.potx"))
    answer = job.inspect("master.potx")
    assert answer["template_kind"] == "slide_master"
    assert answer == job.inspect("master.pptx")


def test_a_macro_enabled_master_is_refused(job: Job, tmp_path: Path) -> None:
    """A macro-enabled presentation is no master; the refusal says what to save it as."""
    mt.as_macro_pptx(mt.make_master(tmp_path / "built.pptx"), job.path("macro.pptx"))
    with pytest.raises(Refusal) as caught:
        master.open_master(job.path("macro.pptx"), job.ctx)
    assert caught.value.code == "unsupported_template"
    error = job.inspect("macro.pptx")["error"]
    assert error.startswith("unsupported_template: ")
    assert "save it as .pptx or .potx" in error


def test_a_damaged_master_is_unreadable(job: Job) -> None:
    """A zip that is not a presentation is told as unreadable."""
    job.path("broken.pptx").write_bytes(b"PK\x03\x04 not really")
    assert job.inspect("broken.pptx")["error"].startswith("unreadable_file: ")


def test_a_long_layout_list_is_cut(job: Job) -> None:
    """The layouts that fit in three times the result limit, then ``truncated`` and how many there are in all."""
    mt.make_master(job.path("master.pptx"))
    small = job.with_max_chars(128)
    answer = small.inspect("master.pptx")
    assert (answer["truncated"], answer["total"]) == (True, 11)
    assert 0 < len(answer["layouts"]) < 11
    assert len(json.dumps(answer["layouts"])) <= 3 * 128
    assert answer["layouts"][0]["number"] == 1


def test_at_most_max_layouts_are_listed(job: Job, monkeypatch: pytest.MonkeyPatch) -> None:
    """No more than ``decks.MAX_LAYOUTS`` layouts, however much room there is."""
    monkeypatch.setattr(decks, "MAX_LAYOUTS", 3)
    mt.make_master(job.path("master.pptx"))
    answer = job.inspect("master.pptx")
    assert [layout["number"] for layout in answer["layouts"]] == [1, 2, 3]
    assert (answer["truncated"], answer["total"]) == (True, 11)


def test_a_long_layout_name_is_cut(job: Job) -> None:
    """A layout's name is cut to ``decks.MAX_LAYOUT_NAME`` characters in the answer, the cut marked."""
    prs = master.open_master(mt.make_master(job.path("master.pptx")), job.ctx)
    prs.slide_layouts[0].name = "Invented layout " + "x" * 200
    name = master.inspect(prs, 10_000)["layouts"][0]["name"]
    assert len(name) == decks.MAX_LAYOUT_NAME
    assert name.startswith("Invented layout ")
    assert name.endswith("…")


# --- fill_template ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["master.pptx", "master.potx"])
def test_fill_template_refuses_a_slide_master(job: Job, name: str) -> None:
    """A slide master is not filled: the refusal names the tool that builds a deck in it, and nothing is written."""
    pptx = mt.make_master(job.path("master.pptx"))
    if name.endswith(".potx"):
        mt.as_potx(pptx, job.path(name))
    args: dict[str, Any] = {
        "path": name,
        "values": {},
        "output_name": "filled.pptx",
        "include_sources": False,
        "budget_s": 30,
    }
    error = RUNNERS["fill_template"](args, job.ctx)["error"]
    assert error.startswith("unsupported_template: ")
    assert "write_presentation" in error
    assert list(job.ctx.out_dir.iterdir()) == []


# --- write_markdown -----------------------------------------------------------------------------------------------


def text_frame() -> Any:
    """The text frame of the body placeholder of a slide of python-pptx's default presentation."""
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[TITLE_AND_CONTENT - 1])
    body: Any = slide.placeholders[1]
    return body.text_frame


def properties(paragraph: Any) -> list[str]:
    """The tags, without namespace, of the children of a paragraph's ``a:pPr``."""
    pPr = paragraph._p.find(qn("a:pPr"))
    return [] if pPr is None else [child.tag.split("}")[1] for child in pPr]


def indent(paragraph: Any) -> tuple[str | None, str | None]:
    """The ``marL`` and ``indent`` of a paragraph's ``a:pPr``, ``None`` where it says none."""
    pPr = paragraph._p.find(qn("a:pPr"))
    return (None, None) if pPr is None else (pPr.get("marL"), pPr.get("indent"))


def test_bullets_keep_their_levels() -> None:
    """Nested bullets are levels 0, 1 and 2, and a deeper one stays at 2; a bullet takes the master's own."""
    frame = text_frame()
    written = text.write_markdown(frame, "- a\n  - b\n    - c\n      - d")
    assert [paragraph.level for paragraph in frame.paragraphs] == [0, 1, 2, 2]
    assert [paragraph.text for paragraph in frame.paragraphs] == ["a", "b", "c", "d"]
    assert [properties(paragraph) for paragraph in frame.paragraphs] == [[]] * 4
    assert written.lines == ((0, 1), (1, 1), (2, 1), (2, 1))
    assert written.replaced == 0


def test_a_plain_paragraph_has_no_bullet() -> None:
    """A paragraph that is not a list item says so with ``a:buNone``; the list item after it does not."""
    frame = text_frame()
    text.write_markdown(frame, "Intro\n\n- a")
    first, second = frame.paragraphs
    assert properties(first) == ["buNone"]
    assert properties(second) == []
    assert (first.text, second.text) == ("Intro", "a")
    assert indent(first) == ("0", "0")
    assert indent(second) == (None, None)


def test_paragraphs_outside_a_list_do_not_hang() -> None:
    """A heading, code line, quote and rule drop the master's hanging indent, as a plain paragraph does.

    The extra paragraph of a list item keeps its level's indent, as it is part of the item.
    """
    frame = text_frame()
    text.write_markdown(frame, "# h\n\n```\nc\n```\n\n> q\n\n---\n\n- a\n\n  more")
    assert [indent(paragraph) for paragraph in frame.paragraphs] == [("0", "0")] * 4 + [(None, None)] * 2
    assert properties(frame.paragraphs[-1]) == ["buNone"]


def test_a_numbered_list_counts_from_its_start() -> None:
    """A list that starts at 3 says so on each item; one that starts at 1 says no start."""
    frame = text_frame()
    text.write_markdown(frame, "3. x\n4. y")
    for paragraph in frame.paragraphs:
        numbering = paragraph._p.find(f"{qn('a:pPr')}/{qn('a:buAutoNum')}")
        assert numbering is not None
        assert dict(numbering.attrib) == {"type": "arabicPeriod", "startAt": "3"}
    frame = text_frame()
    text.write_markdown(frame, "1. x\n2. y")
    for paragraph in frame.paragraphs:
        numbering = paragraph._p.find(f"{qn('a:pPr')}/{qn('a:buAutoNum')}")
        assert dict(numbering.attrib) == {"type": "arabicPeriod"}


@pytest.mark.parametrize(
    ("markdown", "start"),
    [
        ("0. a\n1. b", None),
        ("1. a", None),
        ("2. a", "2"),
        ("32767. a", "32767"),
        ("32768. a", None),
        ("99999. a\n100000. b", None),
        ("999999999. a", None),
    ],
)
def test_a_start_outside_powerpoints_range_is_not_written(markdown: str, start: str | None) -> None:
    """``startAt`` is 1 to 32767; a list that starts elsewhere says no start, so the file stays valid."""
    frame = text_frame()
    text.write_markdown(frame, markdown)
    for paragraph in frame.paragraphs:
        numbering = paragraph._p.find(f"{qn('a:pPr')}/{qn('a:buAutoNum')}")
        assert numbering is not None
        assert numbering.get("startAt") == start
        assert numbering.get("type") == "arabicPeriod"


def test_a_list_nested_in_a_numbered_item_is_a_level_deeper() -> None:
    """A bullet under a numbered item is level 1 and keeps the master's bullet; the numbers go on after it."""
    frame = text_frame()
    text.write_markdown(frame, "1. a\n   - b\n2. c")
    assert [paragraph.level for paragraph in frame.paragraphs] == [0, 1, 0]
    assert [properties(paragraph) for paragraph in frame.paragraphs] == [["buAutoNum"], [], ["buAutoNum"]]


def test_a_second_paragraph_of_an_item_has_no_bullet() -> None:
    """The first paragraph of a list item takes its bullet and the next ones, at the same level, none."""
    frame = text_frame()
    text.write_markdown(frame, "- a\n\n  more\n- b")
    assert [properties(paragraph) for paragraph in frame.paragraphs] == [[], ["buNone"], []]
    assert [paragraph.text for paragraph in frame.paragraphs] == ["a", "more", "b"]


def runs(frame: Any) -> list[tuple[str, bool | None, bool | None]]:
    """Every run of a frame as its text, bold and italic."""
    return [(run.text, run.font.bold, run.font.italic) for paragraph in frame.paragraphs for run in paragraph.runs]


def test_bold_and_italic_are_runs() -> None:
    """Bold and italic text are runs of their own; the rest is a run with neither."""
    frame = text_frame()
    text.write_markdown(frame, "**Fix** it *now*")
    assert runs(frame) == [("Fix", True, None), (" it ", None, None), ("now", None, True)]


def test_bold_and_italic_nest() -> None:
    """Text in both is bold and italic."""
    frame = text_frame()
    text.write_markdown(frame, "***both*** and **bold *and italic***")
    assert runs(frame) == [
        ("both", True, True),
        (" and ", None, None),
        ("bold ", True, None),
        ("and italic", True, True),
    ]


def test_a_link_is_its_text_and_address() -> None:
    """A link is its text and its address in parentheses; a link that is its own address is not said twice."""
    frame = text_frame()
    text.write_markdown(frame, "[docs](https://example.invalid)")
    assert frame.paragraphs[0].text == "docs (https://example.invalid)"
    frame = text_frame()
    text.write_markdown(frame, "<https://example.invalid> and <me@example.invalid> and [me](mailto:me@example.invalid)")
    assert (
        frame.paragraphs[0].text == "https://example.invalid and me@example.invalid and me (mailto:me@example.invalid)"
    )


def test_a_heading_is_a_plain_bold_paragraph() -> None:
    """A heading of any depth is a paragraph without a bullet, bold, and the text after it is its own paragraph."""
    frame = text_frame()
    written = text.write_markdown(frame, "# Plan\n\n###### Small\n\nText")
    assert [properties(paragraph) for paragraph in frame.paragraphs] == [["buNone"]] * 3
    assert runs(frame) == [("Plan", True, None), ("Small", True, None), ("Text", None, None)]
    assert written.lines == ((0, 4), (0, 5), (0, 4))


def test_inline_code_and_images_are_text() -> None:
    """Inline code is its text, an image its alternative text, a soft line break a space."""
    frame = text_frame()
    text.write_markdown(frame, "use `make up`\nnow ![a chart](chart.png)")
    assert frame.paragraphs[0].text == "use make up now a chart"
    assert len(frame.paragraphs) == 1


def test_a_hard_break_is_a_line_break_that_counts_as_a_line() -> None:
    """A hard break stays in its paragraph as a line break, and each line it makes counts on its own."""
    frame = text_frame()
    written = text.write_markdown(frame, "one\\\ntwo three")
    assert len(frame.paragraphs) == 1
    assert frame.paragraphs[0].text == "one\x0btwo three"
    assert written.lines == ((0, 3), (0, 9))
    assert text.estimate_lines(written, 100) == 2


def test_code_is_its_lines_as_paragraphs() -> None:
    """A code block is one plain paragraph per line, blank lines kept; an empty one writes nothing."""
    frame = text_frame()
    written = text.write_markdown(frame, "```\nx = 1\n\n  y = 2\n```\n\n```\n```")
    assert [paragraph.text for paragraph in frame.paragraphs] == ["x = 1", "", "  y = 2"]
    assert [properties(paragraph) for paragraph in frame.paragraphs] == [["buNone"]] * 3
    assert written.lines == ((0, 5), (0, 0), (0, 7))


def test_a_quote_and_a_rule_are_plain_paragraphs() -> None:
    """A quote is its text, without its mark; a thematic break is a plain paragraph of dashes."""
    frame = text_frame()
    text.write_markdown(frame, "> quoted\n\n---\n\nafter")
    assert [paragraph.text for paragraph in frame.paragraphs] == ["quoted", text.RULE, "after"]
    assert [properties(paragraph) for paragraph in frame.paragraphs] == [["buNone"]] * 3


def test_a_table_is_its_text() -> None:
    """A table is not a table here (it goes to a table place): its lines are one paragraph of text."""
    frame = text_frame()
    text.write_markdown(frame, "| a | b |\n| - | - |\n| 1 | 2 |")
    assert len(frame.paragraphs) == 1
    assert frame.paragraphs[0].text == "| a | b | | - | - | | 1 | 2 |"


def test_html_is_text() -> None:
    """HTML is not interpreted: it is written as the text it is."""
    frame = text_frame()
    text.write_markdown(frame, "a <b>b</b>\n\n<div>c</div>")
    assert [paragraph.text for paragraph in frame.paragraphs] == ["a <b>b</b>", "<div>c</div>"]


def test_characters_xml_cannot_hold_are_replaced() -> None:
    """A control character becomes U+FFFD and is counted, in a paragraph, a link's text and a code block."""
    frame = text_frame()
    written = text.write_markdown(frame, "a\x01b")
    assert written.replaced == 1
    assert frame.paragraphs[0].text == "a\ufffdb"
    frame = text_frame()
    written = text.write_markdown(frame, "[x\x02](https://example.invalid)\n\n```\n\x04\x05\n```")
    assert written.replaced == 3
    assert [paragraph.text for paragraph in frame.paragraphs] == ["x\ufffd (https://example.invalid)", "\ufffd\ufffd"]
    assert written.lines == ((0, 28), (0, 2))


def test_the_frame_is_cleared_first() -> None:
    """What the frame held goes, its first paragraph's level and bullet too; a second write replaces the first."""
    frame = text_frame()
    text.write_markdown(frame, "- a\n  - b\n\nplain")
    text.write_markdown(frame, "- c")
    assert [paragraph.text for paragraph in frame.paragraphs] == ["c"]
    assert frame.paragraphs[0].level == 0
    assert properties(frame.paragraphs[0]) == []
    frame.paragraphs[0].text = "old"
    frame.add_paragraph().text = "older"
    assert text.write_markdown(frame, "").lines == ()
    assert [paragraph.text for paragraph in frame.paragraphs] == [""]


def test_the_first_paragraph_is_the_frames_own() -> None:
    """The frame's first paragraph is reused, not a new one added before it."""
    frame = text_frame()
    first = frame.paragraphs[0]._p
    text.write_markdown(frame, "a\n\nb")
    assert frame.paragraphs[0]._p is first
    assert len(frame.paragraphs) == 2


def test_the_lines_are_the_levels_and_lengths() -> None:
    """Each paragraph is its level and its characters, a link's address and a bullet's text included."""
    frame = text_frame()
    written = text.write_markdown(frame, "Intro\n\n- [ab](x)\n  - cde")
    assert written.lines == ((0, 5), (0, 6), (1, 3))


def test_a_long_word_counts_as_several_lines() -> None:
    """One 300-character word on lines of 60 characters takes 5 lines, though it is one paragraph."""
    frame = text_frame()
    written = text.write_markdown(frame, "x" * 300)
    assert written.lines == ((0, 300),)
    assert text.estimate_lines(written, 60) == 5


def test_a_level_narrows_the_line_and_an_empty_paragraph_counts() -> None:
    """Each level takes four characters of the line; a paragraph with no characters is a line; a line is at least 1."""
    written = text.Written(lines=((0, 60), (1, 60), (2, 0), (0, 61)), replaced=0)
    assert text.estimate_lines(written, 60) == 1 + 2 + 1 + 2
    assert text.estimate_lines(text.Written(lines=((2, 5),), replaced=0), 8) == 5
    assert text.estimate_lines(text.Written(lines=((2, 5),), replaced=0), 1) == 5
    assert text.estimate_lines(text.Written(lines=(), replaced=0), 60) == 0


# --- write_presentation -------------------------------------------------------------------------------------------

TITLE_SLIDE = 1
TITLE_ONLY = 6
PRESENTATION_MAIN = "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"
FOOTERS = frozenset({PP_PLACEHOLDER.DATE, PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER})
REPORT = {"kind": "file", "path": "harbour-report.pdf", "virtual": f"/workspace/{CHAT}/harbour-report.pdf", "page": 2}


def kinds(slide: Any) -> dict[PP_PLACEHOLDER, Any]:
    """A slide's placeholders by type."""
    return {shape.placeholder_format.type: shape for shape in slide.placeholders}


def slide_targets(path: Path) -> list[str]:
    """The slide parts of a deck in the order its presentation part lists them."""
    with zipfile.ZipFile(path) as package:
        presentation = etree.fromstring(package.read("ppt/presentation.xml"))
        relationships = etree.fromstring(package.read("ppt/_rels/presentation.xml.rels"))
    targets = {relationship.get("Id"): relationship.get("Target") for relationship in relationships}
    return [targets[entry.get(qn("r:id"))] for entry in presentation.find(qn("p:sldIdLst"))]


def autofit(shape: Any) -> Any:
    """The ``a:normAutofit`` of a shape's text, or ``None``."""
    return shape._element.find(f"{qn('p:txBody')}/{qn('a:bodyPr')}/{qn('a:normAutofit')}")


def resaved(path: Path, change: Any) -> Path:
    """Open a master, change it and save it where it was."""
    prs = Presentation(str(path))
    change(prs)
    prs.save(str(path))
    return path


def test_a_deck_has_the_slides_asked_for(job: Job) -> None:
    """A layout by number, by name in another case, or as digits that name none: the slides in order, as asked."""
    mt.make_master(job.path("master.pptx"))
    answer = job.write(
        [
            {"layout": TITLE_SLIDE, "title": "Invented harbour review", "subtitle": "A synthetic year"},
            {"layout": "titel und inhalt", "title": "Invented tonnage", "content": ["- North pier\n- South pier"]},
            {"layout": str(TITLE_ONLY), "title": "Invented outlook"},
        ]
    )
    assert answer == {"slides": 3, "warnings": []}
    slides = list(job.deck().slides)
    assert len(slides) == 3
    titles = [slide.shapes.title.text for slide in slides]
    assert titles == ["Invented harbour review", "Invented tonnage", "Invented outlook"]
    assert [slide.slide_layout.name for slide in slides] == ["Title Slide", "Titel und Inhalt", "Title Only"]
    assert kinds(slides[0])[PP_PLACEHOLDER.SUBTITLE].text_frame.text == "A synthetic year"
    body = slides[1].placeholders[1]
    assert [paragraph.text for paragraph in body.text_frame.paragraphs] == ["North pier", "South pier"]


def test_the_example_slides_are_gone(job: Job) -> None:
    """No example slide is left, nor its chart, picture, notes, custom show or section; the slides are numbered anew."""
    mt.make_master(job.path("master.pptx"))
    titles = ["Invented first", "Invented second", "Invented third"]
    job.write([{"layout": TITLE_AND_CONTENT, "title": title} for title in titles])
    out = job.ctx.out_dir / "deck.pptx"
    with zipfile.ZipFile(out) as package:
        names = package.namelist()
        parts = {name: package.read(name) for name in names}
    assert len(names) == len(set(names))
    examples = ("ppt/charts/", "ppt/media/", "ppt/embeddings/", "ppt/notesSlides/")
    assert [name for name in names if name.startswith(examples)] == []
    said = (mt.EXAMPLE_TITLE.encode(), b"Invented example", b"of the example", b"Invented short show")
    assert [name for name, data in parts.items() if any(text in data for text in said)] == []
    assert b"custShowLst" not in parts["ppt/presentation.xml"]
    assert b"sectionLst" not in parts["ppt/presentation.xml"]
    assert slide_targets(out) == ["slides/slide1.xml", "slides/slide2.xml", "slides/slide3.xml"]
    slide_parts = sorted(name for name in names if name.startswith("ppt/slides/slide"))
    assert slide_parts == ["ppt/slides/slide1.xml", "ppt/slides/slide2.xml", "ppt/slides/slide3.xml"]
    for number, title in enumerate(titles, 1):
        assert title.encode() in parts[f"ppt/slides/slide{number}.xml"]


def test_a_potx_master_gives_a_pptx(job: Job, tmp_path: Path) -> None:
    """A deck built in a ``.potx`` is a presentation, not a template."""
    mt.as_potx(mt.make_master(tmp_path / "built.pptx"), job.path("master.potx"))
    assert job.write([{"layout": TITLE_AND_CONTENT, "title": "Invented"}], path="master.potx")["slides"] == 1
    with zipfile.ZipFile(job.ctx.out_dir / "deck.pptx") as package:
        types = etree.fromstring(package.read("[Content_Types].xml"))
    overrides = {entry.get("PartName"): entry.get("ContentType") for entry in types if entry.tag.endswith("Override")}
    assert overrides["/ppt/presentation.xml"] == PRESENTATION_MAIN


def test_footer_and_slide_number_are_copied(job: Job) -> None:
    """The layout's footer with its text and its slide number with the slide's, where the layout has them; no date."""
    mt.make_master(job.path("master.pptx"))
    job.write([{"layout": TITLE_AND_CONTENT, "title": "Invented one"}, {"layout": TITLE_AND_CONTENT, "title": "Two"}])
    slide = job.deck().slides[1]
    found = kinds(slide)
    assert found[PP_PLACEHOLDER.FOOTER].text_frame.text == mt.MASTER_FOOTER
    number = found[PP_PLACEHOLDER.SLIDE_NUMBER]
    field = number._element.find(f".//{qn('a:fld')}")
    assert (field.get("type"), field.find(qn("a:t")).text) == ("slidenum", "2")
    assert PP_PLACEHOLDER.DATE not in found
    on_layout = kinds(slide.slide_layout)
    for kind in (PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER):
        assert len(found[kind]._element.find(qn("p:spPr"))) == 0
        assert (found[kind].left, found[kind].top) == (on_layout[kind].left, on_layout[kind].top)
    ids = [shape.shape_id for shape in slide.shapes]
    assert len(ids) == len(set(ids))


def test_a_fixed_date_is_copied_and_an_empty_footer_is_not(job: Job) -> None:
    """A date of fixed text is copied; a footer with no text is not; a footer's link is left out and its text kept."""

    def change(prs: Any) -> None:
        found = kinds(prs.slide_layouts[TITLE_AND_CONTENT - 1])
        found[PP_PLACEHOLDER.DATE].text_frame.text = "Invented day"
        found[PP_PLACEHOLDER.FOOTER].text_frame.text = ""
        footer = kinds(prs.slide_layouts[TITLE_ONLY - 1])[PP_PLACEHOLDER.FOOTER]
        footer.text_frame.paragraphs[0].runs[0].hyperlink.address = "https://example.invalid/"

    resaved(mt.make_master(job.path("master.pptx")), change)
    job.write([{"layout": TITLE_AND_CONTENT, "title": "Invented"}, {"layout": TITLE_ONLY, "title": "Invented"}])
    first, second = job.deck().slides
    assert kinds(first)[PP_PLACEHOLDER.DATE].text_frame.text == "Invented day"
    assert PP_PLACEHOLDER.FOOTER not in kinds(first)
    footer = kinds(second)[PP_PLACEHOLDER.FOOTER]
    assert footer.text_frame.text == mt.MASTER_FOOTER
    assert footer._element.xpath(".//a:hlinkClick") == []
    assert footer._element.xpath(".//@r:id") == []


def test_a_hidden_footer_stays_hidden(job: Job) -> None:
    """The master's ``p:hf`` turns the footer, number and date off: none is copied, and no layout reports one."""
    mt.make_master(job.path("master.pptx"), hidden_footer=True)
    job.write([{"layout": TITLE_AND_CONTENT, "title": "Invented"}])
    assert set(kinds(job.deck().slides[0])) == {PP_PLACEHOLDER.TITLE}
    assert not any(layout["footer"] for layout in job.inspect("master.pptx")["layouts"])


def test_a_layouts_own_hf_decides_for_it(job: Job) -> None:
    """A layout's ``p:hf`` overrides its master's, an attribute left out meaning on: its footer shows, no number."""

    def change(prs: Any) -> None:
        layout = prs.slide_layouts[TITLE_AND_CONTENT - 1]._element
        layout.find(qn("p:clrMapOvr")).addnext(parse_xml(f'<p:hf {nsdecls("p")} sldNum="0" dt="0"/>'))

    resaved(mt.make_master(job.path("master.pptx"), hidden_footer=True), change)
    job.write([{"layout": TITLE_AND_CONTENT, "title": "Invented"}, {"layout": TITLE_ONLY, "title": "Invented"}])
    first, second = job.deck().slides
    assert set(kinds(first)) == {PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.FOOTER}
    assert set(kinds(second)) == {PP_PLACEHOLDER.TITLE}
    flags = [layout["footer"] for layout in job.inspect("master.pptx")["layouts"]]
    assert flags == [number == TITLE_AND_CONTENT for number in range(1, 12)]


def test_unknown_and_shared_layout_names_are_refused(job: Job) -> None:
    """An unknown layout lists the layouts; a name two layouts share asks for the number; a number always works."""
    mt.make_master(job.path("master.pptx"), duplicate_name=True)
    found = master.layouts(master.open_master(job.path("master.pptx"), job.ctx))
    with pytest.raises(Refusal) as caught:
        deck.resolve(found, "Invented layout", 3)
    assert caught.value.code == "invalid_value"
    listed = "the layouts are 1 Title Slide, 2 Titel und Inhalt, 3 Zwei Inhalte, 4 Zwei Inhalte, 5 Comparison, "
    assert caught.value.message.startswith(f"slide 3: no layout 'Invented layout'; {listed}")
    assert caught.value.message.endswith("11 Vertical Title and Text")
    with pytest.raises(Refusal) as caught:
        deck.resolve(found, 12, 3)
    assert caught.value.message.startswith(f"slide 3: no layout 12; {listed}")
    with pytest.raises(Refusal) as caught:
        deck.resolve(found, "zwei inhalte", 3)
    shared = "slide 3: two layouts are named 'Zwei Inhalte'; give its number: 3 or 4"
    assert (caught.value.code, caught.value.message) == ("invalid_value", shared)
    assert deck.resolve(found, 3, 1).number == 3
    assert deck.resolve(found, "4", 1).number == 4
    error = job.write([{"layout": "Zwei Inhalte", "title": "Invented"}])["error"]
    assert error == "invalid_value: slide 1: two layouts are named 'Zwei Inhalte'; give its number: 3 or 4"
    assert job.write([{"layout": 3, "title": "Invented"}])["slides"] == 1


def test_a_name_of_digits_is_a_name_before_a_number(job: Job) -> None:
    """A layout named "2" is found by that name; the number 2 is still layout 2."""
    resaved(mt.make_master(job.path("master.pptx")), lambda prs: setattr(prs.slide_layouts[6], "name", "2"))
    found = master.layouts(master.open_master(job.path("master.pptx"), job.ctx))
    assert deck.resolve(found, "2", 1).number == 7
    assert deck.resolve(found, 2, 1).number == 2


def test_notes_end_with_the_sources(job: Job) -> None:
    """The notes, a blank line, then each source as kind and reference; a source's page is checked in its file."""
    mt.make_master(job.path("master.pptx"))
    mk.make_pdf(job.path("harbour-report.pdf"), ["Invented page one", "Invented page two"])
    slide = {
        "layout": TITLE_AND_CONTENT,
        "title": "Invented",
        "notes": "Invented speaker notes.",
        "sources": [REPORT, {"kind": "user"}],
    }
    job.write([slide])
    notes = job.deck().slides[0].notes_slide.notes_text_frame.text
    assert notes.endswith("Sources:\nfile: /workspace/chat-1/harbour-report.pdf, page 2\nuser")
    assert notes.startswith("Invented speaker notes.\n\nSources:\n")

    job.write([slide], name="plain.pptx", include_sources=False)
    assert job.deck("plain.pptx").slides[0].notes_slide.notes_text_frame.text == "Invented speaker notes."

    others = [{"kind": "user", "note": "Invented remark"}, {"kind": "assistant"}, {"kind": "job", "job_id": JOB_ID}]
    job.write([{"layout": TITLE_ONLY, "title": "Invented", "sources": others}], name="others.pptx")
    wanted = f"Sources:\nuser: Invented remark\nassistant\njob: {JOB_ID}"
    assert job.deck("others.pptx").slides[0].notes_slide.notes_text_frame.text == wanted

    error = job.write([{**slide, "sources": [{**REPORT, "page": 9}]}], name="bad.pptx")["error"]
    assert error.startswith("invalid_source: slide 1, source 1: ")
    assert not (job.ctx.out_dir / "bad.pptx").exists()


def test_a_slide_without_notes_has_no_notes_page(job: Job) -> None:
    """Neither notes nor sources: no notes page is made."""
    mt.make_master(job.path("master.pptx"))
    job.write([{"layout": TITLE_ONLY, "title": "Invented"}])
    assert not job.deck().slides[0].has_notes_slide


def test_empty_places_are_removed(job: Job) -> None:
    """A place no block fills does not stay to say "Click to add text"; block i fills place i in reading order."""
    mt.make_master(job.path("master.pptx"))
    job.write(
        [
            {"layout": TITLE_AND_CONTENT, "title": "Invented title only"},
            {"layout": COMPARISON, "content": ["Invented A", "", "Invented C"]},
        ]
    )
    first, second = job.deck().slides
    assert set(kinds(first)) == {PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER}
    left = {
        shape.placeholder_format.idx: shape.text_frame.text
        for shape in second.placeholders
        if shape.placeholder_format.type not in FOOTERS
    }
    assert left == {1: "Invented A", 2: "Invented C"}


def test_overflowing_text_is_scaled_and_warned(job: Job) -> None:
    """Bullets of 80 characters, as many as a block holds, shrink to 60 % and are warned of; three short ones do not."""
    mt.make_master(job.path("master.pptx"))
    long = "\n".join(f"- {'Invented harbour figure, ' * 4}"[:82] for _ in range(24))
    assert len(long) <= decks.MAX_TEXT
    short = "- Invented one\n- Invented two\n- Invented three"
    answer = job.write(
        [{"layout": TITLE_AND_CONTENT, "content": [long]}, {"layout": TITLE_AND_CONTENT, "content": [short]}]
    )
    assert len(answer["warnings"]) == 1
    assert answer["warnings"][0].startswith("slide 1: the text may not fit")
    first, second = job.deck().slides
    fitted = autofit(first.placeholders[1])
    assert (fitted.get("fontScale"), fitted.get("lnSpcReduction")) == ("60000", "10000")
    assert autofit(second.placeholders[1]) is None


def test_fit_scales_the_font_to_the_place() -> None:
    """The scale is the place's lines over those needed, at least 60 %; another autofit is replaced, in schema order."""
    frame = text_frame()
    body = frame._txBody.bodyPr
    body.append(parse_xml(f"<a:noAutofit {nsdecls('a')}/>"))
    body.append(parse_xml(f"<a:extLst {nsdecls('a')}/>"))
    place = master.Place(role="text", lines=10, chars=40, idx=1, shape=None)
    assert deck.fit(frame, 10, place) is True
    assert [child.tag.split("}")[1] for child in body] == ["noAutofit", "extLst"]
    assert deck.fit(frame, 12, place) is True
    assert [child.tag.split("}")[1] for child in body] == ["normAutofit", "extLst"]
    assert (body[0].get("fontScale"), body[0].get("lnSpcReduction")) == ("83333", "10000")
    assert deck.fit(frame, 30, place) is False
    assert body[0].get("fontScale") == "60000"


def test_the_answer_names_no_path(job: Job) -> None:
    """The answer is the count and the warnings: no path of the chat or the job, no file name."""
    mt.make_master(job.path("master.pptx"))
    mk.make_pdf(job.path("harbour-report.pdf"), ["Invented page one", "Invented page two"])
    answer = job.write(
        [{"layout": TITLE_AND_CONTENT, "title": "Invented", "subtitle": "Invented", "sources": [REPORT]}]
    )
    assert answer == {"slides": 1, "warnings": ["slide 1: the layout has no place for a subtitle; it was left out"]}
    said = json.dumps(answer)
    for name in ("/workspace", str(job.ctx.work_dir), JOB_ID, "master.pptx", "deck.pptx", "harbour-report"):
        assert name not in said


def test_a_title_without_a_place_is_left_out_with_a_warning(job: Job) -> None:
    """A title for a layout with no title place is not written, and the answer says so."""
    mt.make_master(job.path("master.pptx"))
    answer = job.write([{"layout": 7, "title": "Invented"}])
    assert answer["warnings"] == ["slide 1: the layout has no place for a title; it was left out"]
    assert set(kinds(job.deck().slides[0])) == {PP_PLACEHOLDER.FOOTER, PP_PLACEHOLDER.SLIDE_NUMBER}


def test_many_warnings_are_cut_to_the_listed(job: Job) -> None:
    """At most ``LISTED`` warnings, the last saying how many more there are."""
    mt.make_master(job.path("master.pptx"))
    slides = [{"layout": TITLE_AND_CONTENT, "title": "Invented", "subtitle": "Invented"} for _ in range(25)]
    warnings = job.write(slides)["warnings"]
    assert len(warnings) == LISTED
    assert warnings[-1] == "6 more warnings are not shown."


def test_characters_a_deck_cannot_hold_are_replaced_with_a_warning(job: Job) -> None:
    """A control character in a title, a text or the notes becomes U+FFFD, and each place is warned of once."""
    mt.make_master(job.path("master.pptx"))
    slide = {"layout": TITLE_AND_CONTENT, "title": "A\x01b", "content": ["C\x02d"], "notes": "E\x03f"}
    warnings = job.write([slide])["warnings"]
    assert warnings == [
        f"slide 1, {place}: characters a document cannot hold were replaced with �"
        for place in ("title", "block 1", "notes")
    ]
    written = job.deck().slides[0]
    assert written.shapes.title.text == "A�b"
    assert written.placeholders[1].text_frame.text == "C�d"
    assert written.notes_slide.notes_text_frame.text == "E�f"


def test_a_master_whose_parts_would_clash_is_refused(job: Job) -> None:
    """An example slide another part still relates to would share its part name with a new slide: no deck is written."""

    def change(prs: Any) -> None:
        prs.slide_layouts[TITLE_AND_CONTENT - 1].part.relate_to(prs.slides[0].part, RT.SLIDE)

    resaved(mt.make_master(job.path("master.pptx")), change)
    prs = master.open_master(job.path("master.pptx"), job.ctx)
    deck.remove_slides(prs)
    prs.slides.add_slide(prs.slide_layouts[0])
    with pytest.raises(Refusal) as caught:
        deck.check_parts(prs)
    refused = ("invalid_template", "the master cannot be written as a new deck")
    assert (caught.value.code, caught.value.message) == refused
    assert job.write([{"layout": TITLE_SLIDE, "title": "Invented"}])["error"] == ": ".join(refused)
    assert list(job.ctx.out_dir.iterdir()) == []


@pytest.mark.parametrize(
    ("change", "wanted"),
    [
        ({"name": "deck.docx"}, "invalid_value: the name is not a plain file name ending in .pptx"),
        ({"name": "out/deck.pptx"}, "invalid_value: the name is not a plain file name ending in .pptx"),
        ({"slides": []}, "invalid_value: a deck has 1 to 30 slides"),
        ({"slides": [{"layout": 0}]}, "invalid_value: slide 1: layout is a number from 1"),
        ({"path": "missing.pptx"}, "file_not_found: /workspace/chat-1/missing.pptx is not in"),
        ({"path": "report.docx"}, "unsupported_template: a deck is built in a .pptx or .potx slide master"),
        (
            {"slides": [{"layout": TITLE_AND_CONTENT, "content": ["Invented", "Invented"]}]},
            "invalid_value: slide 1: 2 blocks, but layout 2 has 1 place",
        ),
        (
            {"slides": [{"layout": TITLE_SLIDE, "content": ["Invented"]}]},
            "invalid_value: slide 1: 1 block, but layout 1 has no place for one",
        ),
        (
            {"slides": [{"layout": PICTURE_WITH_CAPTION, "content": ["Invented"]}]},
            "invalid_value: slide 1, block 1: text goes into a text or content place, and place 1 takes a picture",
        ),
        (
            {"slides": [{"layout": TITLE_AND_CONTENT, "content": [{"table": [["Invented"]]}]}]},
            "invalid_value: slide 1, block 1: ",
        ),
    ],
)
def test_a_deck_that_cannot_be_written_is_refused(job: Job, change: dict[str, Any], wanted: str) -> None:
    """A bad name, deck, master or block is refused with what is wrong, and no file is written."""
    mt.make_master(job.path("master.pptx"))
    job.path("report.docx").write_bytes(b"Invented")
    args = {"path": "master.pptx", "name": "deck.pptx", "slides": [{"layout": 2}], "budget_s": 30, **change}
    assert RUNNERS["write_presentation"](args, job.ctx)["error"].startswith(wanted)
    assert list(job.ctx.out_dir.iterdir()) == []
