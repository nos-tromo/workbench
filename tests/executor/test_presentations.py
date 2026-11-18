"""Slide masters in a folder laid out as a job's: their layouts, places and capacity, and ``inspect_template`` on one.

Every master is built at test time from python-pptx's default presentation (``tests/fixtures/make_templates.py``), all
its text invented.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pptx.enum.shapes import PP_PLACEHOLDER
from pptx.oxml import parse_xml
from pptx.oxml.ns import nsdecls, qn

from tests.fixtures import make_templates as mt
from workbench import decks
from workbench.executor.presentations import master
from workbench.executor.runners import RUNNERS, JobContext
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
