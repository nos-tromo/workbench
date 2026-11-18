"""Value types shared across the api."""

from dataclasses import dataclass
from typing import Literal

VIRTUAL_ROOT = "/workspace"


@dataclass(frozen=True)
class Principal:
    """Who is calling and which chat folder their files go to by default.

    The user is the access boundary: every file lives under the user's own root.
    The chat id is only a folder inside that root and never selects another
    user's data, whatever value a client sends.

    Attributes:
        caller: Authenticated calling service, for example ``ghostwriter``.
        user_id: The caller's own id for the end user.
        chat_id: Chat folder used when a path names none; ``default`` without a session.
        confined: The principal reaches only its own chat folder, ``chat_id``.
    """

    caller: str
    user_id: str
    chat_id: str
    confined: bool = False


@dataclass(frozen=True)
class FileRecord:
    """One stored file and its metadata.

    Attributes:
        id: Opaque file id (``uuid4().hex``); the only thing audit lines name a file by.
        caller: Authenticated calling service that owns the file.
        user_id: The caller's id for the owning end user.
        chat_id: Chat folder the file is in.
        name: Path of the file below its chat folder: the plain file name, or
            ``folder/name`` for a file inside a folder.
        size: Size in bytes.
        mime: Media type guessed from the name; ``application/octet-stream`` if unknown.
        kind: ``upload`` for a file the user put there, ``output`` for one a tool made.
        created_at: Creation time in seconds since the epoch; retention counts from it.
        job_id: Id of the job that made the file; ``None`` for an upload.
    """

    id: str
    caller: str
    user_id: str
    chat_id: str
    name: str
    size: int
    mime: str
    kind: Literal["upload", "output"]
    created_at: float
    job_id: str | None

    @property
    def virtual_path(self) -> str:
        """The path the caller sees: ``/workspace/<chat-id>/<name>``."""
        return f"{VIRTUAL_ROOT}/{self.chat_id}/{self.name}"


@dataclass(frozen=True)
class DirEntry:
    """One entry of a folder listing.

    Attributes:
        name: The entry's own name, without its folder.
        type: ``file`` or ``directory``.
        size: Size in bytes; 0 for a directory.
        modified: Last modification time in seconds since the epoch.
    """

    name: str
    type: Literal["file", "directory"]
    size: int
    modified: float


@dataclass(frozen=True)
class JobRecord:
    """One tool run, kept so its outputs and sources can be tied to the user who ran it.

    Attributes:
        id: Opaque job id (``uuid4().hex``).
        caller: Authenticated calling service.
        user_id: The caller's id for the end user who ran it.
        chat_id: Chat folder the job ran for.
        tool: Name of the tool.
        created_at: Start time in seconds since the epoch.
        status: ``running`` until the job finishes, then its final status.
    """

    id: str
    caller: str
    user_id: str
    chat_id: str
    tool: str
    created_at: float
    status: str
