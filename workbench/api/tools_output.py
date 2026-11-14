"""The output tools, ``write_table`` and ``write_document``, on the router of :mod:`workbench.api.tools`.

Each makes one new file of the chat from the model's values: a table as an Excel workbook or a CSV file, a document as
a Word file written from Markdown or as the Markdown itself. The runners (:mod:`workbench.executor.runners.outputs`)
write the file in the executor; the job is given no file. Here the api checks the request before a job is sent: the
file's name, and a table by the rules of :mod:`workbench.tables` (the sheet's name, every cell's place, its length, a
number within Excel's range), which the runner checks again with the same functions; a refusal is a 422 that names the
field, or the row and the column counted from 1, and costs no job. A number that is not finite must be refused here:
the request goes to the executor as JSON, which has no NaN or infinity. A Markdown table wider than Word's 63 columns
is refused by the runner, which parses the Markdown, before it writes anything.

Each is recorded as a ``collect`` of the new file, by its id; ``write_table`` with how many rows and columns the table
has. Never a name, a column's name or a value. A new file whose event cannot be written is removed again.
"""

from collections.abc import Mapping
from typing import Any, Final, Literal

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from workbench.api.audit import AuditLog
from workbench.api.deps import AuditDep, JobsDep, PrincipalDep, SettingsDep, StoreDep
from workbench.api.jobs import JobQueue
from workbench.api.store import Store
from workbench.api.tools import (
    MAX_HINT,
    _capped,
    _clean,
    _parse,
    _payload,
    _readable,
    _remove_outputs,
    _remove_unsent,
    _to_the_end,
    _within,
    router,
)
from workbench.models import FileRecord, Principal
from workbench.paths import PathError, check_filename
from workbench.tables import MAX_COLUMNS, MAX_ROWS, MAX_SHEET_NAME, MAX_WORD_COLUMNS, Delimiter, table_problem
from workbench.utils.audit_event import AuditWriteError

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

    path: str = Field(description="The new file's path; show it with display_file.")
    rows: int = Field(description="How many rows were written below the header.")
    hint: str | None = Field(default=None, max_length=MAX_HINT, description="What to tell the user about the file.")


class WrittenDocument(BaseModel):
    """What ``write_document`` answers.

    Attributes:
        path: The new file's path.
        hint: What to tell the user.
    """

    path: str = Field(description="The new file's path; show it with display_file.")
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


def _record_output(
    audit: AuditLog, store: Store, p: Principal, tool: str, output: FileRecord, criteria: Mapping[str, object] | None
) -> None:
    """Record a new file by its id.

    As with an upload, a file whose making cannot be recorded does not stay: it is removed again.

    Args:
        audit: The audit log.
        store: The file store.
        p: The caller, user and chat.
        tool: ``write_table`` or ``write_document``.
        output: The new file.
        criteria: The event's criteria, never content.

    Raises:
        AuditWriteError: The event could not be written.
    """
    try:
        audit.record(tool, p, file_ids=[output.id], criteria=criteria)
    except AuditWriteError:
        _remove_outputs(store, [output])  # the audit failure is the answer
        raise


async def _write(
    p: Principal,
    store: Store,
    audit: AuditLog,
    jobs: JobQueue,
    tool: str,
    args: dict[str, Any],
    criteria: Mapping[str, object] | None = None,
) -> tuple[FileRecord, _Written]:
    """Write the file in the executor, keep it and record it.

    Args:
        p: The caller, user and chat.
        store: The file store.
        audit: The audit log.
        jobs: The job queue.
        tool: ``write_table`` or ``write_document``.
        args: The runner's arguments.
        criteria: The event's criteria.

    Returns:
        The new file and the runner's answer.

    Raises:
        HTTPException: The runner refused a value (its 422), the job failed or ran out of time, its answer cannot be
            read, or its file was not kept (500).
    """
    _, result, made = await jobs.submit(p, tool, args, [])

    def record() -> tuple[FileRecord, _Written]:
        if result.status == "ok" and (not _readable(_Written, result.result) or len(made) != 1):
            # Nothing the model is not told of may stay.
            _remove_unsent(audit, store, p, tool, made)
            if _readable(_Written, result.result):
                note = _clean(result.message) or "the job made no file"
                raise HTTPException(status_code=500, detail=f"the file could not be kept: {note}")
        done = _parse(_Written, _payload(result, "file"))
        (new,) = made
        _record_output(audit, store, p, tool, new, criteria)
        return new, done

    return await _to_the_end(record)


@router.post(
    "/tools/write_table",
    operation_id="write_table",
    summary="Write a table into a new Excel or CSV file",
    description=(
        "Write a table into a new file of this conversation: an Excel workbook (format xlsx, a bold header row that "
        "stays in view) or a CSV file (format csv, UTF-8, comma-separated unless delimiter is ; or a tab). For "
        "people who will open the file in Excel, use xlsx: Excel reads a CSV file by the computer's regional "
        "settings (German Excel splits it on ; and may read 1.5 as text or a date), so CSV is for exchanging data "
        "with other programs. columns are the header's names; rows are lists of values in the columns' order, each "
        "a string, a number, true, false or null; a shorter row is filled with empty cells. At most 100000 rows, "
        "16384 columns and 32767 characters a cell. Values are data, never formulas: =SUM(A1:A3), 0042 or a web "
        "address stay text; a number keeps 15 significant digits in Excel, so write long identifiers as strings. In "
        "a CSV a text that starts with =, +, -, @, a tab or a carriage return gets a leading ' so that a spreadsheet "
        "does not run it, and hint says how many. sheet names the workbook's sheet. One request is at most 1 MB: for "
        "a large table, write the file with run_python into out/ instead. Show the result with display_file."
    ),
    response_model_exclude_none=True,
)
async def write_table(
    body: WriteTableRequest,
    p: PrincipalDep,
    settings: SettingsDep,
    store: StoreDep,
    audit: AuditDep,
    jobs: JobsDep,
) -> WrittenTable:
    """Write the model's table into a new file of the chat, in the executor.

    Args:
        body: The name, the format, the columns, the rows, the sheet and the CSV's delimiter.
        p: The caller, user and chat.
        settings: The api's settings; ``job_wall_s`` gives the time limit, ``result_max_kb`` caps the answer.
        store: The file store.
        audit: The audit log.
        jobs: The job queue.

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
    new, done = await _write(p, store, audit, jobs, "write_table", args, criteria)
    answer = WrittenTable(path=new.virtual_path, rows=len(body.rows), hint=_capped(done.hint, MAX_HINT))
    return _within(answer, settings.result_max_kb * 1024)


@router.post(
    "/tools/write_document",
    operation_id="write_document",
    summary="Write a document into a new Word or Markdown file",
    description=(
        "Write a document into a new file of this conversation from Markdown: a Word document (format docx) or the "
        "Markdown itself (format md). A Word document takes headings # to #### (##### and ###### become level 4), "
        "paragraphs, **bold** and *italic*, bullet and numbered lists nested up to three levels (each numbered list "
        f"counts from its own first number), tables of up to {MAX_WORD_COLUMNS} columns (| a | b | over a |---|---| "
        "line; Word holds no wider one, so write that with write_table) and code, in blocks or "
        "`inline`, in a monospace font. A link is written as its text with the address in parentheses, an image as "
        "its alt text: nothing is fetched. Raw HTML and other syntax stay plain text. At most 200000 characters. "
        "Show the result with display_file."
    ),
    response_model_exclude_none=True,
)
async def write_document(
    body: WriteDocumentRequest,
    p: PrincipalDep,
    settings: SettingsDep,
    store: StoreDep,
    audit: AuditDep,
    jobs: JobsDep,
) -> WrittenDocument:
    """Write the model's Markdown into a new Word or Markdown file of the chat, in the executor.

    Args:
        body: The name, the format and the Markdown.
        p: The caller, user and chat.
        settings: The api's settings; ``job_wall_s`` gives the time limit, ``result_max_kb`` caps the answer.
        store: The file store.
        audit: The audit log.
        jobs: The job queue.

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
    new, done = await _write(p, store, audit, jobs, "write_document", args)
    answer = WrittenDocument(path=new.virtual_path, hint=_capped(done.hint, MAX_HINT))
    return _within(answer, settings.result_max_kb * 1024)
