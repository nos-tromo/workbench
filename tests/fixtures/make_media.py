"""Build the two artifacts Nextext keeps of a clip, at test time and with invented content.

Nextext's JSON Lines has one record per sentence (the keys ``end_seconds``, ``end_ts``, ``language``,
``sentence_index``, ``source_file``, ``source_file_hash``, ``start_seconds``, ``start_ts`` and ``text``, and ``speaker``
only when the clip was diarized; times are in whole seconds). Its keyframes zip holds ``frame_000.jpg`` and on, and
``manifest.json``, which is ``{"frames": [{"file", "index", "time_sec"}]}``. No clip is built: workbench never decodes
one. Every builder writes one file, or returns bytes, and every name, colour and sentence is invented.
"""

import io
import json
import struct
import warnings
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from PIL import Image

from workbench.renders import stamp

#: What the records of a transcript say of their clip, as Nextext writes them.
_SOURCE_FILE = "media.mp4"
_SOURCE_HASH = "sha256:" + "0" * 64


def jsonl_record(start: float, end: float, text: str, *, speaker: str | None = None, index: int = 0) -> dict[str, Any]:
    """Make one record of Nextext's JSON Lines.

    Args:
        start: When the sentence begins, in seconds.
        end: When it ends, in seconds.
        text: The sentence.
        speaker: The speaker's label, such as ``Speaker 1``; left out of the record when ``None``.
        index: The sentence's number in the clip.

    Returns:
        The record, with Nextext's keys.
    """
    record: dict[str, Any] = {
        "source_file": _SOURCE_FILE,
        "source_file_hash": _SOURCE_HASH,
        "language": "en",
        "sentence_index": index,
        "start_seconds": start,
        "end_seconds": end,
        "start_ts": stamp(start),
        "end_ts": stamp(end),
        "text": text,
    }
    if speaker is not None:
        record["speaker"] = speaker
    return record


def make_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> Path:
    """Write records as JSON Lines, one per line, as Nextext does (``ensure_ascii`` off).

    Args:
        path: Where to write it.
        records: The records, such as :func:`jsonl_record` makes.

    Returns:
        ``path``.
    """
    lines = (json.dumps(record, ensure_ascii=False) for record in records)
    path.write_bytes("".join(f"{line}\n" for line in lines).encode("utf-8"))
    return path


def frame_colour(index: int) -> tuple[int, int, int]:
    """Say what colour frame ``index`` of a zip is, so that a test can tell which frame it is looking at.

    Args:
        index: The frame's number in Nextext's count, from 0.

    Returns:
        An RGB colour that differs from its neighbours' by a wide margin.
    """
    return (40 + index * 70) % 256, (200 - index * 37) % 256, (90 + index * 111) % 256


def _jpeg(size: tuple[int, int], colour: tuple[int, int, int]) -> bytes:
    """Encode a flat picture as a JPEG.

    Args:
        size: Width and height.
        colour: The colour of every pixel.

    Returns:
        The JPEG's bytes.
    """
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, "JPEG", quality=90, subsampling=0)
    return buffer.getvalue()


def make_keyframes_zip(
    path: Path,
    frames: int = 3,
    *,
    size: tuple[int, int] = (64, 36),
    times: Sequence[float] | None = (0.0, 4.0, 8.0),
    extra: Mapping[str, bytes] | None = None,
) -> Path:
    """Write a keyframes zip: ``frame_000.jpg`` and on in distinct colours, and a manifest that gives their times.

    Args:
        path: Where to write it.
        frames: How many frames.
        size: Width and height of each, in pixels.
        times: The time of each frame in seconds, for as many frames as it has entries; ``None`` writes no
            ``manifest.json`` at all.
        extra: More members to write after those, by name, such as a second ``frame_000.jpg`` (which makes a
            duplicate) or a name no keyframes zip has.

    Returns:
        ``path``.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # a duplicate name is a case under test
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
            for index in range(frames):
                archive.writestr(f"frame_{index:03d}.jpg", _jpeg(size, frame_colour(index)))
            if times is not None:
                manifest = {
                    "frames": [
                        {"file": f"frame_{index:03d}.jpg", "index": index, "time_sec": time_s}
                        for index, time_s in zip(range(frames), times, strict=False)
                    ]
                }
                archive.writestr("manifest.json", json.dumps(manifest))
            for name, data in (extra or {}).items():
                archive.writestr(name, data)
    return path


def jpeg_claiming(width: int, height: int) -> bytes:
    """Make a small JPEG whose frame header says it is another size, as a decompression bomb's does.

    Args:
        width: The width the header claims, up to 65535.
        height: The height the header claims, up to 65535.

    Returns:
        The JPEG's bytes: an 8 by 8 picture with its ``SOF0`` segment rewritten.
    """
    data = bytearray(_jpeg((8, 8), (200, 30, 30)))
    offset = 2  # past the start-of-image marker
    while offset + 4 <= len(data):
        marker = data[offset + 1]
        length = struct.unpack(">H", data[offset + 2 : offset + 4])[0]
        if marker == 0xC0:  # SOF0: precision (1), height (2), width (2)
            data[offset + 5 : offset + 9] = struct.pack(">HH", height, width)
            return bytes(data)
        offset += 2 + length
    raise ValueError("the picture has no SOF0 segment")  # the test's own mistake: Pillow writes one
