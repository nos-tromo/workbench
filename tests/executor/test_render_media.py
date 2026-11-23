"""A clip's transcript and frames, made in the executor from Nextext's two artifacts (``render/media.py``).

Every artifact is built at test time with invented content (``tests/fixtures/make_media.py``). The clip itself is never
a file here, as it is never one in a job.
"""

import io
import json
import time
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, JpegImagePlugin

from tests.conftest import scaled
from tests.fixtures import make_media
from workbench.executor import readers
from workbench.executor.readers import DAMAGED, UnreadableFile
from workbench.executor.render import media
from workbench.executor.render.media import NOT_KEYFRAMES
from workbench.executor.runners import RUNNERS, JobContext
from workbench.renders import (
    FRAME_LONG_SIDE,
    MEDIA_FRAMES_MAX,
    NO_SPEECH,
    NOTHING_FOUND,
    RENDER_MAX_BYTES,
    RenderAnswer,
    signature_ok,
    stamp,
)

CHAT = "chat-1"
SOURCE_ID = "0123456789abcdef0123456789abcdef"
JSONL = f".derived/{SOURCE_ID}/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.jsonl"
ZIP = f".derived/{SOURCE_ID}/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.zip"
HELLO = [
    make_media.jsonl_record(0.0, 2.5, "Welcome aboard.", speaker="Speaker 1", index=0),
    make_media.jsonl_record(84.2, 86.0, "The quay opens at nine.", speaker="Speaker 2", index=1),
    make_media.jsonl_record(90.0, 91.0, "Thanks.", index=2),
]


@pytest.fixture
def ctx(tmp_path: Path) -> JobContext:
    """An empty job folder for ``chat-1``, as the supervisor makes it, with the room for the artifacts' folder."""
    work = tmp_path / "data" / "jobs" / SOURCE_ID / "workspace" / CHAT
    (work / "out").mkdir(parents=True)
    (work / ".derived" / SOURCE_ID).mkdir(parents=True)
    return JobContext(work_dir=work, out_dir=work / "out", chat_id=CHAT, max_chars=16 * 1024, file_mb=50)


def _jsonl(ctx: JobContext, records: Sequence[dict[str, Any]] = HELLO) -> Path:
    """Put the transcript's artifact where the api puts it for a job."""
    return make_media.make_jsonl(ctx.work_dir / JSONL, records)


def _zip(ctx: JobContext, **options: Any) -> Path:
    """Put the keyframes artifact where the api puts it for a job."""
    return make_media.make_keyframes_zip(ctx.work_dir / ZIP, **options)


def _prepare(
    ctx: JobContext,
    *,
    jsonl: bool = True,
    keyframes: bool = True,
    video: bool = True,
    name: str = "harbour-tour.mp4",
    **extra: Any,
) -> dict[str, Any]:
    """Run ``prepare_file`` on a clip whose artifacts are given by their paths, as the api asks for it."""
    request = {"nextext_jsonl": JSONL if jsonl else None, "nextext_zip": ZIP if keyframes else None, "video": video}
    args: dict[str, Any] = {"path": name, "pages": None, "strips": 1, "have": [], "budget_s": 60, **extra}
    args["media"] = request
    return RUNNERS["prepare_file"](args, ctx)


def _written(ctx: JobContext) -> list[str]:
    """The names in ``out/``."""
    return sorted(path.name for path in ctx.out_dir.iterdir())


def _transcript(tmp_path: Path, records: Sequence[dict[str, Any]]) -> media.Transcript:
    """Write records to a file and read them as a transcript."""
    return media.read_transcript(make_media.make_jsonl(tmp_path / "docint.jsonl", records))


def _close(pixel: object, want: tuple[int, ...], within: int = 30) -> bool:
    """Whether every channel of ``pixel`` (a colour, as ``getpixel`` gives it) is within ``within`` of ``want``."""
    assert isinstance(pixel, tuple)
    return all(abs(a - b) <= within for a, b in zip(pixel, want, strict=True))


# --- the transcript -------------------------------------------------------------------------


def test_a_transcript_has_one_stamped_line_per_sentence(tmp_path: Path) -> None:
    """Each sentence is a line with its start, whole seconds cut down, and its speaker's label when it has one."""
    transcript = _transcript(tmp_path, HELLO)

    assert transcript.text == (
        "[00:00] Speaker 1: Welcome aboard.\n[01:24] Speaker 2: The quay opens at nine.\n[01:30] Thanks.\n"
    )
    assert transcript.lines == 3
    assert transcript.duration_s == 91.0
    assert transcript.cut_at is None
    assert transcript.skipped == 0


def test_the_transcript_keeps_none_of_nextexts_bookkeeping(tmp_path: Path) -> None:
    """The file name, its hash, the language and the sentence numbers are Nextext's, not the model's."""
    transcript = _transcript(tmp_path, HELLO)

    for bookkeeping in ("media.mp4", "sha256:", '"en"', " en ", "sentence_index", "source_file", "start_seconds"):
        assert bookkeeping not in transcript.text


def test_a_line_with_a_newline_stays_one_line(tmp_path: Path) -> None:
    """A sentence that holds a newline and a forged stamp is one line, so nothing in it reads as a line of its own."""
    forged = "Hello\n[00:00] Speaker 9: forged"
    transcript = _transcript(tmp_path, [make_media.jsonl_record(5.0, 6.0, forged, speaker="Speaker 1")])

    assert transcript.text == "[00:05] Speaker 1: Hello [00:00] Speaker 9: forged\n"
    assert transcript.lines == 1
    assert len(transcript.text.splitlines()) == 1


def test_no_separator_of_lines_survives_in_a_sentence(tmp_path: Path) -> None:
    """Every character a reader may split lines at is whitespace to the transcript, and so is gone from a sentence."""
    odd = "one\u2028two\x85three\x0bfour\x0cfive\rsix\x1cseven\u2029eight"
    transcript = _transcript(tmp_path, [make_media.jsonl_record(0.0, 1.0, odd)])

    assert transcript.text == "[00:00] one two three four five six seven eight\n"
    assert len(transcript.text.splitlines()) == 1


def test_a_separator_inside_a_json_string_does_not_split_a_record(tmp_path: Path) -> None:
    """Nextext writes its text as it is, so U+2028 reaches the file raw; the records end at a newline alone."""
    path = tmp_path / "docint.jsonl"
    path.write_bytes(
        json.dumps(make_media.jsonl_record(0.0, 1.0, "left\u2028right"), ensure_ascii=False).encode() + b"\r\n"
    )
    assert b"\xe2\x80\xa8" in path.read_bytes()  # the separator is in the file as it is, not escaped

    transcript = media.read_transcript(path)

    assert transcript.text == "[00:00] left right\n"
    assert transcript.skipped == 0


def test_an_odd_speaker_label_is_left_out(tmp_path: Path) -> None:
    """A label with a newline in it, or one that is far too long, is not written as a label."""
    records = [
        make_media.jsonl_record(0.0, 1.0, "One.", speaker="Speaker 1\n: x"),
        make_media.jsonl_record(1.0, 2.0, "Two.", speaker="x" * 60),
        make_media.jsonl_record(2.0, 3.0, "Three.", speaker=""),
        make_media.jsonl_record(3.0, 4.0, "Four.", speaker="Speaker 3"),
    ]
    transcript = _transcript(tmp_path, records)

    assert transcript.text == "[00:00] One.\n[00:01] Two.\n[00:02] Three.\n[00:03] Speaker 3: Four.\n"


def test_a_label_that_is_not_a_string_is_left_out(tmp_path: Path) -> None:
    """A speaker that is a number or a list says nothing."""
    records = [make_media.jsonl_record(0.0, 1.0, "One."), make_media.jsonl_record(1.0, 2.0, "Two.")]
    records[0]["speaker"] = 7
    records[1]["speaker"] = ["Speaker 1"]

    assert _transcript(tmp_path, records).text == "[00:00] One.\n[00:01] Two.\n"


def test_past_an_hour_the_stamp_has_hours(tmp_path: Path) -> None:
    """A start of 3723 seconds is ``[1:02:03]``, and one just short of an hour is still ``mm:ss``."""
    records = [
        make_media.jsonl_record(3599.9, 3600.0, "Almost."),
        make_media.jsonl_record(3723.0, 3724.0, "Later."),
    ]
    transcript = _transcript(tmp_path, records)

    assert transcript.text == "[59:59] Almost.\n[1:02:03] Later.\n"
    assert transcript.duration_s == 3724.0


def test_a_long_transcript_is_cut_on_a_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the cap the text stops at a whole line, says where, and still knows how long the clip is."""
    monkeypatch.setattr(media, "TRANSCRIPT_MAX_BYTES", 100)
    records = [make_media.jsonl_record(float(i * 10), float(i * 10 + 5), "Sentence number here.") for i in range(8)]

    transcript = _transcript(tmp_path, records)

    assert transcript.text.endswith("\n")
    assert len(transcript.text.encode()) <= 100
    assert transcript.lines == len(transcript.text.splitlines()) < 8
    assert transcript.cut_at == float(transcript.lines * 10)
    assert transcript.duration_s == 75.0  # the last record's end, past the cut as well


def test_a_first_line_over_the_cap_leaves_no_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When not even the first line fits there is nothing to keep, and the file is no transcript."""
    monkeypatch.setattr(media, "TRANSCRIPT_MAX_BYTES", 10)

    with pytest.raises(UnreadableFile):
        _transcript(tmp_path, [make_media.jsonl_record(0.0, 1.0, "A sentence longer than ten bytes.")])


def test_records_without_times_or_text_are_skipped(tmp_path: Path) -> None:
    """A record with no usable time, or no text left once blanks are gone, is counted and left out."""
    records = [
        make_media.jsonl_record(0.0, 1.0, "Kept."),
        {"text": "No times."},
        make_media.jsonl_record(2.0, 3.0, "   \n  "),
        make_media.jsonl_record(4.0, 5.0, "Also kept."),
    ]
    transcript = _transcript(tmp_path, records)

    assert transcript.text == "[00:00] Kept.\n[00:04] Also kept.\n"
    assert transcript.skipped == 2


@pytest.mark.parametrize(
    "bad",
    [True, False, "3", None, [3], float("nan"), float("inf"), 10**400, -1, -0.5, 360_001, 1e300],
    ids=[
        "true",
        "false",
        "string",
        "null",
        "list",
        "nan",
        "inf",
        "huge-int",
        "minus-1",
        "minus-half",
        "100h-and-a-s",
        "1e300",
    ],
)
def test_a_time_that_is_no_time_of_a_clip_skips_the_record(tmp_path: Path, bad: object) -> None:
    """A boolean is not a number of seconds, nor is a string; no time is infinite, below 0, or past 100 hours."""
    path = tmp_path / "docint.jsonl"
    record = make_media.jsonl_record(0.0, 1.0, "Odd.")
    record["start_seconds"] = bad
    path.write_text(json.dumps(record) + "\n" + json.dumps(make_media.jsonl_record(2.0, 3.0, "Fine.")) + "\n")

    transcript = media.read_transcript(path)

    assert transcript.text == "[00:02] Fine.\n"
    assert transcript.skipped == 1


def test_a_line_that_is_json_but_no_object_is_skipped(tmp_path: Path) -> None:
    """A number or a list on a line is no record, and no reason to refuse the rest."""
    path = tmp_path / "docint.jsonl"
    path.write_text("[1, 2]\n7\n" + json.dumps(make_media.jsonl_record(0.0, 1.0, "Kept.")) + "\n")

    transcript = media.read_transcript(path)

    assert (transcript.text, transcript.skipped) == ("[00:00] Kept.\n", 2)


def test_blank_lines_are_ignored(tmp_path: Path) -> None:
    """A blank line between records is neither a record nor a skipped one."""
    path = tmp_path / "docint.jsonl"
    path.write_text("\n" + json.dumps(make_media.jsonl_record(0.0, 1.0, "Kept.")) + "\n\n  \n")

    transcript = media.read_transcript(path)

    assert (transcript.lines, transcript.skipped) == (1, 0)


def test_a_byte_order_mark_leading_the_file_is_no_reason_to_refuse_it(tmp_path: Path) -> None:
    """The api reads a first line that a BOM leads, so the executor does too: the transcript is the same."""
    path = make_media.make_jsonl(tmp_path / "docint.jsonl", HELLO)
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())

    transcript = media.read_transcript(path)

    assert transcript == _transcript(tmp_path, HELLO)
    assert transcript.text.startswith("[00:00] Speaker 1: Welcome aboard.\n")


def test_a_lone_surrogate_is_replaced_and_the_text_stays_encodable(tmp_path: Path) -> None:
    """JSON can carry half of a surrogate pair; the transcript is UTF-8, so it cannot, and the sentence is kept."""
    path = tmp_path / "docint.jsonl"
    record = json.dumps(make_media.jsonl_record(0.0, 1.0, "X"), ensure_ascii=False).replace('"X"', '"a\\ud800b"')
    path.write_text(record + "\n")

    transcript = media.read_transcript(path)

    assert transcript.text.startswith("[00:00] a")
    assert transcript.text.endswith("b\n")
    transcript.text.encode("utf-8")


@pytest.mark.parametrize(
    "content",
    [
        b"this is not json\n",
        b'{"start_seconds": 0, "end_seconds": 1, "text": "ok"}\nnot json\n',
        b'{"text": ',
        b"\xff\xfe\x00 not utf-8\n",
        b"[" * 100_000 + b"\n",
        b"",
        b"\n\n",
        b'{"text": "no times"}\n',
        b'{"start_seconds": 0, "end_seconds": 1, "text": "   "}\n',
    ],
    ids=[
        "text",
        "a-bad-second-line",
        "truncated",
        "not-utf8",
        "deeply-nested",
        "empty",
        "blank",
        "no-times",
        "no-words",
    ],
)
def test_a_jsonl_that_is_not_json_lines_is_unreadable(tmp_path: Path, content: bytes) -> None:
    """A line that is no JSON, a file that is no UTF-8, or one with no sentence in it, is no transcript."""
    path = tmp_path / "docint.jsonl"
    path.write_bytes(content)

    with pytest.raises(UnreadableFile) as caught:
        media.read_transcript(path)

    assert str(caught.value) == DAMAGED


def test_a_jsonl_over_the_cap_is_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The api keeps the cap; the executor holds to it as well, and reads nothing of a larger file."""
    monkeypatch.setattr(media, "JSONL_MAX_BYTES", 50)
    path = make_media.make_jsonl(tmp_path / "docint.jsonl", HELLO)

    with pytest.raises(UnreadableFile):
        media.read_transcript(path)


# --- the frames -----------------------------------------------------------------------------


def test_frames_are_scaled_to_1280_without_metadata(tmp_path: Path) -> None:
    """Three 4K frames come out 1280 by 720, under the cap, in time order, each with its time and none of its EXIF."""
    keyframes = make_media.make_keyframes_zip(tmp_path / "keyframes.zip", size=(3840, 2160), exif=True)
    assert keyframes.read_bytes().count(b"Exif") == 3  # every source frame does carry its EXIF block
    assert keyframes.read_bytes().count(make_media.CAMERA_MODEL.encode()) == 3

    frames, held = media.read_frames(keyframes, file_mb=50)

    assert held == 3
    assert [frame.n for frame in frames] == [1, 2, 3]
    assert [frame.time_s for frame in frames] == [0.0, 4.0, 8.0]
    for index, frame in enumerate(frames):
        assert (frame.width, frame.height, frame.mime) == (1280, 720, "image/jpeg")
        assert signature_ok(frame.data, frame.mime)
        assert len(frame.data) < RENDER_MAX_BYTES
        assert b"Exif" not in frame.data
        assert make_media.CAMERA_MODEL.encode() not in frame.data
        decoded = Image.open(io.BytesIO(frame.data))
        assert decoded.size == (1280, 720)
        assert not decoded.getexif()
        assert not {"exif", "icc_profile", "xmp"} & set(decoded.info)
        assert _close(decoded.convert("RGB").getpixel((640, 360)), make_media.frame_colour(index))


def test_frames_are_made_at_the_spec_quality_and_subsampling(tmp_path: Path) -> None:
    """The frames are 4:2:0 JPEGs, as pages are, at the frame's long side."""
    keyframes = make_media.make_keyframes_zip(tmp_path / "keyframes.zip", frames=1, size=(2560, 1440), times=(0.0,))

    (frame,), _ = media.read_frames(keyframes, file_mb=50)

    assert max(frame.width, frame.height) == FRAME_LONG_SIDE
    assert JpegImagePlugin.get_sampling(Image.open(io.BytesIO(frame.data))) == 2  # 4:2:0


def test_a_small_frame_is_never_enlarged(tmp_path: Path) -> None:
    """A frame smaller than the long side stays as it is."""
    keyframes = make_media.make_keyframes_zip(tmp_path / "keyframes.zip", size=(64, 36))

    frames, _ = media.read_frames(keyframes, file_mb=50)

    assert {(frame.width, frame.height) for frame in frames} == {(64, 36)}


def test_frames_without_a_manifest_have_no_times(tmp_path: Path) -> None:
    """A zip with no ``manifest.json`` still gives its frames, in order, with no time on any."""
    keyframes = make_media.make_keyframes_zip(tmp_path / "keyframes.zip", times=None)

    frames, held = media.read_frames(keyframes, file_mb=50)

    assert held == 3
    assert [frame.n for frame in frames] == [1, 2, 3]
    assert [frame.time_s for frame in frames] == [None, None, None]


@pytest.mark.parametrize(
    "manifest",
    [b"not json", b"[]", b'{"frames": 3}', b'{"frames": ["x"]}', b"\xff\xfe", b"[" * 100_000, b" " * (2 * 1024 * 1024)],
    ids=["text", "list", "frames-not-a-list", "entry-not-an-object", "not-utf8", "deeply-nested", "over-1-mib"],
)
def test_a_manifest_that_does_not_read_is_ignored(tmp_path: Path, manifest: bytes) -> None:
    """The times are a courtesy: a manifest that does not read leaves the frames, without times."""
    path = tmp_path / "keyframes.zip"
    make_media.make_keyframes_zip(path, times=None, extra={"manifest.json": manifest})

    frames, held = media.read_frames(path, file_mb=50)

    assert (held, [frame.time_s for frame in frames]) == (3, [None, None, None])


def test_a_time_that_is_no_time_is_left_off_its_frame(tmp_path: Path) -> None:
    """A manifest time that is negative, a string, a boolean or missing leaves its frame none; the rest keep theirs."""
    path = tmp_path / "keyframes.zip"
    make_media.make_keyframes_zip(path, frames=5, times=None)
    manifest = {
        "frames": [
            {"file": "frame_000.jpg", "index": 0, "time_sec": 1.5},
            {"file": "frame_001.jpg", "index": 1, "time_sec": -3},
            {"file": "frame_002.jpg", "index": 2, "time_sec": "4"},
            {"file": "frame_003.jpg", "index": 3, "time_sec": True},
            {"file": "frame_009.jpg", "index": 9, "time_sec": 9},  # a frame the zip does not hold
        ]
    }
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))

    frames, _ = media.read_frames(path, file_mb=50)

    assert [frame.time_s for frame in frames] == [1.5, None, None, None, None]


@pytest.mark.parametrize(
    "times",
    [(5.0, 2.0, 9.0), (0.0, 4.0, 3.9), (9.0, 4.0, 0.0), (0.0, 2.0, 1e-3)],
    ids=["middle-back", "last-back", "all-back", "last-a-hair-back"],
)
def test_times_that_run_backwards_are_not_kept(tmp_path: Path, times: tuple[float, ...]) -> None:
    """A frame is numbered by its place in time; a manifest that says otherwise is dropped whole, like a bad one."""
    keyframes = make_media.make_keyframes_zip(tmp_path / "keyframes.zip", times=times, size=(8, 8))

    frames, held = media.read_frames(keyframes, file_mb=50)

    assert (held, [frame.n for frame in frames]) == (3, [1, 2, 3])
    assert [frame.time_s for frame in frames] == [None, None, None]


def test_times_that_stand_still_or_run_forward_are_kept(tmp_path: Path) -> None:
    """Two frames at one moment are fine: the times never decrease."""
    keyframes = make_media.make_keyframes_zip(tmp_path / "keyframes.zip", times=(1.0, 1.0, 2.5), size=(8, 8))

    frames, _ = media.read_frames(keyframes, file_mb=50)

    assert [frame.time_s for frame in frames] == [1.0, 1.0, 2.5]


def test_the_order_of_times_is_judged_over_every_frame_not_only_those_kept(tmp_path: Path) -> None:
    """A frame left out for length that is out of order still marks the manifest as one to distrust."""
    times = [float(i) for i in range(30)]
    times[1], times[2] = times[2], times[1]  # frames 2 and 3 of 30 are not among the 19 kept
    keyframes = make_media.make_keyframes_zip(tmp_path / "keyframes.zip", frames=30, times=times, size=(8, 8))

    frames, held = media.read_frames(keyframes, file_mb=50)

    assert held == 30
    assert {frame.time_s for frame in frames} == {None}


def test_a_gap_in_the_manifest_does_not_make_the_rest_distrusted(tmp_path: Path) -> None:
    """A frame the manifest says nothing of has no time of its own; the others, in order, keep theirs."""
    path = tmp_path / "keyframes.zip"
    make_media.make_keyframes_zip(path, frames=3, times=None, size=(8, 8))
    manifest = {"frames": [{"file": "frame_000.jpg", "time_sec": 1.0}, {"file": "frame_002.jpg", "time_sec": 2.0}]}
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))

    frames, _ = media.read_frames(path, file_mb=50)

    assert [frame.time_s for frame in frames] == [1.0, None, 2.0]


def test_frames_come_in_the_order_of_their_numbers(tmp_path: Path) -> None:
    """A zip that lists its frames out of order is read in the order of the numbers in their names."""
    path = tmp_path / "keyframes.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for index in (2, 0, 1):
            archive.writestr(f"frame_{index:03d}.jpg", make_media.jpeg((64, 36), make_media.frame_colour(index)))
        archive.writestr(
            "manifest.json",
            json.dumps({"frames": [{"file": f"frame_{i:03d}.jpg", "index": i, "time_sec": i * 5} for i in range(3)]}),
        )

    frames, _ = media.read_frames(path, file_mb=50)

    assert [frame.time_s for frame in frames] == [0.0, 5.0, 10.0]
    for index, frame in enumerate(frames):
        assert _close(
            Image.open(io.BytesIO(frame.data)).convert("RGB").getpixel((32, 18)), make_media.frame_colour(index)
        )


@pytest.mark.parametrize(
    "member",
    [
        "../x.jpg",
        "frame_001.jpg.exe",
        "notes.txt",
        "sub/frame_002.jpg",
        "frame_000.jpg",
        "frame_00.jpg",
        "frame_1.jpg",
        "frame_000.JPG",
        "frame_\u0660\u0660\u0663.jpg",  # Arabic-Indic digits are no digits here
        "frame_003.jpg/",
        "/frame_003.jpg",
        "manifest.json.bak",
        "frame_0000.jpg",
    ],
    ids=lambda member: member.encode("ascii", "backslashreplace").decode(),
)
def test_a_zip_with_any_other_member_is_not_keyframes(tmp_path: Path, member: str) -> None:
    """One member that is not ``frame_NNN.jpg`` or the manifest refuses the whole zip.

    A second ``frame_000.jpg``, a path out of the folder, a double extension and a subfolder are among them.
    """
    path = tmp_path / "keyframes.zip"
    make_media.make_keyframes_zip(path, extra={member: make_media.jpeg_claiming(8, 8)})

    with pytest.raises(UnreadableFile) as caught:
        media.read_frames(path, file_mb=50)

    assert str(caught.value) == NOT_KEYFRAMES


def test_two_frames_with_one_number_are_not_keyframes(tmp_path: Path) -> None:
    """``frame_001.jpg`` and ``frame_0001.jpg`` are one frame twice."""
    path = tmp_path / "keyframes.zip"
    make_media.make_keyframes_zip(path, frames=2, extra={"frame_0001.jpg": make_media.jpeg_claiming(8, 8)})

    with pytest.raises(UnreadableFile, match=NOT_KEYFRAMES):
        media.read_frames(path, file_mb=50)


def test_a_zip_with_no_frame_is_not_keyframes(tmp_path: Path) -> None:
    """A zip of a manifest alone holds no frames."""
    path = tmp_path / "keyframes.zip"
    make_media.make_keyframes_zip(path, frames=0, times=())

    with pytest.raises(UnreadableFile, match=NOT_KEYFRAMES):
        media.read_frames(path, file_mb=50)


def test_a_png_named_as_a_frame_is_refused(tmp_path: Path) -> None:
    """The name says JPEG and the bytes say PNG: the whole zip is refused, whatever a decoder would have made of it."""
    path = tmp_path / "keyframes.zip"
    png = io.BytesIO()
    Image.new("RGB", (16, 16), (0, 0, 255)).save(png, "PNG")
    make_media.make_keyframes_zip(path, frames=2, extra={"frame_002.jpg": png.getvalue()})

    with pytest.raises(UnreadableFile) as caught:
        media.read_frames(path, file_mb=50)

    assert str(caught.value) == NOT_KEYFRAMES


def test_a_frame_claiming_12000_by_12000_is_refused_within_2_s(tmp_path: Path) -> None:
    """A JPEG whose header claims 144 megapixels is refused from its header, with no pixel read."""
    path = tmp_path / "keyframes.zip"
    make_media.make_keyframes_zip(path, frames=2, extra={"frame_002.jpg": make_media.jpeg_claiming(12000, 12000)})

    started = time.monotonic()
    with pytest.raises(UnreadableFile, match="120 megapixels"):
        media.read_frames(path, file_mb=50)

    assert time.monotonic() - started < scaled(2)


def test_a_member_bigger_than_a_zip_may_be_is_refused_unread(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A member that declares more than the zip's own cap is refused before one byte of it is read."""
    monkeypatch.setattr(media, "ZIP_MAX_BYTES", 1000)
    path = tmp_path / "keyframes.zip"
    make_media.make_keyframes_zip(
        path, frames=1, size=(8, 8), times=None, extra={"frame_001.jpg": b"\xff\xd8\xff" * 1000}
    )

    with pytest.raises(UnreadableFile, match=NOT_KEYFRAMES):
        media.read_frames(path, file_mb=50)


def test_a_zip_that_is_not_a_zip_is_unreadable(tmp_path: Path) -> None:
    """Bytes that are no zip, and a zip cut short, are damaged files and say so."""
    path = tmp_path / "keyframes.zip"
    path.write_bytes(b"PK\x03\x04 not really a zip")
    with pytest.raises(UnreadableFile, match=DAMAGED):
        media.read_frames(path, file_mb=50)

    whole = make_media.make_keyframes_zip(tmp_path / "whole.zip").read_bytes()
    path.write_bytes(whole[: len(whole) // 2])
    with pytest.raises(UnreadableFile):
        media.read_frames(path, file_mb=50)


def test_a_frame_that_is_not_a_picture_is_unreadable(tmp_path: Path) -> None:
    """A member with a JPEG's first bytes and nothing after them is damaged."""
    path = tmp_path / "keyframes.zip"
    make_media.make_keyframes_zip(path, frames=1, times=None, extra={"frame_001.jpg": b"\xff\xd8\xff\xe0 nothing else"})

    with pytest.raises(UnreadableFile, match=DAMAGED):
        media.read_frames(path, file_mb=50)


def test_more_frames_than_kept_are_spread_evenly(tmp_path: Path) -> None:
    """Thirty frames give 19, the first and the last among them, in order, and the count held is 30."""
    keyframes = make_media.make_keyframes_zip(
        tmp_path / "keyframes.zip", frames=30, times=[i * 3.0 for i in range(30)], size=(16, 16)
    )

    frames, held = media.read_frames(keyframes, file_mb=50)

    assert held == 30
    assert len(frames) == MEDIA_FRAMES_MAX == 19
    assert [frame.n for frame in frames] == list(range(1, 20))
    positions = [round(i * 29 / 18) for i in range(19)]
    assert positions[0] == 0
    assert positions[-1] == 29
    assert [frame.time_s for frame in frames] == [position * 3.0 for position in positions]
    for frame, position in zip(frames, positions, strict=True):
        assert _close(
            Image.open(io.BytesIO(frame.data)).convert("RGB").getpixel((8, 8)), make_media.frame_colour(position)
        )


@pytest.mark.parametrize("count", [19, 20, 1000])
def test_the_frames_kept_are_distinct_and_end_on_the_last(count: int, tmp_path: Path) -> None:
    """Nineteen frames are all kept; 20 or 1000 keep 19 different ones, from the first to the last."""
    keyframes = make_media.make_keyframes_zip(
        tmp_path / "keyframes.zip", frames=count, times=[float(i) for i in range(count)], size=(8, 8)
    )

    frames, held = media.read_frames(keyframes, file_mb=50)

    assert held == count
    times = [frame.time_s for frame in frames if frame.time_s is not None]
    assert len(times) == 19
    assert times == sorted(set(times))
    assert (times[0], times[-1]) == (0.0, float(count - 1))


def test_only_the_frames_kept_are_decoded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A frame that is not kept is never opened, so a bad one among those left out costs nothing."""
    keyframes = make_media.make_keyframes_zip(tmp_path / "keyframes.zip", frames=30, times=None, size=(8, 8))
    opened: list[int] = []
    real = media.images.normalise

    def spy(source: Any, **kwargs: Any) -> Any:
        opened.append(1)
        return real(source, **kwargs)

    monkeypatch.setattr(media.images, "normalise", spy)

    media.read_frames(keyframes, file_mb=50)

    assert len(opened) == 19


def test_the_deadline_is_looked_at_between_frames(tmp_path: Path) -> None:
    """A spent deadline stops the frames at the next one."""
    keyframes = make_media.make_keyframes_zip(tmp_path / "keyframes.zip", size=(8, 8))

    readers.set_deadline(time.monotonic() - 1)
    try:
        with pytest.raises(readers.ReadTimeout):
            media.read_frames(keyframes, file_mb=50)
    finally:
        readers.set_deadline(None)


# --- the runner -----------------------------------------------------------------------------


def test_prepare_media_answers_frames_and_a_transcript(ctx: JobContext) -> None:
    """Both artifacts give a transcript and three frames; the answer is a RenderAnswer that names no path."""
    _jsonl(ctx)
    _zip(ctx)

    answer = _prepare(ctx)

    result = RenderAnswer.model_validate(answer)
    assert (result.kind, result.unit, result.total) == ("media", "frame", 3)
    assert (result.frames, result.pages_without_text, result.hint) == (None, [], None)
    assert result.transcript is not None
    assert (result.transcript.name, result.transcript.lines) == ("transcript.txt", 3)
    assert [(entry.role, entry.n, entry.time_s) for entry in result.renders] == [
        ("frame", 1, 0.0),
        ("frame", 2, 4.0),
        ("frame", 3, 8.0),
    ]
    assert all((entry.strip, entry.strips, entry.mime) == (1, 1, "image/jpeg") for entry in result.renders)
    assert result.duration_s == 91.0
    assert _written(ctx) == ["p0001.jpg", "p0002.jpg", "p0003.jpg", "transcript.txt"]
    assert (ctx.out_dir / "transcript.txt").read_text() == (
        "[00:00] Speaker 1: Welcome aboard.\n[01:24] Speaker 2: The quay opens at nine.\n[01:30] Thanks.\n"
    )
    for entry in result.renders:
        data = (ctx.out_dir / entry.name).read_bytes()
        assert signature_ok(data, entry.mime)
        assert Image.open(io.BytesIO(data)).size == (entry.width, entry.height)
    text = json.dumps(answer)
    assert "/workspace" not in text
    assert "media.mp4" not in text
    assert "harbour" not in text
    assert str(ctx.work_dir) not in text


def test_the_answer_is_the_renderanswer_as_a_dict(ctx: JobContext) -> None:
    """Every key of the answer is the model's own, each present, as the api reads it back."""
    _jsonl(ctx)
    _zip(ctx)

    answer = _prepare(ctx)

    assert set(answer) == {
        "kind",
        "unit",
        "total",
        "pages_without_text",
        "frames",
        "hint",
        "duration_s",
        "renders",
        "transcript",
    }
    assert answer == RenderAnswer.model_validate(answer).model_dump()


def test_a_bad_transcript_leaves_the_frames(ctx: JobContext) -> None:
    """A jsonl that cannot be read costs the transcript and nothing else, and the hint says so."""
    (ctx.work_dir / JSONL).write_text("this is not json lines\n")
    _zip(ctx)

    result = RenderAnswer.model_validate(_prepare(ctx))

    assert (result.total, result.transcript, result.hint) == (3, None, "The transcript could not be read.")
    assert result.duration_s == 8.0  # the frames still say how long it ran
    assert _written(ctx) == ["p0001.jpg", "p0002.jpg", "p0003.jpg"]


def test_bad_frames_leave_the_transcript(ctx: JobContext) -> None:
    """A zip that cannot be read costs the frames and nothing else, and the hint says so."""
    _jsonl(ctx)
    _zip(ctx, extra={"notes.txt": b"x"})

    result = RenderAnswer.model_validate(_prepare(ctx))

    assert (result.total, result.renders, result.hint) == (0, [], "The frames could not be read.")
    assert result.transcript is not None
    assert result.transcript.lines == 3
    assert result.duration_s == 91.0
    assert _written(ctx) == ["transcript.txt"]


def test_a_hostile_zip_leaves_the_transcript_whatever_its_trick(ctx: JobContext) -> None:
    """A member out of the folder, a PNG as a frame and a header that claims 144 megapixels each give the same."""
    png = io.BytesIO()
    Image.new("RGB", (16, 16), (0, 0, 255)).save(png, "PNG")
    tricks = (
        {"../x.jpg": b"x"},
        {"frame_003.jpg": png.getvalue()},
        {"frame_003.jpg": make_media.jpeg_claiming(12000, 12000)},
    )
    for extra in tricks:
        _jsonl(ctx)
        _zip(ctx, extra=extra)

        result = RenderAnswer.model_validate(_prepare(ctx))

        assert (result.total, result.hint) == (0, "The frames could not be read.")
        assert result.transcript is not None
        assert _written(ctx) == ["transcript.txt"]
        for leftover in ctx.out_dir.iterdir():
            leftover.unlink()


def test_a_manifest_that_runs_backwards_gives_no_times_and_no_duration(ctx: JobContext) -> None:
    """A late time in an out-of-order manifest sets neither a frame's time nor the length of the clip."""
    _jsonl(ctx)
    _zip(ctx, times=(359_000.0, 10.0, 20.0))

    result = RenderAnswer.model_validate(_prepare(ctx))

    assert [entry.time_s for entry in result.renders] == [None, None, None]
    assert result.duration_s == 91.0  # the speech's own end, as the frames say nothing

    _zip(ctx, times=(359_000.0, 10.0, 20.0))
    assert RenderAnswer.model_validate(_prepare(ctx, jsonl=False)).duration_s is None


def test_nothing_usable_is_unreadable_file(ctx: JobContext) -> None:
    """When neither artifact reads, the job fails with a fixed text, and writes nothing."""
    (ctx.work_dir / JSONL).write_text("not json\n")
    (ctx.work_dir / ZIP).write_bytes(b"PK\x03\x04 not a zip")

    answer = _prepare(ctx)

    assert answer == {"error": "unreadable_file: nothing in the clip's artifacts could be read"}
    assert _written(ctx) == []


@pytest.mark.parametrize("which", ["jsonl", "zip"])
def test_a_clip_with_one_artifact_that_does_not_read_is_unreadable(ctx: JobContext, which: str) -> None:
    """With one artifact present and that one bad, nothing usable came from the inputs there are."""
    if which == "jsonl":
        (ctx.work_dir / JSONL).write_text("not json\n")
    else:
        (ctx.work_dir / ZIP).write_bytes(b"PK\x03\x04 not a zip")

    answer = _prepare(ctx, jsonl=which == "jsonl", keyframes=which == "zip")

    assert answer["error"].startswith("unreadable_file: ")


def test_an_artifact_that_is_not_there_is_unreadable_not_a_crash(ctx: JobContext) -> None:
    """A path the job was not given a file at costs that part, as a damaged file does."""
    _zip(ctx)

    result = RenderAnswer.model_validate(_prepare(ctx))  # the jsonl is named and absent

    assert (result.total, result.transcript, result.hint) == (3, None, "The transcript could not be read.")


def _refusing_to_look(monkeypatch: pytest.MonkeyPatch, suffix: str) -> None:
    """Make ``Path.is_file`` fail with ``EACCES`` for the files with a suffix, as a folder the job cannot enter does."""
    real = Path.is_file

    def is_file(self: Path, **kwargs: Any) -> bool:
        if self.suffix == suffix:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file)


@pytest.mark.parametrize("suffix", [".jsonl", ".zip"])
def test_an_artifact_that_cannot_be_looked_at_costs_its_part_and_names_no_path(
    ctx: JobContext, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    """An ``OSError`` of a look at a file is a part that could not be read, not a job that dies quoting its path."""
    _jsonl(ctx)
    _zip(ctx)
    _refusing_to_look(monkeypatch, suffix)

    result = RenderAnswer.model_validate(_prepare(ctx))

    if suffix == ".jsonl":
        assert (result.total, result.transcript, result.hint) == (3, None, "The transcript could not be read.")
    else:
        assert (result.total, result.hint) == (0, "The frames could not be read.")
        assert result.transcript is not None
    text = json.dumps(result.model_dump())
    assert str(ctx.work_dir) not in text
    assert "Permission" not in text


def test_when_nothing_can_be_looked_at_the_error_is_the_fixed_text(
    ctx: JobContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both artifacts out of reach end as ``unreadable_file`` with no path and no ``errno``."""
    _jsonl(ctx)
    _zip(ctx)
    real = Path.is_file

    def is_file(self: Path, **kwargs: Any) -> bool:
        if self.suffix in (".jsonl", ".zip"):
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, **kwargs)

    monkeypatch.setattr(Path, "is_file", is_file)

    answer = _prepare(ctx)

    assert answer == {"error": "unreadable_file: nothing in the clip's artifacts could be read"}


def test_a_zip_check_that_fails_with_an_os_error_is_unreadable_and_names_no_path(
    ctx: JobContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``check_zip`` falls back to opening the file, which can fail as well: that is the frames lost, not the job."""

    def failing(path: Path, file_mb: int) -> None:
        raise PermissionError(13, "Permission denied", str(path))

    _jsonl(ctx)
    _zip(ctx)
    monkeypatch.setattr(readers, "check_zip", failing)

    result = RenderAnswer.model_validate(_prepare(ctx))

    assert (result.total, result.hint) == (0, "The frames could not be read.")
    assert result.transcript is not None
    assert str(ctx.work_dir) not in json.dumps(result.model_dump())

    with pytest.raises(UnreadableFile) as caught:
        media.read_frames(ctx.work_dir / ZIP, file_mb=50)
    assert str(caught.value) == DAMAGED


def test_audio_says_nothing_of_frames(ctx: JobContext) -> None:
    """A voice memo with a transcript only has no frames, and its hint names none."""
    _jsonl(ctx)

    result = RenderAnswer.model_validate(_prepare(ctx, keyframes=False, video=False, name="voice-memo.m4a"))

    assert (result.kind, result.unit, result.total, result.renders) == ("media", "frame", 0, [])
    assert result.transcript is not None
    assert result.hint is None
    assert "frame" not in json.dumps(result.hint or "")
    assert result.duration_s == 91.0
    assert _written(ctx) == ["transcript.txt"]


def test_a_video_without_frames_says_so(ctx: JobContext) -> None:
    """A video that has a transcript and no keyframes says no frames were taken from it."""
    _jsonl(ctx)

    result = RenderAnswer.model_validate(_prepare(ctx, keyframes=False))

    assert (result.total, result.hint) == (0, "No frames were taken from this clip.")
    assert result.transcript is not None


def test_a_clip_with_pictures_and_no_speech_says_so(ctx: JobContext) -> None:
    """A video Nextext heard no speech in has no jsonl: its frames come, and the hint says there is no speech."""
    _zip(ctx)

    result = RenderAnswer.model_validate(_prepare(ctx, jsonl=False))

    assert (result.total, result.transcript, result.hint) == (3, None, NO_SPEECH)
    assert result.duration_s == 8.0


def test_a_clip_with_neither_artifact_is_nothing_found(ctx: JobContext) -> None:
    """The api answers this itself and sends no job; a job that is sent anyway says what the api would."""
    result = RenderAnswer.model_validate(_prepare(ctx, jsonl=False, keyframes=False))

    assert (result.total, result.transcript, result.renders, result.duration_s) == (0, None, [], None)
    assert result.hint == NOTHING_FOUND
    assert _written(ctx) == []


def test_the_clip_itself_is_never_read(ctx: JobContext) -> None:
    """The job folder holds the artifacts and no clip, and the runner answers all the same."""
    _jsonl(ctx)
    _zip(ctx)
    assert not (ctx.work_dir / "harbour-tour.mp4").exists()

    answer = _prepare(ctx, name="harbour-tour.mp4")

    assert "error" not in answer
    assert RenderAnswer.model_validate(answer).total == 3


def test_a_clip_that_is_there_is_not_read_either(ctx: JobContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a file at the clip's own path is never opened: the runner reads the artifacts and nothing else."""
    _jsonl(ctx)
    _zip(ctx)
    clip = ctx.work_dir / "harbour-tour.mp4"
    clip.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    opened: list[str] = []
    real_open = Path.open
    real_read = Path.read_bytes

    def open_spy(self: Path, *args: Any, **kwargs: Any) -> Any:
        opened.append(self.name)
        return real_open(self, *args, **kwargs)

    def read_spy(self: Path) -> bytes:
        opened.append(self.name)
        return real_read(self)

    monkeypatch.setattr(Path, "open", open_spy)
    monkeypatch.setattr(Path, "read_bytes", read_spy)

    _prepare(ctx)

    assert "harbour-tour.mp4" not in opened


def test_duration_is_the_later_of_speech_and_frames(ctx: JobContext) -> None:
    """Whisper's last sentence ends at 18 s in a clip whose last frame is at 27 s: the clip is 27 s long."""
    _jsonl(ctx, [make_media.jsonl_record(10.0, 18.0, "That is all.")])
    _zip(ctx, times=(0.0, 9.0, 27.0))

    result = RenderAnswer.model_validate(_prepare(ctx))

    assert result.duration_s == 27.0
    assert media.read_transcript(ctx.work_dir / JSONL).duration_s == 18.0  # the transcript's own stays the records'


def test_speech_that_outlasts_the_frames_sets_the_duration(ctx: JobContext) -> None:
    """When the last sentence ends after the last frame, that is the length."""
    _jsonl(ctx)
    _zip(ctx)

    assert RenderAnswer.model_validate(_prepare(ctx)).duration_s == 91.0


def test_a_clip_with_neither_a_time_nor_speech_has_no_duration(ctx: JobContext) -> None:
    """Frames with no manifest and no jsonl say nothing of how long the clip is."""
    _zip(ctx, times=None)

    result = RenderAnswer.model_validate(_prepare(ctx, jsonl=False))

    assert (result.total, result.duration_s) == (3, None)


def test_a_cut_transcript_and_spread_frames_say_so_in_order(ctx: JobContext, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both notes are in the hint, the transcript's first, and the hint stays within the model's bound."""
    monkeypatch.setattr(media, "TRANSCRIPT_MAX_BYTES", 100)
    _jsonl(ctx, [make_media.jsonl_record(i * 10.0, i * 10.0 + 5, "Sentence number here.") for i in range(8)])
    _zip(ctx, frames=30, times=[i * 3.0 for i in range(30)], size=(16, 16))

    result = RenderAnswer.model_validate(_prepare(ctx))

    assert result.transcript is not None
    cut = f"The transcript is cut at {stamp(result.transcript.lines * 10.0)}: it is longer than workbench keeps."
    assert result.hint == f"{cut} 19 of the clip's 30 frames are kept, spread evenly."
    assert result.total == 19
    assert len(_written(ctx)) == 20  # 19 frames and the transcript: a job's outputs, no more


def test_the_frames_are_named_as_pages_are(ctx: JobContext) -> None:
    """A frame's file is ``p0001.jpg`` and on, as ``render_name`` makes it, and never cut into strips."""
    _zip(ctx)

    result = RenderAnswer.model_validate(_prepare(ctx, jsonl=False, strips=3))

    assert [entry.name for entry in result.renders] == ["p0001.jpg", "p0002.jpg", "p0003.jpg"]
    assert all(entry.strips == 1 for entry in result.renders)


def test_a_4k_clips_keyframes_stay_within_the_caps(ctx: JobContext) -> None:
    """Nineteen 4K keyframes become 19 frames of 1280 by 720, each under the cap, and a transcript beside them."""
    _jsonl(ctx)
    _zip(ctx, frames=19, times=[i * 2.0 for i in range(19)], size=(3840, 2160))

    result = RenderAnswer.model_validate(_prepare(ctx))

    assert result.total == 19
    assert {(entry.width, entry.height) for entry in result.renders} == {(1280, 720)}
    assert all((ctx.out_dir / entry.name).stat().st_size < RENDER_MAX_BYTES for entry in result.renders)
    assert len(_written(ctx)) == 20


def test_a_spent_budget_is_a_read_timeout(ctx: JobContext) -> None:
    """A job whose time is up at once answers ``read_timeout`` and leaves nothing in ``out/``, deadline cleared."""
    _jsonl(ctx)
    _zip(ctx)

    answer = _prepare(ctx, budget_s=0)

    assert answer["error"].startswith("read_timeout: ")
    assert _written(ctx) == []
    readers.check()  # would raise ReadTimeout if the spent deadline were still set


def test_a_file_that_cannot_be_written_is_a_fixed_error_that_leaves_nothing(
    ctx: JobContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk at the second output ends in a text with no path; every file written, begun ones too, is removed."""
    _jsonl(ctx)
    _zip(ctx)
    real = Path.write_bytes
    written: list[str] = []

    def second_fails(self: Path, data: bytes) -> int:
        if self.parent != ctx.out_dir:
            return real(self, data)
        written.append(self.name)
        if len(written) == 2:
            real(self, data[:10])  # a file that was begun
            raise OSError(28, "No space left on device", str(self))
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", second_fails)

    answer = _prepare(ctx)

    assert answer == {"error": "unreadable_file: the renders could not be written"}
    assert str(ctx.work_dir) not in json.dumps(answer)
    assert len(written) == 2
    assert _written(ctx) == []


def test_the_deadline_is_taken_away_afterwards(ctx: JobContext) -> None:
    """The readers' deadline is the process's: it is cleared however the runner ends."""
    _jsonl(ctx)
    _zip(ctx)

    _prepare(ctx, budget_s=0)
    readers.check()
    _prepare(ctx)
    readers.check()


@pytest.mark.parametrize("bad", ["../escape.jsonl", "/abs/path.jsonl", ".derived/../x.zip", "a//b.zip"])
def test_an_artifact_path_outside_the_chat_folder_is_a_mistake(ctx: JobContext, bad: str) -> None:
    """The artifacts are paths below the chat folder, as an input's are: anything else is the caller's mistake."""
    args = {"path": "harbour-tour.mp4", "media": {"nextext_jsonl": bad, "nextext_zip": None, "video": True}}

    with pytest.raises(ValueError):
        RUNNERS["prepare_file"](args, ctx)


@pytest.mark.parametrize(
    "bad",
    [{}, {"video": "maybe", "nextext_jsonl": None}, {"video": True, "extra": 1}, "text", 7, []],
    ids=["empty", "video-not-a-bool", "unknown-key", "string", "number", "list"],
)
def test_a_media_argument_that_is_not_one_is_a_mistake(ctx: JobContext, bad: object) -> None:
    """``media`` is a ``MediaRequest``; anything else is the caller's mistake, and its text quotes nothing."""
    with pytest.raises(ValueError) as caught:
        RUNNERS["prepare_file"]({"path": "harbour-tour.mp4", "media": bad}, ctx)

    assert "maybe" not in str(caught.value)


def test_media_with_a_file_that_is_no_clip_is_a_mistake(ctx: JobContext) -> None:
    """``media`` names the artifacts of a clip: given with a picture or a PDF it is the caller's mistake."""
    Image.new("RGB", (8, 8)).save(ctx.work_dir / "photo.jpg")
    request = {"nextext_jsonl": None, "nextext_zip": None, "video": False}

    with pytest.raises(ValueError):
        RUNNERS["prepare_file"]({"path": "photo.jpg", "media": request}, ctx)


def test_media_and_office_together_are_a_mistake(ctx: JobContext) -> None:
    """``office`` names a converted PDF, and a clip is none: given with ``media`` it is not silently dropped."""
    request = {"nextext_jsonl": JSONL, "nextext_zip": None, "video": False}

    with pytest.raises(ValueError):
        RUNNERS["prepare_file"](
            {"path": "voice-memo.m4a", "media": request, "office": {"unit": "page"}},
            ctx,
        )


def test_a_clip_without_a_media_argument_is_still_refused(ctx: JobContext) -> None:
    """Task 1's guard stands: with no ``media`` argument a clip is unsupported, never read."""
    (ctx.work_dir / "harbour-tour.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)

    answer = RUNNERS["prepare_file"]({"path": "harbour-tour.mp4"}, ctx)

    assert answer == {"error": "unsupported_format: only pictures and PDFs can be shown"}


def test_a_clip_is_told_by_its_suffix_in_any_case(ctx: JobContext) -> None:
    """The suffix, in any case, decides that a path is a clip's."""
    _jsonl(ctx)
    _zip(ctx)

    assert RenderAnswer.model_validate(_prepare(ctx, name="VOICE-MEMO.MP3")).kind == "media"
