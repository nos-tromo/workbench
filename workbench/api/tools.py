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

from workbench.api.deps import PrincipalDep, SettingsDep, StoreDep
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
        files: The files of the chat, newest first.
        truncated: Whether older files were left out to keep the answer within the result limit.
        hint: When ``truncated``, how many older files were left out.
    """

    files: list[FileInfo]
    truncated: bool = Field(default=False, description="True if older files exist than are listed here.")
    hint: str | None = Field(default=None, description="When truncated, how many older files are not listed.")


def _json_size(model: BaseModel) -> int:
    """Measure a model as the answer sends it: compact JSON, in bytes, without the hint if there is none.

    Args:
        model: The model.

    Returns:
        The size in bytes of its JSON.
    """
    return len(model.model_dump_json(exclude_none=True).encode())


def _notice(left: int, total: int, limit_kb: int) -> str:
    """Say how many older files an answer leaves out.

    Args:
        left: Files not listed; they are the oldest.
        total: Files in the chat.
        limit_kb: The result limit in kilobytes.

    Returns:
        The hint.
    """
    noun = "file" if left == 1 else "files"
    return (
        f"{left} older {noun} not listed ({total} in all). The list is cut at {limit_kb} KB, newest first; "
        "ask the user which file they mean, or to delete files they no longer need."
    )


def _fit(files: list[FileInfo], limit_kb: int) -> FileList:
    """Keep as many files, newest first, as the answer can hold within the result limit.

    Args:
        files: Every file of the chat, newest first.
        limit_kb: The result limit, in kilobytes.

    Returns:
        All the files if the answer fits, else the longest run from the newest that fits together
        with the notice that says how many older ones were left out.
    """
    limit = limit_kb * 1024
    everything = FileList(files=files)
    if _json_size(everything) <= limit:
        return everything
    total = len(files)
    used = _json_size(FileList(files=[], truncated=True, hint=_notice(total, total, limit_kb)))
    kept: list[FileInfo] = []
    for info in files:
        added = _json_size(info) + (1 if kept else 0)
        if used + added > limit:
            break
        used += added
        kept.append(info)
    return FileList(files=kept, truncated=True, hint=_notice(total - len(kept), total, limit_kb))


@router.post(
    "/tools/list_files",
    operation_id="list_files",
    summary="List the files of this conversation",
    description=(
        "List every file in this conversation's workspace, the user's uploads and the files "
        "tools made, newest first, with path, size, date and kind. Takes no arguments. Call it first "
        "to see what there is to work with. A very long list is cut: truncated is then true, and hint "
        "says how many older files are not listed."
    ),
    response_model_exclude_none=True,
)
def list_files(p: PrincipalDep, settings: SettingsDep, store: StoreDep) -> FileList:
    """List the files of the principal's chat, newest first, within the result limit.

    Listing is the user's own metadata and is not recorded (spec section 7). Files made at the
    same moment are in name order. A cut list drops the oldest files, so the file a tool has just
    written is always in it.

    Args:
        p: The caller, user and chat.
        settings: The api's settings; ``result_max_kb`` caps the answer.
        store: The file store.

    Returns:
        The files of the chat, newest first, cut with a hint if they would not fit the limit.
    """
    newest_first = sorted(store.files_in_chat(p), key=lambda rec: (-rec.created_at, rec.name))
    files = [
        FileInfo(
            path=rec.virtual_path,
            size=rec.size,
            modified=datetime.fromtimestamp(rec.created_at, tz=UTC).isoformat(timespec="seconds"),
            kind=rec.kind,
        )
        for rec in newest_first
    ]
    return _fit(files, settings.result_max_kb)
