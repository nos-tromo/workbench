"""Slide masters in a folder laid out as a job's: their layouts, ``inspect_template`` and ``write_presentation``.

A master's layouts, places and capacity are read, ``inspect_template`` lists them, and ``write_presentation`` builds
decks in it. Every master is built at test time from python-pptx's default presentation
(``tests/fixtures/make_templates.py``), all its text invented.
"""

import io
import json
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from lxml import etree
from PIL import Image as PILImage
from pptx import Presentation
from pptx.enum.chart import XL_CHART_TYPE, XL_LEGEND_POSITION
from pptx.enum.shapes import MSO_SHAPE_TYPE, PP_PLACEHOLDER
from pptx.opc.constants import RELATIONSHIP_TYPE as RT
from pptx.opc.oxml import serialize_part_xml
from pptx.opc.packuri import PackURI
from pptx.oxml import parse_xml
from pptx.oxml.ns import nsdecls, qn
from pptx.util import Inches, Pt

from tests.fixtures import make_fixtures as mk
from tests.fixtures import make_templates as mt
from workbench import decks
from workbench.executor.presentations import blocks, deck, master, text
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

    def inspect(self, name: str | None) -> dict[str, Any]:
        """Run ``inspect_template`` on an input, or on the plain master when ``name`` is ``None``."""
        args: dict[str, Any] = {"budget_s": 30} if name is None else {"path": name, "budget_s": 30}
        return RUNNERS["inspect_template"](args, self.ctx)

    def with_max_chars(self, max_chars: int) -> "Job":
        """The same folder with another result limit."""
        return Job(JobContext(self.ctx.work_dir, self.ctx.out_dir, CHAT, max_chars, self.ctx.file_mb))

    def write(
        self, slides: list[dict[str, Any]], *, path: str | None = "master.pptx", name: str = "deck.pptx", **extra: Any
    ) -> dict[str, Any]:
        """Run ``write_presentation`` on a master of the job, or on the plain master when ``path`` is ``None``."""
        args = {"name": name, "slides": slides, "budget_s": 30, **extra}
        if path is not None:
            args["path"] = path
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


@pytest.mark.parametrize(("higher", "order"), [(5, [1, 2]), (91440, [1, 2]), (91441, [2, 1])])
def test_places_side_by_side_are_one_row(job: Job, higher: int, order: list[int]) -> None:
    """Places whose tops are at most 0.1 in apart are one row, read left to right; further apart, the higher first."""
    mt.make_master(job.path("master.pptx"))

    def change(prs: Any) -> None:
        right = prs.slide_layouts[TWO_CONTENT - 1].placeholders.get(idx=2)
        right.top -= higher

    resaved(job.path("master.pptx"), change)
    found = master.layouts(master.open_master(job.path("master.pptx"), job.ctx))[TWO_CONTENT - 1]
    assert [place.idx for place in found.places] == order
    (left,) = [place for place in found.places if place.idx == 1]
    assert left.shape.top - next(place for place in found.places if place.idx == 2).shape.top == higher


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


def test_a_layout_name_is_matched_without_its_outer_spaces(job: Job) -> None:
    """A layout named "Title Slide " is found by "title slide", and by the name with spaces of its own."""
    resaved(mt.make_master(job.path("master.pptx")), lambda prs: setattr(prs.slide_layouts[0], "name", "Title Slide "))
    found = master.layouts(master.open_master(job.path("master.pptx"), job.ctx))
    for said in ("title slide", "Title Slide ", " TITLE SLIDE"):
        assert deck.resolve(found, said, 1).number == TITLE_SLIDE
    assert job.write([{"layout": "Title Slide", "title": "Invented"}])["slides"] == 1


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


def test_an_empty_string_leaves_any_place_empty(job: Job) -> None:
    """A title, a subtitle or a block of "" leaves its place empty, whatever the place takes; the place is removed."""
    mt.make_master(job.path("master.pptx"))
    answer = job.write(
        [
            {"layout": TITLE_SLIDE, "title": "", "subtitle": "Invented subtitle"},
            {"layout": PICTURE_WITH_CAPTION, "title": "Invented", "subtitle": "", "content": ["", "Invented caption"]},
        ]
    )
    assert answer == {"slides": 2, "warnings": []}
    first, second = job.deck().slides
    assert set(kinds(first)) - FOOTERS == {PP_PLACEHOLDER.SUBTITLE}
    assert kinds(first)[PP_PLACEHOLDER.SUBTITLE].text_frame.text == "Invented subtitle"
    assert set(kinds(second)) - FOOTERS == {PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.BODY}
    assert kinds(second)[PP_PLACEHOLDER.BODY].text_frame.text == "Invented caption"
    assert [shape for shape in second.shapes if shape.shape_type == MSO_SHAPE_TYPE.PICTURE] == []
    error = job.write([{"layout": TITLE_SLIDE, "content": [""]}], name="past.pptx")["error"]
    wanted = "slide 1, block 1: text goes into a text or content place; layout 'Title Slide' has: none"
    assert error == f"invalid_value: {wanted}"


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
    assert blocks.fit(frame, 10, place) is True
    assert [child.tag.split("}")[1] for child in body] == ["noAutofit", "extLst"]
    assert blocks.fit(frame, 12, place) is True
    assert [child.tag.split("}")[1] for child in body] == ["normAutofit", "extLst"]
    assert (body[0].get("fontScale"), body[0].get("lnSpcReduction")) == ("83333", "10000")
    assert blocks.fit(frame, 30, place) is False
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


def test_the_metadata_of_the_example_slides_is_dropped(job: Job) -> None:
    """The thumbnail and ``docProps/app.xml`` (the example slides' titles) go, and the custom show; core.xml is new."""
    with zipfile.ZipFile(mt.make_master(job.path("master.pptx"))) as package:
        assert "docProps/thumbnail.jpeg" in package.namelist()
        assert mt.EXAMPLE_TITLE.encode() in package.read("docProps/app.xml")
    job.write([{"layout": TITLE_AND_CONTENT, "title": "Invented"}])
    with zipfile.ZipFile(job.ctx.out_dir / "deck.pptx") as package:
        names = package.namelist()
        parts = {name: package.read(name) for name in names}
    assert "docProps/thumbnail.jpeg" not in names
    assert "docProps/app.xml" not in names
    assert "docProps/core.xml" in names
    for listing in ("[Content_Types].xml", "_rels/.rels"):
        for said in (b"thumbnail", b"app.xml", b"extended-properties"):
            assert said not in parts[listing]
    assert [name for name, data in parts.items() if mt.EXAMPLE_TITLE.encode() in data] == []
    settings = etree.fromstring(parts["ppt/presProps.xml"])
    assert settings.find(f"{qn('p:showPr')}/{qn('p:custShow')}") is None
    assert settings.find(f"{qn('p:showPr')}/{qn('p:present')}") is not None


def test_the_show_runs_the_whole_deck(job: Job) -> None:
    """A slide show of a range of the master's slides would start at a slide the deck may not have: it is not kept."""

    def change(prs: Any) -> None:
        properties = prs.part.part_related_by(RT.PRES_PROPS)
        settings = parse_xml(properties.blob)
        shown = settings.find(qn("p:showPr"))
        shown.replace(shown.find(qn("p:custShow")), parse_xml(f'<p:sldRg {nsdecls("p")} st="2" end="2"/>'))
        properties.blob = serialize_part_xml(settings)

    resaved(mt.make_master(job.path("master.pptx")), change)
    assert job.write([{"layout": TITLE_AND_CONTENT, "title": "Invented"}]) == {"slides": 1, "warnings": []}
    with zipfile.ZipFile(job.ctx.out_dir / "deck.pptx") as package:
        settings = etree.fromstring(package.read("ppt/presProps.xml"))
    shown = settings.find(qn("p:showPr"))
    assert (shown.find(qn("p:sldRg")), shown.find(qn("p:custShow"))) == (None, None)
    assert shown.find(qn("p:present")) is not None


def test_slides_collapsed_in_the_outline_are_forgotten(job: Job) -> None:
    """The outline view lists the master's slides it collapses, by links of its own: the list and the links go."""

    def change(prs: Any) -> None:
        view = prs.part.part_related_by(RT.VIEW_PROPS)
        rid = view.relate_to(prs.slides[1].part, RT.SLIDE)
        root = parse_xml(view.blob)
        outline = (
            f'<p:outlineViewPr {nsdecls("p", "a", "r")}><p:cViewPr><p:scale><a:sx n="33" d="100"/><a:sy n="33" '
            f'd="100"/></p:scale><p:origin x="0" y="0"/></p:cViewPr><p:sldLst><p:sld r:id="{rid}" collapse="1"/>'
            "</p:sldLst></p:outlineViewPr>"
        )
        root.find(qn("p:slideViewPr")).addnext(parse_xml(outline))
        view.blob = serialize_part_xml(root)

    resaved(mt.make_master(job.path("master.pptx")), change)
    assert job.write([{"layout": TITLE_AND_CONTENT, "title": "Invented"}]) == {"slides": 1, "warnings": []}
    with zipfile.ZipFile(job.ctx.out_dir / "deck.pptx") as package:
        names = package.namelist()
        view = etree.fromstring(package.read("ppt/viewProps.xml"))
    outline = view.find(qn("p:outlineViewPr"))
    assert outline is not None
    assert outline.find(qn("p:sldLst")) is None
    assert "ppt/_rels/viewProps.xml.rels" not in names
    assert slide_targets(job.ctx.out_dir / "deck.pptx") == ["slides/slide1.xml"]


def test_the_masters_history_is_reset(job: Job) -> None:
    """The deck names none of the master's people and none of its slides, and keeps none of its dates.

    The core properties are emptied, revision 1, made and modified now, the language kept; the parts that name who
    edited the master or commented on it go.
    """
    mt.make_master(job.path("master.pptx"), history=True)
    before = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    job.write([{"layout": TITLE_AND_CONTENT, "title": "Invented"}])
    after = datetime.now(UTC).replace(tzinfo=None)
    with zipfile.ZipFile(job.ctx.out_dir / "deck.pptx") as package:
        parts = {name: package.read(name) for name in package.namelist()}
    assert [name for name in mt.HISTORY_PARTS if name.lstrip("/") in parts] == []
    for listing in ("[Content_Types].xml", "ppt/_rels/presentation.xml.rels"):
        for reltype, content_type, _ in mt.HISTORY_PARTS.values():
            assert reltype.encode() not in parts[listing]
            assert content_type.encode() not in parts[listing]
    said = (mt.MASTER_AUTHOR.encode(), mt.MASTER_EDITOR.encode(), mt.EXAMPLE_TITLE.encode(), b"Invented subject")
    assert [name for name, data in parts.items() if any(text in data for text in said)] == []
    core = job.deck().core_properties
    texts = (core.title, core.subject, core.keywords, core.comments, core.category, core.author, core.last_modified_by)
    assert (*texts, core.identifier, core.content_status, core.version) == ("",) * 10
    assert (core.revision, core.last_printed, core.language) == (1, None, mt.MASTER_LANGUAGE)
    assert core.created == core.modified
    assert core.created is not None and before <= core.created <= after


def link_to_example(prs: Any, index: int) -> None:
    """Give layout 2's title a slide-jump link to an example slide, as a "back to the agenda" button has."""
    layout = prs.slide_layouts[TITLE_AND_CONTENT - 1]
    rid = layout.part.relate_to(prs.slides[index].part, RT.SLIDE)
    run = kinds(layout)[PP_PLACEHOLDER.TITLE].text_frame.paragraphs[0].runs[0]
    jump = f'<a:hlinkClick {nsdecls("a", "r")} r:id="{rid}" action="ppaction://hlinksldjump"/>'
    run._r.get_or_add_rPr().append(parse_xml(jump))


@pytest.mark.parametrize("index", [0, 1])
def test_a_master_linking_to_its_own_slides_is_refused(job: Job, index: int) -> None:
    """An example slide a layout links to would stay in the deck, hidden or under a new slide's name: no deck."""
    resaved(mt.make_master(job.path("master.pptx")), lambda prs: link_to_example(prs, index))
    error = job.write([{"layout": TITLE_SLIDE, "title": "Invented"}])["error"]
    assert error == (
        "invalid_template: the master links to its own slides (a link on a layout or the master); remove the links or "
        "save it without slides"
    )
    assert list(job.ctx.out_dir.iterdir()) == []


def test_parts_of_one_name_are_refused(job: Job) -> None:
    """Two parts under one name, whatever its case, cannot both be written: the master is refused."""
    prs = master.open_master(mt.make_master(job.path("master.pptx"), examples=0), job.ctx)
    deck.remove_slides(prs)
    deck.check_parts(prs)
    first, second = prs.slide_layouts[0].part, prs.slide_layouts[1].part
    second.partname = PackURI(str(first.partname).replace("slideLayout", "SlideLayout"))
    with pytest.raises(Refusal) as caught:
        deck.check_parts(prs)
    assert (caught.value.code, caught.value.message) == (
        "invalid_template",
        "the master cannot be written as a new deck",
    )


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
            "invalid_value: slide 1, block 2: text goes into a text or content place; layout 'Titel und Inhalt' has: "
            "content",
        ),
        (
            {"slides": [{"layout": TITLE_SLIDE, "subtitle": "Invented", "content": ["Invented"]}]},
            "invalid_value: slide 1, block 1: text goes into a text or content place; layout 'Title Slide' has: none",
        ),
        (
            {"slides": [{"layout": PICTURE_WITH_CAPTION, "content": ["Invented"]}]},
            "invalid_value: slide 1, block 1: text goes into a text or content place; layout 'Picture with Caption' "
            "has: picture, text",
        ),
        (
            {"slides": [{"layout": TITLE_AND_CONTENT, "content": [{"image": "missing.png"}]}]},
            "invalid_value: slide 1, block 1: /workspace/chat-1/missing.png is not in this conversation",
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


# --- tables, charts and images ------------------------------------------------------------------------------------

TWO_CONTENT = 4
CATEGORIES = ["Invented north", "Invented south", "Invented east"]
TONNES = {"Invented tonnes": [12, 7, 3]}
TWO_SERIES = {**TONNES, "Invented crates": [4, 5, 6]}
CHART_PART = re.compile(r"ppt/charts/chart\d+\.xml")
WORKBOOK_PART = re.compile(r"ppt/embeddings/Microsoft_Excel_Sheet\d+\.xlsx")


def image(name: str, **extra: Any) -> dict[str, Any]:
    """An image block as the api hands it to the runner: the path below the chat folder and its virtual path."""
    return {"image": name, "virtual": f"/workspace/{CHAT}/{name}", **extra}


def chart(kind: str, series: dict[str, list[Any]], categories: list[Any] | None = None) -> dict[str, Any]:
    """A chart block."""
    return {"chart": {"type": kind, "categories": CATEGORIES if categories is None else categories, "series": series}}


def place_of(layout: int, index: int = 0) -> Any:
    """A place of python-pptx's default master, the master ``make_master`` builds on, by layout number and order."""
    return master.layouts(Presentation())[layout - 1].places[index]


def frame_idx(shape: Any) -> str | None:
    """The ``p:ph idx`` a graphic frame or picture carries, if any."""
    found = shape._element.find(f"./*/{qn('p:nvPr')}/{qn('p:ph')}")
    return None if found is None else found.get("idx", "0")


def pillow(kind: str, size: tuple[int, int] = (4, 2)) -> bytes:
    """A small picture written by Pillow in another format: ``JPEG``, ``GIF`` or ``BMP``."""
    buffer = io.BytesIO()
    PILImage.new("RGB", size, (128, 128, 128)).save(buffer, kind)
    return buffer.getvalue()


def claimed(width: int, height: int) -> bytes:
    """A PNG whose header claims a size its few bytes do not hold: only the header is read before it is refused."""
    header = b"IHDR" + struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    png = mk._png()
    return png[:8] + struct.pack(">I", 13) + header + struct.pack(">I", zlib.crc32(header)) + png[33:]


def test_a_table_fills_a_content_place(job: Job) -> None:
    """A native table takes the content place, its ``ph idx`` kept: the header first, a short row padded."""
    mt.make_master(job.path("master.pptx"))
    rows = [["Invented pier", "Invented tonnes"], ["Invented north", 12], ["Invented south"]]
    answer = job.write([{"layout": TITLE_AND_CONTENT, "title": "Invented", "content": [{"table": rows}]}])
    assert answer == {"slides": 1, "warnings": []}
    slide = job.deck().slides[0]
    place = place_of(TITLE_AND_CONTENT)
    frames = slide.shapes._spTree.findall(qn("p:graphicFrame"))
    assert len(frames) == 1
    assert frames[0].find(f".//{qn('a:tbl')}") is not None
    shape = next(shape for shape in slide.shapes if shape.has_table)
    assert frame_idx(shape) == str(place.idx)
    table = shape.table
    assert (len(table.rows), len(table.columns)) == (3, 2)
    assert [[cell.text for cell in row.cells] for row in table.rows] == [
        ["Invented pier", "Invented tonnes"],
        ["Invented north", "12"],
        ["Invented south", ""],
    ]
    assert table.first_row is True
    assert (shape.left, shape.top, shape.width) == (place.shape.left, place.shape.top, place.shape.width)


def row_height(size: int) -> int:
    """A table row's natural height at ``size`` pt: one line, 1.2 times the size, and python-pptx's cell margins."""
    return int(Pt(size) * 1.2) + 2 * Inches(0.05)


@pytest.mark.parametrize(
    ("count", "size", "fits"),
    [
        (3, 18, True),
        (12, 18, True),
        (13, 16, True),
        (14, 14, True),
        (16, 12, True),
        (18, 10, True),
        (19, 10, False),
        (30, 10, False),
    ],
)
def test_a_table_takes_the_size_that_fits_its_place(job: Job, count: int, size: int, fits: bool) -> None:
    """The font steps down from 18 pt until the rows, each its natural height, fit the place; at 10 pt, a warning."""
    mt.make_master(job.path("master.pptx"))
    rows = [["Invented column", "Invented value"], *[[f"Invented {number}"] for number in range(count - 1)]]
    warnings = job.write([{"layout": TITLE_AND_CONTENT, "content": [{"table": rows}]}])["warnings"]
    assert warnings == ([] if fits else ["slide 1, block 1: the table may not fit; split it over two slides"])
    shape = next(shape for shape in job.deck().slides[0].shapes if shape.has_table)
    table = shape.table
    assert len(table.rows) == count
    paragraphs = [p for row in table.rows for cell in row.cells for p in cell.text_frame.paragraphs]
    assert {run.font.size for p in paragraphs for run in p.runs} == {Pt(size)}
    assert {p._p.endParaRPr.sz for p in paragraphs} == {Pt(size).centipoints}
    assert [row.height for row in table.rows] == [row_height(size)] * count
    assert shape.height == row_height(size) * count
    assert (shape.height <= place_of(TITLE_AND_CONTENT).shape.height) is fits
    if count == 3:
        assert shape.height < place_of(TITLE_AND_CONTENT).shape.height // 2


def test_a_tables_wrapped_cells_count_in_its_height(job: Job) -> None:
    """A cell wraps at its column: a row is as high as its cell of most lines, and the size steps down to fit them.

    Three columns of 3 in in a place of 4.95 in: an 80-character cell takes four lines at 18 and 16 pt (22 and 25
    characters a line) but three at 14 pt (28), where six such rows fit; a cell's paragraphs each take their own.
    """
    mt.make_master(job.path("master.pptx"))
    rows = [[("Invented harbour figure " * 4)[:80], "Invented", "Invented"] for _ in range(6)]
    many = [["Invented header", "Invented", "Invented"]]
    many += [[("Invented quay wall " * 11)[:200], "Invented", "Invented"] for _ in range(9)]
    paragraphs = [["Invented a", "Invented b"], ["Invented one\nInvented two\nInvented three", "Invented c"]]
    warnings = job.write(
        [
            {"layout": TITLE_AND_CONTENT, "content": [{"table": rows}]},
            {"layout": TITLE_AND_CONTENT, "content": [{"table": many}]},
            {"layout": TITLE_AND_CONTENT, "content": [{"table": paragraphs}]},
        ]
    )["warnings"]
    assert warnings == ["slide 2, block 1: the table may not fit; split it over two slides"]
    wrapped, crowded, broken = (next(shape for shape in slide.shapes if shape.has_table) for slide in job.deck().slides)
    room = place_of(TITLE_AND_CONTENT).shape.height

    def lines(count: int, size: int) -> int:
        return int(count * Pt(size) * 1.2) + 2 * Inches(0.05)

    cells = [cell for row in wrapped.table.rows for cell in row.cells]
    assert {run.font.size for cell in cells for run in cell.text_frame.paragraphs[0].runs} == {Pt(14)}
    assert [row.height for row in wrapped.table.rows] == [lines(3, 14)] * 6
    assert wrapped.height == lines(3, 14) * 6 <= room
    assert [row.height for row in crowded.table.rows] == [lines(1, 10), *[lines(5, 10)] * 9]
    assert crowded.height > room
    assert [row.height for row in broken.table.rows] == [lines(1, 18), lines(3, 18)]


def test_a_tables_values_are_shown_as_text(job: Job) -> None:
    """A number, a bool and null are shown as text, the null empty; a character XML cannot hold is replaced."""
    mt.make_master(job.path("master.pptx"))
    rows = [["Invented a", "Invented b", "Invented c"], [1.5, True, None], [-3, False, "Invented\x01d"]]
    answer = job.write([{"layout": TITLE_AND_CONTENT, "content": [{"table": rows}]}])
    assert answer["warnings"] == ["slide 1, block 1: characters a document cannot hold were replaced with �"]
    table = next(shape for shape in job.deck().slides[0].shapes if shape.has_table).table
    assert [[cell.text for cell in row.cells] for row in table.rows][1:] == [
        ["1.5", "true", ""],
        ["-3", "false", "Invented�d"],
    ]


def test_a_chart_is_native_and_editable(job: Job) -> None:
    """Each type is PowerPoint's own chart in the content place, its data in an embedded workbook; no colour is set."""
    mt.make_master(job.path("master.pptx"))
    kinds_asked = [("column", TONNES), ("bar", TWO_SERIES), ("line", TWO_SERIES), ("pie", TONNES)]
    slides = [{"layout": TITLE_AND_CONTENT, "content": [chart(kind, series)]} for kind, series in kinds_asked]
    assert job.write(slides)["warnings"] == []
    out = job.ctx.out_dir / "deck.pptx"
    with zipfile.ZipFile(out) as package:
        names = package.namelist()
        charts = [package.read(name) for name in names if CHART_PART.fullmatch(name)]
    assert len(charts) == 4
    assert len([name for name in names if WORKBOOK_PART.fullmatch(name)]) == 4
    for xml in charts:
        assert b"srgbClr" not in xml
        assert b"schemeClr" not in xml
    frames: list[Any] = [next(s for s in slide.shapes if s.has_chart) for slide in Presentation(str(out)).slides]
    assert [frame_idx(frame) for frame in frames] == ["1"] * 4
    drawn = [frame.chart for frame in frames]
    assert [one.chart_type for one in drawn] == [
        XL_CHART_TYPE.COLUMN_CLUSTERED,
        XL_CHART_TYPE.BAR_CLUSTERED,
        XL_CHART_TYPE.LINE_MARKERS,
        XL_CHART_TYPE.PIE,
    ]
    assert [one.has_legend for one in drawn] == [False, True, True, True]
    assert [one.has_title for one in drawn] == [False] * 4
    assert [one._chartSpace.chart.autoTitleDeleted.val for one in drawn] == [True] * 4
    for one in drawn[1:]:
        assert (one.legend.position, one.legend.include_in_layout) == (XL_LEGEND_POSITION.BOTTOM, False)
    column, bar = drawn[0], drawn[1]
    assert (column.category_axis.reverse_order, bar.category_axis.reverse_order) == (False, True)
    crossing = [one._chartSpace.find(f".//{qn('c:valAx')}/{qn('c:crosses')}").get("val") for one in (column, bar)]
    assert crossing == ["autoZero", "max"]
    for one in drawn:
        assert one.part.chart_workbook.xlsx_part is not None
    assert [list(one.plots[0].categories) for one in drawn] == [CATEGORIES] * 4
    assert [series.name for series in drawn[2].series] == list(TWO_SERIES)
    assert list(drawn[2].series[1].values) == TWO_SERIES["Invented crates"]


def test_a_chart_keeps_its_gaps(job: Job) -> None:
    """A null value is a gap: no point is written for it, in a line and in a pie."""
    mt.make_master(job.path("master.pptx"))
    gappy = {"Invented tonnes": [1, None, 3]}
    job.write([{"layout": TITLE_AND_CONTENT, "content": [chart(kind, gappy)]} for kind in ("line", "pie")])
    with zipfile.ZipFile(job.ctx.out_dir / "deck.pptx") as package:
        charts = [
            etree.fromstring(package.read(name)) for name in sorted(package.namelist()) if CHART_PART.fullmatch(name)
        ]
    assert len(charts) == 2
    for root in charts:
        (values,) = root.iter(qn("c:val"))
        points = values.findall(f".//{qn('c:pt')}")
        assert [(point.get("idx"), point.findtext(qn("c:v"))) for point in points] == [("0", "1"), ("2", "3")]


def test_chart_categories_are_numbers_or_text(job: Job) -> None:
    """Categories that are all numbers stay numbers; a mix is written as text, so the chart stays valid."""
    mt.make_master(job.path("master.pptx"))
    series = {"Invented tonnes": [1, 2]}
    job.write(
        [
            {"layout": TITLE_AND_CONTENT, "content": [chart("column", series, [2024, 2025.5])]},
            {"layout": TITLE_AND_CONTENT, "content": [chart("column", series, [2024, "Invented later"])]},
        ]
    )
    first, second = (next(s for s in slide.shapes if s.has_chart).chart for slide in job.deck().slides)
    numbers = first._chartSpace.find(f".//{qn('c:cat')}")
    assert numbers[0].tag == qn("c:numRef")
    assert [point.findtext(qn("c:v")) for point in numbers.iter(qn("c:pt"))] == ["2024", "2025.5"]
    texts = second._chartSpace.find(f".//{qn('c:cat')}")
    assert texts[0].tag == qn("c:strRef")
    assert list(second.plots[0].categories) == ["2024", "Invented later"]


def test_a_charts_workbook_holds_text_as_text(job: Job) -> None:
    """A label that looks like a formula or a link is text in the embedded workbook, never a formula or a link."""
    mt.make_master(job.path("master.pptx"))
    labels = ["=1+2", "https://example.invalid/", "@Invented"]
    job.write([{"layout": TITLE_AND_CONTENT, "content": [chart("line", {"=SUM(1,2)": [1, 2, 3]}, labels)]}])
    with zipfile.ZipFile(job.ctx.out_dir / "deck.pptx") as package:
        (name,) = [name for name in package.namelist() if WORKBOOK_PART.fullmatch(name)]
        embedded = package.read(name)
    with zipfile.ZipFile(io.BytesIO(embedded)) as workbook:
        sheet = workbook.read("xl/worksheets/sheet1.xml")
        parts = workbook.namelist()
    assert b"<f>" not in sheet
    assert b"hyperlink" not in sheet
    assert not [part for part in parts if part.endswith("sheet1.xml.rels")]
    drawn = next(shape for shape in job.deck().slides[0].shapes if shape.has_chart).chart
    assert list(drawn.plots[0].categories) == labels


def test_an_image_is_cropped_in_a_picture_place_and_fitted_in_a_content_place(job: Job) -> None:
    """A picture place is filled, the picture cropped; in a content place it is fitted, centred, its aspect kept."""
    mt.make_master(job.path("master.pptx"))
    job.path("square.png").write_bytes(mk._png(8))
    job.path("figures").mkdir()
    job.path("figures/wide.png").write_bytes(mk._png(16, 8))
    slides = [
        {"layout": PICTURE_WITH_CAPTION, "title": "Invented", "content": [image("square.png", alt="Invented grey")]},
        {"layout": TITLE_AND_CONTENT, "title": "Invented", "content": [image("figures/wide.png")]},
    ]
    assert job.write(slides)["warnings"] == []
    first, second = job.deck().slides

    cropped = first.placeholders[1]
    assert cropped._element.tag == qn("p:pic")
    assert (cropped.crop_left, cropped.crop_right) == (0, 0)
    assert cropped.crop_top == pytest.approx(0.125) and cropped.crop_bottom == pytest.approx(0.125)
    assert cropped._element.nvPicPr.cNvPr.get("descr") == "Invented grey"

    place = place_of(TITLE_AND_CONTENT).shape
    pictures = [shape for shape in second.shapes if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
    assert len(pictures) == 1
    fitted = pictures[0]
    assert not fitted.is_placeholder
    assert 1 not in {shape.placeholder_format.idx for shape in second.placeholders}
    assert fitted.width / fitted.height == pytest.approx(2, rel=1e-4)
    assert (fitted.crop_left, fitted.crop_right, fitted.crop_top, fitted.crop_bottom) == (0, 0, 0, 0)
    assert place.left <= fitted.left and fitted.left + fitted.width <= place.left + place.width
    assert place.top <= fitted.top and fitted.top + fitted.height <= place.top + place.height
    assert fitted.width == place.width
    assert abs((fitted.top - place.top) - (place.top + place.height - fitted.top - fitted.height)) <= 1
    assert list(second.shapes).index(fitted) == 1
    assert fitted._element.nvPicPr.cNvPr.get("descr") is None
    with zipfile.ZipFile(job.ctx.out_dir / "deck.pptx") as package:
        assert [name for name, data in ((n, package.read(n)) for n in package.namelist()) if b"wide.png" in data] == []


def test_a_jpeg_and_a_gif_are_placed_by_their_content(job: Job) -> None:
    """A picture is known by its content, not its extension: a JPEG named .png is placed as the JPEG it is."""
    mt.make_master(job.path("master.pptx"))
    job.path("photo.png").write_bytes(pillow("JPEG"))
    job.path("drawing.gif").write_bytes(pillow("GIF"))
    slides = [{"layout": TITLE_AND_CONTENT, "content": [image(name)]} for name in ("photo.png", "drawing.gif")]
    assert job.write(slides)["warnings"] == []
    with zipfile.ZipFile(job.ctx.out_dir / "deck.pptx") as package:
        media = sorted(name.rsplit(".", 1)[1] for name in package.namelist() if name.startswith("ppt/media/"))
    assert media == ["gif", "jpg"]
    for slide in job.deck().slides:
        (picture,) = [shape for shape in slide.shapes if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
        assert picture.width / picture.height == pytest.approx(2, rel=1e-4)


@pytest.mark.parametrize(
    ("content", "wanted"),
    [
        (b"Invented text, not a picture", "is not a PNG, JPEG or GIF image"),
        (pillow("BMP"), "is not a PNG, JPEG or GIF image"),
        (claimed(8000, 6000), "is larger than 40 megapixels"),
        (claimed(20000, 10000), "is larger than 40 megapixels"),
    ],
)
def test_a_bad_image_is_refused(job: Job, content: bytes, wanted: str) -> None:
    """A file that is not a PNG, JPEG or GIF, whatever its extension, or too many pixels: refused, nothing written."""
    mt.make_master(job.path("master.pptx"))
    job.path("figure.png").write_bytes(content)
    slides = [
        {"layout": TITLE_ONLY, "title": "Invented"},
        {"layout": TITLE_AND_CONTENT, "content": [image("figure.png")]},
    ]
    error = job.write(slides)["error"]
    assert error == f"invalid_value: slide 2, block 1: /workspace/{CHAT}/figure.png {wanted}"
    assert list(job.ctx.out_dir.iterdir()) == []


@pytest.mark.parametrize("name", ["missing.png", "out/figure.png", "../figure.png", "folder.png"])
def test_an_image_not_in_the_chat_is_refused(job: Job, name: str) -> None:
    """A picture that is not a file among the job's inputs is not in this conversation; no path of the job is said."""
    mt.make_master(job.path("master.pptx"))
    (job.ctx.out_dir / "figure.png").write_bytes(mk._png())
    job.path("folder.png").mkdir()
    error = job.write([{"layout": TITLE_AND_CONTENT, "content": [image(name)]}])["error"]
    assert error == f"invalid_value: slide 1, block 1: /workspace/{CHAT}/{name} is not in this conversation"
    assert str(job.ctx.work_dir) not in error


def test_images_over_their_bytes_are_refused(job: Job, monkeypatch: pytest.MonkeyPatch) -> None:
    """The executor measures the files too: one over the bytes of a picture, or all together over the deck's."""
    mt.make_master(job.path("master.pptx"))
    figure = mk._png(8)
    job.path("figure.png").write_bytes(figure)
    slides = [{"layout": TWO_CONTENT, "content": [image("figure.png"), image("figure.png")]}]
    monkeypatch.setattr(decks, "MAX_IMAGES_BYTES", len(figure) * 2 - 1)
    error = job.write(slides)["error"]
    assert error == "invalid_value: slide 1, block 2: the deck's images take more than 0 MB together"
    monkeypatch.setattr(decks, "MAX_IMAGE_BYTES", len(figure) - 1)
    error = job.write(slides)["error"]
    assert error == f"invalid_value: slide 1, block 1: /workspace/{CHAT}/figure.png is larger than 0 MB"
    assert list(job.ctx.out_dir.iterdir()) == []


def test_blocks_that_do_not_fit_the_layout_are_refused(job: Job) -> None:
    """A block its place does not take, or one past the layout's places, is refused with the layout's places."""
    mt.make_master(job.path("master.pptx"))
    job.path("figure.png").write_bytes(mk._png())
    cases = [
        (
            [{"layout": TITLE_ONLY, "title": "Invented"}, {"layout": TITLE_ONLY, "content": [{"table": [["A"]]}]}],
            "slide 2, block 1: a table goes into a table or content place; layout 'Title Only' has: none",
        ),
        (
            [{"layout": COMPARISON, "content": [image("figure.png")]}],
            "slide 1, block 1: an image goes into a picture or content place; layout 'Comparison' has: text, text, "
            "content, content",
        ),
        (
            [{"layout": TITLE_AND_CONTENT, "content": ["Invented", chart("pie", TONNES), "Invented"]}],
            "slide 1, block 2: a chart goes into a chart or content place; layout 'Titel und Inhalt' has: content",
        ),
        (
            [{"layout": PICTURE_WITH_CAPTION, "content": [image("figure.png"), image("figure.png")]}],
            "slide 1, block 2: an image goes into a picture or content place; layout 'Picture with Caption' has: "
            "picture, text",
        ),
    ]
    for slides, wanted in cases:
        assert job.write(slides)["error"] == f"invalid_value: {wanted}"
        assert list(job.ctx.out_dir.iterdir()) == []


def test_a_table_and_a_chart_take_their_own_places(job: Job) -> None:
    """A table place takes a table and a chart place a chart, each keeping its ``ph``; text goes into neither."""

    def retype(prs: Any) -> None:
        layout = prs.slide_layouts[TWO_CONTENT - 1]
        for idx, kind in ((1, "tbl"), (2, "chart")):
            layout.placeholders.get(idx=idx)._element.ph.set("type", kind)

    resaved(mt.make_master(job.path("master.pptx")), retype)
    answer = job.write(
        [{"layout": TWO_CONTENT, "content": [{"table": [["Invented"], ["Invented"]]}, chart("bar", TONNES)]}]
    )
    assert answer["warnings"] == []
    slide = job.deck().slides[0]
    table = next(shape for shape in slide.shapes if shape.has_table)
    drawn = next(shape for shape in slide.shapes if shape.has_chart)
    assert (table.placeholder_format.type, frame_idx(table)) == (PP_PLACEHOLDER.TABLE, "1")
    assert (drawn.placeholder_format.type, frame_idx(drawn)) == (PP_PLACEHOLDER.CHART, "2")
    error = job.write([{"layout": TWO_CONTENT, "content": ["Invented"]}], name="text.pptx")["error"]
    wanted = "slide 1, block 1: text goes into a text or content place; layout 'Two Content' has: table, chart"
    assert error == f"invalid_value: {wanted}"


def test_characters_a_chart_or_an_alt_text_cannot_hold_are_replaced(job: Job) -> None:
    """A category, a series' name and an alt text with a control character are written with U+FFFD, and warned of."""
    mt.make_master(job.path("master.pptx"))
    job.path("figure.png").write_bytes(mk._png())
    content = [
        chart("column", {"Invented\x02tonnes": [1, 2]}, ["Invented\x01north", "South"]),
        image("figure.png", alt="A\x03b"),
    ]
    warnings = job.write([{"layout": TWO_CONTENT, "content": content}])["warnings"]
    assert warnings == [
        f"slide 1, block {block}: characters a document cannot hold were replaced with �" for block in (1, 2)
    ]
    slide = job.deck().slides[0]
    drawn = next(shape for shape in slide.shapes if shape.has_chart).chart
    assert list(drawn.plots[0].categories) == ["Invented�north", "South"]
    assert drawn.series[0].name == "Invented�tonnes"
    (picture,) = [shape for shape in slide.shapes if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
    assert picture._element.nvPicPr.cNvPr.get("descr") == "A�b"


def test_a_deck_of_every_block_kind_has_parts_of_their_own(job: Job) -> None:
    """Text, tables, charts and pictures across slides: every part a name of its own, a picture used twice kept once."""
    mt.make_master(job.path("master.pptx"))
    job.path("figure.png").write_bytes(mk._png(16, 8))
    slides = [
        {"layout": TWO_CONTENT, "content": ["- Invented point", {"table": [["Invented"], [1]]}]},
        {"layout": TWO_CONTENT, "content": [chart("column", TONNES), image("figure.png")]},
        {"layout": TWO_CONTENT, "content": [chart("pie", TONNES), image("figure.png", alt="Invented")]},
    ]
    assert job.write(slides) == {"slides": 3, "warnings": []}
    out = job.ctx.out_dir / "deck.pptx"
    with zipfile.ZipFile(out) as package:
        names = package.namelist()
    assert len({name.lower() for name in names}) == len(names)
    assert len([name for name in names if CHART_PART.fullmatch(name)]) == 2
    assert len([name for name in names if WORKBOOK_PART.fullmatch(name)]) == 2
    assert len([name for name in names if name.startswith("ppt/media/")]) == 1
    reopened = Presentation(str(out))
    assert len({str(part.partname).lower() for part in reopened.part.package.iter_parts()}) == len(
        list(reopened.part.package.iter_parts())
    )
    for slide in reopened.slides:
        ids = [shape.shape_id for shape in slide.shapes]
        assert len(ids) == len(set(ids))


# --- the plain master ---------------------------------------------------------------------------------------------

WIDE = 12192000
HIGH = 6858000
PLAIN_LAYOUTS = [
    "Title Slide",
    "Title and Content",
    "Section Header",
    "Two Content",
    "Comparison",
    "Title Only",
    "Blank",
    "Content with Caption",
    "Picture with Caption",
]


def test_the_plain_master_is_inspected_without_a_path(job: Job) -> None:
    """Without a path, the inspection lists the plain master's nine layouts; it has no example slides."""
    answer = job.inspect(None)
    assert answer["template_kind"] == "slide_master"
    assert (answer["slides"], answer["total"], answer["truncated"]) == (0, 9, False)
    assert [layout["name"] for layout in answer["layouts"]] == PLAIN_LAYOUTS
    assert [place["role"] for place in answer["layouts"][PICTURE_WITH_CAPTION - 1]["places"]] == ["picture", "text"]


def test_the_plain_master_is_wide_and_every_shape_fits_it() -> None:
    """The plain master is 16:9, each shape of its master and layouts within the slide, wider than python-pptx's own."""
    prs = master.plain_master()
    assert (prs.slide_width, prs.slide_height) == (WIDE, HIGH)
    for part in [prs.slide_masters[0], *prs.slide_layouts]:
        for shape in part.shapes:
            assert 0 <= shape.left and shape.left + shape.width <= WIDE
            assert 0 <= shape.top and shape.top + shape.height <= HIGH
    plain = master.layouts(prs)[TITLE_AND_CONTENT - 1].places[0]
    narrow = master.layouts(Presentation())[TITLE_AND_CONTENT - 1].places[0]
    assert plain.lines == narrow.lines
    assert plain.chars > narrow.chars


def test_each_plain_master_is_a_fresh_one() -> None:
    """A deck built in one plain master leaves the next one as it was."""
    first = master.plain_master()
    first.slides.add_slide(first.slide_layouts[0])
    assert len(master.plain_master().slides) == 0


def test_a_deck_is_built_in_the_plain_master(job: Job) -> None:
    """Without a path, every block kind goes into the plain master's places, with notes and slide numbers."""
    job.path("figure.png").write_bytes(mk._png(16, 8))
    slides: list[dict[str, Any]] = [
        {"layout": TITLE_SLIDE, "title": "Invented harbour review", "subtitle": "A synthetic year"},
        {"layout": "Title and Content", "title": "Invented tonnage", "content": ["- North pier\n- South pier"]},
        {
            "layout": TWO_CONTENT,
            "title": "Invented figures",
            "content": [{"table": [["Invented pier"], [12]]}, chart("column", TONNES)],
        },
        {"layout": PICTURE_WITH_CAPTION, "title": "Invented view", "content": [image("figure.png"), "Invented"]},
    ]
    slides[1]["notes"] = "Invented note"
    slides[1]["sources"] = [{"kind": "assistant"}]
    assert job.write(slides, path=None) == {"slides": 4, "warnings": []}
    deck_ = job.deck()
    assert (deck_.slide_width, deck_.slide_height) == (WIDE, HIGH)
    assert [slide.slide_layout.name for slide in deck_.slides] == [PLAIN_LAYOUTS[i - 1] for i in (1, 2, 4, 9)]
    second = deck_.slides[1]
    assert second.notes_slide.notes_text_frame.text == "Invented note\n\nSources:\nassistant"
    for number, slide in enumerate(deck_.slides, 1):
        found = kinds(slide)
        field = found[PP_PLACEHOLDER.SLIDE_NUMBER]._element.find(f".//{qn('a:fld')}")
        assert field.find(qn("a:t")).text == str(number)
        assert PP_PLACEHOLDER.DATE not in found and PP_PLACEHOLDER.FOOTER not in found
    assert {MSO_SHAPE_TYPE.TABLE, MSO_SHAPE_TYPE.CHART} <= {shape.shape_type for shape in deck_.slides[2].shapes}
    assert deck_.slides[3].placeholders[1]._element.tag == qn("p:pic")


def test_a_slide_that_names_no_layout_takes_the_first_that_fits(job: Job) -> None:
    """A model leaves a layout out: the first layout with the slide's title, subtitle and one place a block is used.

    A layout whose title and subtitle are exactly the slide's goes first, so a title alone is a title-only slide.
    """
    job.path("figure.png").write_bytes(mk._png(16, 8))
    slides: list[dict[str, Any]] = [
        {"title": "Invented harbour review", "subtitle": "A synthetic year"},
        {"title": "Invented tonnage", "content": ["- North pier"]},
        {"title": "Invented section"},
        {"title": "Invented view", "content": [image("figure.png"), "Invented"]},
    ]
    assert job.write(slides, path=None) == {"slides": 4, "warnings": []}
    names = [slide.slide_layout.name for slide in job.deck().slides]
    assert names == [PLAIN_LAYOUTS[i - 1] for i in (TITLE_SLIDE, TITLE_AND_CONTENT, TITLE_ONLY, TWO_CONTENT)]


def test_a_slide_no_layout_takes_is_refused_with_the_layouts(job: Job) -> None:
    """Naming none, a slide no layout takes is refused, and the model hears the layouts to name one."""
    error = job.write([{"title": "Invented", "content": ["- a", "- b", "- c", "- d", "- e"]}], path=None)["error"]
    assert error.startswith(
        "invalid_value: slide 1: it names no layout, and none takes it as it is; give its layout: 1 Title Slide, "
        "2 Title and Content, "
    )
    assert list(job.ctx.out_dir.iterdir()) == []


def test_a_title_slides_only_text_is_its_subtitle(job: Job) -> None:
    """A model writes a title slide's subtitle as its content: on a layout with a subtitle and no place, it is one.

    Within a subtitle's 500 characters only; a longer block is refused as before.
    """
    slides = [{"layout": TITLE_SLIDE, "title": "Invented harbour review", "content": ["A synthetic year"]}]
    assert job.write(slides, path=None) == {"slides": 1, "warnings": []}
    found = kinds(job.deck().slides[0])
    assert found[PP_PLACEHOLDER.SUBTITLE].text_frame.text == "A synthetic year"
    for refused in (
        {"layout": TITLE_SLIDE, "subtitle": "Invented", "content": ["A synthetic year"]},
        {"layout": TITLE_SLIDE, "content": ["Invented", "A synthetic year"]},
        {"layout": TITLE_SLIDE, "content": [{"table": [["Invented"]]}]},
        {"layout": TITLE_SLIDE, "content": ["x" * 501]},
    ):
        error = job.write([refused], path=None, name="refused.pptx")["error"]
        assert error.startswith("invalid_value: slide 1, block 1: "), error


def test_a_deck_in_the_plain_master_names_no_one(job: Job) -> None:
    """The plain master's own core properties, which name python-pptx's author, are not carried into a deck."""
    job.write([{"layout": TITLE_SLIDE, "title": "Invented"}], path=None)
    core = job.deck().core_properties
    assert (core.title, core.author, core.last_modified_by, core.revision) == ("", "", "", 1)
