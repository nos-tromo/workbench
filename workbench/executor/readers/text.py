"""Plain text, Markdown, JSON Lines, CSV and TSV, by line; also the paging the HTML, e-mail and JSON readers use.

The text is decoded without being told its encoding: a byte order mark, strict UTF-8, then
charset-normalizer's guess for non-Latin scripts, then Windows-1252 with replacement characters. A file is read by line
numbers (``lines 41-80``); a line longer than ``LINE_CAP`` characters is cut and says so, so a file of
one huge line cannot fill the answer.
"""

import codecs
import json
import re
import unicodedata
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Final

from charset_normalizer import from_bytes

from workbench.executor.readers import DocumentText, Part, tick
from workbench.executor.readers import paging as pg

#: Longest line shown; the rest of a longer line is cut.
LINE_CAP: Final = 2000

#: Bytes of a file that charset detection looks at.
_SAMPLE: Final = 1_000_000

_SAMPLE_LETTERS: Final = 20_000

#: A file with a smaller share of non-ASCII bytes than this is Western European text with a few accents, not
#: a script of its own (such files are mostly non-ASCII).
_HIGH_SHARE: Final = 0.25

_BOMS: Final = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_NEWLINE: Final = re.compile(r"\r\n|\r|\n")


def _latin(text: str) -> bool:
    """Tell whether every letter of the start of a text is from the Latin script.

    Args:
        text: Decoded text.

    Returns:
        ``True`` if so, or if it has no letters.
    """
    letters = [char for char in text[:_SAMPLE_LETTERS] if char.isalpha()]
    return all(unicodedata.name(char, "").startswith("LATIN") for char in letters)


def decode(data: bytes) -> str:
    """Decode bytes of unknown encoding.

    A byte order mark decides; else strict UTF-8; else charset-normalizer's guess. Its guess among the
    single-byte code pages is unreliable for Western European text (it takes German for Hungarian), so
    when the file is mostly ASCII or the guess reads as Latin script the text is decoded as Windows-1252,
    the code page of nearly all such files (Latin-1 included, for every letter); a guess in another script
    (Cyrillic, Greek, Arabic, CJK) is kept.

    Args:
        data: The file's bytes.

    Returns:
        The text; characters that cannot be decoded are replaced.
    """
    for mark, encoding in _BOMS:
        if data.startswith(mark):
            return data.decode(encoding, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    sample = data[:_SAMPLE]
    mostly_ascii = len(sample.translate(None, bytes(range(128)))) < len(sample) * _HIGH_SHARE
    guess = None if mostly_ascii else from_bytes(sample).best()
    if guess is not None:
        try:
            decoded = data.decode(guess.encoding, errors="replace")
        except LookupError:
            decoded = None
        if decoded is not None and not _latin(decoded):
            return decoded
    return data.decode("cp1252", errors="replace")


def split_lines(text: str) -> list[str]:
    """Split a text into lines on any kind of line break; a final break does not make an empty last line.

    Args:
        text: The text.

    Returns:
        The lines, without their breaks.
    """
    lines = _NEWLINE.split(text)
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _shown(line: str) -> str:
    """Cut a line that is longer than ``LINE_CAP``.

    Args:
        line: One line.

    Returns:
        The line, or its start and how much was left out.
    """
    if len(line) <= LINE_CAP:
        return line
    return f"{line[:LINE_CAP]}… [+{len(line) - LINE_CAP} characters]"


def read_lines(kind: str, lines: Sequence[str], *, part: str | None, max_chars: int) -> DocumentText:
    """Read lines from ``part`` on, as one part labelled with the numbers of the lines in it.

    Args:
        kind: The reader family to report.
        lines: The document's lines.
        part: ``None``, ``line N``, ``lines N-M`` or ``N``.
        max_chars: The budget in bytes of JSON.

    Returns:
        One part with the lines that fit, covering their numbers as units, and where to continue.

    Raises:
        NoSuchPart: The part is not a line reference, or the line is past the end.
    """
    total = len(lines)
    if total == 0:
        return DocumentText(kind=kind, parts=[], pages_without_text=[], total_parts=0)
    first, last = pg.numbered_window(part, total, "line")
    left = max_chars - pg.part_size(f"lines {total}-{total}", "", (total, total))
    taken: list[str] = []
    cut = False
    number = first
    while number <= last:
        line = _shown(lines[number - 1])
        cost = len(json.dumps(line, ensure_ascii=False).encode())
        if cost > left:
            if taken:
                break
            line = line[: max(1, left // 6)] + pg.CUT_MARK
            cost, cut = left, True
        left -= cost
        taken.append(line)
        number += 1
        if cut:
            break
    end = number - 1
    label = f"line {first}" if end == first else f"lines {first}-{end}"
    more = end < last
    return DocumentText(
        kind=kind,
        parts=[Part(label, "\n".join(taken), (first, end))],
        pages_without_text=[],
        total_parts=total,
        truncated=more or cut,
        next_part=f"line {end + 1}" if more else None,
        cut=label if cut else None,
    )


def line_units(lines: Sequence[str]) -> Iterator[Part]:
    """Walk the lines that have text.

    Args:
        lines: The document's lines.

    Yields:
        Each non-blank line as ``line N``, whole.
    """
    for number, line in enumerate(lines, 1):
        tick()
        if line.strip():
            yield Part(f"line {number}", line)


def _kind(path: Path) -> str:
    """Name the family of a text file by its extension.

    Args:
        path: The file.

    Returns:
        ``csv`` for CSV and TSV, ``text`` otherwise.
    """
    return "csv" if path.suffix.lower() in {".csv", ".tsv"} else "text"


def read(path: Path, *, part: str | None, max_chars: int) -> DocumentText:
    """Read a text file from ``part`` on.

    Args:
        path: The file.
        part: ``None``, ``line N`` or ``lines N-M``.
        max_chars: The budget in bytes of JSON.

    Returns:
        The lines that fit and where to continue.
    """
    return read_lines(_kind(path), split_lines(decode(path.read_bytes())), part=part, max_chars=max_chars)


def units(path: Path) -> Iterator[Part]:
    """Walk the lines of a text file.

    Args:
        path: The file.

    Yields:
        Each non-blank line as ``line N``.
    """
    yield from line_units(split_lines(decode(path.read_bytes())))
