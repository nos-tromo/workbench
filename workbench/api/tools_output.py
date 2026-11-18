"""The output tools, ``write_table`` and ``write_document``, two rows of :mod:`workbench.api.tool_table`.

Each makes one new file of the chat from the model's values: a table as an Excel workbook or a CSV file, a document as
a Word file written from Markdown or as the Markdown itself. The runners (:mod:`workbench.executor.runners.outputs`)
write the file in the executor; the job is given no file. Here the api checks the request before a job is sent: the
file's name, and a table by the rules of :mod:`workbench.tables` (the sheet's name, every cell's place, its length, a
number within Excel's range), which the runner checks again with the same functions; a refusal is a 422 that names the
field, or the row and the column counted from 1, and costs no job. A number that is not finite must be refused here:
the request goes to the executor as JSON, which has no NaN or infinity. A Markdown table wider than Word's 63 columns
is refused by the runner, which parses the Markdown, before it writes anything.

Each is recorded as a ``collect`` of the new file, by its id, before the file becomes visible; ``write_table`` with how
many rows and columns the table has. Never a name, a column's name or a value. A new file whose event cannot be written
is never kept, and a job that refused is recorded too, by the refusal's code.
"""

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, Literal

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from workbench.api.audit import AuditLog
from workbench.api.tools import (
    MAX_HINT,
    OutputKeeper,
    _capped,
    _clean,
    _parse,
    _payload,
    _readable,
    _within,
    judged,
)
from workbench.config import Settings
from workbench.models import FileRecord, Principal
from workbench.paths import PathError, check_filename
from workbench.protocol import JobResult
from workbench.tables import MAX_COLUMNS, MAX_ROWS, MAX_SHEET_NAME, Delimiter, table_problem

if TYPE_CHECKING:  # tools_mcp imports this module, through tool_table
    from workbench.api.tools_mcp import CallJobs

#: Most characters of a document's Markdown; the runner checks it again (a test ties the two).
MAX_MARKDOWN: Final = 200_000

#: The longest name the model may give a new file, before its extension is added.
MAX_NAME: Final = 255

_NAME: Final = (
    "The new file's name. The extension is added if it is missing (report.txt becomes report.txt.docx); a name that "
    "is taken gets a number."
)

Cell = str | int | float | bool | None


class WriteTableRequest(BaseModel):
    """Body of ``write_table``.

    Attributes:
        name: The new file's name.
        format: ``xlsx`` or ``csv``.
        columns: The header's names.
        rows: The rows, each a list of values in the columns' order.
        sheet: The workbook's sheet.
        delimiter: What separates a CSV file's cells.
    """

    name: str = Field(min_length=1, max_length=MAX_NAME, description=_NAME)
    format: Literal["xlsx", "csv"] = Field(description="xlsx: an Excel workbook. csv: a CSV file, UTF-8.")
    columns: list[str] = Field(
        min_length=1, max_length=MAX_COLUMNS, description="The header's names, one per column; 1 to 16384 columns."
    )
    rows: list[list[Cell]] = Field(
        max_length=MAX_ROWS,
        description=(
            "The rows, at most 100000, each a list of values in the columns' order: a string, a number, true, false "
            "or null. A row shorter than columns is filled with empty cells; a cell holds at most 32767 characters."
        ),
    )
    sheet: str = Field(
        default="Sheet1",
        min_length=1,
        max_length=MAX_SHEET_NAME,
        description="The sheet's name in a workbook: 1 to 31 characters, none of [ ] : * ? / \\, not History.",
    )
    delimiter: Delimiter = Field(
        default=",",
        description="What separates a CSV file's cells: a comma (the default), a semicolon or a tab. Not for xlsx.",
    )


class WriteDocumentRequest(BaseModel):
    """Body of ``write_document``.

    Attributes:
        name: The new file's name.
        format: ``docx`` or ``md``.
        markdown: The document, in Markdown.
    """

    name: str = Field(min_length=1, max_length=MAX_NAME, description=_NAME)
    format: Literal["docx", "md"] = Field(description="docx: a Word document. md: the Markdown itself, as given.")
    markdown: str = Field(max_length=MAX_MARKDOWN, description="The document in Markdown; at most 200000 characters.")


class WrittenTable(BaseModel):
    """What ``write_table`` answers.

    Attributes:
        path: The new file's path.
        rows: How many rows were written below the header.
        hint: What to tell the user.
    """

    path: str = Field(description="The new file's path; the user sees it as a card in the chat.")
    rows: int = Field(description="How many rows were written below the header.")
    hint: str | None = Field(default=None, max_length=MAX_HINT, description="What to tell the user about the file.")


class WrittenDocument(BaseModel):
    """What ``write_document`` answers.

    Attributes:
        path: The new file's path.
        hint: What to tell the user.
    """

    path: str = Field(description="The new file's path; the user sees it as a card in the chat.")
    hint: str | None = Field(default=None, max_length=MAX_HINT, description="What to tell the user about the file.")


class _Written(BaseModel):
    """What an output runner answers; checked before anything of it is sent on.

    Attributes:
        hint: What the model should know; cut to the answer's bound when the answer is built.
    """

    hint: str | None = None


def _output_name(wanted: str, extension: str) -> str:
    """Name the new file: as asked, with the extension added unless the name ends in it already, in any case.

    Args:
        wanted: The name the model gave.
        extension: The format's extension, without its dot.

    Returns:
        A plain file name.

    Raises:
        HTTPException: The name, with its extension, is not a plain file name of at most 200 characters and 255 bytes
            in UTF-8, or is the extension alone (422).
    """
    suffix = f".{extension}"
    name = wanted if wanted.lower().endswith(suffix) else f"{wanted}{suffix}"
    try:
        check_filename(name)
    except PathError:
        name = ""
    if len(name) <= len(suffix):
        raise HTTPException(
            status_code=422,
            detail=(
                f"name: a plain file name without /, of at most 200 characters and 255 bytes with its {suffix}, "
                "and something before the extension"
            ),
        )
    return name


async def _write(
    p: Principal,
    audit: AuditLog,
    jobs: "CallJobs",
    tool: str,
    args: dict[str, Any],
    criteria: Mapping[str, object] | None = None,
) -> tuple[FileRecord, _Written]:
    """Write the file in the executor, record it, then keep it.

    Args:
        p: The caller, user and chat.
        audit: The audit log.
        jobs: The queue, within the call's limits.
        tool: ``write_table`` or ``write_document``.
        args: The runner's arguments.
        criteria: The event's criteria.

    Returns:
        The new file and the runner's answer.

    Raises:
        HTTPException: The runner refused a value (its 422), the job failed or ran out of time, its answer cannot be
            read, or its file was not kept (500).
    """
    base = dict(criteria or {})

    def decide(result: JobResult, planned: Sequence[str]) -> tuple[list[str], str, dict[str, object]]:
        # One new file, of an answer the model can be given; otherwise nothing is kept, the model never learning of it.
        if result.status == "ok" and _readable(_Written, result.result) and len(planned) == 1:
            return list(planned), "ok", base
        outcome, more = judged(result, False)
        return [], outcome, {**base, "outputs": 0, **more}

    _, result, made = await jobs.submit(p, tool, args, [], OutputKeeper(audit, p, tool, [], decide))
    done = _parse(_Written, _payload(result, "file"))
    if len(made) != 1:
        note = _clean(result.message) or "the job made no file"
        raise HTTPException(status_code=500, detail=f"the file could not be kept: {note}")
    (new,) = made
    return new, done


async def write_table(
    body: WriteTableRequest,
    p: Principal,
    settings: Settings,
    audit: AuditLog,
    jobs: "CallJobs",
) -> WrittenTable:
    """Write the model's table into a new file of the chat, in the executor.

    Args:
        body: The name, the format, the columns, the rows, the sheet and the CSV's delimiter.
        p: The caller, user and chat.
        settings: The api's settings; ``job_wall_s`` gives the time limit, ``result_max_kb`` caps the answer.
        audit: The audit log.
        jobs: The queue, within the call's limits.

    Returns:
        The new file's path, how many rows it has, and the runner's hint.

    Raises:
        HTTPException: The name, the sheet or a cell is refused (422), or the file could not be written (see
            :func:`_write`).
    """
    name = _output_name(body.name, body.format)
    problem = await run_in_threadpool(table_problem, body.columns, body.rows, body.sheet)
    if problem is not None:
        raise HTTPException(status_code=422, detail=problem)
    args: dict[str, Any] = {
        "name": name,
        "format": body.format,
        "columns": body.columns,
        "rows": body.rows,
        "sheet": body.sheet,
        "delimiter": body.delimiter,
        "budget_s": max(1, settings.job_wall_s * 3 // 4),
    }
    criteria = {"rows": len(body.rows), "columns": len(body.columns)}
    new, done = await _write(p, audit, jobs, "write_table", args, criteria)
    answer = WrittenTable(path=new.virtual_path, rows=len(body.rows), hint=_capped(done.hint, MAX_HINT))
    return _within(answer, settings.result_max_kb * 1024)


async def write_document(
    body: WriteDocumentRequest,
    p: Principal,
    settings: Settings,
    audit: AuditLog,
    jobs: "CallJobs",
) -> WrittenDocument:
    """Write the model's Markdown into a new Word or Markdown file of the chat, in the executor.

    Args:
        body: The name, the format and the Markdown.
        p: The caller, user and chat.
        settings: The api's settings; ``job_wall_s`` gives the time limit, ``result_max_kb`` caps the answer.
        audit: The audit log.
        jobs: The queue, within the call's limits.

    Returns:
        The new file's path, and the runner's hint.

    Raises:
        HTTPException: The name is refused (422), or the file could not be written (see :func:`_write`).
    """
    name = _output_name(body.name, body.format)
    args: dict[str, Any] = {
        "name": name,
        "format": body.format,
        "markdown": body.markdown,
        "budget_s": max(1, settings.job_wall_s * 3 // 4),
    }
    new, done = await _write(p, audit, jobs, "write_document", args)
    answer = WrittenDocument(path=new.virtual_path, hint=_capped(done.hint, MAX_HINT))
    return _within(answer, settings.result_max_kb * 1024)
