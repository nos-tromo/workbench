"""The bounds of a render: one rule a test, shared by the api and the executor."""

import math
from itertools import pairwise
from typing import Any

import pytest
from pydantic import ValidationError

from workbench import renders


def _entry(**fields: Any) -> dict[str, Any]:
    """Build the fields of a valid render entry, with the given ones on top.

    Args:
        **fields: What differs from a whole, valid entry.

    Returns:
        The fields, to pass as keyword arguments to :class:`renders.RenderEntry`.
    """
    base: dict[str, Any] = {
        "name": "p0001.jpg",
        "role": "page",
        "n": 1,
        "width": 1131,
        "height": 1600,
        "mime": "image/jpeg",
    }
    return base | fields


def _facts(**fields: Any) -> dict[str, Any]:
    """Build the fields of valid render facts, with the given ones on top.

    Args:
        **fields: What differs from whole, valid facts.

    Returns:
        The fields, to pass as keyword arguments to :class:`renders.RenderFacts`.
    """
    base: dict[str, Any] = {"kind": "pdf", "total": 24, "pages_without_text": [3, 7], "frames": None, "hint": None}
    return base | fields


def test_two_strips_of_a_page_overlap_by_six_percent() -> None:
    """Two strips of a 1600 px page overlap by 96 px."""
    assert renders.strip_spans(1600, 2) == [(0, 848), (752, 1600)]


def test_three_strips_of_a_page_overlap_by_six_percent() -> None:
    """Three strips of a 1600 px page overlap by 96 px each."""
    assert renders.strip_spans(1600, 3) == [(0, 598), (501, 1099), (1002, 1600)]


def test_one_strip_is_the_whole_length() -> None:
    """One strip is the whole length."""
    assert renders.strip_spans(1600, 1) == [(0, 1600)]


@pytest.mark.parametrize("length", [10, 999, 1601, 2048])
@pytest.mark.parametrize("strips", [2, 3])
def test_strips_cover_the_length_and_overlap_by_at_least_the_rule(length: int, strips: int) -> None:
    """The strips run from 0 to the length, each neighbour overlapping by at least the rule."""
    spans = renders.strip_spans(length, strips)
    assert len(spans) == strips
    assert spans[0][0] == 0
    assert spans[-1][1] == length
    overlap = round(renders.STRIP_OVERLAP * length)
    for (_, end), (start, _) in pairwise(spans):
        assert end - start >= overlap


def test_a_phone_photo_is_scaled_to_both_caps_and_keeps_its_ratio() -> None:
    """A 12 MP photo lands within both caps and keeps its ratio to 1 %."""
    width, height = renders.target_size(4032, 3024, long_side=2048, max_pixels=2_400_000)
    assert max(width, height) <= 2048
    assert width * height <= 2_400_000
    assert math.isclose(width / height, 4032 / 3024, rel_tol=0.01)


def test_a_small_image_is_never_enlarged() -> None:
    """A size within both caps is returned as it is."""
    assert renders.target_size(100, 50, long_side=2048, max_pixels=2_400_000) == (100, 50)


def test_a_long_strip_is_held_by_its_long_side_alone() -> None:
    """With no pixel cap, only the long side holds."""
    assert renders.target_size(10000, 100, long_side=2048) == (2048, 20)


def test_a_page_is_held_at_its_long_side_without_losing_a_pixel_to_rounding() -> None:
    """A page of A4's ratio is drawn at 1131 by 1600."""
    assert renders.target_size(1240, 1754, long_side=1600) == (1131, 1600)


def test_a_long_side_that_the_scale_would_round_below_the_bound_still_lands_on_it() -> None:
    """A scale that floating point puts a hair under the bound still gives the bound."""
    # 2156 * (1600 / 2156) is 1599.9999999999998 in floating point; the epsilon keeps it at 1600.
    assert renders.target_size(2156, 1078, long_side=1600) == (1600, 800)


def test_a_side_is_never_scaled_to_nothing() -> None:
    """A very thin picture keeps one pixel on its short side."""
    assert renders.target_size(100_000, 1, long_side=2048) == (2048, 1)


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("Scan.PDF", "pdf"),
        ("scan.pdf", "pdf"),
        ("photo.HEIC", "image"),
        ("photo.jpeg", "image"),
        ("deck.pptx", "none"),
        ("noext", "none"),
        (".png", "none"),
    ],
)
def test_the_kind_of_a_file_is_told_by_its_lower_cased_suffix(name: str, kind: str) -> None:
    """A file is an image, a PDF or neither by its suffix alone, in any case."""
    assert renders.kind_of(name) == kind


def test_every_image_suffix_is_one_kind_of_image() -> None:
    """Every suffix of the set is an image."""
    for suffix in renders.IMAGE_SUFFIXES:
        assert renders.kind_of(f"file{suffix}") == "image"


def test_a_render_is_named_by_its_page_and_strip() -> None:
    """A whole page has no strip in its name; a strip names its place of the strips."""
    assert renders.render_name(3, 1, 2, "image/png") == "p0003-s1of2.png"
    assert renders.render_name(1, 1, 1, "image/jpeg") == "p0001.jpg"


def test_a_render_entry_takes_a_whole_entry() -> None:
    """An entry with the defaults is one strip of one."""
    entry = renders.RenderEntry(**_entry())
    assert (entry.strip, entry.strips) == (1, 1)


@pytest.mark.parametrize(
    "fields",
    [
        {"strip": 3, "strips": 2},
        {"strip": 0},
        {"strips": 4},
        {"n": 0},
        {"width": 0},
        {"width": 2049},
        {"height": 0},
        {"height": 2049},
        {"mime": "image/gif"},
        {"role": "frame"},
        {"extra": "x"},
    ],
)
def test_a_render_entry_refuses_what_is_out_of_bounds(fields: dict[str, Any]) -> None:
    """An entry is refused for a bad strip, a number, a size, a type, a role or a key it does not declare."""
    with pytest.raises(ValidationError):
        renders.RenderEntry(**_entry(**fields))


def test_a_render_entry_takes_the_widest_it_may() -> None:
    """An entry at the bound is whole."""
    entry = renders.RenderEntry(**_entry(width=2048, height=2048, strip=3, strips=3, mime="image/png"))
    assert (entry.width, entry.height) == (2048, 2048)


def test_render_facts_take_whole_facts() -> None:
    """Whole facts, with a frame count and a hint, are taken."""
    facts = renders.RenderFacts(**_facts(frames=2, hint="one frame shown"))
    assert facts.frames == 2


@pytest.mark.parametrize(
    "fields",
    [
        {"frames": 1},
        {"total": 0},
        {"kind": "none"},
        {"hint": "x" * 301},
        {"pages_without_text": list(range(1, renders.PAGES_CHECKED_MAX + 2))},
        {"extra": "x"},
    ],
)
def test_render_facts_refuse_what_is_out_of_bounds(fields: dict[str, Any]) -> None:
    """Facts are refused for a single frame, no total, a kind of none, a long hint, too many pages or a stray key."""
    with pytest.raises(ValidationError):
        renders.RenderFacts(**_facts(**fields))


def test_render_facts_take_the_longest_hint_and_the_most_pages_it_may() -> None:
    """Facts at the bounds are whole."""
    facts = renders.RenderFacts(
        **_facts(hint="x" * 300, pages_without_text=list(range(1, renders.PAGES_CHECKED_MAX + 1)))
    )
    assert len(facts.pages_without_text) == renders.PAGES_CHECKED_MAX


def test_an_answer_is_the_facts_with_the_renders() -> None:
    """An answer is the facts and the renders."""
    answer = renders.RenderAnswer(**_facts(), renders=[_entry()])
    assert [entry.name for entry in answer.renders] == ["p0001.jpg"]


def test_an_answer_holds_at_most_one_job_of_renders() -> None:
    """An answer holds at most the renders of one job."""
    renders.RenderAnswer(**_facts(), renders=[_entry(n=n) for n in range(1, renders.RENDERS_PER_JOB + 1)])
    with pytest.raises(ValidationError):
        renders.RenderAnswer(**_facts(), renders=[_entry(n=n) for n in range(1, renders.RENDERS_PER_JOB + 2)])


def test_a_refusal_never_quotes_what_it_was_given() -> None:
    """A refusal's text does not carry the input."""
    with pytest.raises(ValidationError) as caught:
        renders.RenderFacts(**_facts(hint="secret-" + "x" * 300))
    assert "secret-" not in str(caught.value)


def test_a_jpeg_is_known_by_its_signature() -> None:
    """A JPEG's first bytes pass as a JPEG."""
    assert renders.signature_ok(b"\xff\xd8\xff\xe0" + b"\x00" * 16, "image/jpeg")


def test_a_png_is_known_by_its_signature() -> None:
    """A PNG's first bytes pass as a PNG."""
    assert renders.signature_ok(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "image/png")


def test_a_png_signature_does_not_pass_for_a_jpeg() -> None:
    """A PNG's first bytes do not pass as a JPEG."""
    assert not renders.signature_ok(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16, "image/jpeg")


def test_a_type_with_no_signature_is_refused() -> None:
    """A type that is not a render has no signature to match."""
    assert not renders.signature_ok(b"GIF89a" + b"\x00" * 16, "image/gif")


def test_the_bounds_are_the_spec_s() -> None:
    """The constants are the numbers of the spec."""
    assert renders.SOURCE_MAX_PIXELS == 120_000_000
    assert (renders.IMAGE_LONG_SIDE, renders.IMAGE_MAX_PIXELS) == (2048, 2_400_000)
    assert renders.PAGE_LONG_SIDE == 1600
    assert (renders.IMAGE_QUALITY, renders.PAGE_QUALITY, renders.QUALITY_FLOOR) == (90, 85, 50)
    assert renders.RENDER_MAX_BYTES == 768 * 1024
    assert (renders.FIRST_PAGES, renders.PAGES_CHECKED_MAX, renders.RENDERS_PER_JOB) == (20, 1000, 20)
    assert (renders.MAX_STRIPS, renders.STRIP_OVERLAP) == (3, 0.06)
    assert renders.DERIVED_MAX_BYTES == 64 * 1024 * 1024
    assert renders.PDF_SUFFIX == ".pdf"
