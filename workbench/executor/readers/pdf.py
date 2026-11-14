"""PDF files, by page, from the text layer only.

There is no OCR. A page whose text layer is empty (a scan, a picture) is never returned as an empty
page: it is listed in ``pages_without_text``, so the model can tell the user the file needs OCR elsewhere.
A PDF that asks for a password to open is :class:`~workbench.executor.readers.UnreadableFile`.
"""

import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

from pypdf import PdfReader

from workbench.executor.readers import DocumentText, Part, UnreadableFile
from workbench.executor.readers import paging as pg

#: Most pages one read looks at, so a long run of scanned pages cannot keep the job busy.
MAX_PAGES_LOOKED_AT: Final = 200

_ENCRYPTED: Final = "the PDF is password-protected (encrypted)"

# pypdf reports damage it recovers from as warnings; the job's log is not where they belong.
logging.getLogger("pypdf").setLevel(logging.ERROR)


def _open(path: Path) -> PdfReader:
    """Open a PDF that can be read without a password.

    Args:
        path: The file.

    Returns:
        The reader.

    Raises:
        UnreadableFile: The file is encrypted and not openable with an empty password.
    """
    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        try:
            opened = reader.decrypt("")
        except Exception:
            raise UnreadableFile(_ENCRYPTED) from None
        if not opened:
            raise UnreadableFile(_ENCRYPTED)
    return reader


def _page_text(reader: PdfReader, number: int) -> str:
    """Extract one page's text, tidied; a page that cannot be extracted has none.

    Args:
        reader: The open PDF.
        number: The page number, from 1.

    Returns:
        The text, with runs of blank lines and trailing spaces removed; empty if there is none.
    """
    try:
        raw: Any = reader.pages[number - 1].extract_text()
    except Exception:
        return ""
    text = re.sub(r"[ \t]+\n", "\n", str(raw))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def read(path: Path, *, part: str | None, max_chars: int) -> DocumentText:
    """Read pages from ``part`` on.

    Args:
        path: The PDF.
        part: ``None``, ``page N`` or ``pages N-M``.
        max_chars: The budget in bytes of JSON.

    Returns:
        The pages that have text, whole, in order, the numbers of the pages looked at that have none,
        and where to continue.

    Raises:
        UnreadableFile: The PDF is encrypted.
        NoSuchPart: The page is not in the PDF.
    """
    reader = _open(path)
    total = len(reader.pages)
    first, last = pg.numbered_window(part, total, "page")
    collector = pg.Collector(max_chars)
    without: list[int] = []
    for number in range(first, last + 1):
        if number - first >= MAX_PAGES_LOOKED_AT:
            collector.next_part = f"page {number}"
            break
        text = _page_text(reader, number)
        if not text:
            without.append(number)
        elif not collector.add(f"page {number}", text):
            break
    return DocumentText(
        kind="pdf",
        parts=collector.parts,
        pages_without_text=without,
        total_parts=total,
        truncated=collector.truncated,
        next_part=collector.next_part,
        cut=collector.cut,
    )


def units(path: Path) -> Iterator[Part]:
    """Walk the pages.

    Args:
        path: The PDF.

    Yields:
        Each page as ``page N``; a page without a text layer has empty text.

    Raises:
        UnreadableFile: The PDF is encrypted.
    """
    reader = _open(path)
    for number in range(1, len(reader.pages) + 1):
        yield Part(f"page {number}", _page_text(reader, number))
