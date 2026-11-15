"""File operations below a folder that is already open, never following a symlink.

The executor runs as root. Everything it touches below the data volume is opened relative to
a descriptor of the folder above it, with ``O_NOFOLLOW``, so a symlink planted by a job or by
the api in place of a file or folder is refused instead of followed, whatever it points at.
"""

import contextlib
import dataclasses
import os
import stat
from collections.abc import Iterable, Iterator

DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC  # a FIFO does not block the open
CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC

_COPY_CHUNK = 1024 * 1024


@contextlib.contextmanager
def closing(fd: int) -> Iterator[int]:
    """Close ``fd`` when the block ends.

    Args:
        fd: An open descriptor.

    Yields:
        ``fd``.
    """
    try:
        yield fd
    finally:
        os.close(fd)


def open_dir(name: str, parent: int) -> int:
    """Open a folder below ``parent`` without following a symlink.

    Args:
        name: One path segment.
        parent: The folder it is in, opened.

    Returns:
        The folder, opened.

    Raises:
        OSError: It is missing, a symlink or not a folder.
    """
    return os.open(name, DIR_FLAGS, dir_fd=parent)


def settle(fd: int, ids: tuple[int, int], mode: int) -> None:
    """Give an opened file or folder its owner and mode.

    Args:
        fd: It, opened.
        ids: Owner and group.
        mode: Permission bits.
    """
    os.fchown(fd, *ids)
    os.fchmod(fd, mode)


def make_dir(name: str, parent: int, ids: tuple[int, int], mode: int, *, exist_ok: bool = False) -> int:
    """Make a folder below ``parent`` and open it.

    Args:
        name: One path segment.
        parent: The folder to make it in, opened.
        ids: Its owner and group.
        mode: Its permission bits.
        exist_ok: Whether a folder already there is used.

    Returns:
        The folder, opened.

    Raises:
        OSError: It exists (unless allowed), or is a symlink or a file.
    """
    try:
        os.mkdir(name, 0o700, dir_fd=parent)
    except FileExistsError:
        if not exist_ok:
            raise
    fd = open_dir(name, parent)
    try:
        settle(fd, ids, mode)
    except BaseException:
        os.close(fd)
        raise
    return fd


def copy_bytes(source: int, target: int, size: int) -> int:
    """Copy at most ``size`` bytes from one descriptor to another.

    Args:
        source: Read from here.
        target: Written to here.
        size: Most bytes to copy.

    Returns:
        How many bytes were copied; fewer than ``size`` if the source ended first.
    """
    copied = 0
    while copied < size:
        chunk = os.read(source, min(_COPY_CHUNK, size - copied))
        if not chunk:
            break
        write_all(target, chunk)
        copied += len(chunk)
    return copied


def write_all(fd: int, data: bytes) -> None:
    """Write all of ``data``, however many calls it takes.

    Args:
        fd: Written to here.
        data: The bytes.
    """
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def open_regular(parent: int, parts: list[str]) -> int:
    """Open a regular file below ``parent`` through real folders only.

    Args:
        parent: Where the path starts, opened.
        parts: The path's segments.

    Returns:
        The file, opened for reading.

    Raises:
        OSError: A segment is missing, a symlink or of the wrong kind; the file is not regular.
    """
    folders: list[int] = []
    try:
        here = parent
        for name in parts[:-1]:
            here = open_dir(name, here)
            folders.append(here)
        fd = os.open(parts[-1], READ_FLAGS, dir_fd=here)
    finally:
        for folder in folders:
            os.close(folder)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise IsADirectoryError("not a regular file")
    return fd


@dataclasses.dataclass
class Use:
    """What a job's folders hold, as the watchdog measured it.

    Attributes:
        bytes: Disk the entries take, as the file system counts it (blocks), files and folders.
        entries: Names in the folders, every kind, the folders' own included.
        depth: The deepest folder reached, below the folders given (they are 0).
    """

    bytes: int = 0
    entries: int = 0
    depth: int = 0


def measure(tops: Iterable[int], *, max_entries: int, max_depth: int) -> Use:
    """Measure the trees below opened folders, without following a link, one descriptor per level at most.

    A walk stops counting once ``max_entries`` is passed, and enters no folder below ``max_depth``: past either, the
    caller has its answer. An entry that vanishes or changes while it is looked at is left out; the next measure finds
    it.

    Args:
        tops: The folders, opened; they stay open.
        max_entries: Past this many entries the walk stops.
        max_depth: Folders below this depth are not entered; ``depth`` then reads ``max_depth + 1``.

    Returns:
        What was found.

    Raises:
        OSError: A folder given in ``tops`` cannot be listed; every descriptor the walk opened is closed again.
    """
    use = Use()

    def listed(fd: int) -> Iterator[str]:
        folders: list[str] = []
        with os.scandir(fd) as entries:
            for entry in entries:
                use.entries += 1
                if use.entries > max_entries:
                    break
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                use.bytes += st.st_blocks * 512
                if stat.S_ISDIR(st.st_mode):
                    folders.append(entry.name)
        return iter(folders)

    for top in tops:
        frames: list[tuple[int, Iterator[str]]] = []
        try:
            here = os.dup(top)
            try:
                found = listed(here)
            except BaseException:
                os.close(here)
                raise
            frames.append((here, found))
            while frames and use.entries <= max_entries:
                fd, names = frames[-1]
                name = next(names, None)
                if name is None:
                    os.close(fd)
                    frames.pop()
                    continue
                if len(frames) > max_depth:
                    use.depth = max_depth + 1
                    break
                try:
                    child = os.open(name, DIR_FLAGS, dir_fd=fd)
                except OSError:
                    continue
                try:
                    found = listed(child)
                except OSError:
                    os.close(child)
                    continue
                frames.append((child, found))
                use.depth = max(use.depth, len(frames) - 1)
        finally:
            for fd, _ in frames:
                os.close(fd)
        if use.entries > max_entries or use.depth > max_depth:
            break
    return use
