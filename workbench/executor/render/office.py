"""Office files as a PDF: LibreOfficeKit, in-process through ctypes, in a child the job forks for one conversion.

LibreOfficeKit starts once per process and cannot be started again, and a C call that hangs inside it can only be
stopped by killing the process that made it. So the job never loads LibreOffice itself: :func:`convert` forks a child
(:func:`run_in_child`), which loads the library, opens the document and saves it as a PDF, then ends in ``os._exit``
without ``destroy`` (which can hang). The parent waits for the child until the deadline and kills and reaps it past
that. ``soffice`` is never run: it needs a socket, which the job's seccomp filter refuses; LibreOfficeKit needs none.

No macro runs: a document is loaded with ``Batch=true`` and without LibreOfficeKit's ``EnableMacrosExecution``, and the
profile (:data:`PROFILE_SETTINGS`) sets the macro security to very high, turns macros off and never updates links.
The image bakes that profile once, warm, in :data:`PROFILE` (``python -m workbench.executor.render.office bake
<dir>``), root's and read-only like the font cache; each conversion copies it into the job's private tmp
(:func:`seed_profile`).

The file is untrusted, and LibreOffice's own error text may name its path: it is never read (``getError`` is in the
vtable only for the order of the fields). A failure is an exit code of the child, which :func:`convert` turns into a
short fixed text. Standard library only, besides the readers' errors.

LibreOffice leaves out a picture it has no address space left to decode, saves the PDF all the same and says it
succeeded. The PDF cannot tell, but the child's peak address space can: a picture is lost only when its request did
not fit under ``RLIMIT_AS``, so the peak came within that request of the limit. The child reads its peak once the save
is done and answers :data:`EXIT_AT_CEILING` when it came within the caller's ``headroom`` (:class:`AtCeiling`).
"""

import ctypes
import json
import os
import resource
import shutil
import signal
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import Any, Final

from workbench.executor.readers import ReadTimeout, UnreadableFile

#: Where LibreOffice's program folder is in the image.
PROGRAM: Final = Path("/usr/lib/libreoffice/program")
#: The libraries that may hold ``libreofficekit_hook_2``, tried in this order: a merged build has the first.
LIBRARIES: Final = ("libmergedlo.so", "libsofficeapp.so")
#: The profile the image bakes, root's and read-only; each conversion works on a copy.
PROFILE: Final = Path("/opt/lo-profile")

#: How a document is loaded: no dialog, no interaction, and so no macro prompt to say yes to.
LOAD_OPTIONS: Final = b"Batch=true"

EXIT_OK: Final = 0
EXIT_NO_LIBRARY: Final = 3
EXIT_NO_START: Final = 4
EXIT_NO_LOAD: Final = 5
EXIT_NO_SAVE: Final = 6
#: The child's peak address space came within its headroom of the limit, saved or not (:class:`AtCeiling`).
EXIT_AT_CEILING: Final = 7
EXIT_FAILED: Final = 70

#: How long the image's bake may take to start LibreOfficeKit once.
_BAKE_S: Final = 120.0

_NOT_OPENED: Final = "LibreOffice could not open the file: it is damaged, or not what its extension says"
_NOT_CONVERTED: Final = "the file could not be converted to pages"
_NO_START: Final = "LibreOffice could not start"
#: What a file is told whose pictures cannot all be drawn within a job's address space.
TOO_MANY_PICTURES: Final = "the file holds more or larger pictures than can be drawn within the memory limit"

_STATUS: Final = Path("/proc/self/status")
_PEAK_FIELD: Final = b"VmPeak:"
_SIZE_FIELD: Final = b"VmSize:"


class AtCeiling(UnreadableFile):
    """The child's peak address space came within ``headroom`` of the limit: pictures may be lost from its PDF.

    Writer and Impress leave such a picture out and save; Impress may fail the save instead. A PDF that was saved stays
    at the target. Its pages are all there, since the layout never waits for a picture to be decoded, so its page count
    holds; its pictures may not, and it is never kept as it is.
    """

    def __init__(self) -> None:
        """Say so in the fixed text, :data:`TOO_MANY_PICTURES`."""
        super().__init__(TOO_MANY_PICTURES)


#: The profile's settings, as officecfg names them: macro security very high (3) and macros off, no link updated
#: (Writer's ``Link`` 2 and Calc's ``Link`` 1 are "never"), no autosave or recovery, no first-run, no lock files.
_SETTINGS: Final = (
    ("/org.openoffice.Office.Common/Security/Scripting", "MacroSecurityLevel", "3"),
    ("/org.openoffice.Office.Common/Security/Scripting", "DisableMacrosExecution", "true"),
    ("/org.openoffice.Office.Common/Security/Scripting", "BlockUntrustedRefererLinks", "true"),
    ("/org.openoffice.Office.Writer/Content/Update", "Link", "2"),
    ("/org.openoffice.Office.Calc/Content/Update", "Link", "1"),
    ("/org.openoffice.Office.Recovery/AutoSave", "Enabled", "false"),
    ("/org.openoffice.Office.Recovery/RecoveryInfo", "Enabled", "false"),
    ("/org.openoffice.Office.Common/Misc", "FirstRun", "false"),
    ("/org.openoffice.Office.Common/Misc", "UseLocking", "false"),
)

#: The profile's ``user/registrymodifications.xcu``.
PROFILE_SETTINGS: Final = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
    + "".join(
        f'<item oor:path="{path}"><prop oor:name="{name}" oor:op="fuse"><value>{value}</value></prop></item>\n'
        for path, name, value in _SETTINGS
    )
    + "</oor:items>\n"
)

#: Pictures at most 150 dpi: the pages are drawn at 1600 px, and a deck of photos stays small.
_SMALL_PICTURES: Final = {
    "ReduceImageResolution": {"type": "boolean", "value": "true"},
    "MaxImageResolution": {"type": "long", "value": "150"},
}
_WRITER: Final = _SMALL_PICTURES
#: Hidden slides drawn and no notes pages, so that slide N is page N.
_IMPRESS: Final = {
    **_SMALL_PICTURES,
    "ExportHiddenSlides": {"type": "boolean", "value": "true"},
    "ExportNotesPages": {"type": "boolean", "value": "false"},
}

#: The PDF filter's data for a document, as LibreOffice's JSON filter options.
WRITER_OPTIONS: Final = json.dumps(_WRITER, separators=(",", ":")).encode()
#: The PDF filter's data for a deck.
IMPRESS_OPTIONS: Final = json.dumps(_IMPRESS, separators=(",", ":")).encode()


def filter_options(*, deck: bool, pages: tuple[int, int] | None = None) -> bytes:
    """The PDF filter's data: Impress's or Writer's, and ``PageRange`` when only some pages are drawn.

    A range counts the pages of the PDF the whole file gives: hidden slides included, and the empty pages Writer
    inserts (and leaves out of the PDF) not. A range past the last page gives fewer pages, which the caller checks.

    Args:
        deck: The file is a presentation.
        pages: The first and last page to draw, counted from 1; ``None`` for all.

    Returns:
        The JSON, as bytes.

    Raises:
        ValueError: ``pages`` is not a range of pages.
    """
    if pages is None:
        return IMPRESS_OPTIONS if deck else WRITER_OPTIONS
    first, last = pages
    if not 1 <= first <= last:
        raise ValueError("a page range runs from page 1 or later to a page no earlier")
    data = {**(_IMPRESS if deck else _WRITER), "PageRange": {"type": "string", "value": f"{first}-{last}"}}
    return json.dumps(data, separators=(",", ":")).encode()


def at_ceiling(peak: int | None, limit: int | None, headroom: int) -> bool:
    """Tell whether a peak address space came within ``headroom`` of its limit, so that a request may have failed.

    Args:
        peak: The process's peak address space in bytes (``VmPeak``); ``None`` when it could not be read.
        limit: ``RLIMIT_AS`` in bytes; ``None`` when there is none.
        headroom: The bytes that must have stayed free: the largest request that may fail without a crash.

    Returns:
        ``True`` when the peak came that close, or when there is a limit and the peak could not be read.
    """
    if limit is None:
        return False
    return peak is None or peak + headroom >= limit


# LibreOfficeKit's ABI (LibreOfficeKit.h): each object is a pointer to its class, a table of functions that starts
# with its size. Only the fields up to the last function used are declared, in the header's order.


class _Doc(ctypes.Structure):
    """``LibreOfficeKitDocument``: a pointer to its class."""


class _Office(ctypes.Structure):
    """``LibreOfficeKit``: a pointer to its class."""


_DocP: Final = ctypes.POINTER(_Doc)
_OfficeP: Final = ctypes.POINTER(_Office)


class _DocClass(ctypes.Structure):
    """``LibreOfficeKitDocumentClass``, as far as ``saveAs``."""

    _fields_ = (
        ("nSize", ctypes.c_size_t),
        ("destroy", ctypes.CFUNCTYPE(None, _DocP)),
        ("saveAs", ctypes.CFUNCTYPE(ctypes.c_int, _DocP, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p)),
    )


class _OfficeClass(ctypes.Structure):
    """``LibreOfficeKitClass``, as far as ``documentLoadWithOptions``."""

    _fields_ = (
        ("nSize", ctypes.c_size_t),
        ("destroy", ctypes.CFUNCTYPE(None, _OfficeP)),
        ("documentLoad", ctypes.CFUNCTYPE(_DocP, _OfficeP, ctypes.c_char_p)),
        ("getError", ctypes.CFUNCTYPE(ctypes.c_char_p, _OfficeP)),
        ("documentLoadWithOptions", ctypes.CFUNCTYPE(_DocP, _OfficeP, ctypes.c_char_p, ctypes.c_char_p)),
    )


_Doc._fields_ = (("pClass", ctypes.POINTER(_DocClass)),)
_Office._fields_ = (("pClass", ctypes.POINTER(_OfficeClass)),)


def run_in_child(work: Callable[[], int], deadline: float, *, poll_s: float = 0.05) -> int:
    """Run ``work`` in a forked child and wait for it until ``deadline``.

    The child never returns into the caller's code: it ends in ``os._exit`` with what ``work`` returned, or
    :data:`EXIT_FAILED` when ``work`` raised. The parent polls, so a time limit of the job's own can raise while it
    waits; whatever ends the wait early, the child is killed and reaped before the error goes on.

    Args:
        work: What the child does; it returns the child's exit code.
        deadline: A :func:`time.monotonic` time after which the child is killed.
        poll_s: How long the parent sleeps between two looks.

    Returns:
        The child's exit code, or ``-N`` when signal ``N`` ended it.

    Raises:
        ReadTimeout: The child still ran at the deadline; it has been killed and reaped.
    """
    pid = os.fork()
    if pid == 0:  # the child never returns into the job's code
        code = EXIT_FAILED
        try:
            code = work()
        except BaseException:  # whatever happens, the child ends here
            pass
        os._exit(code)
    reaped = False
    try:
        while True:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                reaped = True
                return os.waitstatus_to_exitcode(status)
            if time.monotonic() >= deadline:
                raise ReadTimeout
            time.sleep(poll_s)  # the job's own limit_until may raise here too
    finally:
        if not reaped:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def seed_profile(*, under: Path | None = None, prebuilt: Path = PROFILE) -> Path:
    """Give one conversion a LibreOffice profile of its own, in a new folder of the job's private tmp.

    The image's profile is root's and read-only, and LibreOffice writes to its profile, so it is copied, as
    ``runners/python.py`` copies matplotlib's font cache. Outside the image there is none, and the folder gets the
    settings alone; LibreOffice lays out the rest on its first start.

    Args:
        under: Where the folder is made; the job's ``TMPDIR`` by default.
        prebuilt: The baked profile.

    Returns:
        The folder, which holds ``user/``.
    """
    folder = Path(tempfile.mkdtemp(prefix="lo-", dir=under))
    if prebuilt.is_dir():
        shutil.copytree(prebuilt, folder, dirs_exist_ok=True)
    else:
        _write_settings(folder)
    return folder


def convert(
    source: Path,
    target: Path,
    *,
    deck: bool,
    deadline: float,
    headroom: int,
    pages: tuple[int, int] | None = None,
    test_only_load_options: bytes = LOAD_OPTIONS,
    test_only_profile_settings: str | None = None,
) -> None:
    """Convert an Office file to a PDF, in a forked child, by the deadline.

    The child's exit code decides the answer. Only a child that could not start LibreOffice is the executor's error:
    no library, no start, or an error of this module's own code in the child (:data:`EXIT_FAILED`). Everything else
    that ends without a PDF is the file's: LibreOffice could not open it, could not save it, exited by itself or died
    of a signal (a file that crashes it), or said it saved and left no regular file at ``target``. A child whose peak
    came within ``headroom`` of the address-space limit is :class:`AtCeiling`. The profile's copy is removed once the
    child is gone.

    Args:
        source: The file, already checked (``readers.check_zip``, no encryption).
        target: Where the PDF is written.
        deck: The file is a presentation: Impress's filter data (hidden slides drawn, no notes pages).
        deadline: A :func:`time.monotonic` time after which the child is killed.
        headroom: The bytes of ``RLIMIT_AS`` that must stay free at the child's peak: at least the largest request
            LibreOffice may make and survive the failure of, which is the largest picture it decodes.
        pages: The first and last page to draw (:func:`filter_options`); ``None`` for all.
        test_only_load_options: For tests only, never in a job: what the document is loaded with in place of
            :data:`LOAD_OPTIONS` (the macro test's positive control asks LibreOfficeKit to run macros).
        test_only_profile_settings: For tests only, never in a job: a ``registrymodifications.xcu`` written over the
            seeded profile's; ``None``, the default, keeps the baked profile as it is.

    Raises:
        AtCeiling: The PDF's pictures may be incomplete, or the save failed for want of address space
            (:data:`EXIT_AT_CEILING`).
        UnreadableFile: LibreOffice could not open the file (:data:`EXIT_NO_LOAD`), or not convert it
            (:data:`EXIT_NO_SAVE`, any other nonzero code, a signal, or a PDF said to be saved that is not there): a
            fixed text that names no path.
        RuntimeError: LibreOffice could not start (:data:`EXIT_NO_LIBRARY`, :data:`EXIT_NO_START`,
            :data:`EXIT_FAILED`).
        ReadTimeout: The conversion ran past the deadline; the child has been killed and reaped.
    """
    options = filter_options(deck=deck, pages=pages)
    profile = seed_profile()
    try:
        if test_only_profile_settings is not None:
            _write_settings(profile, test_only_profile_settings)
        work = partial(
            _convert_in_child, source, target, profile, options, headroom, load_options=test_only_load_options
        )
        code = run_in_child(work, deadline)
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    if code == EXIT_AT_CEILING:
        raise AtCeiling
    if code == EXIT_OK and _is_regular(target):
        return
    if code in (EXIT_NO_LIBRARY, EXIT_NO_START, EXIT_FAILED):
        raise RuntimeError(_NO_START)
    if code == EXIT_NO_LOAD:
        raise UnreadableFile(_NOT_OPENED)
    raise UnreadableFile(_NOT_CONVERTED)


def bake(profile: Path) -> None:
    """Make the image's profile: the settings, then one start of LibreOfficeKit, so every job copies a warm one.

    The child's ``HOME`` and ``TMPDIR`` are a folder removed afterwards, so the start leaves nothing in the image
    outside the profile; nor does the profile keep the lock file of a process that is gone.

    Args:
        profile: The folder to make it in.

    Raises:
        RuntimeError: LibreOfficeKit did not start.
    """
    _write_settings(profile)
    with tempfile.TemporaryDirectory(prefix="lo-bake-") as scratch:
        code = run_in_child(partial(_start_in_child, profile, Path(scratch)), time.monotonic() + _BAKE_S)
    if code != EXIT_OK:
        raise RuntimeError(f"{_NO_START}: exit code {code}")
    (profile / ".lock").unlink(missing_ok=True)


def main(argv: Sequence[str]) -> int:
    """Run ``bake <dir>``, as the image's build does.

    Args:
        argv: The arguments after the module's name.

    Returns:
        The exit code: 0 when baked, 2 for a wrong command line.
    """
    if len(argv) != 2 or argv[0] != "bake":
        print("usage: python -m workbench.executor.render.office bake <dir>", file=sys.stderr)
        return 2
    bake(Path(argv[1]))
    return 0


def _write_settings(profile: Path, settings: str = PROFILE_SETTINGS) -> None:
    """Write the settings as ``user/registrymodifications.xcu`` of ``profile``.

    Args:
        profile: The profile's folder.
        settings: The file's text: :data:`PROFILE_SETTINGS`, but in a test.
    """
    path = profile / "user" / "registrymodifications.xcu"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(settings, encoding="utf-8")


def _is_regular(path: Path) -> bool:
    """Tell whether ``path`` is a regular file, not following a symlink.

    Args:
        path: The path.

    Returns:
        ``True`` when it is.
    """
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _hook() -> Any:
    """Load LibreOfficeKit's library, in the forked child only.

    Returns:
        ``libreofficekit_hook_2``, typed, or ``None`` when no library holds it.
    """
    for name in LIBRARIES:
        try:
            hook = ctypes.CDLL(str(PROGRAM / name), mode=ctypes.RTLD_GLOBAL).libreofficekit_hook_2
        except (OSError, AttributeError):
            continue
        hook.argtypes = (ctypes.c_char_p, ctypes.c_char_p)
        hook.restype = _OfficeP
        return hook
    return None


def _start(profile: Path) -> tuple[int, Any]:
    """Start LibreOfficeKit on ``profile``, headless, in the forked child only.

    Args:
        profile: The profile's folder.

    Returns:
        :data:`EXIT_OK` and the ``LibreOfficeKit`` pointer, or the exit code that says why it did not start and
        ``None``.
    """
    os.environ["SAL_USE_VCLPLUGIN"] = "svp"
    hook = _hook()
    if hook is None:
        return EXIT_NO_LIBRARY, None
    office = hook(os.fsencode(PROGRAM), profile.as_uri().encode())
    if not office:
        return EXIT_NO_START, None
    return EXIT_OK, office


def _start_in_child(profile: Path, scratch: Path) -> int:
    """Start LibreOfficeKit once and end: the bake's child.

    Args:
        profile: The profile's folder.
        scratch: The child's ``HOME`` and ``TMPDIR``.

    Returns:
        The child's exit code.
    """
    os.environ.update(HOME=str(scratch), TMPDIR=str(scratch))
    return _start(profile)[0]


def _status(field: bytes) -> int | None:
    """Read one of this process's sizes in ``/proc/self/status``, such as ``VmPeak:``.

    Args:
        field: The field's name with its colon.

    Returns:
        The size in bytes, or ``None`` when it cannot be read (no ``/proc``).
    """
    try:
        lines = _STATUS.read_bytes().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith(field):
            try:
                return int(line.split()[1]) * 1024
            except (IndexError, ValueError):
                return None
    return None


def address_space() -> tuple[int | None, int | None]:
    """Read this process's address space: what it holds now (``VmSize``) and its limit (``RLIMIT_AS``).

    A child forked now starts with what the process holds.

    Returns:
        Both in bytes; the first ``None`` when it cannot be read, the second ``None`` when there is no limit.
    """
    soft, _ = resource.getrlimit(resource.RLIMIT_AS)
    return _status(_SIZE_FIELD), None if soft == resource.RLIM_INFINITY else soft


def _convert_in_child(
    source: Path, target: Path, profile: Path, options: bytes, headroom: int, *, load_options: bytes = LOAD_OPTIONS
) -> int:
    """Open ``source`` and save it as a PDF at ``target``: the conversion's child, which never calls ``destroy``.

    Args:
        source: The file.
        target: Where the PDF goes.
        profile: The profile's folder.
        options: The PDF filter's data.
        headroom: The bytes of the address-space limit that must have stayed free at the child's peak.
        load_options: What the document is loaded with: :data:`LOAD_OPTIONS`, but in a test
            (``convert``'s ``test_only_load_options``).

    Returns:
        The child's exit code: :data:`EXIT_AT_CEILING` when its peak came within ``headroom`` of the limit, whether
        the PDF was saved or not (Impress fails the save of a deck of pictures with alpha at the ceiling).
    """
    code, office = _start(profile)
    if office is None:
        return code
    document = office.contents.pClass.contents.documentLoadWithOptions(
        office, source.absolute().as_uri().encode(), load_options
    )
    if not document:
        return EXIT_NO_LOAD
    saved = document.contents.pClass.contents.saveAs(document, target.absolute().as_uri().encode(), b"pdf", options)
    if at_ceiling(_status(_PEAK_FIELD), address_space()[1], headroom):
        return EXIT_AT_CEILING
    return EXIT_OK if saved else EXIT_NO_SAVE


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
