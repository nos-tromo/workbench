"""A clip's transcript and frames, made from the two artifacts Nextext keeps of it.

The clip itself never enters a job: no decoder of audio or video lives in workbench. What a job reads are Nextext's
JSON Lines (one record per sentence) and its keyframes zip, which the api keeps as derived files of the clip and hands
the job at their derived paths. The JSON Lines becomes ``transcript.txt``, one stamped line per sentence; the zip's
frames are brought to :data:`~workbench.renders.FRAME_LONG_SIDE` pixels as any picture is
(:mod:`~workbench.executor.render.images`) and encoded without metadata. A transcript that cannot be read leaves the
frames, and frames that cannot be read leave the transcript, each with a hint; only when nothing usable comes from the
inputs there are does the job fail.

Both artifacts are hostile input like any file a user brings: a zip is checked as an Office file's is
(:func:`workbench.executor.readers.check_zip`) and then member by member, and a picture's header is read before one
pixel is. Nothing is written until every output is made, so a failure leaves ``out/`` as it was. The answer is a
:class:`workbench.renders.RenderAnswer` as JSON: no path, no file name, no line of the transcript.
"""

import io
import itertools
import json
import re
import time
import zipfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from PIL import Image
from pydantic import ValidationError

from workbench import protocol
from workbench.executor import readers
from workbench.executor.context import JobContext
from workbench.executor.readers import DAMAGED, TOO_BIG, UnreadableFile
from workbench.executor.render import encode, images
from workbench.executor.runners.documents import error
from workbench.executor.timer import limit_until
from workbench.renders import (
    FRAME_LONG_SIDE,
    FRAME_QUALITY,
    IMAGE_MAX_PIXELS,
    JSONL_MAX_BYTES,
    MEDIA_FRAMES_MAX,
    NO_SPEECH,
    NOTHING_FOUND,
    SIGNATURES,
    TRANSCRIPT_MAX_BYTES,
    TRANSCRIPT_NAME,
    TRANSCRIPT_UNREADABLE,
    ZIP_MAX_BYTES,
    MediaRequest,
    RenderAnswer,
    RenderEntry,
    render_name,
    stamp,
)

#: What a zip that is not a set of keyframes is told: a member that is neither ``frame_NNN.jpg`` nor the manifest, a
#: frame that is no JPEG, no frame at all.
NOT_KEYFRAMES: Final = "the file is not a set of keyframes"

#: What the job answers when neither artifact gave anything, and when the outputs could not be written.
_NOTHING_USABLE: Final = "nothing in the clip's artifacts could be read"
_NOT_WRITTEN: Final = "the renders could not be written"

#: What the hint says of each thing that went wrong or was left out, in the order the hint gives them (the
#: transcript's, which a read of the clip says too, is ``workbench.renders.TRANSCRIPT_UNREADABLE``).
_FRAMES_UNREADABLE: Final = "The frames could not be read."
_NO_FRAMES_TAKEN: Final = "No frames were taken from this clip."

_BUDGET_S: Final = 60.0

#: A speaker's label as the transcript writes it: a plain, short name such as ``Speaker 2``. Anything else (a newline,
#: a long text, a label that is no text) is left out, so a label can never be a line or a sentence of its own.
_SPEAKER: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}")

#: The one name a frame of Nextext's zip has, and the one other member it may hold. ASCII digits only.
_FRAME_NAME: Final = re.compile(r"frame_([0-9]{3,6})\.jpg")
_MANIFEST: Final = "manifest.json"
_MANIFEST_MAX_BYTES: Final = 1024 * 1024

#: The latest a time of a clip may be: 100 hours, past what a stamp reads (``renders.parse_stamp`` stops at 99:59:59).
_SECONDS_MAX: Final = 100.0 * 3600

#: Pillow's JPEG chroma subsampling for a frame: 4:2:0, as for a page.
_FRAME_SUBSAMPLING: Final = 2


@dataclass(frozen=True)
class Transcript:
    """What a clip's JSON Lines came to.

    Attributes:
        text: The transcript, one ``[mm:ss] Speaker N: text`` line per sentence, each ending in a newline.
        lines: How many lines ``text`` has, at least 1.
        duration_s: When the last record ends, in seconds, over every record that counts, also those past a cut.
        cut_at: Where the text stops, the start in seconds of the first sentence left out for length; ``None`` when
            all of it is kept.
        skipped: How many records were left out for having no usable time or no words.
    """

    text: str
    lines: int
    duration_s: float | None
    cut_at: float | None
    skipped: int


@dataclass(frozen=True)
class Frame:
    """One frame of a clip, encoded, not yet written.

    Attributes:
        n: The frame's place among those kept, in time order, counted from 1.
        time_s: When it was taken, in seconds; ``None`` when the zip had no time for it.
        data: The JPEG.
        width: Its width, read from its header.
        height: Its height.
        mime: Its type, always ``image/jpeg``.
    """

    n: int
    time_s: float | None
    data: bytes
    width: int
    height: int
    mime: str


def _seconds(value: object) -> float | None:
    """Read a time in seconds from a JSON value.

    Args:
        value: What a record or the manifest says.

    Returns:
        The number as a float; ``None`` for a boolean, anything that is not a number, and a number that is not a time
        of a clip: not finite (``NaN``, an infinity, an integer too large for a float), below 0, or past
        ``_SECONDS_MAX``, so that no stamp in a line or a hint is longer than a clip's.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if 0 <= seconds <= _SECONDS_MAX else None  # false for NaN


def read_transcript(path: Path) -> Transcript:
    """Make the transcript of a clip out of Nextext's JSON Lines.

    A record is a line of JSON, and the lines end at a newline alone: Nextext writes its text as it is, so a sentence
    may hold a character that other readers take for a line break. Each record gives one line, its start as the stamp,
    the speaker's label when it is a plain one, and its words with every run of whitespace, a line break among it, made
    one space. Nothing else of the record is kept (not the file's name or hash, the language or the sentence's number).

    Args:
        path: The JSON Lines file.

    Returns:
        The transcript, cut at the last whole line within ``TRANSCRIPT_MAX_BYTES``.

    Raises:
        UnreadableFile: The file is too large, is not UTF-8, has a line that is not JSON, or no record in it has a
            usable time and words (or the first line alone passes the cap).
        readers.ReadTimeout: The deadline passed.
    """
    try:
        if path.stat().st_size > JSONL_MAX_BYTES:
            raise UnreadableFile(TOO_BIG)
        text = path.read_bytes().decode("utf-8-sig")  # a BOM leads a file some tools wrote
    except UnreadableFile:
        raise
    except MemoryError:
        raise UnreadableFile(TOO_BIG) from None
    except (OSError, UnicodeDecodeError):  # its text would quote the path or the bytes
        raise UnreadableFile(DAMAGED) from None
    chunks: list[bytes] = []
    size = 0
    skipped = 0
    duration: float | None = None
    cut_at: float | None = None
    for raw in text.split("\n"):
        readers.tick()
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except (ValueError, RecursionError):  # a decoder's message holds a piece of the line
            raise UnreadableFile(DAMAGED) from None
        if not isinstance(record, dict):
            skipped += 1
            continue
        start, end = _seconds(record.get("start_seconds")), _seconds(record.get("end_seconds"))
        said = record.get("text")
        if start is None or end is None or not isinstance(said, str) or not (words := " ".join(said.split())):
            skipped += 1
            continue
        speaker = record.get("speaker")
        who = f"{speaker.strip()}: " if isinstance(speaker, str) and _SPEAKER.fullmatch(speaker.strip()) else ""
        duration = max(duration or 0.0, end)  # every record counts, also past a cut
        line = f"[{stamp(start)}] {who}{words}\n".encode("utf-8", "replace")  # JSON may carry a lone surrogate
        if cut_at is None and size + len(line) > TRANSCRIPT_MAX_BYTES:
            cut_at = start
        if cut_at is None:
            chunks.append(line)
            size += len(line)
    if not chunks:
        raise UnreadableFile(DAMAGED)
    return Transcript(
        text=b"".join(chunks).decode("utf-8"), lines=len(chunks), duration_s=duration, cut_at=cut_at, skipped=skipped
    )


def _members(archive: zipfile.ZipFile) -> tuple[list[zipfile.ZipInfo], zipfile.ZipInfo | None]:
    """Sort the members of a keyframes zip, and refuse the zip for any that does not belong.

    Args:
        archive: The opened zip, whose directory ``check_zip`` has passed.

    Returns:
        The frames in the order of the numbers in their names, and the manifest, or ``None`` when there is none.

    Raises:
        UnreadableFile: A member is a folder, is named other than ``frame_NNN.jpg`` or ``manifest.json``, is named
            twice or by a number twice, or declares more bytes than a zip may; or there is no frame.
    """
    frames: dict[int, zipfile.ZipInfo] = {}
    manifest: zipfile.ZipInfo | None = None
    for info in archive.infolist():
        if info.is_dir() or info.file_size > ZIP_MAX_BYTES:
            raise UnreadableFile(NOT_KEYFRAMES)
        if info.filename == _MANIFEST:
            if manifest is not None:
                raise UnreadableFile(NOT_KEYFRAMES)
            manifest = info
            continue
        named = _FRAME_NAME.fullmatch(info.filename)
        if named is None or int(named.group(1)) in frames:
            raise UnreadableFile(NOT_KEYFRAMES)
        frames[int(named.group(1))] = info
    if not frames:
        raise UnreadableFile(NOT_KEYFRAMES)
    return [frames[number] for number in sorted(frames)], manifest


def _times(archive: zipfile.ZipFile, manifest: zipfile.ZipInfo | None) -> dict[str, float]:
    """Read when each frame was taken from the manifest, as far as it says.

    Args:
        archive: The opened zip.
        manifest: Its ``manifest.json``, or ``None``.

    Returns:
        The time in seconds of each frame the manifest names by its file name, for those given a time of a clip. A
        manifest that is too large or does not read is ignored: it is a courtesy.
    """
    if manifest is None or manifest.file_size > _MANIFEST_MAX_BYTES:
        return {}
    try:
        with archive.open(manifest) as member:
            document = json.loads(member.read(manifest.file_size))
    except Exception:  # a damaged or odd manifest must not cost the frames
        return {}
    entries = document.get("frames") if isinstance(document, dict) else None
    times: dict[str, float] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            continue
        seconds = _seconds(entry.get("time_sec"))
        if seconds is not None:
            times[entry["file"]] = seconds
    return times


def _ordered(times: dict[str, float], members: list[zipfile.ZipInfo]) -> dict[str, float]:
    """Trust the manifest's times only when they run forward with the frame numbers.

    Frames are numbered by their names, and a frame's number is its place in time order. A manifest that puts a later
    frame earlier in the clip than one before it contradicts that, and nothing in it can be told from a mistake, so
    none of its times is kept, as when it does not read.

    Args:
        times: The time of each frame the manifest gave one.
        members: The frames of the zip in the order of their numbers.

    Returns:
        ``times`` when those given never decrease along the frames; otherwise no times.
    """
    given = [times[info.filename] for info in members if info.filename in times]
    return times if all(later >= earlier for earlier, later in itertools.pairwise(given)) else {}


def _spread(count: int) -> list[int]:
    """Pick which of ``count`` frames to keep.

    Args:
        count: How many frames the zip holds.

    Returns:
        Every position when there are at most ``MEDIA_FRAMES_MAX``; otherwise that many, from the first to the last,
        spread evenly.
    """
    if count <= MEDIA_FRAMES_MAX:
        return list(range(count))
    return [round(i * (count - 1) / (MEDIA_FRAMES_MAX - 1)) for i in range(MEDIA_FRAMES_MAX)]


def _size_of(data: bytes) -> tuple[int, int]:
    """Read the size an encoded picture was written at from its header.

    Args:
        data: The JPEG.

    Returns:
        Its width and height.
    """
    with Image.open(io.BytesIO(data)) as header:
        return header.size


def read_frames(path: Path, *, file_mb: int) -> tuple[list[Frame], int]:
    """Make the frames of a clip out of Nextext's keyframes zip.

    The zip is checked as an Office file's is, and then by its names: ``frame_NNN.jpg`` and ``manifest.json``, once
    each, and nothing else. At most ``MEDIA_FRAMES_MAX`` frames are kept, spread evenly from the first to the last, and
    only those are read. The manifest's times are kept only when they never decrease along the frames. Each frame is
    read as far as it declares, must begin as a JPEG, and is brought to ``FRAME_LONG_SIDE`` and encoded under the render
    cap with none of its metadata. Any frame that fails refuses the whole set.

    Args:
        path: The zip.
        file_mb: The largest file a job may write, in megabytes; it bounds what the zip may unpack to.

    Returns:
        The frames kept, in time order and numbered from 1, and how many frames the zip held.

    Raises:
        UnreadableFile: ``NOT_KEYFRAMES`` for a zip that is none, or a frame that is no JPEG; otherwise the zip or a
            frame is damaged, encrypted, too large, or a picture over 120 megapixels.
        readers.ReadTimeout: The deadline passed.
    """
    try:
        readers.check_zip(path, file_mb)
        with zipfile.ZipFile(path) as archive:
            members, manifest = _members(archive)
            times = _ordered(_times(archive, manifest), members)
            frames: list[Frame] = []
            for position in _spread(len(members)):
                readers.check()
                info = members[position]
                with archive.open(info) as member:
                    data = member.read(info.file_size)  # never more than it declares
                if not data.startswith(SIGNATURES["image/jpeg"]):
                    raise UnreadableFile(NOT_KEYFRAMES)
                normalised = images.normalise(io.BytesIO(data), long_side=FRAME_LONG_SIDE, max_pixels=IMAGE_MAX_PIXELS)
                readers.check()
                jpeg, mime = encode.encode(
                    normalised.image, lossless=False, quality=FRAME_QUALITY, subsampling=_FRAME_SUBSAMPLING
                )
                width, height = _size_of(jpeg)
                frames.append(
                    Frame(
                        n=len(frames) + 1,
                        time_s=times.get(info.filename),
                        data=jpeg,
                        width=width,
                        height=height,
                        mime=mime,
                    )
                )
    except UnreadableFile:
        raise
    except MemoryError:
        raise UnreadableFile(TOO_BIG) from None
    except Exception:  # a damaged member or an unsupported compression: nothing here names the file
        raise UnreadableFile(DAMAGED) from None
    return frames, len(members)


def _artifact(ctx: JobContext, relative: str) -> Path:
    """Find an artifact the job was handed.

    Args:
        ctx: The job's surroundings.
        relative: Its path below the chat folder, as the request names it.

    Returns:
        The file.

    Raises:
        UnreadableFile: There is no such file in the job folder, or it cannot be looked at.
    """
    real = ctx.work_dir / relative
    try:
        found = real.is_file()
    except OSError:  # its text holds the path
        found = False
    if not found:
        raise UnreadableFile(DAMAGED)
    return real


def prepare_media(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
    """Make the transcript and the frames of a clip into ``out/``, from the artifacts the job was handed.

    Args:
        args: ``media``, a :class:`workbench.renders.MediaRequest` as JSON: the paths of the two artifacts below the
            chat folder (each may be ``None``) and whether the clip is a video. ``path``, the clip's own, is never
            read: the clip is no input of the job. ``budget_s``, the seconds after which the job stops, may be given.
        ctx: The job's surroundings.

    Returns:
        A :class:`workbench.renders.RenderAnswer` as a dict, of kind ``media``: ``total`` the frames, ``renders`` the
        frames as ``p0001.jpg`` and on, and ``transcript`` when there is one (``transcript.txt``), every file in
        ``ctx.out_dir``; with a hint for what could not be read, what was cut and what the clip has none of. Or an
        ``error`` result: ``unreadable_file`` (neither artifact gave anything usable, or the outputs could not be
        written) or ``read_timeout``, neither of which names a path.

    Raises:
        ValueError: ``media`` is not a ``MediaRequest``, or names a path that is not one below the chat folder; that is
            the caller's mistake, never the file's.
    """
    try:
        request = MediaRequest.model_validate(args.get("media"))
    except ValidationError:  # its text would quote the argument
        raise ValueError("media is the clip's artifacts, nextext_jsonl and nextext_zip, and video") from None
    for relative in (request.nextext_jsonl, request.nextext_zip):
        if relative is not None:
            protocol.check_input_path(relative)
    if request.nextext_jsonl is None and request.nextext_zip is None:
        return RenderAnswer.model_validate(
            {
                "kind": "media",
                "unit": "frame",
                "total": 0,
                "pages_without_text": [],
                "frames": None,
                "hint": NOTHING_FOUND,
                "renders": [],
            }
        ).model_dump()
    budget = float(args.get("budget_s", _BUDGET_S))
    deadline = time.monotonic() + budget
    readers.set_deadline(deadline)
    transcript: Transcript | None = None
    frames: list[Frame] = []
    held = 0
    unreadable: list[str] = []
    try:
        with limit_until(deadline, readers.ReadTimeout):
            if request.nextext_jsonl is not None:
                try:
                    transcript = read_transcript(_artifact(ctx, request.nextext_jsonl))
                except UnreadableFile:
                    unreadable.append(TRANSCRIPT_UNREADABLE)
            if request.nextext_zip is not None:
                try:
                    frames, held = read_frames(_artifact(ctx, request.nextext_zip), file_mb=ctx.file_mb)
                except UnreadableFile:
                    unreadable.append(_FRAMES_UNREADABLE)
    except readers.ReadTimeout:
        return error(
            "read_timeout",
            f"reading the clip's artifacts took longer than {budget:g} s; they are too large or too complex",
        )
    finally:
        readers.set_deadline(None)
    if transcript is None and not frames:
        return error("unreadable_file", _NOTHING_USABLE)
    notes = list(unreadable)
    if transcript is not None and transcript.cut_at is not None:
        notes.append(f"The transcript is cut at {stamp(transcript.cut_at)}: it is longer than workbench keeps.")
    if held > len(frames):
        notes.append(f"{len(frames)} of the clip's {held} frames are kept, spread evenly.")
    if request.nextext_jsonl is None:
        notes.append(NO_SPEECH)
    if request.video and request.nextext_zip is None:
        notes.append(_NO_FRAMES_TAKEN)
    entries = [
        RenderEntry.model_validate(
            {
                "name": render_name(frame.n, 1, 1, frame.mime),
                "role": "frame",
                "n": frame.n,
                "width": frame.width,
                "height": frame.height,
                "mime": frame.mime,
                "time_s": frame.time_s,
            }
        )
        for frame in frames
    ]
    ends = [t for t in (transcript.duration_s if transcript else None, *(f.time_s for f in frames)) if t is not None]
    answer = RenderAnswer.model_validate(
        {
            "kind": "media",
            "unit": "frame",
            "total": len(frames),
            "pages_without_text": [],
            "frames": None,
            "hint": " ".join(notes) or None,
            "duration_s": max(ends, default=None),
            "renders": [entry.model_dump() for entry in entries],
            "transcript": None if transcript is None else {"name": TRANSCRIPT_NAME, "lines": transcript.lines},
        }
    ).model_dump()
    outputs: list[tuple[str, bytes]] = [(entry.name, frame.data) for entry, frame in zip(entries, frames, strict=True)]
    if transcript is not None:
        outputs.insert(0, (TRANSCRIPT_NAME, transcript.text.encode("utf-8")))
    written: list[Path] = []
    try:
        for name, data in outputs:
            written.append(ctx.out_dir / name)  # noted first: a file that was begun is removed too
            written[-1].write_bytes(data)
    except OSError:  # its text holds the path of the file
        for path in written:
            with suppress(OSError):
                path.unlink(missing_ok=True)
        return error("unreadable_file", _NOT_WRITTEN)
    return answer
