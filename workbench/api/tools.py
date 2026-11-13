"""The tools the model calls: routes tagged :data:`~workbench.api.openapi.TOOL_TAG`, one operation each.

Open WebUI makes a tool of every operation with an ``operationId``, so each route sets its
own, and the description is written for the model rather than taken from the docstring.
``display_file`` lives with the file browser in :mod:`workbench.api.files`, since it shares
that module's audit rule; the rest of the tools arrive with the executor.
"""

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from workbench.api.deps import PrincipalDep, StoreDep
from workbench.api.openapi import TOOL_TAG

router = APIRouter(tags=[TOOL_TAG])


class FileInfo(BaseModel):
    """One file in the chat's workspace.

    Attributes:
        path: The file's absolute path.
        size: Size in bytes.
        modified: When the file was made, ISO 8601 in UTC.
        kind: ``upload`` for a file the user put there, ``output`` for one a tool made.
    """

    path: str = Field(description="The file's absolute path; use it as is in other tools.")
    size: int = Field(description="Size in bytes.")
    modified: str = Field(description="When the file was made, ISO 8601 in UTC.")
    kind: Literal["upload", "output"] = Field(description="upload: the user put it there. output: a tool made it.")


class FileList(BaseModel):
    """What ``list_files`` answers.

    Attributes:
        files: The files of the chat, oldest first.
    """

    files: list[FileInfo]


@router.post(
    "/tools/list_files",
    operation_id="list_files",
    summary="List the files of this conversation",
    description=(
        "List every file in this conversation's workspace, the user's uploads and the files "
        "tools made, with path, size, date and kind. Takes no arguments. Call it first to see "
        "what there is to work with."
    ),
)
def list_files(p: PrincipalDep, store: StoreDep) -> FileList:
    """List the files of the principal's chat.

    Listing is the user's own metadata and is not recorded (spec section 7).

    Args:
        p: The caller, user and chat.
        store: The file store.

    Returns:
        The files of the chat, oldest first.
    """
    return FileList(
        files=[
            FileInfo(
                path=rec.virtual_path,
                size=rec.size,
                modified=datetime.fromtimestamp(rec.created_at, tz=UTC).isoformat(timespec="seconds"),
                kind=rec.kind,
            )
            for rec in store.files_in_chat(p)
        ]
    )
