"""Virtual ``/workspace`` paths, confined to one user's own root on the data volume.

Open WebUI sends ``/workspace/<chat-id>/<name>``. Every such path maps to
``<data_dir>/workspaces/<caller>/<user-id>/<chat-id>/<name>`` and to nothing
else: the caller and the user id name the root, so no path reaches another
user's files. Symlinks are never followed; an existing one in the way is
refused.
"""

import os
import posixpath
import re
import stat
from pathlib import Path

from workbench.models import VIRTUAL_ROOT, Principal

DEFAULT_CHAT = "default"

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
#: The longest file name, in characters and in UTF-8 bytes (Linux ``NAME_MAX``).
MAX_FILENAME = 200
MAX_FILENAME_BYTES = 255
_PRIVATE = 0o700


class PathError(ValueError):
    """A path or an id is outside what a workspace allows."""


def check_id(value: str, *, what: str) -> str:
    """Accept an id that is safe as one folder name.

    Args:
        value: The caller, user or chat id.
        what: Name of the field, used in the error message.

    Returns:
        ``value``, unchanged.

    Raises:
        PathError: If it is empty, too long, starts with a separator character,
            holds anything outside letters, digits and ``_.:-``, or contains ``..``.
    """
    if _ID.fullmatch(value) is None or ".." in value:
        raise PathError(f"invalid {what}")
    return value


def check_filename(name: str) -> str:
    """Accept a name that is one plain path segment.

    Args:
        name: A file or folder name.

    Returns:
        ``name``, unchanged.

    Raises:
        PathError: If it is empty, over 200 characters or over 255 bytes in UTF-8 (Linux
            ``NAME_MAX``), cannot be encoded as UTF-8, holds NUL or ``/``, or is ``.`` or ``..``.
    """
    if not 1 <= len(name) <= MAX_FILENAME or "\x00" in name or "/" in name or name in {".", ".."}:
        raise PathError("invalid file name")
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise PathError("invalid file name") from exc
    if len(encoded) > MAX_FILENAME_BYTES:
        raise PathError("invalid file name")
    return name


def _check_segments(segments: list[str]) -> list[str]:
    """Check a chat id followed by file names.

    Args:
        segments: The parts below the virtual root.

    Returns:
        ``segments``, unchanged.
    """
    if segments:
        check_id(segments[0], what="chat id")
        for name in segments[1:]:
            check_filename(name)
    return segments


class Workspaces:
    """Maps virtual paths to real ones under the workspaces folder of the data volume."""

    def __init__(self, data_dir: Path) -> None:
        """Remember where the workspaces live.

        Args:
            data_dir: Root of the ``workbench-data`` volume.
        """
        self._base = data_dir / "workspaces"

    def user_root(self, p: Principal) -> Path:
        """Return the folder holding everything one user owns.

        Args:
            p: The caller and user.

        Returns:
            ``<data_dir>/workspaces/<caller>/<user-id>``; not created.

        Raises:
            PathError: If the caller or the user id is not a safe folder name.
        """
        caller = check_id(p.caller, what="caller")
        user_id = check_id(p.user_id, what="user id")
        return self._base / caller / user_id

    def chat_dir(self, p: Principal) -> Path:
        """Return the principal's chat folder, creating it and its parents with mode 0700.

        Args:
            p: The caller, user and chat.

        Returns:
            ``<user root>/<chat-id>``.

        Raises:
            PathError: If an id is unsafe, an existing component is a symlink, or the
                folder's place is taken by something that is not a directory.
        """
        root = self.user_root(p)
        chat = root / check_id(p.chat_id, what="chat id")
        self._refuse_symlinks(chat)
        for path in (self._base, root.parent, root, chat):
            try:
                os.mkdir(path, _PRIVATE)
            except FileExistsError:
                continue
            os.chmod(path, _PRIVATE)
        if not stat.S_ISDIR(os.lstat(chat).st_mode):
            raise PathError("the chat folder is not a directory")
        return chat

    def resolve(self, p: Principal, virtual: str) -> Path:
        """Map a virtual path to its real path inside the user's root.

        Nothing is followed: the path is normalised as text, every segment is
        checked, and every component that already exists is looked at with
        ``lstat`` and refused if it is a symlink. The file need not exist.

        Args:
            p: The caller and user whose root the path maps into.
            virtual: A path such as ``/workspace/<chat-id>/<name>``.

        Returns:
            The real path; the user root itself for ``/workspace``.

        Raises:
            PathError: If the path is outside ``/workspace``, a segment is invalid,
                or an existing component is a symlink.
        """
        if "\x00" in virtual:
            raise PathError("path holds a NUL byte")
        norm = posixpath.normpath(virtual)
        if norm != VIRTUAL_ROOT and not norm.startswith(VIRTUAL_ROOT + "/"):
            raise PathError("path is outside the workspace")
        segments = _check_segments([s for s in norm[len(VIRTUAL_ROOT) :].split("/") if s])
        root = self.user_root(p)
        real = root.joinpath(*segments)
        self._refuse_symlinks(real)
        if real != root and root not in real.parents:
            raise PathError("path is outside the workspace")
        return real

    def to_virtual(self, p: Principal, real: Path) -> str:
        """Map a real path in the user's root back to its virtual path.

        Args:
            p: The caller and user whose root the path is in.
            real: A real path, for example one ``resolve`` returned.

        Returns:
            ``/workspace`` or ``/workspace/<chat-id>/<name>``.

        Raises:
            PathError: If ``real`` is not inside the user's root or holds an invalid segment.
        """
        root = self.user_root(p)
        try:
            rel = real.relative_to(root)
        except ValueError as exc:
            raise PathError("path is not in the workspace") from exc
        segments = _check_segments(list(rel.parts))
        return "/".join([VIRTUAL_ROOT, *segments])

    def _refuse_symlinks(self, real: Path) -> None:
        """Refuse a path if any existing component below the workspaces folder is a symlink.

        Args:
            real: A path under the workspaces folder.

        Raises:
            PathError: If a component is a symlink or cannot be inspected.
        """
        chain = [self._base]
        for part in real.relative_to(self._base).parts:
            chain.append(chain[-1] / part)
        for path in chain:
            try:
                mode = os.lstat(path).st_mode
            except (FileNotFoundError, NotADirectoryError):
                return
            except OSError as exc:
                raise PathError("cannot inspect the path") from exc
            if stat.S_ISLNK(mode):
                raise PathError("symlinks are not allowed")
