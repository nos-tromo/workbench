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
        **fields: What differs from a whole, valid entry. Unless it names the render, the name is the one
            :func:`renders.render_name` gives for the page, strip and type.

    Returns:
        The fields, to pass as keyword arguments to :class:`renders.RenderEntry`.
    """
    base: dict[str, Any] = {"role": "page", "n": 1, "width": 1131, "height": 1600, "mime": "image/jpeg"}
    entry = base | fields
    if "name" not in entry and entry["mime"] in renders.SIGNATURES:
        entry["name"] = renders.render_name(entry["n"], entry.get("strip", 1), entry.get("strips", 1), entry["mime"])
    entry.setdefault("name", "p0001.jpg")
    return entry


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
        ("deck.pptx", "office"),
        ("noext", "none"),
        (".png", "none"),
    ],
)
def test_the_kind_of_a_file_is_told_by_its_lower_cased_suffix(name: str, kind: str) -> None:
    """A file is an image, a PDF or neither by its suffix alone, in any case."""
    assert renders.kind_of(name) == kind


@pytest.mark.parametrize("name", ["Deck.PPTX", "master.potx", "report.docx", "form.dotx", "notes.odt", "Notes.ODT"])
def test_an_office_file_is_told_by_its_lower_cased_suffix(name: str) -> None:
    """A text document, a deck or a template of either is kind office, in any case."""
    assert renders.kind_of(name) == "office"


@pytest.mark.parametrize("name", ["sheet.xlsx", "book.ods", "show.odp", "old.doc", "old.ppt", "memo.rtf"])
def test_the_office_types_left_out_are_none(name: str) -> None:
    """A spreadsheet, an ODF presentation and the old binary formats are not drawn as pages."""
    assert renders.kind_of(name) == "none"


def test_the_office_suffixes_are_the_five_and_the_slide_ones_are_two_of_them() -> None:
    """The decks are a subset of the office suffixes."""
    assert renders.OFFICE_SUFFIXES == {".docx", ".dotx", ".pptx", ".potx", ".odt"}
    assert renders.SLIDE_SUFFIXES == {".pptx", ".potx"}
    assert renders.SLIDE_SUFFIXES < renders.OFFICE_SUFFIXES


@pytest.mark.parametrize(
    ("name", "unit"),
    [("a.POTX", "slide"), ("a.pptx", "slide"), ("a.odt", "page"), ("a.docx", "page"), ("a.dotx", "page")],
)
def test_a_deck_is_drawn_by_slide_and_a_document_by_page(name: str, unit: str) -> None:
    """The unit is told by the lower-cased suffix: a slide for a deck or its template, else a page."""
    assert renders.unit_of(name) == unit


@pytest.mark.parametrize("name", ["scan.pdf", "photo.png", "noext"])
def test_what_is_not_a_deck_is_drawn_by_page(name: str) -> None:
    """A PDF, a picture and a file of no kind count by page, so ``unit`` is always an answer."""
    assert renders.unit_of(name) == "page"


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
        {"role": "strip"},
        {"extra": "x"},
    ],
)
def test_a_render_entry_refuses_what_is_out_of_bounds(fields: dict[str, Any]) -> None:
    """An entry is refused for a bad strip, a number, a size, a type, a role or a key it does not declare."""
    with pytest.raises(ValidationError):
        renders.RenderEntry(**_entry(**fields))


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "../p0001.jpg"},
        {"name": "../../etc/passwd"},
        {"name": "/p0001.jpg"},
        {"name": "p0001.png"},
        {"name": "p0002.jpg"},
        {"name": "p0001-s1of2.jpg"},
        {"name": "p0001.jpg" + "x" * 1_000_000},
        {"name": "p0003-s1of2.png", "n": 3, "mime": "image/jpeg", "strips": 2},
    ],
)
def test_a_render_entry_refuses_a_name_that_is_not_the_one_render_name_gives(fields: dict[str, Any]) -> None:
    """A path, a name of another page, strip or type, and a very long name are all refused."""
    with pytest.raises(ValidationError):
        renders.RenderEntry(**_entry(**fields))


def test_a_render_entry_takes_the_name_render_name_gives() -> None:
    """A whole page and a strip are accepted under their own names."""
    assert renders.RenderEntry(**_entry(name="p0001.jpg")).name == "p0001.jpg"
    strip = renders.RenderEntry(**_entry(name="p0003-s1of2.png", n=3, strips=2, mime="image/png"))
    assert strip.name == "p0003-s1of2.png"


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


def test_facts_a_v07_head_kept_without_a_unit_still_read_as_pages() -> None:
    """The five keys v0.7 kept read, and the unit is a page."""
    facts = renders.RenderFacts.model_validate(_facts())
    assert facts.unit == "page"


def test_facts_of_a_deck_say_slide() -> None:
    """An Office file's facts take kind office and unit slide."""
    facts = renders.RenderFacts(**_facts(kind="office", unit="slide"))
    assert (facts.kind, facts.unit) == ("office", "slide")


@pytest.mark.parametrize("fields", [{"unit": "sheet"}, {"kind": "office", "unit": "sheet"}, {"unit": None}])
def test_facts_refuse_a_unit_that_is_not_a_page_or_a_slide(fields: dict[str, Any]) -> None:
    """A unit other than page or slide is refused."""
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


def test_the_converted_pdf_has_one_name_one_type_and_a_cap() -> None:
    """A converted PDF is ``converted.pdf``, a PDF by its signature, and at most 32 MiB."""
    assert renders.CONVERTED_NAME == "converted.pdf"
    assert renders.CONVERTED_MIME == "application/pdf"
    assert renders.CONVERTED_SIGNATURE == b"%PDF-"
    assert renders.CONVERTED_MAX_BYTES == 32 * 1024 * 1024


def test_a_convert_answer_takes_a_deck_and_a_document() -> None:
    """A document has pages only; a deck has the reader's slide count too."""
    assert renders.ConvertAnswer(name="converted.pdf", pages=12).slides is None
    assert renders.ConvertAnswer(name="converted.pdf", pages=12, slides=12).slides == 12


@pytest.mark.parametrize(
    "fields",
    [
        {"name": "a.pdf"},
        {"name": "../converted.pdf"},
        {"pages": 0},
        {"slides": 0},
        {"extra": "x"},
    ],
)
def test_a_convert_answer_refuses_what_is_not_the_converted_pdf(fields: dict[str, Any]) -> None:
    """The answer names only ``converted.pdf``, has a page, and has no stray key."""
    with pytest.raises(ValidationError):
        renders.ConvertAnswer.model_validate({"name": "converted.pdf", "pages": 3} | fields)


def test_an_office_request_takes_a_unit_and_the_slide_count() -> None:
    """The request carries the unit, and a deck's slide count when the reader had one."""
    assert renders.OfficeRequest(unit="page").slides is None
    request = renders.OfficeRequest.model_validate({"unit": "slide", "slides": 24})
    assert (request.unit, request.slides) == ("slide", 24)


@pytest.mark.parametrize("fields", [{"unit": "sheet"}, {"unit": "frame"}, {"slides": 0}, {"extra": "x"}])
def test_an_office_request_refuses_what_is_out_of_bounds(fields: dict[str, Any]) -> None:
    """A request is refused for another unit (a frame is a clip's), no slide or a stray key."""
    with pytest.raises(ValidationError):
        renders.OfficeRequest.model_validate({"unit": "slide"} | fields)


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


# --- clips ---------------------------------------------------------------------------------------------------------


def _media_facts(**fields: Any) -> dict[str, Any]:
    """Build the fields of the facts of a clip with no frames, with the given ones on top.

    Args:
        **fields: What differs from the facts of a clip with neither pictures nor frames.

    Returns:
        The fields, to pass as keyword arguments to :class:`renders.RenderFacts`.
    """
    base: dict[str, Any] = {
        "kind": "media",
        "unit": "frame",
        "total": 0,
        "pages_without_text": [],
        "frames": None,
        "hint": None,
    }
    return base | fields


def test_clips_are_media() -> None:
    """A video or audio suffix, in any case, makes a clip; a format workbench does not take stays none."""
    assert renders.kind_of("Harbour-Tour.MP4") == "media"
    assert renders.kind_of("voice-memo.opus") == "media"
    assert renders.kind_of("clip.avi") == "none"
    assert len(renders.MEDIA_SUFFIXES) == 11
    assert set(renders.MEDIA_TYPES) == renders.MEDIA_SUFFIXES
    assert renders.MEDIA_SUFFIXES == renders.VIDEO_SUFFIXES | renders.AUDIO_SUFFIXES
    assert not renders.VIDEO_SUFFIXES & renders.AUDIO_SUFFIXES


def test_the_media_types_are_the_spec_s() -> None:
    """Each suffix of a clip has the one type the spec gives it."""
    assert dict(renders.MEDIA_TYPES) == {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".m4v": "video/x-m4v",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".opus": "audio/opus",
        ".flac": "audio/flac",
    }


def test_the_bounds_of_a_clip_are_the_spec_s() -> None:
    """The constants of a clip are the numbers of the spec."""
    assert (renders.FRAME_LONG_SIDE, renders.FRAME_QUALITY) == (1280, 85)
    assert renders.MEDIA_FRAMES_MAX == renders.RENDERS_PER_JOB - 1 == 19
    assert renders.JSONL_MAX_BYTES == 8 * 1024 * 1024
    assert renders.ZIP_MAX_BYTES == 32 * 1024 * 1024
    assert renders.TRANSCRIPT_MAX_BYTES == 4 * 1024 * 1024
    assert renders.TRANSCRIPT_NAME == "transcript.txt"
    assert renders.NOTHING_FOUND == (
        "Nothing was found in this clip: no speech and no pictures, or it has not been transcribed."
    )
    assert renders.NO_SPEECH == "No speech was found in this clip."


def test_stamps() -> None:
    """A time reads as minutes and seconds, with hours from one hour on; a negative time is zero."""
    assert renders.stamp(84.9) == "01:24"
    assert renders.stamp(0) == "00:00"
    assert renders.stamp(3599) == "59:59"
    assert renders.stamp(3600) == "1:00:00"
    assert renders.stamp(3723.4) == "1:02:03"
    assert renders.stamp(-2) == "00:00"
    assert renders.stamp(math.nan) == "00:00"
    assert renders.parse_stamp("01:24") == 84
    assert renders.parse_stamp("1:02:03") == 3723
    assert renders.parse_stamp("75:00") == 4500
    assert renders.parse_stamp("00:00") == 0
    assert renders.parse_stamp("99:59") == 5999
    assert renders.parse_stamp("100:00") == 6000
    assert renders.parse_stamp("5999:59") == 5999 * 60 + 59
    assert renders.parse_stamp("0:01:24") == 84
    arabic_indic = "\u0660\u0661:\u0662\u0664"  # digits that are not ASCII
    for text in (
        "1:60",
        "1:02:60",
        "1:60:03",
        "6000:00",
        "x",
        "",
        "1",
        "1:2",
        "-1:00",
        "1:00:00:00",
        " 01:24",
        arabic_indic,
    ):
        assert renders.parse_stamp(text) is None, text


@pytest.mark.parametrize("seconds", [0, 1, 59, 60, 84, 3599, 3600, 3723, 86399])
def test_a_stamp_parses_back_to_its_whole_seconds(seconds: int) -> None:
    """What ``stamp`` writes, ``parse_stamp`` reads, up to a day."""
    assert renders.parse_stamp(renders.stamp(seconds)) == seconds


def test_frame_label() -> None:
    """A frame is told by its number and, when the zip had times, its time."""
    assert renders.frame_label(3, 84.0) == "frame 3 (01:24)"
    assert renders.frame_label(3, None) == "frame 3"
    assert renders.frame_label(12, 3723.0) == "frame 12 (1:02:03)"


def test_media_facts() -> None:
    """A clip's facts take a total of zero, and no duration; the others refuse one, or a unit of another kind."""
    facts = renders.RenderFacts(**_media_facts())
    assert (facts.kind, facts.unit, facts.total, facts.duration_s) == ("media", "frame", 0, None)
    assert renders.RenderFacts(**_media_facts(total=12, duration_s=134.2)).duration_s == 134.2
    with pytest.raises(ValidationError):
        renders.RenderFacts(**_facts(total=0))
    with pytest.raises(ValidationError):
        renders.RenderFacts(**_media_facts(unit="page"))
    with pytest.raises(ValidationError):
        renders.RenderFacts(**_facts(unit="frame"))
    with pytest.raises(ValidationError):
        renders.RenderFacts(**_media_facts(frames=3))
    with pytest.raises(ValidationError):
        renders.RenderFacts(**_media_facts(pages_without_text=[1]))
    with pytest.raises(ValidationError):
        renders.RenderFacts(**_media_facts(duration_s=-1.0))
    with pytest.raises(ValidationError):
        renders.RenderFacts(**_media_facts(duration_s=math.inf))
    assert renders.RenderFacts(**_media_facts(duration_s=renders.MEDIA_SECONDS_MAX)).duration_s == 360000.0
    with pytest.raises(ValidationError):
        renders.RenderFacts(**_media_facts(duration_s=renders.MEDIA_SECONDS_MAX + 1))
    stage_one = {"kind": "pdf", "total": 24, "pages_without_text": [3], "frames": None, "hint": None}
    facts = renders.RenderFacts.model_validate(stage_one)
    assert (facts.unit, facts.duration_s) == ("page", None)


def test_a_clip_s_facts_take_a_hint() -> None:
    """A clip's facts carry the same hint as any others."""
    facts = renders.RenderFacts(**_media_facts(hint=renders.NOTHING_FOUND))
    assert facts.hint == renders.NOTHING_FOUND


def test_frame_entries_carry_their_time() -> None:
    """Only a frame has a time, and a frame is never a strip; a transcript and a media request are strict."""
    name = renders.render_name(1, 1, 1, "image/jpeg")
    frame = renders.RenderEntry(**_entry(role="frame", name=name, time_s=4.0, width=1280, height=720))
    assert (frame.role, frame.time_s) == ("frame", 4.0)
    assert renders.RenderEntry(**_entry(role="frame", name=name)).time_s is None
    with pytest.raises(ValidationError):
        renders.RenderEntry(**_entry(role="page", time_s=4.0))
    with pytest.raises(ValidationError):
        renders.RenderEntry(**_entry(role="image", time_s=4.0))
    with pytest.raises(ValidationError):
        renders.RenderEntry(**_entry(role="frame", strips=2))
    with pytest.raises(ValidationError):
        renders.RenderEntry(**_entry(role="frame", time_s=-0.5))
    with pytest.raises(ValidationError):
        renders.RenderEntry(**_entry(role="frame", time_s=math.nan))
    assert renders.RenderEntry(**_entry(role="frame", time_s=renders.MEDIA_SECONDS_MAX)).time_s == 360000.0
    with pytest.raises(ValidationError):  # 100 hours is the latest a clip's time may be: no label is longer than that
        renders.RenderEntry(**_entry(role="frame", time_s=renders.MEDIA_SECONDS_MAX + 1))
    with pytest.raises(ValidationError):
        renders.RenderEntry(**_entry(role="frame", time_s=1e300))
    with pytest.raises(ValidationError):
        renders.RenderEntry(**_entry(role="frame", name="../p0001.jpg"))
    assert renders.RenderEntry(**_entry()).time_s is None  # a page's entry is as it was


def test_a_transcript_entry_names_only_the_transcript() -> None:
    """The one file, and at least a line."""
    assert renders.TranscriptEntry(name="transcript.txt", lines=3).lines == 3
    for fields in ({"name": "notes.txt"}, {"name": "../transcript.txt"}, {"lines": 0}, {"extra": "x"}):
        with pytest.raises(ValidationError):
            renders.TranscriptEntry.model_validate({"name": "transcript.txt", "lines": 1} | fields)


def test_a_media_request_takes_the_two_artifacts() -> None:
    """Neither artifact is required; the clip says whether it is a video; a stray key is refused."""
    request = renders.MediaRequest(video=True)
    assert (request.nextext_jsonl, request.nextext_zip, request.video) == (None, None, True)
    both = renders.MediaRequest(nextext_jsonl=".derived/a/b.jsonl", nextext_zip=".derived/a/c.zip", video=False)
    assert both.nextext_zip == ".derived/a/c.zip"
    with pytest.raises(ValidationError):
        renders.MediaRequest.model_validate({"video": True, "extra": "x"})
    with pytest.raises(ValidationError):
        renders.MediaRequest.model_validate({})


def test_an_answer_may_carry_a_transcript() -> None:
    """The answer of a clip lists its frames and, when it has one, its transcript."""
    name = renders.render_name(1, 1, 1, "image/jpeg")
    frame = _entry(role="frame", name=name, time_s=0.0, width=1280, height=720)
    answer = renders.RenderAnswer(
        **_media_facts(total=1, duration_s=9.0),
        renders=[frame],
        transcript=renders.TranscriptEntry(name="transcript.txt", lines=4),
    )
    assert answer.transcript is not None and answer.transcript.lines == 4
    assert renders.RenderAnswer(**_facts(), renders=[_entry()]).transcript is None
