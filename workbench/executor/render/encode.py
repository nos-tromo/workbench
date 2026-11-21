"""Encode a picture as a JPEG or PNG under the size cap, with no metadata, and cut it into strips."""

import io
from typing import Final

from PIL import Image

from workbench.executor.readers import DAMAGED, TOO_BIG, UnreadableFile
from workbench.executor.render.images import on_white
from workbench.renders import QUALITY_FLOOR, RENDER_MAX_BYTES, strip_spans

#: How much each pass of the ladder scales the picture down, and how many passes it may make after the first.
_SCALE_STEP: Final = 0.75
_SCALE_PASSES: Final = 3

#: How many points the quality drops at each step of the ladder.
_QUALITY_STEP: Final = 10

_NO_FIT: Final = "the picture cannot be made small enough to show"

MIME_PNG: Final = "image/png"
MIME_JPEG: Final = "image/jpeg"


def _save(image: Image.Image, fmt: str, **options: int | bool) -> bytes:
    """Save a picture to bytes: a copy with nothing in its ``info``, and no ``exif``, ``icc_profile`` or ``xmp``.

    Args:
        image: The picture.
        fmt: The Pillow format name, ``PNG`` or ``JPEG``.
        **options: The encoder's options (quality, subsampling, ``optimize``).

    Returns:
        The encoded bytes.
    """
    fresh = image.copy()
    fresh.info = {}
    buffer = io.BytesIO()
    fresh.save(buffer, fmt, **options)
    return buffer.getvalue()


def _jpeg_ladder(image: Image.Image, quality: int, subsampling: int) -> bytes | None:
    """Try a JPEG at ``quality``, then ten points lower each step down to the floor.

    Args:
        image: The picture.
        quality: The first quality to try.
        subsampling: The chroma subsampling, Pillow's 0 (4:4:4), 1 (4:2:2) or 2 (4:2:0).

    Returns:
        The first encoding under the cap, or ``None`` when even the floor is over it.
    """
    step = quality
    while True:
        data = _save(image, "JPEG", quality=step, subsampling=subsampling)
        if len(data) < RENDER_MAX_BYTES:
            return data
        if step <= QUALITY_FLOOR:
            return None
        step = max(step - _QUALITY_STEP, QUALITY_FLOOR)


def encode(image: Image.Image, *, lossless: bool, quality: int, subsampling: int) -> tuple[bytes, str]:
    """Encode a picture strictly under ``RENDER_MAX_BYTES``, carrying nothing of what its source carried.

    A lossless source is written as PNG when that fits. Otherwise the picture is a JPEG, its quality stepped down to
    ``QUALITY_FLOOR``; if that is still too big the picture is scaled to three quarters and the steps run again, up to
    three times.

    Args:
        image: The picture, ``RGB`` (``L`` is accepted); another mode is converted to ``RGB`` first, one that carries
            transparency laid on white.
        lossless: Try PNG first.
        quality: The JPEG quality to start from.
        subsampling: The JPEG chroma subsampling, 0 for 4:4:4.

    Returns:
        The bytes and their type, ``image/png`` or ``image/jpeg``.

    Raises:
        UnreadableFile: Nothing fits, or the picture is too large to encode within the memory limit.
    """
    try:
        if image.mode not in ("RGB", "L"):
            image = on_white(image.convert("RGBA") if image.has_transparency_data else image)
        if lossless:
            data = _save(image, "PNG", optimize=False)
            if len(data) < RENDER_MAX_BYTES:
                return data, MIME_PNG
        current = image
        for _ in range(_SCALE_PASSES + 1):
            data = _jpeg_ladder(current, quality, subsampling)
            if data is not None:
                return data, MIME_JPEG
            size = (max(1, int(current.width * _SCALE_STEP)), max(1, int(current.height * _SCALE_STEP)))
            current = current.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
    except MemoryError:
        raise UnreadableFile(TOO_BIG) from None
    except Exception:
        raise UnreadableFile(DAMAGED) from None
    raise UnreadableFile(_NO_FIT)


def cut(image: Image.Image, strips: int) -> list[Image.Image]:
    """Cut a picture into strips along its long side, each overlapping its neighbour.

    A portrait picture (taller than wide, or square) gives horizontal bands, a landscape one vertical bands, as
    :func:`workbench.renders.strip_spans` places them.

    Args:
        image: The picture.
        strips: How many strips, 1 to ``MAX_STRIPS``.

    Returns:
        The strips in order; one strip is the whole picture.
    """
    width, height = image.size
    if height >= width:
        return [image.crop((0, start, width, end)) for start, end in strip_spans(height, strips)]
    return [image.crop((start, 0, end, height)) for start, end in strip_spans(width, strips)]
