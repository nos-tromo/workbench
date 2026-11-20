"""Pictures decoded, turned upright and encoded without their metadata (``workbench.executor.render``).

Every file is built at test time with invented content (``tests/fixtures/make_images.py``).
"""

import dataclasses
import io
import json
import math
import random
import struct
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, override

import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas

from tests.fixtures import make_fixtures, make_images
from workbench.executor import readers
from workbench.executor.readers import DAMAGED, TOO_BIG, DocumentText, UnreadableFile
from workbench.executor.render import encode, images, pages
from workbench.executor.runners import RUNNERS, JobContext
from workbench.executor.runners import render as runner
from workbench.executor.runners.documents import _hint
from workbench.renders import (
    IMAGE_QUALITY,
    PAGE_LONG_SIDE,
    RENDER_MAX_BYTES,
    RenderAnswer,
    signature_ok,
    strip_spans,
)

RED = (255, 0, 0)
WHITE = (255, 255, 255)


def _close(pixel: float | tuple[int, ...] | None, want: tuple[int, ...], within: int = 40) -> bool:
    """Whether every channel of ``pixel`` (a colour, as ``getpixel`` gives it) is within ``within`` of ``want``."""
    assert isinstance(pixel, tuple)
    return all(abs(a - b) <= within for a, b in zip(pixel, want, strict=True))


def _out(image: Image.Image, *, lossless: bool = False, subsampling: int = 0) -> tuple[bytes, str, Image.Image]:
    """Encode ``image`` and decode what came out, as the model's side would."""
    data, mime = encode.encode(image, lossless=lossless, quality=IMAGE_QUALITY, subsampling=subsampling)
    return data, mime, Image.open(io.BytesIO(data))


# --- orientation, metadata ------------------------------------------------------------------


def test_a_rotated_photo_comes_out_upright_without_its_metadata(tmp_path: Path) -> None:
    """Orientation 6 is turned upright (the left half of the pixels is the top), and nothing of the EXIF survives."""
    photo = make_images.make_photo(tmp_path / "photo.jpg", size=(64, 48), orientation=6, gps=True)
    assert b"Exif" in photo.read_bytes()  # the source does carry its EXIF block

    normalised = images.normalise(photo)
    data, mime, decoded = _out(normalised.image)

    assert normalised.image.size == (48, 64)
    assert mime == "image/jpeg"
    assert decoded.size == (48, 64)
    assert _close(decoded.convert("RGB").getpixel((24, 0)), (255, 0, 0))
    assert _close(decoded.convert("RGB").getpixel((24, 63)), (0, 0, 255))
    assert b"Exif" not in data
    assert b"GPS" not in data
    assert b"http://ns.adobe.com/xap" not in data
    assert not decoded.getexif()
    assert not {"exif", "icc_profile", "xmp"} & set(decoded.info)


def test_a_photo_without_an_orientation_keeps_its_pixels_as_stored(tmp_path: Path) -> None:
    """Orientation 1 turns nothing: the left half stays on the left."""
    photo = make_images.make_photo(tmp_path / "photo.jpg", size=(64, 48), orientation=1, gps=False)

    image = images.normalise(photo).image

    assert image.size == (64, 48)
    assert _close(image.getpixel((8, 24)), (255, 0, 0))
    assert _close(image.getpixel((56, 24)), (0, 0, 255))


def test_the_other_orientations_turn_the_picture_upright(tmp_path: Path) -> None:
    """All eight tags end with the red half on the side the tag says: a flip or a turn, never a mirror image."""
    # where the red (stored left) half lies once upright, per tag, and the size
    want = {
        1: ("left", (64, 48)),
        2: ("right", (64, 48)),
        3: ("right", (64, 48)),
        4: ("left", (64, 48)),
        5: ("top", (48, 64)),
        6: ("top", (48, 64)),
        7: ("bottom", (48, 64)),
        8: ("bottom", (48, 64)),
    }
    probes = {"left": (8, 24), "right": (56, 24), "top": (24, 8), "bottom": (24, 56)}
    for tag, (side, size) in want.items():
        photo = make_images.make_photo(tmp_path / f"p{tag}.jpg", orientation=tag, gps=False)

        image = images.normalise(photo).image

        assert image.size == size, tag
        assert _close(image.getpixel(probes[side]), RED), tag


# --- colour ---------------------------------------------------------------------------------


def test_alpha_lies_on_white(tmp_path: Path) -> None:
    """A fully transparent pixel comes out white, and the picture is RGB."""
    image = images.normalise(make_images.make_alpha_png(tmp_path / "alpha.png")).image

    assert image.mode == "RGB"
    assert _close(image.getpixel((0, 0)), WHITE, 2)
    assert _close(image.getpixel((8, 8)), RED, 2)


def test_unusual_colour_modes_keep_their_colours(tmp_path: Path) -> None:
    """A CMYK JPEG, a 16-bit grey PNG and a palette PNG with a transparent index keep the colours of the source."""
    cmyk = images.normalise(make_images.make_cmyk_jpeg(tmp_path / "print.jpg")).image
    grey = images.normalise(make_images.make_png16(tmp_path / "scan.png")).image
    palette = images.normalise(make_images.make_palette_png(tmp_path / "pal.png")).image

    assert cmyk.mode == grey.mode == palette.mode == "RGB"
    assert _close(cmyk.getpixel((8, 8)), RED)
    assert _close(grey.getpixel((8, 8)), (128, 128, 128), 2)
    assert _close(palette.getpixel((0, 0)), WHITE, 2)
    assert _close(palette.getpixel((8, 8)), (0, 0, 255), 2)


def test_a_bilevel_picture_is_smoothed_not_picked_from(tmp_path: Path) -> None:
    """A one-bit scan is scaled as grey, so a thin black line stays visible as a grey one instead of dropping out."""
    scan = Image.new("1", (4096, 2048), 1)
    for x in range(0, 4096, 2):
        scan.putpixel((x, 100), 0)  # a dotted line, one pixel thick
    path = tmp_path / "fax.tif"
    scan.save(path)

    image = images.normalise(path).image

    assert image.size == (2048, 1024)
    row = [image.getpixel((x, 50)) for x in range(2048)]
    assert all(isinstance(pixel, tuple) for pixel in row)
    assert min(pixel[0] for pixel in row if isinstance(pixel, tuple)) < 250  # something of the line is there


# --- bounds and encoding --------------------------------------------------------------------


def test_a_large_photo_is_scaled_within_the_bounds(tmp_path: Path) -> None:
    """A 4032 x 3024 noise JPEG ends within 2048 px on its long side and 2.4 MP, as a JPEG under 768 KiB."""
    photo = make_images.make_noise(tmp_path / "big.jpg", (4032, 3024), "JPEG")

    normalised = images.normalise(photo)
    data, mime, decoded = _out(normalised.image)

    width, height = normalised.image.size
    assert max(width, height) <= 2048
    assert width * height <= 2_400_000
    assert mime == "image/jpeg"
    assert decoded.format == "JPEG"
    assert len(data) < RENDER_MAX_BYTES


def test_a_screenshot_stays_png_when_it_fits(tmp_path: Path) -> None:
    """A flat 1200 x 800 PNG is kept lossless."""
    flat = tmp_path / "screen.png"
    Image.new("RGB", (1200, 800), (30, 60, 90)).save(flat)

    normalised = images.normalise(flat)
    data, mime, decoded = _out(normalised.image, lossless=normalised.lossless)

    assert normalised.lossless
    assert mime == "image/png"
    assert decoded.format == "PNG"
    assert data.startswith(b"\x89PNG\r\n\x1a\n")


def test_a_png_that_does_not_fit_becomes_jpeg(tmp_path: Path) -> None:
    """A noise PNG of 2000 x 1000 is far over the cap as PNG, so it is encoded as a JPEG."""
    noise = make_images.make_noise(tmp_path / "noise.png", (2000, 1000), "PNG")

    normalised = images.normalise(noise)
    data, mime, _ = _out(normalised.image, lossless=normalised.lossless)

    assert normalised.lossless
    assert mime == "image/jpeg"
    assert len(data) < RENDER_MAX_BYTES


def test_every_render_is_under_768_kib() -> None:
    """Noise at 2048 x 1172, the hardest picture there is, still ends under the cap."""
    noise = Image.frombytes("RGB", (2048, 1172), random.Random(3).randbytes(2048 * 1172 * 3))

    data, mime = encode.encode(noise, lossless=False, quality=90, subsampling=0)

    assert mime == "image/jpeg"
    assert len(data) < RENDER_MAX_BYTES


def test_nothing_that_fits_is_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    """If even the smallest scale is over the cap, the render is refused, not stored."""
    monkeypatch.setattr(encode, "RENDER_MAX_BYTES", 100)

    with pytest.raises(UnreadableFile):
        encode.encode(Image.new("RGB", (64, 64), (1, 2, 3)), lossless=False, quality=90, subsampling=0)


def test_an_encoded_picture_holds_no_text_or_profile_in_either_format() -> None:
    """The metadata a picture carries in memory never reaches the file, as PNG or as JPEG."""
    image = Image.new("RGB", (32, 32), RED)
    image.info.update({"exif": b"Exif\x00\x00GPS", "icc_profile": b"profile", "xmp": b"<x/>", "comment": b"secret"})

    for lossless in (True, False):
        data, _ = encode.encode(image, lossless=lossless, quality=90, subsampling=0)

        assert b"Exif" not in data
        assert b"GPS" not in data
        assert b"secret" not in data
        assert b"profile" not in data
        assert b"<x/>" not in data


def test_only_the_first_frame_of_a_gif_is_shown(tmp_path: Path) -> None:
    """Three frames are counted, and frame 0, red, is the picture."""
    normalised = images.normalise(make_images.make_gif(tmp_path / "anim.gif", frames=3))

    assert normalised.frames == 3
    assert _close(normalised.image.getpixel((8, 8)), RED, 2)
    assert normalised.lossless


def test_a_single_frame_counts_one(tmp_path: Path) -> None:
    """A photograph has one frame, and a JPEG is not lossless."""
    normalised = images.normalise(make_images.make_photo(tmp_path / "p.jpg"))

    assert normalised.frames == 1
    assert not normalised.lossless


def test_a_heic_photo_is_decoded(tmp_path: Path) -> None:
    """A HEIC file is read with the decode-only opener, and comes out the size it went in."""
    pytest.importorskip("pillow_heif")
    heic = make_images.make_heic(tmp_path / "photo.heic")

    normalised = images.normalise(heic)

    assert normalised.image.size == (64, 48)
    assert not normalised.lossless
    assert _close(normalised.image.getpixel((8, 24)), RED)


# --- files that are not what they say -------------------------------------------------------


def test_a_decompression_bomb_is_unreadable(tmp_path: Path) -> None:
    """A header that declares 144 megapixels is refused at once, naming the limit, with no pixel read."""
    bomb = make_images.make_bomb_png(tmp_path / "bomb.png", width=12000, height=12000)

    started = time.monotonic()
    with pytest.raises(UnreadableFile, match="120 megapixels"):
        images.normalise(bomb)

    assert time.monotonic() - started < 2


def test_a_bomb_far_over_the_limit_is_refused_alike(tmp_path: Path) -> None:
    """Pillow's own hard stop (over twice the limit) gives the same answer."""
    bomb = make_images.make_bomb_png(tmp_path / "bomb.png", width=40000, height=40000)

    with pytest.raises(UnreadableFile, match="120 megapixels"):
        images.normalise(bomb)


def test_a_text_file_named_png_is_unreadable(tmp_path: Path) -> None:
    """A text file with a picture's name is not a picture, and the answer holds neither its path nor its text."""
    liar = tmp_path / "invented-notes.png"
    liar.write_text("these are not pixels\n")

    with pytest.raises(UnreadableFile) as caught:
        images.normalise(liar)

    assert "invented-notes" not in str(caught.value)
    assert "pixels" not in str(caught.value)


def test_a_png_named_jpg_is_read_by_its_content(tmp_path: Path) -> None:
    """The extension decides nothing: a PNG saved as ``.jpg`` is decoded as the PNG it is."""
    liar = tmp_path / "shot.jpg"
    Image.new("RGB", (40, 30), RED).save(liar, "PNG")

    normalised = images.normalise(liar)

    assert normalised.image.size == (40, 30)
    assert normalised.lossless  # the content is PNG


def test_a_truncated_picture_is_unreadable(tmp_path: Path) -> None:
    """A JPEG cut short in its data is damaged, not a half picture."""
    whole = make_images.make_noise(tmp_path / "whole.jpg", (400, 300), "JPEG").read_bytes()
    cut = tmp_path / "cut.jpg"
    cut.write_bytes(whole[: len(whole) // 3])

    with pytest.raises(UnreadableFile):
        images.normalise(cut)


def test_a_truncated_png_is_unreadable(tmp_path: Path) -> None:
    """A PNG cut short is damaged too: reading its EXIF block, which loads it, must not hide that."""
    whole = make_images.make_noise(tmp_path / "whole.png", (300, 200), "PNG").read_bytes()
    cut = tmp_path / "cut.png"
    cut.write_bytes(whole[: len(whole) // 2])

    with pytest.raises(UnreadableFile):
        images.normalise(cut)


def test_a_format_the_renders_do_not_read_is_unreadable(tmp_path: Path) -> None:
    """A picture of a kind outside the list (an ICO here) is refused, though Pillow can read it."""
    path = tmp_path / "icon.png"
    Image.new("RGB", (16, 16), RED).save(path, "ICO")

    with pytest.raises(UnreadableFile):
        images.normalise(path)


def test_a_broken_profile_falls_back_to_the_plain_conversion(tmp_path: Path) -> None:
    """A CMYK picture with an ICC profile that is not one is still turned to RGB."""
    path = tmp_path / "print.tif"
    Image.new("CMYK", (16, 16), (0, 255, 255, 0)).save(path, icc_profile=b"not a profile at all")

    image = images.normalise(path).image

    assert image.mode == "RGB"
    assert _close(image.getpixel((8, 8)), RED)


def test_the_source_limit_is_checked_before_the_pixels_are_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source of more pixels than the limit is refused whatever Pillow's own limit says."""
    monkeypatch.setattr(images, "SOURCE_MAX_PIXELS", 100)
    path = tmp_path / "small.png"
    Image.new("RGB", (11, 10), RED).save(path)

    with pytest.raises(UnreadableFile, match="120 megapixels"):
        images.normalise(path)


def test_the_pillow_limit_is_put_back(tmp_path: Path) -> None:
    """Decoding sets Pillow's pixel limit for its own run only."""
    before = Image.MAX_IMAGE_PIXELS

    images.normalise(make_images.make_photo(tmp_path / "p.jpg"))

    assert Image.MAX_IMAGE_PIXELS == before


# --- strips ---------------------------------------------------------------------------------


def test_cut_makes_bands_along_the_long_side() -> None:
    """A portrait picture gives horizontal bands, a landscape one vertical bands, both overlapping."""
    portrait = encode.cut(Image.new("RGB", (1131, 1600), RED), 2)
    landscape = encode.cut(Image.new("RGB", (1600, 1131), RED), 2)

    assert [band.size for band in portrait] == [(1131, 848)] * 2
    assert [band.size for band in landscape] == [(848, 1131)] * 2


def test_cut_into_one_is_the_whole_picture() -> None:
    """One strip is the picture as it is."""
    (whole,) = encode.cut(Image.new("RGB", (300, 200), RED), 1)

    assert whole.size == (300, 200)


def test_cut_bands_hold_the_pixels_of_their_span() -> None:
    """Each band is the span of the picture ``strip_spans`` names: the first rows go to the first band."""
    image = Image.new("RGB", (100, 300), RED)
    image.paste(Image.new("RGB", (100, 100), (0, 0, 255)), (0, 200))

    top, middle, bottom = encode.cut(image, 3)

    assert _close(top.getpixel((50, 0)), RED, 2)
    assert _close(bottom.getpixel((50, bottom.height - 1)), (0, 0, 255), 2)
    assert _close(middle.getpixel((50, 0)), RED, 2)
    assert [band.height for band in (top, middle, bottom)] == [top.height] * 3


def test_a_transparent_picture_is_laid_on_white() -> None:
    """A fully transparent RGBA picture encodes as white, in PNG and in JPEG, not as the black its RGB values hold."""
    clear = Image.new("RGBA", (16, 16), (0, 0, 0, 0))

    _, png_mime, png = _out(clear, lossless=True)
    _, jpeg_mime, jpeg = _out(clear)

    assert (png_mime, jpeg_mime) == ("image/png", "image/jpeg")
    assert _close(png.convert("RGB").getpixel((8, 8)), WHITE, 2)
    assert _close(jpeg.convert("RGB").getpixel((8, 8)), WHITE, 2)


def test_a_picture_with_a_transparent_colour_or_a_grey_alpha_is_laid_on_white_too() -> None:
    """The other modes that carry transparency (palette with a key, grey with alpha) go on white, not black."""
    palette = Image.new("P", (8, 8), 0)
    palette.putpalette([0, 0, 0, 255, 0, 0])
    palette.info["transparency"] = 0
    grey_alpha = Image.new("LA", (8, 8), (0, 0))

    for image in (palette, grey_alpha):
        _, _, decoded = _out(image, lossless=True)
        assert _close(decoded.convert("RGB").getpixel((4, 4)), WHITE, 2)


def test_a_conversion_that_fails_is_unreadable_not_a_raw_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mode conversion that runs out of memory, or cannot be made, ends as ``UnreadableFile`` like the rest."""

    def out_of_memory(image: Image.Image) -> Image.Image:
        raise MemoryError

    def cannot(image: Image.Image) -> Image.Image:
        raise ValueError("no such conversion")

    clear = Image.new("RGBA", (4, 4), (0, 0, 0, 0))
    monkeypatch.setattr(encode, "on_white", out_of_memory)
    with pytest.raises(UnreadableFile) as big:
        encode.encode(clear, lossless=False, quality=IMAGE_QUALITY, subsampling=0)
    monkeypatch.setattr(encode, "on_white", cannot)
    with pytest.raises(UnreadableFile) as odd:
        encode.encode(clear, lossless=False, quality=IMAGE_QUALITY, subsampling=0)

    assert (str(big.value), str(odd.value)) == (TOO_BIG, DAMAGED)
    assert big.value.__cause__ is None
    assert odd.value.__cause__ is None


# --- PDF pages ------------------------------------------------------------------------------


class FakeText:
    """A text page that says what it holds and notes that it was closed."""

    def __init__(self, text: str, log: list[str], name: str) -> None:
        """Hold ``text``; ``log`` gets ``name`` once closed."""
        self._text, self._log, self._name = text, log, name

    def get_text_range(self) -> str:
        """The page's text."""
        return self._text

    def close(self) -> None:
        """Note the close."""
        self._log.append(self._name)


class FakePage:
    """A page with a text page, a size and a render, each noting its close."""

    def __init__(self, text: str, log: list[str], name: str, size: tuple[float, float] = (595.0, 842.0)) -> None:
        """Hold ``text``; ``log`` gets ``name`` and ``name`` with ``-text`` once closed."""
        self._text, self._log, self._name, self._size = text, log, name, size
        self.scales: list[float] = []

    def get_textpage(self) -> FakeText:
        """The page's text page."""
        return FakeText(self._text, self._log, f"{self._name}-text")

    def get_size(self) -> tuple[float, float]:
        """The size in points, as pdfium gives it: with the page's own rotation applied."""
        return self._size

    def render(self, scale: float) -> "FakeBitmap":
        """Draw the page; ``scale`` is the only option the code may pass (the page's rotation is drawn already)."""
        self.scales.append(scale)
        return FakeBitmap(
            (math.ceil(self._size[0] * scale), math.ceil(self._size[1] * scale)), self._log, f"{self._name}-bitmap"
        )

    def close(self) -> None:
        """Note the close."""
        self._log.append(self._name)


class FakeBitmap:
    """A rendered page that gives a picture and notes that it was closed."""

    def __init__(self, size: tuple[int, int], log: list[str], name: str) -> None:
        """Hold the size; ``log`` gets ``name`` once closed."""
        self._size, self._log, self._name = size, log, name

    def to_pil(self) -> Image.Image:
        """The bitmap as a picture."""
        return Image.new("RGB", self._size, WHITE)

    def close(self) -> None:
        """Note the close."""
        self._log.append(self._name)


class FakePdf:
    """A document of fake pages."""

    def __init__(self, texts: Sequence[str], log: list[str]) -> None:
        """One page per text."""
        self.pages = [FakePage(text, log, f"page{i + 1}") for i, text in enumerate(texts)]

    def __len__(self) -> int:
        """The number of pages."""
        return len(self.pages)

    def __getitem__(self, index: int) -> FakePage:
        """The page at ``index``, counted from 0."""
        return self.pages[index]


def test_pages_without_text_lists_blank_ones_and_closes_every_handle() -> None:
    """A page whose text is only whitespace is listed; every page and text page opened is closed."""
    log: list[str] = []
    pdf = FakePdf(["words", " \n\t ", "", "more words"], log)

    blank, unchecked = pages.pages_without_text(pdf, 10)

    assert (blank, unchecked) == ([2, 3], False)
    assert sorted(log) == sorted(f"page{i}{kind}" for i in range(1, 5) for kind in ("", "-text"))


def test_pages_without_text_stops_at_its_limit_and_says_so() -> None:
    """Pages past the limit are not looked at; the answer says some were left."""
    log: list[str] = []

    blank, unchecked = pages.pages_without_text(FakePdf(["", "", "", ""], log), 2)

    assert (blank, unchecked) == ([1, 2], True)
    assert sorted(log) == ["page1", "page1-text", "page2", "page2-text"]


def test_pages_without_text_closes_what_it_opened_when_time_runs_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """The clock is read between pages; a deadline that has passed ends the check with nothing left open."""
    log: list[str] = []
    calls: list[None] = []

    def check() -> None:
        calls.append(None)
        if len(calls) == 2:
            raise readers.ReadTimeout

    monkeypatch.setattr(readers, "check", check)

    with pytest.raises(readers.ReadTimeout):
        pages.pages_without_text(FakePdf(["", "", ""], log), 10)

    assert sorted(log) == ["page1", "page1-text"]


def test_render_page_draws_at_the_page_scale_without_turning_it_again() -> None:
    """The scale is the long side over the longer edge, and the only option: pdfium turns a page by its own /Rotate."""
    log: list[str] = []
    pdf = FakePdf([""], log)

    image = pages.render_page(pdf, 1)

    assert pdf[0].scales == pytest.approx([PAGE_LONG_SIDE / 842.0], rel=1e-6)
    assert pdf[0].scales[0] < PAGE_LONG_SIDE / 842.0  # a hair under, so the long side never rounds up to 1601
    assert image.mode == "RGB"
    assert image.height == PAGE_LONG_SIDE
    assert sorted(log) == ["page1", "page1-bitmap"]


def test_render_page_refuses_a_page_with_no_size() -> None:
    """A page of no width or height cannot be drawn; it is damaged, and what was opened is closed."""
    log: list[str] = []
    pdf = FakePdf([""], log)
    pdf.pages[0] = FakePage("", log, "page1", size=(0.0, 842.0))

    with pytest.raises(UnreadableFile) as refused:
        pages.render_page(pdf, 1)

    assert str(refused.value) == DAMAGED
    assert log == ["page1"]


def test_a_failed_draw_is_unreadable_and_closes_the_page() -> None:
    """A library failure while drawing is a damaged file with a fixed text; the page is closed."""
    log: list[str] = []

    class Failing(FakePage):
        @override
        def render(self, scale: float) -> FakeBitmap:
            raise RuntimeError("/private/path/inside/the/library")

    pdf = FakePdf([""], log)
    pdf.pages[0] = Failing("", log, "page1")

    with pytest.raises(UnreadableFile) as refused:
        pages.render_page(pdf, 1)

    assert (str(refused.value), refused.value.__cause__) == (DAMAGED, None)
    assert log == ["page1"]


# --- prepare_file ---------------------------------------------------------------------------

JOB_ID = "0123456789abcdef0123456789abcdef"
CHAT = "chat-1"


@dataclass(frozen=True)
class Job:
    """A job folder as the supervisor makes it, the runner's working folder its chat folder.

    Attributes:
        ctx: What the job entry point gives the runner.
    """

    ctx: JobContext

    def path(self, name: str) -> Path:
        """Where an input of that name lies."""
        return self.ctx.work_dir / name

    def prepare(
        self,
        name: str,
        *,
        pages: list[int] | None = None,
        strips: int = 1,
        have: Sequence[int] = (),
        budget_s: float = 60,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        """Run ``prepare_file`` on an input."""
        ctx = self.ctx if max_chars is None else dataclasses.replace(self.ctx, max_chars=max_chars)
        args = {"path": name, "pages": pages, "strips": strips, "have": list(have), "budget_s": budget_s}
        return RUNNERS["prepare_file"](args, ctx)

    def written(self) -> list[str]:
        """The names in ``out/``."""
        return sorted(path.name for path in self.ctx.out_dir.iterdir())

    def check(self, entry: dict[str, Any]) -> Image.Image:
        """Open a render an entry names, after checking what the api would: present, signed, under the cap, as said."""
        data = (self.ctx.out_dir / entry["name"]).read_bytes()
        assert signature_ok(data, entry["mime"])
        assert len(data) < RENDER_MAX_BYTES
        picture = Image.open(io.BytesIO(data))
        assert picture.format == ("JPEG" if entry["mime"] == "image/jpeg" else "PNG")
        assert picture.size == (entry["width"], entry["height"])
        return picture


@pytest.fixture
def job(tmp_path: Path) -> Job:
    """An empty job folder for ``chat-1``."""
    work = tmp_path / "data" / "jobs" / JOB_ID / "workspace" / CHAT
    (work / "out").mkdir(parents=True)
    return Job(JobContext(work_dir=work, out_dir=work / "out", chat_id=CHAT, max_chars=16 * 1024, file_mb=50))


def _numbers(answer: dict[str, Any]) -> list[int]:
    """The pages the renders of an answer are of, in order."""
    return [entry["n"] for entry in answer["renders"]]


def test_a_pdf_is_counted_and_its_first_pages_rendered(job: Job) -> None:
    """Twenty of 22 pages are drawn at 1600 px, the page with no text is named, and the answer is a RenderAnswer."""
    make_fixtures.make_pdf(job.path("book.pdf"), [f"Page {n}" if n != 2 else None for n in range(1, 23)])

    answer = job.prepare("book.pdf")

    assert (answer["kind"], answer["total"], answer["pages_without_text"]) == ("pdf", 22, [2])
    assert answer["frames"] is None
    assert [entry["name"] for entry in answer["renders"]] == [f"p{n:04d}.jpg" for n in range(1, 21)]
    for entry in answer["renders"]:
        assert (entry["role"], entry["strip"], entry["strips"], entry["mime"]) == ("page", 1, 1, "image/jpeg")
        assert max(entry["width"], entry["height"]) == PAGE_LONG_SIDE
        assert job.check(entry).mode == "RGB"
    assert job.written() == [f"p{n:04d}.jpg" for n in range(1, 21)]
    assert RenderAnswer.model_validate(answer).model_dump() == answer


def test_later_pages_render_on_demand(job: Job) -> None:
    """Named pages are drawn, whichever they are."""
    make_fixtures.make_pdf(job.path("book.pdf"), [f"Page {n}" for n in range(1, 23)])

    answer = job.prepare("book.pdf", pages=[21, 22])

    assert _numbers(answer) == [21, 22]
    assert job.written() == ["p0021.jpg", "p0022.jpg"]
    assert answer["total"] == 22


def test_have_skips_pages_already_rendered(job: Job) -> None:
    """The pages the caller has are not drawn again."""
    make_fixtures.make_pdf(job.path("book.pdf"), [f"Page {n}" for n in range(1, 23)])

    answer = job.prepare("book.pdf", have=[1, 2, 3])

    assert _numbers(answer) == list(range(4, 21))
    assert "p0001.jpg" not in job.written()


def test_a_page_is_drawn_again_as_strips_whatever_the_caller_has(job: Job) -> None:
    """A strip is not the whole page, so ``have`` does not hold it back."""
    make_fixtures.make_pdf(job.path("book.pdf"), ["one", "two"])

    answer = job.prepare("book.pdf", pages=[2], strips=2, have=[1, 2])

    assert _numbers(answer) == [2, 2]


def test_strips_of_a_page(job: Job) -> None:
    """Two strips of a page are cut from one render along its long side, the height split as ``strip_spans`` says."""
    make_fixtures.make_pdf(job.path("book.pdf"), ["one", "two"])

    answer = job.prepare("book.pdf", pages=[2], strips=2)

    first, second = answer["renders"]
    assert (first["name"], second["name"]) == ("p0002-s1of2.jpg", "p0002-s2of2.jpg")
    assert (first["strip"], second["strip"], first["strips"]) == (1, 2, 2)
    height = PAGE_LONG_SIDE
    assert [first["height"], second["height"]] == [end - start for start, end in strip_spans(height, 2)]
    assert first["width"] == second["width"]
    assert job.written() == ["p0002-s1of2.jpg", "p0002-s2of2.jpg"]
    for entry in answer["renders"]:
        job.check(entry)


def test_a_page_turned_by_its_own_rotation_is_drawn_turned_once(job: Job) -> None:
    """A page marked /Rotate 90 comes out landscape, its top left corner at the top right: turned once, not twice."""
    canvas = Canvas(str(job.path("upright.pdf")))
    canvas.rect(10, 740, 90, 90, fill=1)  # a black square at the top left of an A4 page
    canvas.showPage()
    canvas.save()
    writer = PdfWriter()
    writer.add_page(PdfReader(str(job.path("upright.pdf"))).pages[0]).rotate(90)
    with job.path("turned.pdf").open("wb") as stream:
        writer.write(stream)

    (entry,) = job.prepare("turned.pdf", pages=[1])["renders"]
    picture = job.check(entry).convert("RGB")

    assert (entry["width"], entry["height"]) == (PAGE_LONG_SIDE, 1132)
    assert _close(picture.getpixel((entry["width"] - 60, 60)), (0, 0, 0), 40)
    assert _close(picture.getpixel((60, 60)), WHITE, 40)


def test_a_page_is_drawn_to_whole_pixels_whatever_the_float(job: Job) -> None:
    """A page 302 points high, whose scaled height floating point puts a hair over 1600, is still 1600 pixels."""
    assert math.ceil(struct.unpack("f", struct.pack("f", 302.0))[0] * (PAGE_LONG_SIDE / 302.0)) == PAGE_LONG_SIDE + 1
    canvas = Canvas(str(job.path("small.pdf")), pagesize=(200, 302))
    canvas.drawString(10, 10, "invented text")
    canvas.showPage()
    canvas.save()

    (entry,) = job.prepare("small.pdf")["renders"]

    assert entry["height"] == PAGE_LONG_SIDE
    assert entry["width"] <= PAGE_LONG_SIDE


def test_a_page_with_only_a_picture_is_not_blank_to_the_renderer(job: Job) -> None:
    """The scan is listed as without text and drawn like any page."""
    make_fixtures.make_pdf(job.path("scan.pdf"), [None, None, "text"])

    answer = job.prepare("scan.pdf")

    assert (answer["pages_without_text"], _numbers(answer)) == ([1, 2], [1, 2, 3])


def test_no_pages_named_is_the_facts_alone(job: Job) -> None:
    """An empty list of pages draws nothing, and the page count and the pages without text still come."""
    make_fixtures.make_pdf(job.path("scan.pdf"), [None, "text"])

    answer = job.prepare("scan.pdf", pages=[])

    assert (answer["total"], answer["pages_without_text"], answer["renders"]) == (2, [1], [])
    assert job.written() == []


def test_an_image_has_one_render(job: Job) -> None:
    """A photograph is a picture of total 1, one render, role image, with no frame count."""
    make_images.make_photo(job.path("photo.jpg"), size=(64, 48), orientation=6)

    answer = job.prepare("photo.jpg")

    (entry,) = answer["renders"]
    assert (answer["kind"], answer["total"], answer["frames"], answer["pages_without_text"]) == ("image", 1, None, [])
    assert (entry["name"], entry["role"], entry["n"], entry["mime"]) == ("p0001.jpg", "image", 1, "image/jpeg")
    assert (entry["width"], entry["height"]) == (48, 64)  # turned upright
    job.check(entry)
    assert RenderAnswer.model_validate(answer).model_dump() == answer


def test_a_screenshot_is_a_png_render(job: Job) -> None:
    """A PNG that fits stays a PNG, with the name and type that say so."""
    make_images.make_alpha_png(job.path("shot.png"))

    (entry,) = job.prepare("shot.png")["renders"]

    assert (entry["name"], entry["mime"]) == ("p0001.png", "image/png")
    job.check(entry)


def test_an_image_in_strips(job: Job) -> None:
    """A wide picture is cut into vertical strips, with the names and widths ``strip_spans`` gives."""
    make_images.make_photo(job.path("wide.jpg"), size=(300, 100), orientation=1)

    answer = job.prepare("wide.jpg", pages=[1], strips=3)

    assert [entry["name"] for entry in answer["renders"]] == [f"p0001-s{i}of3.jpg" for i in (1, 2, 3)]
    assert [entry["width"] for entry in answer["renders"]] == [end - start for start, end in strip_spans(300, 3)]
    assert {entry["height"] for entry in answer["renders"]} == {100}
    assert {entry["role"] for entry in answer["renders"]} == {"image"}
    for entry in answer["renders"]:
        job.check(entry)


def test_an_image_the_caller_has_is_not_drawn_again(job: Job) -> None:
    """``have`` names page 1 of a picture too; its facts still come."""
    make_images.make_photo(job.path("photo.jpg"), orientation=1)

    answer = job.prepare("photo.jpg", have=[1])

    assert (answer["kind"], answer["total"], answer["renders"]) == ("image", 1, [])
    assert job.written() == []


def test_a_picture_with_frames_shows_the_first_and_says_so(job: Job) -> None:
    """An animated GIF gives its frame count and a hint that only the first is shown."""
    make_images.make_gif(job.path("anim.gif"), frames=3)

    answer = job.prepare("anim.gif")

    assert (answer["frames"], answer["hint"]) == (3, "only the first of 3 frames is shown")
    assert len(answer["renders"]) == 1


def test_an_image_accepts_only_its_one_page(job: Job) -> None:
    """Page 1 is the picture; any other page is no part of it."""
    make_images.make_photo(job.path("photo.jpg"), orientation=1)

    assert len(job.prepare("photo.jpg", pages=[1])["renders"]) == 1
    assert job.prepare("photo.jpg", pages=[2]) == {"error": "no_such_part: a picture has one page"}
    assert job.written() == ["p0001.jpg"]


def test_a_page_past_the_end_is_no_such_part(job: Job) -> None:
    """A page the PDF does not have is named with the count it does, and nothing is drawn."""
    make_fixtures.make_pdf(job.path("short.pdf"), ["one", "two", "three"])

    assert job.prepare("short.pdf", pages=[2, 4]) == {"error": "no_such_part: this document has 3 pages"}
    assert job.prepare("short.pdf", pages=[0]) == {"error": "no_such_part: this document has 3 pages"}
    assert job.written() == []


def test_an_encrypted_pdf_is_unreadable(job: Job) -> None:
    """A PDF that asks for a password is refused with the reader's own words, and nothing is written."""
    make_fixtures.make_pdf(job.path("locked.pdf"), ["one"], password="invented")

    answer = job.prepare("locked.pdf")

    assert answer == {"error": "unreadable_file: the PDF is password-protected (encrypted)"}
    assert job.written() == []


def test_a_truncated_pdf_is_unreadable(job: Job, tmp_path: Path) -> None:
    """Half a PDF is damaged, whatever its name says, and nothing is written."""
    whole = make_fixtures.make_pdf(tmp_path / "whole.pdf", ["one", None, "three"])
    make_fixtures.make_truncated(whole, job.path("cut.pdf"))

    assert job.prepare("cut.pdf") == {"error": f"unreadable_file: {DAMAGED}"}
    assert job.written() == []


def test_a_pdf_that_is_not_a_pdf_is_unreadable(job: Job) -> None:
    """A text file named ``.pdf`` is damaged; an empty one too."""
    job.path("note.pdf").write_text("only words")
    job.path("empty.pdf").write_bytes(b"")

    assert job.prepare("note.pdf") == {"error": f"unreadable_file: {DAMAGED}"}
    assert job.prepare("empty.pdf") == {"error": f"unreadable_file: {DAMAGED}"}


def test_a_text_file_named_png_is_unreadable_here_too(job: Job) -> None:
    """A picture's name decides nothing: a text file called ``.png`` is damaged, and nothing is written."""
    job.path("fake.png").write_text("only words")

    assert job.prepare("fake.png") == {"error": f"unreadable_file: {DAMAGED}"}
    assert job.written() == []


def test_a_long_pdf_is_checked_up_to_its_limit(job: Job, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pages past ``PAGES_CHECKED_MAX`` are not looked at for text, and the hint names the page the check stopped at."""
    monkeypatch.setattr(runner, "PAGES_CHECKED_MAX", 5)
    make_fixtures.make_pdf(job.path("long.pdf"), ["a", "b", None, "d", "e", None, None])

    answer = job.prepare("long.pdf", pages=[1])

    assert (answer["total"], answer["pages_without_text"]) == (7, [3])
    assert answer["hint"] == "pages after 5 were not checked for text"


def test_the_answer_fits_its_limit(job: Job) -> None:
    """With 60 pages without text and little room, the list is cut, and the hint says where."""
    make_fixtures.make_pdf(job.path("scans.pdf"), [None] * 60)

    answer = job.prepare("scans.pdf", pages=[1], max_chars=450)

    listed = answer["pages_without_text"]
    assert 0 < len(listed) < 60
    assert listed == list(range(1, len(listed) + 1))
    assert answer["hint"] == f"pages without text are listed up to page {listed[-1]}"
    assert len(json.dumps(answer)) <= 450
    assert len(json.dumps({**answer, "pages_without_text": [*listed, len(listed) + 1]})) > 450  # as many as fit
    RenderAnswer.model_validate(answer)


def test_the_answer_is_whole_when_it_fits(job: Job) -> None:
    """With room enough, nothing is cut and no hint is added."""
    make_fixtures.make_pdf(job.path("scans.pdf"), [None] * 12)

    answer = job.prepare("scans.pdf", pages=[1])

    assert (answer["pages_without_text"], answer["hint"]) == (list(range(1, 13)), None)


def test_the_hints_join_when_there_are_several(job: Job, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unchecked tail and a cut list are both said, within the hint's own limit."""
    monkeypatch.setattr(runner, "PAGES_CHECKED_MAX", 40)
    make_fixtures.make_pdf(job.path("scans.pdf"), [None] * 45)

    answer = job.prepare("scans.pdf", pages=[1], max_chars=330)

    assert answer["hint"].startswith(
        "pages after 40 were not checked for text; pages without text are listed up to page "
    )
    RenderAnswer.model_validate(answer)


def test_the_answer_names_no_path(job: Job) -> None:
    """Neither the virtual ``/workspace`` path nor the job folder is in the answer."""
    make_fixtures.make_pdf(job.path("book.pdf"), ["one", None])
    make_images.make_photo(job.path("photo.jpg"))

    for name in ("book.pdf", "photo.jpg"):
        text = json.dumps(job.prepare(name))
        assert "/workspace" not in text
        assert str(job.ctx.work_dir) not in text
        assert JOB_ID not in text
        assert name not in text


def test_a_word_file_is_unsupported(job: Job) -> None:
    """A file that is neither a picture nor a PDF is not for this tool."""
    make_fixtures.make_docx(job.path("memo.docx"))

    answer = job.prepare("memo.docx")

    assert answer == {"error": "unsupported_format: only pictures and PDFs can be shown"}
    assert "memo" not in json.dumps(answer)


def test_a_missing_file_is_not_found(job: Job) -> None:
    """A path that is no file of the chat, or lies in the job's own ``out/``, is not found; the answer names no path."""
    (job.ctx.out_dir / "p0001.jpg").write_bytes(b"x")

    for name in ("none.pdf", "none.jpg", "out/p0001.jpg"):
        answer = job.prepare(name)
        assert set(answer) == {"error"}
        assert answer["error"].startswith("file_not_found: ")
        assert name not in answer["error"]
        assert "/workspace" not in answer["error"]


def test_a_spent_budget_is_a_read_timeout(job: Job) -> None:
    """A job whose time is up at once answers ``read_timeout`` and leaves nothing in ``out/``."""
    make_fixtures.make_pdf(job.path("book.pdf"), ["one", "two"])
    make_images.make_photo(job.path("photo.jpg"))

    for name in ("book.pdf", "photo.jpg"):
        answer = job.prepare(name, budget_s=0)
        assert answer["error"].startswith("read_timeout: ")
    assert job.written() == []


def test_the_deadline_is_taken_away_afterwards(job: Job) -> None:
    """The readers' deadline is the process's: it is cleared however the runner ends."""
    make_fixtures.make_pdf(job.path("book.pdf"), ["one"])

    job.prepare("book.pdf", budget_s=0)
    readers.check()  # would raise ReadTimeout if the spent deadline were still set
    job.prepare("book.pdf")
    readers.check()


def test_a_render_that_cannot_be_encoded_ends_the_job_without_files(job: Job, monkeypatch: pytest.MonkeyPatch) -> None:
    """When a later page fails, the earlier ones are not left in ``out/``."""
    make_fixtures.make_pdf(job.path("book.pdf"), ["one", "two", "three"])
    real = encode.encode
    calls: list[None] = []

    def second_fails(*args: Any, **kwargs: Any) -> tuple[bytes, str]:
        calls.append(None)
        if len(calls) == 2:
            raise UnreadableFile(DAMAGED)
        return real(*args, **kwargs)

    monkeypatch.setattr(encode, "encode", second_fails)

    assert job.prepare("book.pdf") == {"error": f"unreadable_file: {DAMAGED}"}
    assert job.written() == []


def test_too_many_renders_for_one_job_is_refused_before_any_is_drawn(job: Job) -> None:
    """Pages times strips past the renders of one job is the caller's mistake, not a damaged file."""
    make_fixtures.make_pdf(job.path("book.pdf"), [f"p{n}" for n in range(1, 10)])

    with pytest.raises(ValueError, match="too many renders"):
        job.prepare("book.pdf", pages=list(range(1, 9)), strips=3)
    assert job.written() == []


def test_a_strip_count_out_of_range_is_refused(job: Job) -> None:
    """Strips are 1 to 3."""
    make_fixtures.make_pdf(job.path("book.pdf"), ["one"])

    for bad in (0, 4):
        with pytest.raises(ValueError, match="strips"):
            job.prepare("book.pdf", pages=[1], strips=bad)


# --- what read_document says of a page with no text -----------------------------------------


def test_the_hint_on_a_scanned_pdf_says_what_to_do_without_the_tool_name() -> None:
    """The PDF sentence names the pages and tells the model not to treat them as blank."""
    doc = DocumentText(kind="pdf", parts=[], pages_without_text=[2, 5], total_parts=9)

    assert _hint(doc) == (
        "pages 2, 5 of those looked at have no text layer (scans or pictures); do not treat them as blank."
    )


def test_the_hint_on_a_deck_keeps_its_words_without_the_tool_name() -> None:
    """The slide sentence is as it was, less the clause about OCR."""
    doc = DocumentText(kind="pptx", parts=[], pages_without_text=[3], total_parts=4)

    hint = _hint(doc)

    assert hint == "slide 3 hold no text (pictures only?)."
    assert "OCR" not in (hint or "")
