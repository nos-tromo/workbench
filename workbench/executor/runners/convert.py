"""The runner of ``convert_office``: an Office file converted once to a PDF, which ``prepare_file`` then draws.

The job's working folder is the chat folder, as for ``prepare_file``. LibreOffice
(:mod:`workbench.executor.render.office`) sees a file only after the checks that need no LibreOffice: the zip guard
(bombs, an old or encrypted container), an OpenDocument manifest that says its content is encrypted, and, for a deck, a
count of its slides that is not zero. A file refused there is ``unreadable_file`` or ``unsupported_format``, and no
child is forked. Then Pillow reads the size of every picture in the zip from its header, decoding none; a picture of
more than ``SOURCE_MAX_PIXELS`` is refused there too.

LibreOffice leaves out a picture it has no address space left to decode and still saves the PDF, so the converter is
told how much of ``RLIMIT_AS`` must stay free at its child's peak: the largest picture at ``_BYTES_PER_PIXEL``, and
``_SLACK``. The whole file is converted first. When that conversion comes within the headroom
(:class:`~workbench.executor.render.office.AtCeiling`), its PDF still gives the number of pages (a deck that saved none
has as many as slides), and the file is converted again in parts, a page range in a fresh child each, so that each
child decodes only the pictures of its pages; a part that comes within the headroom again is halved. The parts are
joined into one PDF. A single page that still comes within it, a document that saved no PDF at it, or a deadline that
passes while the parts are made, is ``unreadable_file`` (:data:`~workbench.executor.render.office.TOO_MANY_PICTURES`):
never a PDF with pictures missing.

The PDF is made in a new folder of the job's temporary directory, never in ``out/``: the converter can leave a part of a
file behind when it fails, and ``out/`` must hold only a PDF that was checked. It is a regular file of at most
``CONVERTED_MAX_BYTES``, it starts with the PDF signature, and pdfium counts its pages; only then is it moved to
``out/converted.pdf``, and the answer is a :class:`workbench.renders.ConvertAnswer` as JSON: no path, no file name, and
nothing LibreOffice said. The folder is removed however the runner ends.

Everything runs under one deadline, :func:`workbench.executor.readers.set_deadline` and a timer. Only the file's own
failures are answers; a ``RuntimeError`` (LibreOffice could not start) is the job's error and propagates.
"""

import os
import shutil
import stat
import tempfile
import time
import zipfile
import zlib
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Final, Protocol

from workbench import protocol
from workbench.executor import readers
from workbench.executor.context import JobContext
from workbench.executor.render import office
from workbench.executor.runners.documents import error
from workbench.executor.timer import limit_until
from workbench.renders import (
    CONVERTED_MAX_BYTES,
    CONVERTED_NAME,
    CONVERTED_SIGNATURE,
    OFFICE_SUFFIXES,
    SOURCE_MAX_PIXELS,
    ConvertAnswer,
    unit_of,
)

_BUDGET_S: Final = 90.0

_MIB: Final = 1024 * 1024

#: The part of an OpenDocument file that says which members are encrypted, and the most of it that is read.
_MANIFEST: Final = "META-INF/manifest.xml"
_MANIFEST_READ_MAX: Final = _MIB
_ENCRYPTION_MARK: Final = b"encryption-data"

#: The most LibreOffice asks for at once to decode one pixel of a picture: 3 bytes measured for RGB, 4 with alpha.
_BYTES_PER_PIXEL: Final = 4
#: Address space kept free at a conversion's peak besides the largest picture, for LibreOffice's other requests.
_SLACK: Final = 128 * _MIB
#: What a conversion's child adds to the job's process before it draws a page, besides the file's own size:
#: LibreOffice started (about 400 MB) and the file loaded (about 210 MB and the file's size; 610 to 740 MB in all,
#: measured). It sizes the first parts only; a part that is too large is halved.
_LIBREOFFICE: Final = 640 * _MIB
#: The picture formats LibreOffice draws whose size Pillow reads from the header.
_PICTURE_FORMATS: Final = ("JPEG", "PNG", "GIF", "BMP", "TIFF", "WEBP", "PSD", "TGA", "PCX", "PPM", "SUN", "XBM", "XPM")

_LOCKED: Final = "the file is password-protected (encrypted)"
_NOT_CONVERTED: Final = "the file could not be converted to pages"
_NOT_WRITTEN: Final = "the converted file could not be written"
_NO_SLIDES: Final = "the deck has no slides; inspect_template lists the layouts of a slide master"
_TOO_LARGE: Final = f"the converted file is larger than {CONVERTED_MAX_BYTES // _MIB} MB"
_PICTURE_TOO_LARGE: Final = f"the file holds a picture larger than {SOURCE_MAX_PIXELS // 1_000_000} megapixels"


class Converter(Protocol):
    """What converts an Office file to a PDF: :func:`workbench.executor.render.office.convert`, or a test's stand-in."""

    def __call__(
        self,
        source: Path,
        target: Path,
        *,
        deck: bool,
        deadline: float,
        headroom: int,
        pages: tuple[int, int] | None,
    ) -> None:
        """Convert ``source`` to a PDF at ``target``.

        Args:
            source: The file, already checked.
            target: Where the PDF is written.
            deck: The file is a presentation.
            deadline: A :func:`time.monotonic` time after which the conversion must stop.
            headroom: The bytes of the address-space limit that must stay free at the conversion's peak.
            pages: The first and last page to draw, counted from 1; ``None`` for all.

        Raises:
            office.AtCeiling: The conversion came to the address-space ceiling: a PDF at ``target`` has all its
                pages but may lack pictures, and there may be none.
            readers.UnreadableFile: The file cannot be converted.
            readers.ReadTimeout: The deadline passed.
            RuntimeError: The converter could not start.
        """
        ...


@dataclass(frozen=True)
class _Pictures:
    """The pictures of an Office file, by the sizes their headers declare.

    Attributes:
        largest: The pixels of the largest.
        total: The pixels of all of them, each member of the zip once.
    """

    largest: int = 0
    total: int = 0


def _locked_odt(path: Path) -> bool:
    """Tell whether an OpenDocument file's manifest says some of its members are encrypted.

    Args:
        path: The ``.odt``, whose zip ``readers.check_zip`` has accepted.

    Returns:
        ``True`` when the first mebibyte of ``META-INF/manifest.xml`` holds ``encryption-data``; ``False`` when it does
        not, or when there is no manifest (LibreOffice judges a file of that kind itself).

    Raises:
        readers.UnreadableFile: The manifest cannot be read from the zip.
    """
    try:
        with zipfile.ZipFile(path) as archive, archive.open(_MANIFEST) as member:
            return _ENCRYPTION_MARK in member.read(_MANIFEST_READ_MAX)
    except KeyError:
        return False
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError):
        raise readers.UnreadableFile(readers.DAMAGED) from None


def _slides_of(path: Path, file_mb: int) -> int | None:
    """Run the checks LibreOffice must not be the first to meet, and count a deck's slides.

    Args:
        path: The Office file.
        file_mb: The largest file a job may write, in megabytes; the zip guard's base.

    Returns:
        The slides of a deck, or ``None`` for a text document.

    Raises:
        readers.UnreadableFile: The file is not a good zip (an old binary format and a password-protected Office file
            are not), has too many parts or unpacks to too much, or its OpenDocument manifest says it is encrypted.
        readers.UnsupportedFormat: A deck that is macro-enabled or a slide show.
        readers.ReadTimeout: The deadline passed.
    """
    readers.check_zip(path, file_mb)
    if path.suffix.lower() == ".odt" and _locked_odt(path):
        raise readers.UnreadableFile(_LOCKED)
    if unit_of(path.name) == "slide":
        return readers.count_pages(path, file_mb=file_mb)
    return None


def _pictures(path: Path) -> _Pictures:
    """Read the size of every picture in an Office file from its header, decoding none.

    Every member of the zip is tried, whatever its name, since a relationship can point at any member as a picture,
    and Pillow knows a picture by its content. A member it does not know (XML, a metafile, an embedded object) counts
    for nothing. Pillow's own pixel limit is lifted for the read, which decodes nothing; this one's is applied instead.

    Args:
        path: The Office file, whose zip ``readers.check_zip`` has accepted.

    Returns:
        The largest picture and all of them, in pixels.

    Raises:
        readers.UnreadableFile: A picture declares more than ``SOURCE_MAX_PIXELS``, or the zip cannot be read.
        readers.ReadTimeout: The deadline passed.
    """
    from PIL import Image

    largest = total = 0
    before = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = None
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                readers.check()
                if info.is_dir():
                    continue
                try:
                    with archive.open(info) as member, Image.open(member, formats=_PICTURE_FORMATS) as picture:
                        pixels = picture.width * picture.height
                except Exception:  # not a picture Pillow knows, or not one it can size: it counts for nothing
                    continue
                if pixels > SOURCE_MAX_PIXELS:
                    raise readers.UnreadableFile(_PICTURE_TOO_LARGE)
                largest = max(largest, pixels)
                total += pixels
    except (zipfile.BadZipFile, zlib.error, EOFError, OSError):
        raise readers.UnreadableFile(readers.DAMAGED) from None
    finally:
        Image.MAX_IMAGE_PIXELS = before
    return _Pictures(largest, total)


def _headroom(pictures: _Pictures) -> int:
    """The bytes of ``RLIMIT_AS`` a conversion must leave free at its peak, for its pictures to be whole.

    LibreOffice loses a picture only when the request to decode it does not fit, so a peak further from the limit than
    the largest request means that nothing was lost. The largest is the largest picture's, at ``_BYTES_PER_PIXEL``;
    ``_SLACK`` covers the rest (a metafile's bitmap, an effect drawn as a bitmap).

    Args:
        pictures: The file's pictures.

    Returns:
        The headroom in bytes.
    """
    return _SLACK + _BYTES_PER_PIXEL * pictures.largest


def _pages_per_part(pages: int, pictures: _Pictures, headroom: int, file_bytes: int) -> int:
    """How many pages each part has at first, once the whole file came within its headroom.

    As many as the room under the limit holds when every page holds the pictures' average, and at most half the file.
    The room is what a child has left once it has started LibreOffice and loaded the file (``_LIBREOFFICE``), over what
    the job's process holds when it forks it. Without a limit, a size to start from or a picture Pillow could size, the
    file is halved.

    Args:
        pages: The file's pages.
        pictures: Its pictures.
        headroom: The bytes each conversion must leave free.
        file_bytes: The file's size.

    Returns:
        The pages of a part, at least 1.
    """
    half = (pages + 1) // 2
    held, limit = office.address_space()
    need = _BYTES_PER_PIXEL * pictures.total
    if held is None or limit is None or need == 0:
        return half
    room = limit - headroom - held - _LIBREOFFICE - file_bytes
    return max(1, min(half, room * pages // need))


def _parts(
    convert: Converter,
    source: Path,
    folder: Path,
    *,
    deck: bool,
    deadline: float,
    headroom: int,
    pages: int,
    per_part: int,
) -> list[Path]:
    """Convert a file in page ranges, each in a child of its own, halving a range that comes within the headroom.

    Args:
        convert: The converter.
        source: The file.
        folder: Where the parts are written.
        deck: The file is a presentation.
        deadline: When the conversion must stop.
        headroom: The bytes each conversion must leave free.
        pages: The file's pages.
        per_part: The pages of a part at first.

    Returns:
        The parts' PDFs, in the order of their pages.

    Raises:
        office.AtCeiling: A single page came within the headroom.
        readers.UnreadableFile: A part could not be converted, does not have its range's pages, or the parts pass
            ``CONVERTED_MAX_BYTES`` together.
        readers.ReadTimeout: The deadline passed.
    """
    todo = [(first, min(first + per_part - 1, pages)) for first in range(1, pages + 1, per_part)]
    todo.reverse()
    made: list[Path] = []
    size = 0
    while todo:
        first, last = todo.pop()
        part = folder / f"part-{first}-{last}.pdf"
        try:
            convert(source, part, deck=deck, deadline=deadline, headroom=headroom, pages=(first, last))
        except office.AtCeiling:
            if first == last:
                raise
            with suppress(OSError):
                part.unlink()
            middle = (first + last) // 2
            todo += [(middle + 1, last), (first, middle)]
            continue
        if _count_pages(part) != last - first + 1:
            raise readers.UnreadableFile(_NOT_CONVERTED)
        size += part.stat().st_size
        if size > CONVERTED_MAX_BYTES:
            raise readers.UnreadableFile(_TOO_LARGE)
        made.append(part)
    return made


def _join(parts: list[Path], target: Path) -> None:
    """Join the parts' PDFs into one at ``target``, their pages in order.

    Args:
        parts: The parts, each checked.
        target: Where the PDF is written.

    Raises:
        readers.UnreadableFile: pdfium could not join or write them.
    """
    import pypdfium2

    from workbench.executor.render import pages

    joined = pypdfium2.PdfDocument.new()
    try:
        for part in parts:
            pdf = pages.open_pdf(part)
            try:
                joined.import_pages(pdf)
            finally:
                pdf.close()
        joined.save(target)
    except (pypdfium2.PdfiumError, OSError):
        raise readers.UnreadableFile(_NOT_CONVERTED) from None
    finally:
        joined.close()


def _convert(
    convert: Converter, source: Path, folder: Path, *, slides: int | None, deadline: float, pictures: _Pictures
) -> int:
    """Convert a file to ``folder / CONVERTED_NAME``: whole, or in parts when the whole came within its headroom.

    The parts need the number of pages. A PDF the whole conversion saved has them all, only pictures may be missing
    from it; when it saved none, a deck has as many as slides (hidden slides are drawn), and a document's are unknown.

    Args:
        convert: The converter.
        source: The file, checked.
        folder: A new folder of the job's tmp.
        slides: A deck's slides, or ``None`` for a document.
        deadline: When the conversion must stop.
        pictures: The file's pictures.

    Returns:
        The PDF's pages.

    Raises:
        readers.UnreadableFile: The file could not be converted, its PDF is not one or is too large, or its pictures
            cannot all be drawn: a single page still comes within the headroom, a document's whole conversion saved
            no PDF at it, or the deadline passed while the parts were made
            (:data:`~workbench.executor.render.office.TOO_MANY_PICTURES`).
        readers.ReadTimeout: The deadline passed while the whole file was converted.
    """
    deck = slides is not None
    headroom = _headroom(pictures)
    staged = folder / CONVERTED_NAME
    try:
        convert(source, staged, deck=deck, deadline=deadline, headroom=headroom, pages=None)
        return _count_pages(staged)
    except office.AtCeiling:
        if os.path.lexists(staged):
            pages = _count_pages(staged)
        elif slides is not None:
            pages = slides
        else:
            raise
    with suppress(OSError):
        staged.unlink()
    if pages == 1:
        raise office.AtCeiling
    try:
        per_part = _pages_per_part(pages, pictures, headroom, source.stat().st_size)
        parts = _parts(
            convert, source, folder, deck=deck, deadline=deadline, headroom=headroom, pages=pages, per_part=per_part
        )
        _join(parts, staged)
    except readers.ReadTimeout:
        raise readers.UnreadableFile(office.TOO_MANY_PICTURES) from None
    if _count_pages(staged) != pages:
        raise readers.UnreadableFile(_NOT_CONVERTED)
    return pages


def _count_pages(staged: Path) -> int:
    """Check the PDF a conversion made and count its pages.

    Args:
        staged: Where the converter wrote it.

    Returns:
        Its pages, at least 1.

    Raises:
        readers.UnreadableFile: It is no regular file (a link, or nothing), is over ``CONVERTED_MAX_BYTES``, does not
            start with the PDF signature, or pdfium cannot open it or finds no page in it.
    """
    from workbench.executor.render import pages

    try:
        info = os.lstat(staged)
    except OSError:
        raise readers.UnreadableFile(_NOT_CONVERTED) from None
    if not stat.S_ISREG(info.st_mode):
        raise readers.UnreadableFile(_NOT_CONVERTED)
    if info.st_size > CONVERTED_MAX_BYTES:
        raise readers.UnreadableFile(_TOO_LARGE)
    try:
        with staged.open("rb") as handle:
            head = handle.read(len(CONVERTED_SIGNATURE))
    except OSError:
        raise readers.UnreadableFile(_NOT_CONVERTED) from None
    if head != CONVERTED_SIGNATURE:
        raise readers.UnreadableFile(_NOT_CONVERTED)
    pdf = pages.open_pdf(staged)
    try:
        return len(pdf)
    finally:
        pdf.close()


def convert_office(args: dict[str, Any], ctx: JobContext, *, convert: Converter | None = None) -> dict[str, Any]:
    """Convert an Office file to one PDF in ``out/``, for ``prepare_file`` to draw.

    Args:
        args: ``path``, relative to the chat folder: a ``.docx``, ``.dotx``, ``.pptx``, ``.potx`` or ``.odt``; and
            ``budget_s``, the seconds after which the job stops (90 by default).
        ctx: The job's surroundings.
        convert: The converter; ``None`` is :func:`workbench.executor.render.office.convert`, looked up when the job
            runs so that a test can put its own in its place. It is called with production's defaults and no more: no
            argument of the job reaches its test-only parameters.

    Returns:
        A :class:`workbench.renders.ConvertAnswer` as a dict; the PDF is ``ctx.out_dir / CONVERTED_NAME``. Or an
        ``error`` result: ``file_not_found``, ``unsupported_format`` (another type, or a deck with no slides),
        ``unreadable_file`` (damaged, encrypted, a bomb, a picture over 120 megapixels, not convertible, pictures that
        cannot all be drawn within the memory limit, or a PDF over 32 MB) or ``read_timeout``, none of which names a
        path or quotes LibreOffice. ``out/`` is empty in each case.

    Raises:
        ValueError: ``path`` is not a path below the chat folder; that is the caller's mistake, never the file's.
        RuntimeError: The converter could not start LibreOffice; that is the job's error, never the file's.
    """
    name = protocol.check_input_path(str(args["path"]))
    real = ctx.work_dir / name
    if name.split("/")[0] == "out" or not real.is_file():
        return error("file_not_found", "there is no such file in this conversation's workspace")
    if PurePosixPath(name).suffix.lower() not in OFFICE_SUFFIXES:
        return error("unsupported_format", "only Word, PowerPoint and OpenDocument text files are converted")
    budget = float(args.get("budget_s", _BUDGET_S))
    deadline = time.monotonic() + budget
    readers.set_deadline(deadline)
    folder: Path | None = None
    try:
        with limit_until(deadline, readers.ReadTimeout):
            slides = _slides_of(real, ctx.file_mb)
            if slides == 0:
                return error("unsupported_format", _NO_SLIDES)
            pictures = _pictures(real)
            try:
                folder = Path(tempfile.mkdtemp())
            except OSError:  # its text holds the path
                return error("unreadable_file", _NOT_WRITTEN)
            converter = convert or office.convert
            pages = _convert(converter, real, folder, slides=slides, deadline=deadline, pictures=pictures)
            try:
                os.replace(folder / CONVERTED_NAME, ctx.out_dir / CONVERTED_NAME)
            except OSError:  # its text holds both paths
                return error("unreadable_file", _NOT_WRITTEN)
            return ConvertAnswer(name=CONVERTED_NAME, pages=pages, slides=slides).model_dump()
    except readers.UnsupportedFormat as exc:
        return error("unsupported_format", str(exc))
    except readers.UnreadableFile as exc:
        return error("unreadable_file", str(exc))
    except readers.ReadTimeout:
        return error(
            "read_timeout", f"converting the file took longer than {budget:g} s; it is too large or too complex"
        )
    finally:
        readers.set_deadline(None)
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
