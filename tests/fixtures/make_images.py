"""Build small synthetic pictures at test time.

Every builder writes one file and returns its path. All content is invented: flat colours, noise from a fixed seed, and
GPS coordinates that name no place. The libraries are the ones the executor carries (Pillow) plus ``pillow_heif``, a dev
dependency, for the one HEIC writer; it is imported where it is used, so the other builders need none of it.
"""

import random
import struct
import zlib
from pathlib import Path

from PIL import ExifTags, Image

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)

#: Invented GPS values, in the EXIF form: degrees, minutes and seconds as rationals.
_GPS = {
    ExifTags.GPS.GPSLatitudeRef: "N",
    ExifTags.GPS.GPSLatitude: (12.0, 34.0, 56.0),
    ExifTags.GPS.GPSLongitudeRef: "E",
    ExifTags.GPS.GPSLongitude: (78.0, 9.0, 10.0),
}


def make_photo(path: Path, *, size: tuple[int, int] = (64, 48), orientation: int = 6, gps: bool = True) -> Path:
    """Write a JPEG photo: its left half red, its right half blue, with EXIF orientation and a GPS block.

    Args:
        path: Where to write it.
        size: Width and height of the stored pixels.
        orientation: The EXIF orientation tag; 6 means the picture is shown turned a quarter clockwise, so the left
            half of the stored pixels is the top of the picture.
        gps: Whether to add a GPS block with invented coordinates.

    Returns:
        ``path``.
    """
    width, height = size
    image = Image.new("RGB", size, RED)
    image.paste(Image.new("RGB", (width - width // 2, height), BLUE), (width // 2, 0))
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = orientation
    exif[ExifTags.Base.Make] = "Invented Camera Co."
    if gps:
        exif[ExifTags.Base.GPSInfo] = dict(_GPS)
    image.save(path, "JPEG", exif=exif, quality=95, subsampling=0)
    return path


def make_alpha_png(path: Path) -> Path:
    """Write an RGBA PNG, opaque red but for a fully transparent pixel at (0, 0).

    Args:
        path: Where to write it.

    Returns:
        ``path``.
    """
    image = Image.new("RGBA", (16, 16), (*RED, 255))
    image.putpixel((0, 0), (0, 0, 0, 0))
    image.save(path, "PNG")
    return path


def make_palette_png(path: Path) -> Path:
    """Write a palette PNG whose index 0 is red and transparent, and whose index 1 is blue; (0, 0) is index 0.

    Args:
        path: Where to write it.

    Returns:
        ``path``.
    """
    image = Image.new("P", (16, 16), 1)
    image.putpalette([*RED, *BLUE])
    image.putpixel((0, 0), 0)
    image.save(path, "PNG", transparency=0)
    return path


def make_noise(path: Path, size: tuple[int, int], fmt: str) -> Path:
    """Write a picture of random pixels, which no encoder makes small.

    Args:
        path: Where to write it.
        size: Width and height.
        fmt: The Pillow format name, such as ``JPEG`` or ``PNG``.

    Returns:
        ``path``.
    """
    width, height = size
    data = random.Random(7).randbytes(width * height * 3)
    Image.frombytes("RGB", size, data).save(path, fmt)
    return path


def make_gif(path: Path, frames: int = 3) -> Path:
    """Write an animated GIF: frame 0 red, the others green.

    Each green frame carries a mark of its own, since the encoder drops a frame that equals the one before it.

    Args:
        path: Where to write it.
        frames: How many frames, at least 2.

    Returns:
        ``path``.
    """
    images = [Image.new("RGB", (16, 16), RED)]
    for i in range(1, frames):
        green = Image.new("RGB", (16, 16), GREEN)
        green.putpixel((i, 15), BLUE)
        images.append(green)
    images[0].save(path, "GIF", save_all=True, append_images=images[1:], duration=100, loop=0)
    return path


def make_bomb_png(path: Path, width: int = 12000, height: int = 12000) -> Path:
    """Write a PNG that declares a huge size and holds almost no data, written by hand like a real bomb's header.

    Args:
        path: Where to write it.
        width: The width in the header.
        height: The height in the header.

    Returns:
        ``path``.
    """

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00" * 64))
        + chunk(b"IEND", b"")
    )
    return path


def make_cmyk_jpeg(path: Path) -> Path:
    """Write a CMYK JPEG of one colour, ``(0, 255, 255, 0)``: red.

    Args:
        path: Where to write it.

    Returns:
        ``path``.
    """
    Image.new("CMYK", (16, 16), (0, 255, 255, 0)).save(path, "JPEG", quality=95)
    return path


#: The colour a wide-gamut picture stores, and what it is in sRGB under :func:`wide_gamut_profile`.
WIDE_STORED = (200, 120, 60)
WIDE_IN_SRGB = (213, 115, 42)


def _s15(value: float) -> bytes:
    """An ICC ``s15Fixed16Number``: a signed fixed-point number, 16 bits of fraction, big-endian."""
    return struct.pack(">i", round(value * 65536))


def _xyz(x: float, y: float, z: float) -> bytes:
    """An ICC ``XYZType`` tag of one colour."""
    return b"XYZ \0\0\0\0" + _s15(x) + _s15(y) + _s15(z)


def wide_gamut_profile() -> bytes:
    """Build an ICC v4 RGB display profile of a wide gamut, as phones and screenshots carry one, from its numbers.

    A matrix/TRC profile: the colorants are those of Display P3 adapted to D50 (published values), and each channel
    has the sRGB tone curve as a parametric curve. It is built here, byte by byte, so that no binary fixture and no
    profile of the system is needed. Under it, :data:`WIDE_STORED` is :data:`WIDE_IN_SRGB` in sRGB.

    Returns:
        The profile's bytes.
    """
    d50 = (0.9642, 1.0, 0.8249)
    curve = b"para\0\0\0\0" + struct.pack(">HH", 3, 0)
    curve += b"".join(_s15(v) for v in (2.4, 1 / 1.055, 0.055 / 1.055, 1 / 12.92, 0.04045))
    name = "Invented wide gamut".encode("utf-16-be")
    description = b"mluc\0\0\0\0" + struct.pack(">II", 1, 12) + b"enUS" + struct.pack(">II", len(name), 28) + name
    tags = [
        (b"desc", description),
        (b"wtpt", _xyz(*d50)),
        (b"rXYZ", _xyz(0.515121, 0.241196, -0.001053)),
        (b"gXYZ", _xyz(0.291977, 0.692245, 0.041885)),
        (b"bXYZ", _xyz(0.157104, 0.066574, 0.784073)),
        (b"rTRC", curve),
        (b"gTRC", curve),
        (b"bTRC", curve),
    ]
    offset = 128 + 4 + 12 * len(tags)
    table, body = struct.pack(">I", len(tags)), b""
    for signature, data in tags:
        data += b"\0" * (-len(data) % 4)
        table += signature + struct.pack(">II", offset + len(body), len(data))
        body += data
    header = (
        struct.pack(">I", 128 + len(table) + len(body))
        + b"\0\0\0\0"
        + struct.pack(">I", 0x04300000)
        + b"mntrRGB XYZ "
        + struct.pack(">6H", 2026, 1, 1, 0, 0, 0)
        + b"acsp"
        + b"\0" * 28
        + b"".join(_s15(v) for v in d50)
        + b"\0" * 48
    )
    return header + table + body


def make_wide_gamut(path: Path, fmt: str, *, mode: str = "RGB") -> Path:
    """Write a picture of :data:`WIDE_STORED` tagged with :func:`wide_gamut_profile`.

    Args:
        path: Where to write it.
        fmt: ``PNG`` or ``JPEG``.
        mode: ``RGB``; ``RGBA``, its right half fully transparent; or ``P``, a palette of that one colour.

    Returns:
        ``path``.
    """
    image = Image.new("RGB", (16, 16), WIDE_STORED)
    if mode == "RGBA":
        image = image.convert("RGBA")
        image.paste(Image.new("RGBA", (8, 16), (0, 0, 0, 0)), (8, 0))
    elif mode == "P":
        image = image.convert("P", palette=Image.Palette.ADAPTIVE, colors=2)
    extra = {"quality": 95, "subsampling": 0} if fmt == "JPEG" else {}
    image.save(path, fmt, icc_profile=wide_gamut_profile(), **extra)
    return path


def make_png16(path: Path) -> Path:
    """Write a 16-bit grey PNG (mode ``I;16``) of one value, 32768: mid grey.

    Args:
        path: Where to write it.

    Returns:
        ``path``.
    """
    Image.new("I;16", (16, 16), 32768).save(path, "PNG")
    return path


def make_heic(path: Path) -> Path:
    """Write a 64 x 48 HEIC photograph, left half red and right half blue.

    Written by ``pillow_heif`` directly, without registering an opener with Pillow, so a test that opens the file is
    served by the executor's own decoder and no other.

    Args:
        path: Where to write it.

    Returns:
        ``path``.
    """
    import pillow_heif

    image = Image.new("RGB", (64, 48), RED)
    image.paste(Image.new("RGB", (32, 48), BLUE), (32, 0))
    pillow_heif.from_pillow(image).save(path, quality=90)
    return path
