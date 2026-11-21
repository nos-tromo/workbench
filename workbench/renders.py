"""The bounds and rules of the renders of a picture, a PDF or an Office file, shared by the api and the executor.

A render is a JPEG or PNG a model can look at: a photograph scaled down, or a page of a PDF drawn at a fixed size. An
Office file (a text document or a deck) is converted to a PDF once, and its pages or slides are then drawn from that
PDF as any other. The executor, which holds the files and the imaging libraries, makes the renders and answers with a
:class:`RenderAnswer`; the api, which has no imaging library, checks that answer, a render's signature and its size,
and never decodes a file. Neither side copies the other: both import this module, as both import
:mod:`workbench.decks`. It imports no imaging library, and nothing of LibreOffice, so the api's image can load it.
"""

import math
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

#: What a file is to the render tools: a picture, a PDF, an Office file, or none of them.
Kind = Literal["image", "pdf", "office", "none"]

#: What a render of a PDF page counts: a page of a document, or a slide of a deck.
Unit = Literal["page", "slide"]

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


def kind_of(name: str) -> Kind:
    """Tell what a file is to the render tools, by its suffix alone.

    Args:
        name: A file name or path.

    Returns:
        ``"image"`` for a picture suffix, ``"pdf"`` for ``.pdf``, ``"office"`` for a suffix of ``OFFICE_SUFFIXES`` (any
        of them in any case), else ``"none"``.
    """
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix == PDF_SUFFIX:
        return "pdf"
    if suffix in OFFICE_SUFFIXES:
        return "office"
    return "none"


def unit_of(name: str) -> Unit:
    """Tell what the renders of a file count, by its suffix alone.

    Args:
        name: A file name or path.

    Returns:
        ``"slide"`` for a suffix of ``SLIDE_SUFFIXES`` in any case, else ``"page"``: a picture, a PDF and a file of no
        kind count by page too, so that a unit is always an answer.
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


class _Strict(BaseModel):
    """A model that refuses a field it does not declare, and whose refusal never quotes what it was given."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class RenderEntry(_Strict):
    """One render the executor made: a picture, or a page or strip of a PDF.

    Attributes:
        name: The file name of the render in the job's output: always the one :func:`render_name` gives for the other
            fields, so the executor's answer cannot name a path, or anything but a render.
        role: ``image`` for a picture, ``page`` for a page of a PDF.
        n: The page, counted from 1; 1 for a picture.
        strip: Which strip of the page this is, counted from 1.
        strips: How many strips the page is cut into.
        width: The render's width in pixels.
        height: The render's height in pixels.
        mime: The render's type.
    """

    name: str
    role: Literal["image", "page"]
    n: int = Field(ge=1)
    strip: int = Field(default=1, ge=1, le=MAX_STRIPS)
    strips: int = Field(default=1, ge=1, le=MAX_STRIPS)
    width: int = Field(ge=1, le=IMAGE_LONG_SIDE)
    height: int = Field(ge=1, le=IMAGE_LONG_SIDE)
    mime: Literal["image/jpeg", "image/png"]

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

    unit: Unit
    slides: int | None = Field(default=None, ge=1)


class RenderFacts(_Strict):
    """What the executor found out about a file besides its renders.

    Attributes:
        kind: ``image``, ``pdf`` or ``office``.
        unit: What ``total`` and the pages count: ``slide`` for a deck, else ``page``. A head kept before this was
            written has none, and reads as ``page``.
        total: Pictures are 1; for a PDF or an Office file, its pages or slides.
        pages_without_text: The pages, counted from 1, of a PDF or an Office file with no text of their own, at most
            ``PAGES_CHECKED_MAX`` checked.
        frames: The frame count of a picture with several (only the first is rendered); ``None`` otherwise.
        hint: A short note for the model, such as why only some pages were checked; ``None`` for none.
    """

    kind: Literal["image", "pdf", "office"]
    unit: Unit = "page"
    total: int = Field(ge=1)
    pages_without_text: list[int] = Field(max_length=PAGES_CHECKED_MAX)
    frames: int | None = Field(ge=2)
    hint: str | None = Field(max_length=300)


class RenderAnswer(RenderFacts):
    """The runner's whole answer to a render job: the facts and the renders it made.

    Attributes:
        renders: The renders, at most ``RENDERS_PER_JOB``.
    """

    renders: list[RenderEntry] = Field(max_length=RENDERS_PER_JOB)
