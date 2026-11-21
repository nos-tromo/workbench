"""The runner of ``convert_office``: an Office file converted once to a PDF, which ``prepare_file`` then draws.

The job's working folder is the chat folder, as for ``prepare_file``. LibreOffice
(:mod:`workbench.executor.render.office`) sees a file only after the checks that need no LibreOffice: the zip guard
(bombs, an old or encrypted container), an OpenDocument manifest that says its content is encrypted, and, for a deck, a
count of its slides that is not zero. A file refused there is ``unreadable_file`` or ``unsupported_format``, and no
child is forked.

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
    ConvertAnswer,
    unit_of,
)

_BUDGET_S: Final = 90.0

_MIB: Final = 1024 * 1024

#: The part of an OpenDocument file that says which members are encrypted, and the most of it that is read.
_MANIFEST: Final = "META-INF/manifest.xml"
_MANIFEST_READ_MAX: Final = _MIB
_ENCRYPTION_MARK: Final = b"encryption-data"

_LOCKED: Final = "the file is password-protected (encrypted)"
_NOT_CONVERTED: Final = "the file could not be converted to pages"
_NOT_WRITTEN: Final = "the converted file could not be written"
_NO_SLIDES: Final = "the deck has no slides; inspect_template lists the layouts of a slide master"


class Converter(Protocol):
    """What converts an Office file to a PDF: :func:`workbench.executor.render.office.convert`, or a test's stand-in."""

    def __call__(self, source: Path, target: Path, *, deck: bool, deadline: float) -> None:
        """Convert ``source`` to a PDF at ``target``.

        Args:
            source: The file, already checked.
            target: Where the PDF is written.
            deck: The file is a presentation.
            deadline: A :func:`time.monotonic` time after which the conversion must stop.

        Raises:
            readers.UnreadableFile: The file cannot be converted.
            readers.ReadTimeout: The deadline passed.
            RuntimeError: The converter could not start.
        """
        ...


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
        raise readers.UnreadableFile(f"the converted file is larger than {CONVERTED_MAX_BYTES // _MIB} MB")
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
        ``unreadable_file`` (damaged, encrypted, a bomb, not convertible, or a PDF over 32 MB) or ``read_timeout``,
        none of which names a path or quotes LibreOffice. ``out/`` is empty in each case.

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
            try:
                folder = Path(tempfile.mkdtemp())
            except OSError:  # its text holds the path
                return error("unreadable_file", _NOT_WRITTEN)
            staged = folder / CONVERTED_NAME
            (convert or office.convert)(real, staged, deck=slides is not None, deadline=deadline)
            pages = _count_pages(staged)
            try:
                os.replace(staged, ctx.out_dir / CONVERTED_NAME)
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
