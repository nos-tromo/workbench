"""Value types shared across the api."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from workbench.layout import DERIVED

VIRTUAL_ROOT = "/workspace"

#: What a derived file is of its source: ``image`` and ``page`` are the renders of a picture and of a page of a PDF,
#: ``pdf`` the PDF an Office file was converted to, ``frame`` and ``transcript`` what a clip's artifacts were made into,
#: and ``nextext_jsonl`` and ``nextext_zip`` those two artifacts as Nextext made them. The table's ``CHECK`` has all.
DerivedRole = Literal["image", "page", "pdf", "frame", "transcript", "nextext_jsonl", "nextext_zip"]


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


@dataclass(frozen=True)
class DerivedSpec:
    """What a derived file is, as it is registered; the rest of its record the store settles.

    Attributes:
        role: What it is of its source.
        ordinal: Its place among the files of that role, from 0.
        mime: Its media type; ``image/jpeg`` or ``image/png`` for a render.
        width: Its width in pixels; ``None`` where it has none.
        height: Its height in pixels; ``None`` where it has none.
        meta: Small facts about it, stored as JSON: for a strip of a page, ``strip`` and ``strips``.
    """

    role: DerivedRole
    ordinal: int
    mime: str
    width: int | None
    height: int | None
    meta: Mapping[str, object]


def _count(meta: Mapping[str, object], key: str) -> int:
    """Read a whole number of a derived file's facts, ``1`` where it has none.

    Args:
        meta: The facts.
        key: ``strip`` or ``strips``.

    Returns:
        The number, or ``1`` for a missing or no whole number.
    """
    value = meta.get(key, 1)
    return value if isinstance(value, int) and not isinstance(value, bool) else 1


@dataclass(frozen=True)
class DerivedRecord:
    """One derived file, such as the render of a page, kept for the life of its source file.

    It is bound to its source: it is made old with it, counted in the user's quota, hidden from every listing, and
    deleted with it. Not hashable, for ``meta`` is a mapping.

    Attributes:
        id: Opaque file id (``uuid4().hex``); the only thing audit lines name it by.
        source_id: Id of the file it was made from.
        caller: Authenticated calling service that owns it, as its source's owner.
        user_id: The caller's id for the owning end user.
        chat_id: Chat folder it is in.
        role: What it is of its source.
        ordinal: Its place among the files of that role, from 0.
        name: Its file name, ``<id>.jpg`` or ``<id>.png``.
        size: Size in bytes.
        mime: Media type.
        width: Width in pixels; ``None`` where it has none.
        height: Height in pixels; ``None`` where it has none.
        meta: Small facts about it, such as ``strip`` and ``strips``.
        created_at: Its source's ``created_at``: retention counts from it, so both go together.
    """

    id: str
    source_id: str
    caller: str
    user_id: str
    chat_id: str
    role: DerivedRole
    ordinal: int
    name: str
    size: int
    mime: str
    width: int | None
    height: int | None
    meta: Mapping[str, object]
    created_at: float

    @property
    def relative_path(self) -> str:
        """Its path below its chat folder: ``.derived/<source-id>/<name>``."""
        return f"{DERIVED}/{self.source_id}/{self.name}"

    @property
    def strip(self) -> int:
        """Which strip of its page it is, counted from 1; ``1`` for a whole page."""
        return _count(self.meta, "strip")

    @property
    def strips(self) -> int:
        """How many strips its page is cut into; ``1`` for a whole page."""
        return _count(self.meta, "strips")
