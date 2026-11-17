"""JSON files, shown indented, by line: a file written on one line is read and searched like any other.

The text is decoded as :mod:`~workbench.executor.readers.text` decodes it, parsed, and written out again with an
indent of two spaces, characters outside ASCII as themselves rather than escaped. A minified file is one line, which
``LINE_CAP`` would cut after its first characters; indented, each value is on a line of its own, so the whole file
can be paged (``lines 41-80``), and a search names the same lines a read shows. The values are shown as Python's
parser reads them: a key that appears twice in one object is shown once, with its last value, and a number is
written in its shortest form (``1.50`` as ``1.5``).

A file that is not valid JSON (JSON with comments, a file cut short) or that nests too deeply to be parsed is read
as it is, by line, like a text file. JSON Lines (``.jsonl``, ``.ndjson``) is not read here: it holds one record per
line, so it is read as text.
"""

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from workbench.executor.readers import DocumentText, Part
from workbench.executor.readers import text as txt

#: Writes what ``json.dumps(value, indent=2, ensure_ascii=False)`` does, piece by piece.
_ENCODER: Final = json.JSONEncoder(indent=2, ensure_ascii=False)


def _lines(path: Path) -> list[str]:
    """Turn a JSON file into the lines it is shown as.

    The indented text is written piece by piece into one buffer, and the parsed value is let go before the text is
    split: ``json.dumps`` would first hold every piece at once, more than twice the memory at its peak.

    Args:
        path: The file.

    Returns:
        The lines of the indented JSON, or the file's own lines when it does not parse.
    """
    raw = txt.decode(path.read_bytes())
    shown = io.StringIO()
    try:
        value = json.loads(raw)
        for piece in _ENCODER.iterencode(value):
            shown.write(piece)
    except (ValueError, RecursionError):
        return txt.split_lines(raw)
    del value, raw
    return txt.split_lines(shown.getvalue())


def read(path: Path, *, part: str | None, max_chars: int) -> DocumentText:
    """Read a JSON file from ``part`` on.

    Args:
        path: The file.
        part: ``None``, ``line N`` or ``lines N-M``.
        max_chars: The budget in bytes of JSON.

    Returns:
        The lines that fit and where to continue.
    """
    return txt.read_lines("json", _lines(path), part=part, max_chars=max_chars)


def units(path: Path) -> Iterator[Part]:
    """Walk the lines of a JSON file as :func:`read` shows them.

    Args:
        path: The file.

    Yields:
        Each non-blank line as ``line N``.
    """
    yield from txt.line_units(_lines(path))
