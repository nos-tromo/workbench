"""Readers for the documents ``read_document`` and ``search_files`` open, run inside the sandboxed job.

Every reader turns one file into text with a reference for each piece of it (``page 3``,
``Sheet1!A1:F200``, ``slide 2``, ``paragraph 12``, ``lines 1-80``), in two ways:

- :func:`read` pages through a document: it starts where ``part`` says and returns whole parts
  until the character budget is used up, then says where to continue (``next_part``);
- :func:`units` walks the document in the finest pieces it has (a page, a cell, a paragraph, a
  table row, a line), which is what a search matches against.

:func:`count_pages` and :func:`sheet_extents` tell, under the same guards, what a template's source may
cite: the pages of a PDF or the slides of a deck, the sheets of a workbook and the cells of each that
hold something.

The files are untrusted. Nothing here runs macros, follows links, loads external entities or
fetches anything, and a file that cannot be read is :class:`UnreadableFile`, never a hang: Office
and OpenDocument files are zip files, whose unpacked size is checked against a limit before any
part is opened (:func:`check_zip`), and every failure inside a parser becomes a short fixed message
that names no path. The messages are written for the model, which sees them.

The readers load their libraries on first use, so a job that reads a PDF does not import
openpyxl.
"""

import time
import zipfile
from collections.abc import Generator, Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

from workbench.executor.timer import TimeLimit, limit_until

__all__ = [
    "DocumentText",
    "Extent",
    "NoSuchPart",
    "Part",
    "ReadTimeout",
    "UnreadableFile",
    "UnsupportedFormat",
    "check",
    "check_zip",
    "count_pages",
    "read",
    "set_deadline",
    "sheet_extents",
    "tick",
    "units",
]

_MIB: Final = 1024 * 1024

#: A zip file may unpack to this many times the largest file a job may write, at the most.
ZIP_EXPANSION: Final = 20

#: Most members a zip file may hold; the biggest real documents have a few thousand.
MAX_ZIP_MEMBERS: Final = 10_000

_OLE_HEADER: Final = bytes.fromhex("d0cf11e0a1b11ae1")

DAMAGED: Final = "the file is damaged or truncated, or is not what its extension says"
TOO_BIG: Final = "the file is too large to read within the memory limit"
_OFFICE_ENCRYPTED: Final = (
    "the file is password-protected (encrypted) or in an old binary format: "
    "remove the password and save it as a current .docx, .xlsx or .pptx"
)

#: The reader family of each file extension.
_FAMILIES: Final = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".dotx": "docx",
    ".xlsx": "xlsx",
    ".xlsm": "xlsx",
    ".csv": "csv",
    ".tsv": "csv",
    ".pptx": "pptx",
    ".odt": "odt",
    ".ods": "ods",
    ".eml": "eml",
    ".html": "html",
    ".htm": "html",
    ".txt": "text",
    ".md": "text",
}

#: Extensions that are zip files; they pass :func:`check_zip` first.
_ZIPPED: Final = frozenset({".docx", ".dotx", ".xlsx", ".xlsm", ".pptx", ".odt", ".ods"})

_HINTS: Final = {
    ".msg": "save it as .eml or PDF",
    ".doc": "save it as .docx or PDF",
    ".rtf": "save it as .docx or PDF",
    ".xls": "save it as .xlsx or .csv",
    ".ppt": "save it as .pptx or PDF",
    ".odp": "save it as .pptx or PDF",
}


class UnsupportedFormat(Exception):
    """The file's type is not one the readers handle; the message says what to do instead."""


class UnreadableFile(Exception):
    """The file is of a supported type but cannot be read: encrypted, damaged, truncated or too large.

    The message is a short fixed text; it never holds a path or a parser's own message.
    """


class NoSuchPart(Exception):
    """The ``part`` asked for is not in the document; the message names the parts there are."""


class ReadTimeout(TimeLimit):
    """Reading ran past the deadline it was given (:func:`set_deadline`); what was read so far is lost.

    Like every :class:`~workbench.executor.timer.TimeLimit` it is not an ``Exception``, so code that catches
    ``Exception`` (ours, and the libraries') does not swallow it.
    """


_deadline: float | None = None
_ticks = 0
_CHECK_EVERY: Final = 64


def set_deadline(at: float | None) -> None:
    """Give the readers a time after which they stop, or take the limit away.

    The job is a single-threaded process that reads one file at a time, so the deadline is the process's.
    The readers check it in their loops through :func:`tick` and :func:`check`, and :func:`read` and
    :func:`units` also make it binding with a timer (:mod:`workbench.executor.timer`), so that a long call
    into a library that never looks at a clock is interrupted too.

    Args:
        at: A :func:`time.monotonic` time, or ``None`` for no deadline.
    """
    global _deadline
    _deadline = at


def tick() -> None:
    """Note one step of a loop; every few steps, look at the clock.

    Raises:
        ReadTimeout: The deadline has passed.
    """
    global _ticks
    _ticks += 1
    if _ticks % _CHECK_EVERY == 0:
        check()


def check() -> None:
    """Look at the clock now: for a loop whose every step can be slow, such as one step per sheet.

    Raises:
        ReadTimeout: The deadline has passed.
    """
    if _deadline is not None and time.monotonic() > _deadline:
        raise ReadTimeout


@dataclass
class Part:
    """One piece of a document with its reference.

    Attributes:
        label: Where the text is, such as ``page 3``, ``Sheet1!A1:F200``, ``slide 2`` or ``paragraph 12``.
            Passed back as ``part``, it starts a read there.
        text: The text.
    """

    label: str
    text: str


@dataclass
class DocumentText:
    """What a read of one document returns.

    Attributes:
        kind: The reader family: ``pdf``, ``docx``, ``xlsx``, ``csv``, ``pptx``, ``odt``, ``ods``, ``eml``,
            ``html`` or ``text``.
        parts: The parts read, in order. A page or slide without text is never in here.
        pages_without_text: Numbers of the pages or slides looked at that hold no text (a scan, a picture).
        total_parts: How many pages, sheets, slides, paragraphs and tables or lines the document has.
        truncated: Whether the answer leaves out text: parts that did not fit, or a part cut short.
        next_part: Where to continue, to pass as ``part``; ``None`` when nothing follows.
        cut: Label of the one part that was longer than the whole answer may be and was cut short, if any.
    """

    kind: str
    parts: list[Part]
    pages_without_text: list[int]
    total_parts: int
    truncated: bool = False
    next_part: str | None = None
    cut: str | None = None


class _Reader(Protocol):
    """What each reader module offers."""

    def read(self, path: Path, *, part: str | None, max_chars: int) -> DocumentText:
        """Read from ``part`` on, within ``max_chars``."""
        ...

    def units(self, path: Path) -> Iterator[Part]:
        """Walk the document in its finest pieces."""
        ...


def _family(path: Path) -> str:
    """Name the reader family of a file by its extension.

    Args:
        path: The file.

    Returns:
        The family.

    Raises:
        UnsupportedFormat: The extension is not one of the readable types; ``.msg`` and the old Office
            formats say what to save the file as.
    """
    ext = path.suffix.lower()
    family = _FAMILIES.get(ext)
    if family is None:
        hint = _HINTS.get(ext)
        shown = ext if ext else "a file without an extension"
        text = f"{shown} cannot be read here"
        if hint:
            text += f"; {hint}"
        else:
            text += "; readable are PDF (text layer only), DOCX, XLSX, CSV, PPTX, ODT, ODS, EML, HTML, TXT and MD"
        raise UnsupportedFormat(text)
    return family


def supported(path: Path) -> bool:
    """Tell whether a file has a type the readers handle, by its extension.

    Args:
        path: The file.

    Returns:
        ``True`` if :func:`read` would try it.
    """
    return path.suffix.lower() in _FAMILIES


def check_zip(path: Path, file_mb: int) -> None:
    """Refuse a zip file that is not one, is encrypted, or unpacks to more than it may.

    The sizes are the ones the zip's directory declares, and the reading side never yields more
    than a member declares, so the sum is a bound on what any reader can unpack.

    Args:
        path: An Office or OpenDocument file.
        file_mb: The largest file a job may write, in megabytes; the limit is ``ZIP_EXPANSION`` times it.

    Raises:
        UnreadableFile: It is an old or encrypted Office file (not a zip), damaged, holds more than
            ``MAX_ZIP_MEMBERS`` members, has an encrypted member, or unpacks to more than the limit.
    """
    limit = ZIP_EXPANSION * file_mb * _MIB
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, OSError):
        with path.open("rb") as handle:
            head = handle.read(len(_OLE_HEADER))
        raise UnreadableFile(_OFFICE_ENCRYPTED if head == _OLE_HEADER else DAMAGED) from None
    if len(members) > MAX_ZIP_MEMBERS:
        raise UnreadableFile("the file holds too many parts to be a document")
    if any(member.flag_bits & 0x1 for member in members):
        raise UnreadableFile("the file is password-protected (encrypted)")
    if sum(member.file_size for member in members) > limit:
        raise UnreadableFile(
            f"the file unpacks to more than {ZIP_EXPANSION * file_mb} MB, which is more than allowed "
            "(it may be a decompression bomb)"
        )


def _prepare(path: Path, file_mb: int) -> str:
    """Pick the reader family and run the zip check for a zipped type.

    Args:
        path: The file.
        file_mb: The largest file a job may write, in megabytes.

    Returns:
        The family.

    Raises:
        UnsupportedFormat: The type is not readable.
        UnreadableFile: The zip check refused the file.
    """
    family = _family(path)
    if path.suffix.lower() in _ZIPPED:
        check_zip(path, file_mb)
    return family


def _reader(family: str) -> _Reader:
    """Load the reader module of a family.

    Args:
        family: A value of ``_FAMILIES``.

    Returns:
        The module, which offers ``read`` and ``units``.
    """
    # Imported here so that a job pays only for the library its file needs.
    if family == "pdf":
        from workbench.executor.readers import pdf

        return pdf
    if family == "docx":
        from workbench.executor.readers import docx

        return docx
    if family == "xlsx":
        from workbench.executor.readers import sheet

        return sheet
    if family == "pptx":
        from workbench.executor.readers import pptx

        return pptx
    if family in {"odt", "ods"}:
        from workbench.executor.readers import odf

        return odf
    if family == "eml":
        from workbench.executor.readers import email

        return email
    if family == "html":
        from workbench.executor.readers import html

        return html
    from workbench.executor.readers import text

    return text


def _backstop() -> AbstractContextManager[None]:
    """Make the deadline binding for library code that never looks at a clock, while a reader runs.

    For :func:`units` the limit stays in force while the generator is suspended, so the code that consumes the
    pieces is under the same deadline; its own, shorter limits (a search's regular expression) nest inside it.

    Returns:
        A context manager: a time limit until the deadline of :func:`set_deadline`, or nothing if there is none.
    """
    if _deadline is None:
        return nullcontext()
    return limit_until(_deadline, ReadTimeout)


def read(path: Path, *, part: str | None, max_chars: int, file_mb: int = 50) -> DocumentText:
    """Read a document from ``part`` on, as much as fits in ``max_chars``.

    Args:
        path: The file; its extension picks the reader.
        part: Where to start, as an earlier answer's ``next_part`` or a part label; ``None`` for the start.
        max_chars: The most the parts may take, counted as the bytes of their JSON.
        file_mb: The largest file a job may write, in megabytes; a zip file may unpack to 20 times it.

    Returns:
        The parts read, and where to continue.

    Raises:
        UnsupportedFormat: The type is not readable (``.msg`` included: save it as .eml or PDF).
        UnreadableFile: The file is encrypted, damaged, truncated, a decompression bomb, or too big to read.
        NoSuchPart: ``part`` is not in the document.
        ReadTimeout: The deadline (:func:`set_deadline`) passed while reading.
    """
    with _backstop():
        family = _prepare(path, file_mb)
        try:
            return _reader(family).read(path, part=part, max_chars=max_chars)
        except (UnsupportedFormat, UnreadableFile, NoSuchPart):
            raise
        except MemoryError:
            raise UnreadableFile(TOO_BIG) from None
        except Exception:
            raise UnreadableFile(DAMAGED) from None


def count_pages(path: Path, *, file_mb: int = 50) -> int:
    """Count the pages of a PDF or the slides of a presentation, under the same guards as :func:`read`.

    Args:
        path: A ``.pdf`` or ``.pptx`` file.
        file_mb: The largest file a job may write, in megabytes.

    Returns:
        How many pages or slides it has.

    Raises:
        UnsupportedFormat: It is neither.
        UnreadableFile: It is encrypted, damaged, truncated, a decompression bomb, or too big to read.
        ReadTimeout: The deadline (:func:`set_deadline`) passed while counting.
    """
    if path.suffix.lower() not in {".pdf", ".pptx"}:
        raise UnsupportedFormat("only a PDF has pages and only a presentation has slides")
    with _backstop():
        family = _prepare(path, file_mb)
        try:
            if family == "pdf":
                from workbench.executor.readers import pdf

                return len(pdf._open(path).pages)
            from pptx import Presentation

            return len(Presentation(str(path)).slides)
        except (UnsupportedFormat, UnreadableFile):
            raise
        except MemoryError:
            raise UnreadableFile(TOO_BIG) from None
        except Exception:
            raise UnreadableFile(DAMAGED) from None


#: The rows and columns a sheet's cells with content span: first row, last row, first column, last column.
Extent = tuple[int, int, int, int]


def sheet_extents(path: Path, *, file_mb: int = 50) -> dict[str, Extent | None]:
    """List the sheets of a workbook and the cells of each that hold something, under the same guards as :func:`read`.

    Args:
        path: An ``.xlsx``, ``.xlsm`` or ``.ods`` file.
        file_mb: The largest file a job may write, in megabytes.

    Returns:
        Each sheet's name, in order, with the extent of its cells that hold something; ``None`` for an empty sheet.

    Raises:
        UnsupportedFormat: It is not a workbook.
        UnreadableFile: It is encrypted, damaged, truncated, a decompression bomb, or too big to read.
        ReadTimeout: The deadline (:func:`set_deadline`) passed while reading.
    """
    if path.suffix.lower() not in {".xlsx", ".xlsm", ".ods"}:
        raise UnsupportedFormat("only a workbook has sheets and cells")
    with _backstop():
        family = _prepare(path, file_mb)
        try:
            from workbench.executor.readers import odf, sheet

            source: Any = sheet.Workbook(path) if family == "xlsx" else odf.Sheets(path)
            try:
                extents: dict[str, Extent | None] = dict.fromkeys(source.names)
                for name, number, row in source.walk(None, 1, cap=1):
                    tick()
                    columns = [column for column, text in enumerate(row, 1) if text]
                    if not columns:
                        continue
                    seen = extents[name]
                    if seen is None:
                        extents[name] = (number, number, columns[0], columns[-1])
                    else:
                        extents[name] = (seen[0], number, min(seen[2], columns[0]), max(seen[3], columns[-1]))
                return extents
            finally:
                if family == "xlsx":
                    source.close()
        except (UnsupportedFormat, UnreadableFile):
            raise
        except MemoryError:
            raise UnreadableFile(TOO_BIG) from None
        except Exception:
            raise UnreadableFile(DAMAGED) from None


def units(path: Path, *, file_mb: int = 50) -> Generator[Part, None, None]:
    """Walk a document in the finest pieces it has, for a search.

    A piece is a page or slide (a page without text comes with an empty text), a cell, a paragraph,
    a table row or a line, with its exact location as the label.

    Args:
        path: The file; its extension picks the reader.
        file_mb: The largest file a job may write, in megabytes.

    Yields:
        The pieces, in document order.

    Raises:
        UnsupportedFormat: The type is not readable.
        UnreadableFile: The file is encrypted, damaged, truncated, a decompression bomb, or too big to read;
            it can surface while iterating.
        ReadTimeout: The deadline (:func:`set_deadline`) passed while reading.
    """
    with _backstop():
        family = _prepare(path, file_mb)
        try:
            yield from _reader(family).units(path)
        except (UnsupportedFormat, UnreadableFile, NoSuchPart):
            raise
        except MemoryError:
            raise UnreadableFile(TOO_BIG) from None
        except Exception:
            raise UnreadableFile(DAMAGED) from None
