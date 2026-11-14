"""Lay out the data volume: ``python -m workbench.executor.prepare_volume``, once, as root, before both services.

The layout below the volume root (``WORKBENCH_DATA_DIR``, ``/data`` in the images):

- the root itself: owner root, group of the api, mode ``1771``. The api may add files, which
  SQLite needs for the journal of its database, but the sticky bit keeps it from renaming or
  removing what root owns, ``jobs`` above all; jobs may pass through but not list it.
- ``workspaces/``: the api's, ``0700``. The users' files.
- ``staging/``: the api's, ``0700``. Outputs the executor hands over, one folder per job.
- ``jobs/``: root's, ``0711``. One private folder per running job; nobody may list it.
- ``workbench.db``: the api's, ``0600``.

Nothing is ever followed: the root and every part are opened with ``O_NOFOLLOW``, and a
symlink, or a part of the wrong kind, stops the run before it is changed. Running it again
changes no content; it only puts owners and modes back.
"""

import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from workbench.config import ConfigError, load_settings
from workbench.executor.fs import CREATE_FLAGS, DIR_FLAGS, READ_FLAGS, closing, settle

API_UID = 10001
API_GID = 10001

WORKSPACES = "workspaces"
STAGING = "staging"
JOBS = "jobs"
DATABASE = "workbench.db"

_ROOT_MODE = 0o1771
_FOLDERS = ((WORKSPACES, "api", 0o700), (STAGING, "api", 0o700), (JOBS, "root", 0o711))
_DATABASE_MODE = 0o600


class VolumeError(Exception):
    """A part of the layout is a symlink or of the wrong kind; nothing was changed through it."""


@dataclass(frozen=True)
class Owners:
    """The user and group ids the layout is handed to.

    Attributes:
        root: Owner of the volume root and of ``jobs``; the group of the root is the api's.
        api: Owner of ``workspaces``, ``staging`` and the database.
    """

    root: tuple[int, int] = (0, 0)
    api: tuple[int, int] = (API_UID, API_GID)


def _kind(name: str, parent: int) -> int | None:
    """Look at a part without following it.

    Args:
        name: The part's name in the volume root.
        parent: The volume root, opened.

    Returns:
        Its ``st_mode``, or ``None`` when it does not exist.

    Raises:
        VolumeError: It is a symlink.
    """
    try:
        mode = os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode):
        raise VolumeError(f"{name} is a symlink")
    return mode


def _folder(name: str, parent: int, ids: tuple[int, int], mode: int) -> None:
    """Make or repair one folder of the layout.

    Args:
        name: Its name in the volume root.
        parent: The volume root, opened.
        ids: Its owner and group.
        mode: Its permission bits.

    Raises:
        VolumeError: It is a symlink or not a folder.
    """
    found = _kind(name, parent)
    if found is None:
        os.mkdir(name, 0o700, dir_fd=parent)
    elif not stat.S_ISDIR(found):
        raise VolumeError(f"{name} is not a folder")
    try:
        fd = os.open(name, DIR_FLAGS, dir_fd=parent)
    except OSError as exc:  # replaced since it was looked at
        raise VolumeError(f"{name} is not a folder") from exc
    with closing(fd):
        settle(fd, ids, mode)


def _database(parent: int, ids: tuple[int, int]) -> None:
    """Make or repair the database file; an existing one keeps its content.

    It is created exclusively or opened without ``O_CREAT``, which a sticky folder could refuse
    to root for a file root does not own.

    Args:
        parent: The volume root, opened.
        ids: Its owner and group.

    Raises:
        VolumeError: It is a symlink or not a regular file.
    """
    found = _kind(DATABASE, parent)
    if found is None:
        fd = os.open(DATABASE, CREATE_FLAGS, 0o600, dir_fd=parent)
    elif not stat.S_ISREG(found):
        raise VolumeError(f"{DATABASE} is not a regular file")
    else:
        try:
            fd = os.open(DATABASE, READ_FLAGS, dir_fd=parent)
        except OSError as exc:
            raise VolumeError(f"{DATABASE} is not a regular file") from exc
    with closing(fd):
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise VolumeError(f"{DATABASE} is not a regular file")
        settle(fd, ids, _DATABASE_MODE)


def prepare_volume(data_dir: Path, owners: Owners | None = None) -> None:
    """Make the layout below ``data_dir``, or put its owners and modes back.

    Args:
        data_dir: The volume root.
        owners: Who gets what; ``None`` is root and the api's ``10001``.

    Raises:
        VolumeError: The root or a part is a symlink or of the wrong kind.
        OSError: The root is missing, or an owner or mode could not be set.
    """
    owners = owners or Owners()
    if stat.S_ISLNK(os.lstat(data_dir).st_mode):
        raise VolumeError("the data folder is a symlink")
    try:
        root_fd = os.open(data_dir, DIR_FLAGS)
    except OSError as exc:
        raise VolumeError("the data folder is not a folder") from exc
    with closing(root_fd):
        settle(root_fd, (owners.root[0], owners.api[1]), _ROOT_MODE)
        for name, owner, mode in _FOLDERS:
            _folder(name, root_fd, owners.api if owner == "api" else owners.root, mode)
        _database(root_fd, owners.api)


def main() -> int:
    """Prepare the volume named by ``WORKBENCH_DATA_DIR``.

    Returns:
        ``0`` once the layout is in place, ``1`` after a one-line message on stderr.
    """
    try:
        prepare_volume(load_settings(os.environ, role="executor").data_dir)
    except (ConfigError, VolumeError) as exc:
        print(f"workbench prepare-volume: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"workbench prepare-volume: {type(exc).__name__}: {exc.strerror}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
