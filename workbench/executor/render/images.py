"""Decode a source picture, bring it to the bounds of a render and turn it upright.

The order keeps memory small: the picture is opened without reading its pixels, a JPEG is decoded at a reduced size,
and the orientation is applied last, on the small picture, so nothing large is copied for it. The source's metadata is
read for the orientation only; none of it is carried into the result (:mod:`~workbench.executor.render.encode` also
writes none).
"""

import io
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Final, cast

from PIL import ExifTags, Image, ImageCms

from workbench.executor.readers import DAMAGED, TOO_BIG, UnreadableFile
from workbench.renders import IMAGE_LONG_SIDE, IMAGE_MAX_PIXELS, SOURCE_MAX_PIXELS, target_size

#: The formats a picture may be, by what its content says and whatever its name is; ``HEIF`` is pi-heif's.
FORMATS: Final = ["PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF", "HEIF"]

#: The decoded formats that are written as PNG when that fits.
LOSSLESS_FORMATS: Final = frozenset({"PNG", "GIF", "BMP"})

_TOO_MANY_PIXELS: Final = "the picture is larger than 120 megapixels"

#: What each EXIF orientation takes to be upright, as a Pillow transpose; 1 (and anything else) turns nothing.
_UPRIGHT: Final = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}

_WHITE: Final = (255, 255, 255)
_ORIENTATION: Final = ExifTags.Base.Orientation


@dataclass(frozen=True)
class Normalised:
    """A picture ready to encode.

    Attributes:
        image: The picture, ``RGB``, upright, within the bounds asked for, with no metadata.
        lossless: Whether the source was a PNG, GIF or BMP, which is kept as PNG when it fits.
        frames: How many frames the source has; only the first is in ``image``.
    """

    image: Image.Image
    lossless: bool
    frames: int


@cache
def _register_heif() -> None:
    """Teach Pillow to read HEIC once per process: decode only, one decoder thread, nothing but the main image.

    One decoder thread keeps a job within its ``nproc`` and CPU limits; thumbnails, depth maps and auxiliary images
    are never read.
    """
    import pi_heif

    pi_heif.register_heif_opener(decode_threads=1, thumbnails=False, depth_images=False, aux_images=False)


@contextmanager
def _bomb_guard() -> Iterator[None]:
    """Hold Pillow to the source limit for the run of the block: its limit set, its warning an error.

    Pillow warns above ``Image.MAX_IMAGE_PIXELS`` and refuses above twice that; the warning is made an error, so both
    end the same way. Pillow's limit and the warning filters are put back afterwards.

    Raises:
        UnreadableFile: Pillow found a picture over the limit, in the block.
    """
    before = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = SOURCE_MAX_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            yield
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise UnreadableFile(_TOO_MANY_PIXELS) from None
    finally:
        Image.MAX_IMAGE_PIXELS = before


def _open(path: Path) -> Image.Image:
    """Open a picture by its content, refusing one that declares more pixels than a source may have.

    Args:
        path: The file.

    Returns:
        The image, its pixels not yet read.

    Raises:
        UnreadableFile: The picture declares more pixels than a source may have.
        OSError: Not a picture of a known kind (Pillow's ``UnidentifiedImageError``); the caller makes it unreadable.
    """
    image = Image.open(path, formats=FORMATS)
    if image.width * image.height > SOURCE_MAX_PIXELS:  # whatever Pillow's own limit is
        image.close()
        raise UnreadableFile(_TOO_MANY_PIXELS)
    return image


def _orientation(image: Image.Image) -> int:
    """Read the EXIF orientation of a picture; a block that cannot be read turns nothing.

    Args:
        image: The opened picture.

    Returns:
        The tag, 1 to 8; 1 when there is none or it cannot be read.
    """
    try:
        value = image.getexif().get(_ORIENTATION, 1)
    except Exception:  # a damaged EXIF block must not cost the picture
        return 1
    return value if isinstance(value, int) and value in _UPRIGHT else 1


def _grey(image: Image.Image) -> Image.Image:
    """Scale a 16-bit or 32-bit grey picture to 8-bit grey, the whole 16-bit range onto 0-255.

    Args:
        image: A picture of mode ``I;16``, ``I;16L``, ``I;16B``, ``I;16N`` or ``I``.

    Returns:
        The picture in mode ``L``.
    """
    top = 65535
    if image.mode == "I":
        _, high = cast(tuple[int, int], image.getextrema())
        top = 255 if high <= 255 else max(high, 65535)  # 8-bit values stored wide are not shrunk
    elif image.mode != "I;16":
        image = image.convert("I")  # point() reads only the native 16-bit mode; the other byte orders go through I
    return image.point(lambda value: value * (255 / top) + 0.5).convert("L")


def _to_rgb(image: Image.Image) -> Image.Image:
    """Convert a picture to RGB through its ICC profile when it has one that works.

    Args:
        image: A picture of a mode that is not ``RGB`` (CMYK, YCbCr and the like).

    Returns:
        The picture in mode ``RGB``, in sRGB when the profile was used.
    """
    profile = image.info.get("icc_profile")
    if profile:
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(profile))
            converted = ImageCms.profileToProfile(image, source, ImageCms.createProfile("sRGB"), outputMode="RGB")
        except Exception:  # a broken profile, or none the library can use: the plain conversion
            converted = None
        if converted is not None:
            return converted
    return image.convert("RGB")


def _convert(image: Image.Image) -> Image.Image:
    """Bring a decoded picture to a mode a resize keeps the colours in: ``L``, ``RGB`` or ``RGBA``.

    Args:
        image: The picture, its pixels read.

    Returns:
        The picture in mode ``L``, ``RGB`` or ``RGBA``; ``RGBA`` whenever the source says part of it is transparent.
    """
    mode = image.mode
    if mode.startswith("I"):
        return _grey(image)
    if mode == "1":
        return image.convert("L")  # a bilevel scan is scaled as grey, or a thin line drops out
    if mode in ("RGBA", "LA", "PA", "La", "RGBa") or "transparency" in image.info:
        return image.convert("RGBA")
    if mode in ("L", "RGB"):
        return image
    return _to_rgb(image)


def _upright(image: Image.Image, orientation: int) -> Image.Image:
    """Turn a picture so that its EXIF orientation is the normal one.

    Args:
        image: The picture, already small.
        orientation: The tag, 1 to 8.

    Returns:
        The picture, turned or flipped, or as it is for 1.
    """
    method = _UPRIGHT.get(orientation)
    return image if method is None else image.transpose(method)


def on_white(image: Image.Image) -> Image.Image:
    """Lay a picture on white and return it as RGB, so that transparency never reads as black.

    Also used by :func:`~workbench.executor.render.encode.encode` for a picture it is handed that is not already RGB.

    Args:
        image: The picture, ``L``, ``RGB`` or ``RGBA``.

    Returns:
        The picture in mode ``RGB``.
    """
    if image.mode == "RGBA":
        ground = Image.new("RGB", image.size, _WHITE)
        ground.paste(image, mask=image.getchannel("A"))
        return ground
    return image if image.mode == "RGB" else image.convert("RGB")


def normalise(path: Path, *, long_side: int = IMAGE_LONG_SIDE, max_pixels: int = IMAGE_MAX_PIXELS) -> Normalised:
    """Decode a picture and bring it to a size and form a model can look at.

    Args:
        path: The file; its name decides nothing, its content does.
        long_side: Most pixels on the longer side of the result.
        max_pixels: Most pixels in the result.

    Returns:
        The picture upright and ``RGB`` with no metadata, whether its source is kept lossless, and its frame count.

    Raises:
        UnreadableFile: Not a picture of a known kind, damaged, over 120 megapixels, or too large for the memory limit.
    """
    try:
        _register_heif()
        with _bomb_guard():
            source = _open(path)
            try:
                frames = getattr(source, "n_frames", 1)
                source.seek(0)
                lossless = source.format in LOSSLESS_FORMATS
                size = target_size(source.width, source.height, long_side=long_side, max_pixels=max_pixels)
                if source.format in ("JPEG", "MPO"):
                    source.draft("RGB", size)  # the decoder scales by 1/2, 1/4 or 1/8 while it reads
                source.load()
                # After the load, since a PNG's EXIF block may follow its pixels; the block is not read for more.
                orientation = _orientation(source)
                image = _convert(source)
                if image.size != size:
                    image = image.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
                image = on_white(_upright(image, orientation))
                if image is source:
                    image = (
                        source.copy()
                    )  # closing the source frees its pixels; this one is within the bounds, so small
            finally:
                source.close()
    except UnreadableFile:
        raise
    except MemoryError:
        raise UnreadableFile(TOO_BIG) from None
    except Exception:
        raise UnreadableFile(DAMAGED) from None
    image.info = {}
    return Normalised(image=image, lossless=lossless, frames=frames)
