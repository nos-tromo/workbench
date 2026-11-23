"""The bounds and rules of the renders of a picture, a PDF, an Office file or a clip, shared by the api and executor.

A render is a JPEG or PNG a model can look at: a photograph scaled down, or a page of a PDF drawn at a fixed size. An
Office file (a text document or a deck) is converted to a PDF once, and its pages or slides are then drawn from that
PDF as any other. A clip (a video or an audio file) is never decoded here: its transcript and its frames are made from
the two artifacts Nextext produced for it, and a frame is a render too. The executor, which holds the files and the
imaging libraries, makes the renders and answers with a :class:`RenderAnswer`; the api, which has no imaging library,
checks that answer, a render's signature and its size, and never decodes a file. Neither side copies the other: both
import this module, as both import :mod:`workbench.decks`. It imports no imaging library, and nothing of LibreOffice,
so the api's image can load it.
"""

import math
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The extensions of the picture types a render is made from, and of a PDF; both are told by the lower-cased suffix.
IMAGE_SUFFIXES: Final = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif"})
PDF_SUFFIX: Final = ".pdf"

#: The extensions of the Office files that are converted to a PDF and then drawn: text documents and decks, and the
#: templates of either. Spreadsheets, ODF presentations and the old binary formats are left out.
OFFICE_SUFFIXES: Final = frozenset({".docx", ".dotx", ".pptx", ".potx", ".odt"})

#: Those of them that are decks, whose pages are counted as slides.
SLIDE_SUFFIXES: Final = frozenset({".pptx", ".potx"})

#: The extensions of the clips workbench takes: video, and audio. A clip is told by its lower-cased suffix.
VIDEO_SUFFIXES: Final = frozenset({".mp4", ".mov", ".m4v", ".webm", ".mkv"})
AUDIO_SUFFIXES: Final = frozenset({".mp3", ".m4a", ".wav", ".ogg", ".opus", ".flac"})
MEDIA_SUFFIXES: Final = VIDEO_SUFFIXES | AUDIO_SUFFIXES

#: The media type of each clip suffix, one the store serves whatever the host's ``mime.types`` says.
MEDIA_TYPES: Final[Mapping[str, str]] = {
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

#: What a file is to the render tools: a picture, a PDF, an Office file, a clip, or none of them.
Kind = Literal["image", "pdf", "office", "media", "none"]

#: What a render of a PDF page counts: a page of a document, or a slide of a deck.
PagedUnit = Literal["page", "slide"]

#: What a render counts: a page, a slide, or a frame of a clip.
Unit = Literal["page", "slide", "frame"]

#: Most pixels (120 megapixels) of a source picture the executor decodes; a larger one is refused before it is read.
SOURCE_MAX_PIXELS: Final = 120_000_000

#: A picture is rendered to at most this many pixels on its long side and this many in all (2.4 megapixels), as JPEG at
#: ``IMAGE_QUALITY`` (or as PNG, for a PNG, GIF or BMP source, when it fits).
IMAGE_LONG_SIDE: Final = 2048
IMAGE_MAX_PIXELS: Final = 2_400_000
IMAGE_QUALITY: Final = 90

#: A PDF page is drawn to this many pixels on its long side, as JPEG at ``PAGE_QUALITY``.
PAGE_LONG_SIDE: Final = 1600
PAGE_QUALITY: Final = 85

#: The lowest JPEG quality a render is lowered to when it is over ``RENDER_MAX_BYTES``.
QUALITY_FLOOR: Final = 50

#: Every render is strictly smaller than this many bytes.
RENDER_MAX_BYTES: Final = 768 * 1024

#: Pages a prepare renders at once; later ones are rendered when they are viewed.
FIRST_PAGES: Final = 20

#: Most pages of a PDF the executor checks for text, and so most entries of ``pages_without_text``.
PAGES_CHECKED_MAX: Final = 1000

#: Most renders of one job: the supervisor's ``MAX_OUTPUTS``.
RENDERS_PER_JOB: Final = 20

#: Most strips a picture or page is cut into along its long side, and the share of its length two neighbours overlap.
MAX_STRIPS: Final = 3
STRIP_OVERLAP: Final = 0.06

#: Most bytes of renders one source file may have, counted in the user's quota.
DERIVED_MAX_BYTES: Final = 64 * 1024 * 1024

#: A frame of a clip is drawn to this many pixels on its long side, as JPEG at ``FRAME_QUALITY``, strictly under
#: ``RENDER_MAX_BYTES``. A job makes at most ``RENDERS_PER_JOB`` outputs and one of them is the transcript, so a clip
#: keeps at most ``MEDIA_FRAMES_MAX`` frames, spread evenly when its zip holds more.
FRAME_LONG_SIDE: Final = 1280
FRAME_QUALITY: Final = 85
MEDIA_FRAMES_MAX: Final = RENDERS_PER_JOB - 1

#: Most bytes of the two raw artifacts of a clip (Nextext's JSON Lines and its keyframes zip), and of the transcript
#: made from them. The artifacts count toward ``DERIVED_MAX_BYTES``, as the converted PDF of an Office file does.
JSONL_MAX_BYTES: Final = 8 * 1024 * 1024
ZIP_MAX_BYTES: Final = 32 * 1024 * 1024
TRANSCRIPT_MAX_BYTES: Final = 4 * 1024 * 1024

#: The two raw artifacts Nextext made of a clip, which the api keeps as the clip's derived files and only the clip's
#: prepare job reads: each role's media type and file extension, the JSON Lines first (the order a job reads them in).
#: The store, the route and the queue all take the roles, types and extensions from here.
ARTIFACTS: Final[Mapping[str, tuple[str, str]]] = {
    "nextext_jsonl": ("application/x-ndjson", "jsonl"),
    "nextext_zip": ("application/zip", "zip"),
}
ARTIFACT_ROLES: Final = frozenset(ARTIFACTS)

#: The transcript of a clip: its name in the job's output.
TRANSCRIPT_NAME: Final = "transcript.txt"

#: What a manifest says of a clip that has neither artifact (it was not transcribed, or it holds no speech and no
#: pictures), and what a read says of a clip with pictures but no speech.
NOTHING_FOUND: Final = "Nothing was found in this clip: no speech and no pictures, or it has not been transcribed."
NO_SPEECH: Final = "No speech was found in this clip."

#: The PDF an Office file is converted to: its name in the job's output, its type, the first bytes the api checks, and
#: the most bytes it may have (it counts toward ``DERIVED_MAX_BYTES``).
CONVERTED_NAME: Final = "converted.pdf"
CONVERTED_MIME: Final = "application/pdf"
CONVERTED_SIGNATURE: Final = b"%PDF-"
CONVERTED_MAX_BYTES: Final = 32 * 1024 * 1024

#: The first bytes of each type of render; the api checks them without decoding anything.
SIGNATURES: Final[Mapping[str, bytes]] = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n"}

#: The file extension of each type of render.
_EXTENSIONS: Final = {"image/jpeg": "jpg", "image/png": "png"}

#: Added to a scaled side before it is cut to a whole number, so that ``1754 * (1600 / 1754)``, which floating point
#: puts a hair under 1600, still lands on 1600.
_EPSILON: Final = 1e-6

#: A time as a caller writes it: ``mm:ss`` (the minutes may pass 59), or ``h:mm:ss``. Digits are ASCII only.
_STAMP_MINUTES: Final = re.compile(r"([0-9]{1,4}):([0-5][0-9])")
_STAMP_HOURS: Final = re.compile(r"([0-9]{1,4}):([0-5][0-9]):([0-5][0-9])")

#: Most minutes ``mm:ss`` may count: 99 hours and 59 minutes, far past what a clip of the caps can hold.
_STAMP_MINUTES_MAX: Final = 5999


def kind_of(name: str) -> Kind:
    """Tell what a file is to the render tools, by its suffix alone.

    Args:
        name: A file name or path.

    Returns:
        ``"image"`` for a picture suffix, ``"pdf"`` for ``.pdf``, ``"office"`` for a suffix of ``OFFICE_SUFFIXES`` and
        ``"media"`` for one of ``MEDIA_SUFFIXES`` (any of them in any case), else ``"none"``.
    """
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix == PDF_SUFFIX:
        return "pdf"
    if suffix in OFFICE_SUFFIXES:
        return "office"
    if suffix in MEDIA_SUFFIXES:
        return "media"
    return "none"


def unit_of(name: str) -> PagedUnit:
    """Tell what the renders of a file count, by its suffix alone.

    Args:
        name: A file name or path.

    Returns:
        ``"slide"`` for a suffix of ``SLIDE_SUFFIXES`` in any case, else ``"page"``: a picture, a PDF and a file of no
        kind count by page too, so that a unit is always an answer. A clip's facts say ``"frame"`` of their own: that
        is never told by a suffix.
    """
    return "slide" if PurePosixPath(name).suffix.lower() in SLIDE_SUFFIXES else "page"


def signature_ok(data: bytes, mime: str) -> bool:
    """Tell whether a render begins with the signature of its type.

    Args:
        data: The render, or its first bytes.
        mime: The type it claims, ``image/jpeg`` or ``image/png``.

    Returns:
        ``True`` when ``data`` starts with that type's signature; ``False`` for another signature or a type with none.
    """
    signature = SIGNATURES.get(mime)
    return signature is not None and data.startswith(signature)


def target_size(width: int, height: int, *, long_side: int, max_pixels: int | None = None) -> tuple[int, int]:
    """Scale a size down to a long side and, when given, a number of pixels, keeping its ratio.

    Args:
        width: The source width in pixels, at least 1.
        height: The source height in pixels, at least 1.
        long_side: Most pixels of the longer side.
        max_pixels: Most pixels in all; ``None`` for no such cap.

    Returns:
        The new ``(width, height)``, each at least 1. A size already within both caps is returned as it is: this
        never enlarges.
    """
    scale = min(1.0, long_side / max(width, height))
    if max_pixels is not None:
        scale = min(scale, math.sqrt(max_pixels / (width * height)))
    return max(1, int(width * scale + _EPSILON)), max(1, int(height * scale + _EPSILON))


def strip_spans(length: int, strips: int) -> list[tuple[int, int]]:
    """Cut a length into strips that overlap, so a line that falls on a cut is whole in one of them.

    Args:
        length: The length of the long side in pixels.
        strips: How many strips, 1 to ``MAX_STRIPS``.

    Returns:
        ``(start, end)`` for each strip, in order: the first starts at 0, the last ends at ``length``, and two
        neighbours overlap by at least ``STRIP_OVERLAP`` of the length. One strip is the whole length.
    """
    if strips == 1:
        return [(0, length)]
    overlap = round(STRIP_OVERLAP * length)
    size = -(-(length + (strips - 1) * overlap) // strips)
    starts = [i * (length - size) // (strips - 1) for i in range(strips)]
    return [(start, start + size) for start in starts]


def render_name(n: int, strip: int, strips: int, mime: str) -> str:
    """Name a render by its page and strip.

    Args:
        n: The page, or 1 for a picture, counted from 1.
        strip: The strip, counted from 1.
        strips: How many strips the page is cut into.
        mime: The type of the render, ``image/jpeg`` or ``image/png``.

    Returns:
        ``p0003.jpg`` for a whole page, ``p0003-s1of2.png`` for a strip.

    Raises:
        ValueError: ``mime`` is not a type of render.
    """
    extension = _EXTENSIONS.get(mime)
    if extension is None:
        raise ValueError("not a type of render")
    cut = f"-s{strip}of{strips}" if strips > 1 else ""
    return f"p{n:04d}{cut}.{extension}"


def stamp(seconds: float) -> str:
    """Write a time of a clip as a stamp, in whole seconds cut down.

    Args:
        seconds: The time in seconds. A negative or non-finite value counts as 0.

    Returns:
        ``mm:ss``, or ``h:mm:ss`` from one hour on: ``01:24``, ``59:59``, ``1:00:00``, ``1:02:03``.
    """
    whole = int(seconds) if math.isfinite(seconds) and seconds > 0 else 0
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def parse_stamp(text: str) -> float | None:
    """Read a time a caller wrote: the inverse of :func:`stamp`, which also takes minutes past 59.

    Args:
        text: ``mm:ss`` (minutes 0 to 5999, seconds 0 to 59) or ``h:mm:ss`` (minutes and seconds 0 to 59), with no
            blank around it and the seconds, and the minutes of ``h:mm:ss``, in two digits.

    Returns:
        The seconds, or ``None`` for anything else.
    """
    if (hms := _STAMP_HOURS.fullmatch(text)) is not None:
        hours, minutes, secs = (int(part) for part in hms.groups())
        return float(hours * 3600 + minutes * 60 + secs)
    if (ms := _STAMP_MINUTES.fullmatch(text)) is not None:
        minutes, secs = (int(part) for part in ms.groups())
        return float(minutes * 60 + secs) if minutes <= _STAMP_MINUTES_MAX else None
    return None


def frame_label(n: int, time_s: float | None) -> str:
    """Name a frame of a clip as the model reads it.

    Args:
        n: The frame, counted from 1 in time order.
        time_s: The frame's time in seconds; ``None`` when the zip had none.

    Returns:
        ``frame 3 (01:24)``, or ``frame 3`` without a time.
    """
    return f"frame {n}" if time_s is None else f"frame {n} ({stamp(time_s)})"


class _Strict(BaseModel):
    """A model that refuses a field it does not declare, and whose refusal never quotes what it was given."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class RenderEntry(_Strict):
    """One render the executor made: a picture, a page or strip of a PDF, or a frame of a clip.

    Attributes:
        name: The file name of the render in the job's output: always the one :func:`render_name` gives for the other
            fields, so the executor's answer cannot name a path, or anything but a render.
        role: ``image`` for a picture, ``page`` for a page of a PDF, ``frame`` for a frame of a clip.
        n: The page or frame, counted from 1 (a frame in time order); 1 for a picture.
        strip: Which strip of the page this is, counted from 1.
        strips: How many strips the page is cut into; always 1 for a frame, which is never cut.
        width: The render's width in pixels.
        height: The render's height in pixels.
        mime: The render's type.
        time_s: The frame's time in the clip, in seconds; ``None`` for a frame the zip gave no time, and for every
            other role.
    """

    name: str
    role: Literal["image", "page", "frame"]
    n: int = Field(ge=1)
    strip: int = Field(default=1, ge=1, le=MAX_STRIPS)
    strips: int = Field(default=1, ge=1, le=MAX_STRIPS)
    width: int = Field(ge=1, le=IMAGE_LONG_SIDE)
    height: int = Field(ge=1, le=IMAGE_LONG_SIDE)
    mime: Literal["image/jpeg", "image/png"]
    time_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _strip_is_one_of_strips(self) -> Self:
        """Refuse a strip past the last.

        Returns:
            The entry.

        Raises:
            ValueError: ``strip`` is greater than ``strips``.
        """
        if self.strip > self.strips:
            raise ValueError("strip is past the last strip")
        return self

    @model_validator(mode="after")
    def _name_is_the_one_render_name_gives(self) -> Self:
        """Refuse a name that is not the one the executor builds, so a path or a long text never gets through.

        Returns:
            The entry.

        Raises:
            ValueError: ``name`` differs from ``render_name(n, strip, strips, mime)``.
        """
        if self.name != render_name(self.n, self.strip, self.strips, self.mime):
            raise ValueError("name is not the render's own")
        return self

    @model_validator(mode="after")
    def _only_a_frame_has_a_time_and_is_whole(self) -> Self:
        """Refuse a time on anything but a frame, and a frame cut into strips.

        Returns:
            The entry.

        Raises:
            ValueError: ``time_s`` is set on a picture or a page, or a frame has more than one strip.
        """
        if self.time_s is not None and self.role != "frame":
            raise ValueError("only a frame has a time")
        if self.role == "frame" and self.strips != 1:
            raise ValueError("a frame is not cut into strips")
        return self


class ConvertAnswer(_Strict):
    """The runner's whole answer to a conversion job: the PDF it made out of an Office file, which it names.

    Attributes:
        name: The file in the job's output, always ``CONVERTED_NAME``.
        pages: The pages of the PDF, at least 1.
        slides: The slides the reader counted in a deck, at least 1; ``None`` for a text document. A deck whose PDF has
            another number of pages than this is told in the manifest's hint.
    """

    name: Literal["converted.pdf"]
    pages: int = Field(ge=1)
    slides: int | None = Field(default=None, ge=1)


class OfficeRequest(_Strict):
    """The ``office`` argument of a ``prepare_file`` job over a converted PDF: what the PDF stands for.

    Attributes:
        unit: What its pages count, ``slide`` for a deck, else ``page``.
        slides: The slides the reader counted in a deck, at least 1; ``None`` for a text document.
    """

    unit: PagedUnit
    slides: int | None = Field(default=None, ge=1)


class MediaRequest(_Strict):
    """The ``media`` argument of a ``prepare_file`` job over a clip: the artifacts it has, and what the clip is.

    The clip itself never enters the job. Its two artifacts do, at their derived paths.

    Attributes:
        nextext_jsonl: The derived path of Nextext's JSON Lines (the transcript), relative to the chat folder; ``None``
            when the clip has none, which is the case of a clip with no speech.
        nextext_zip: The derived path of Nextext's keyframes zip, relative to the chat folder; ``None`` when the clip
            has none, which is the case of audio.
        video: Whether the clip is a video: a video without a zip has no pictures, and the hint says so.
    """

    nextext_jsonl: str | None = None
    nextext_zip: str | None = None
    video: bool


class TranscriptEntry(_Strict):
    """The transcript the executor made of a clip's JSON Lines: one file, and how many lines it has.

    Attributes:
        name: The file in the job's output, always ``TRANSCRIPT_NAME``.
        lines: The lines of the transcript, at least 1: a clip with no speech has no transcript.
    """

    name: Literal["transcript.txt"]
    lines: int = Field(ge=1)


class RenderFacts(_Strict):
    """What the executor found out about a file besides its renders.

    Attributes:
        kind: ``image``, ``pdf``, ``office`` or ``media``.
        unit: What ``total`` and the pages count: ``slide`` for a deck, ``frame`` for a clip (and only for one), else
            ``page``. A head kept before this was written has none, and reads as ``page``.
        total: Pictures are 1; for a PDF or an Office file, its pages or slides; for a clip, the frames kept, which
            are none for audio and for a clip without pictures.
        pages_without_text: The pages, counted from 1, of a PDF or an Office file with no text of their own, at most
            ``PAGES_CHECKED_MAX`` checked; always empty for a clip.
        frames: The frame count of a picture with several (only the first is rendered); ``None`` otherwise, and for
            every clip, whose frames are ``total``.
        hint: A short note for the model, such as why only some pages were checked; ``None`` for none.
        duration_s: The length of a clip in seconds, as far as its artifacts say; ``None`` for anything else, and for
            a clip they do not say it of. A head kept before this was written has none.
    """

    kind: Literal["image", "pdf", "office", "media"]
    unit: Unit = "page"
    total: int = Field(ge=0)
    pages_without_text: list[int] = Field(max_length=PAGES_CHECKED_MAX)
    frames: int | None = Field(ge=2)
    hint: str | None = Field(max_length=300)
    duration_s: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _only_a_clip_has_no_total(self) -> Self:
        """Refuse a total of zero for anything but a clip.

        Returns:
            The facts.

        Raises:
            ValueError: ``total`` is 0 and the kind is not ``media``.
        """
        if self.total < 1 and self.kind != "media":
            raise ValueError("total is at least 1 but for a clip")
        return self

    @model_validator(mode="after")
    def _a_clip_counts_frames_and_only_a_clip_does(self) -> Self:
        """Refuse a clip that counts anything but frames, a frame count of another kind, and a clip's page facts.

        Returns:
            The facts.

        Raises:
            ValueError: The kind is ``media`` and the unit is not ``frame`` or the other way round, or a clip has
                pages without text or a frame count of a picture.
        """
        if (self.kind == "media") != (self.unit == "frame"):
            raise ValueError("a clip, and only a clip, counts frames")
        if self.kind == "media" and (self.pages_without_text or self.frames is not None):
            raise ValueError("a clip has no pages without text and no multi-frame picture")
        return self


class RenderAnswer(RenderFacts):
    """The runner's whole answer to a render job: the facts and the renders it made.

    Attributes:
        renders: The renders, at most ``RENDERS_PER_JOB``: the frames of a clip, in time order.
        transcript: The transcript made for a clip, which is one more output of the job; ``None`` for anything else
            and for a clip with no speech.
    """

    renders: list[RenderEntry] = Field(max_length=RENDERS_PER_JOB)
    transcript: TranscriptEntry | None = None
