"""Pictures decoded, turned upright and encoded without their metadata (``workbench.executor.render``).

Every file is built at test time with invented content (``tests/fixtures/make_images.py``).
"""

import io
import random
import time
from pathlib import Path

import pytest
from PIL import Image

from tests.fixtures import make_images
from workbench.executor.readers import UnreadableFile
from workbench.executor.render import encode, images
from workbench.renders import IMAGE_QUALITY, RENDER_MAX_BYTES

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
