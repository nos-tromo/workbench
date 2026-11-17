"""The tools the model calls: routes tagged :data:`~workbench.api.openapi.TOOL_TAG`, one operation each.

Open WebUI makes a tool of every operation with an ``operationId``, so each route sets its
own, and the description is written for the model rather than taken from the docstring.
``display_file`` lives with the file browser in :mod:`workbench.api.files`, since it shares
that module's audit rule; ``read_document``, ``search_files`` and ``run_python`` hand the chat's files
to a job in the executor (:mod:`workbench.executor.runners.documents`,
:mod:`workbench.executor.runners.python`) and are built here from what the store knows and what the job
answers. Each later tool has a module of its own on this module's router, sharing its helpers:
``inspect_template`` and ``fill_template`` are in :mod:`workbench.api.tools_templates`, ``write_table`` and
``write_document`` in :mod:`workbench.api.tools_output`.

Nothing the job answers is trusted: its answer is validated into the models below, its paths are
replaced by the store's or checked against the files the job was given, its failures are mapped to
fixed codes (anything else is "the tool failed"), and the whole answer is cut to ``result_max_kb``
measured as the bytes that are sent.

Every job whose answer reached the api is recorded before anything of that answer goes out, a refusal
too: a runner's refusal can quote the file (a sheet's name, a line of a template, a drop-down's
entries), so it is an ``error`` event whose criteria name the refusal's code, never its text
(:func:`record_answer`). A tool that makes files records its job before any of them becomes visible
(:class:`OutputKeeper`, the queue's :class:`~workbench.api.jobs.Keeper`).
"""

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError
from pydantic.json_schema import JsonDict

from workbench.api.audit import AuditLog
from workbench.api.auth import require_tool
from workbench.api.deps import AuditDep, JobsDep, PrincipalDep, SettingsDep, StoreDep
from workbench.api.openapi import TOOL_TAG
from workbench.api.routing import EncodableRoute
from workbench.models import Principal
from workbench.protocol import KILLED, MAX_INPUT_FILES, OUT_DIR, JobResult

router = APIRouter(tags=[TOOL_TAG], route_class=EncodableRoute)


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


def _json_size(model: BaseModel, *, keep_none: bool = False) -> int:
    """Measure a model as the answer sends it: compact JSON, in bytes, without the hint if there is none.

    Args:
        model: The model.
        keep_none: Whether the answer sends its ``None`` fields too, as the inline answer does.

    Returns:
        The size in bytes of its JSON.
    """
    return len(model.model_dump_json(exclude_none=not keep_none).encode())


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
    dependencies=[Depends(require_tool("list_files"))],
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


# --- read_document and search_files ----------------------------------------------------------------------------

#: The most hits a search may return.
MAX_HITS: Final = 200

#: The runner's failures the model is told, and the status each is answered with; any other is a 500. Two are the
#: executor's own: ``storage_full``, the data volume is below its free-space floor, so the job was stopped or never
#: started (507, as a full volume is everywhere in the api); and ``hidden_memory``, the container held shared memory no
#: job's count showed, so every running job was stopped (503: nothing was wrong with the call, retry later).
_RUNNER_ERRORS: Final = {
    "unsupported_format": 422,
    "unreadable_file": 422,
    "no_such_part": 422,
    "read_timeout": 422,
    "file_not_found": 404,
    "bad_regex": 422,
    "regex_timeout": 422,
    "unsupported_template": 422,
    "invalid_template": 422,
    "invalid_value": 422,
    "invalid_source": 422,
    "write_timeout": 422,
    "storage_full": 507,
    "hidden_memory": 503,
}
_ERROR_CAP: Final = 300
_CUT_MARK: Final = " [cut]"

#: The longest hint an answer carries.
MAX_HINT: Final = 2000
_STRING: Final[JsonDict] = {"type": "string"}


class ReadDocumentRequest(BaseModel):
    """Body of ``read_document``.

    Attributes:
        path: The file's absolute path.
        part: Where to start reading, or ``None`` for the start.
    """

    path: str = Field(
        min_length=1,
        max_length=1024,
        description="The file's absolute path, for example /workspace/<chat>/report.pdf, as list_files gives it.",
    )
    part: str | None = Field(
        default=None,
        description=(
            "Where to start reading; leave it out for the start. Use the next_part of an earlier answer to go on. "
            "A page: 'page 3' or 'pages 3-9'. A slide: 'slide 2'. A paragraph: 'paragraph 40'. A table or some of its "
            "rows: 'table 2', 'table 2, row 350' or 'table 2, rows 30-60'. A sheet: its name, or a range of rows: "
            "'Sheet1!A201:F400'. Lines of a text: 'lines 100-200'."
        ),
        json_schema_extra=_STRING,
    )


class DocumentPart(BaseModel):
    """One piece of a document.

    Attributes:
        label: Where the text is, such as ``page 3`` or ``Sheet1!A1:F200``; give it as ``part`` to start there.
        text: The text.
    """

    label: str = Field(max_length=300, description="Where the text is; give it as part to start reading there.")
    text: str = Field(description="The text.")


class DocumentRead(BaseModel):
    """What ``read_document`` answers.

    Attributes:
        path: The file's path.
        kind: ``pdf``, ``docx``, ``xlsx``, ``csv``, ``pptx``, ``odt``, ``ods``, ``eml``, ``html``, ``json`` or
            ``text``.
        parts: The text, in order, each piece with its reference.
        total_parts: How many pages, sheets, slides, paragraphs and tables or lines the file has.
        pages_without_text: Pages or slides looked at that have no text.
        truncated: Whether text was left out of this answer.
        next_part: Where to continue, when the answer was cut.
        hint: What to tell the user, or how to go on.
    """

    path: str = Field(description="The file's path.")
    kind: str = Field(max_length=20, description="The kind of file that was read.")
    parts: list[DocumentPart] = Field(description="The text in order, each piece with its reference.")
    total_parts: int = Field(description="How many pages, sheets, slides, paragraphs and tables or lines there are.")
    pages_without_text: list[int] = Field(
        description="Pages or slides looked at that hold no text (scans or pictures): this tool does no OCR."
    )
    truncated: bool = Field(description="True if text was left out; then call again with part set to next_part.")
    next_part: str | None = Field(default=None, max_length=300, description="Where to continue when truncated.")
    hint: str | None = Field(default=None, max_length=2000, description="What to tell the user, or how to go on.")


class SearchFilesRequest(BaseModel):
    """Body of ``search_files``.

    Attributes:
        query: The text to find, or a regular expression.
        regex: Whether ``query`` is a regular expression.
        max_hits: How many hits to return, at most 200.
    """

    query: str = Field(min_length=1, max_length=1000, description="The text to find, or a regular expression.")
    regex: bool = Field(default=False, description="True if query is a regular expression; false for plain text.")
    max_hits: int = Field(default=50, description="The most hits to return, 50 by default and at most 200.")


class SearchHit(BaseModel):
    """One place a search matched.

    Attributes:
        path: The file's path.
        location: Where in the file: ``page 3``, ``Sheet1!B7``, ``paragraph 12``, ``line 40``.
        snippet: The text around the match, at most 200 characters.
    """

    path: str = Field(description="The file's path.")
    location: str = Field(max_length=300, description="Where in the file: page 3, Sheet1!B7, paragraph 12, line 40.")
    snippet: str = Field(max_length=400, description="The text around the match, at most 200 characters.")


class SkippedFile(BaseModel):
    """A file a search could not look into.

    Attributes:
        path: The file's path.
        reason: Why, in a few words.
    """

    path: str = Field(description="The file's path.")
    reason: str = Field(max_length=400, description="Why it could not be searched.")


class SearchResult(BaseModel):
    """What ``search_files`` answers.

    Attributes:
        hits: The matches, file by file.
        truncated: Whether there is more than is shown, or not every file was searched.
        files_searched: Files of a readable type that were looked into.
        unsupported_files: Files of a type that cannot be searched; they are not listed.
        skipped: Files that could not be searched, and why.
        hint: What to tell the user, or how to go on.
    """

    hits: list[SearchHit] = Field(description="The matches, file by file.")
    truncated: bool = Field(description="True if there are more hits than shown or not every file was searched.")
    files_searched: int = Field(description="How many files were searched.")
    unsupported_files: int = Field(description="How many files are of a type that cannot be searched.")
    skipped: list[SkippedFile] | None = Field(default=None, description="Files that could not be searched, and why.")
    hint: str | None = Field(default=None, max_length=2000, description="What to tell the user, or how to go on.")


def _clean(text: str) -> str:
    """Shorten a message from the job and drop its control characters.

    Args:
        text: The job's message.

    Returns:
        At most ``_ERROR_CAP`` printable characters.
    """
    return "".join(char for char in text if char.isprintable())[:_ERROR_CAP]


def _payload(result: JobResult, what: str) -> dict[str, Any]:
    """Take the runner's answer out of a job's result, or fail with what the model should be told.

    Args:
        result: The executor's answer.
        what: ``file`` or ``search``, for the words of a timeout.

    Returns:
        The runner's answer, of a job that ran to the end.

    Raises:
        HTTPException: The runner said it could not do it (its code decides the status), or the job
            ran out of time or memory (422), or failed in a way that is not one of the codes (500, with a
            fixed text: the job's own text may hold a path).
    """
    if result.status == "ok":
        return result.result
    if result.status == "error":
        error = result.result.get("error")
        if isinstance(error, str):
            status = _RUNNER_ERRORS.get(error.partition(": ")[0])
            if status is not None:
                raise HTTPException(status_code=status, detail=_clean(error))
        raise HTTPException(status_code=500, detail="the tool failed")
    verb = "processed" if what == "file" else "completed"
    raise HTTPException(status_code=422, detail=f"the {what} could not be {verb}: {_clean(result.message)}")


def _parse[M: BaseModel](model: type[M], payload: object) -> M:
    """Check a runner's answer against the model of what the api sends on.

    Args:
        model: The answer's model.
        payload: What the runner returned.

    Returns:
        The validated answer.

    Raises:
        HTTPException: It is not an answer of that shape (500).
    """
    try:
        return model.model_validate(payload)
    except ValidationError:
        raise HTTPException(status_code=500, detail="the tool returned an answer that could not be read") from None


def refusal(result: JobResult) -> str | None:
    """Name the refusal a job answered with, by its code alone.

    Args:
        result: The executor's answer.

    Returns:
        The runner's code, one of the fixed words the model is told (``no_such_part``, ``invalid_template``…), when the
        job failed with one; ``None`` otherwise. Never the message, which can quote the file.
    """
    if result.status != "error":
        return None
    error = result.result.get("error")
    if not isinstance(error, str):
        return None
    code = error.partition(": ")[0]
    return code if code in _RUNNER_ERRORS else None


def judged(result: JobResult, ok: bool) -> tuple[str, dict[str, object]]:
    """Say how a job's event ends, and what its criteria add for a refusal.

    Args:
        result: The executor's answer.
        ok: Whether the answer is one the api sends on: an ``ok`` job whose answer it can read.

    Returns:
        ``("ok", {})``; or ``("error", {"refused": <code>})`` for a refusal; or ``("error", {})`` for any other failure
        (a job past its time or a limit, a failed tool, an answer that could not be read or used).
    """
    if ok:
        return "ok", {}
    code = refusal(result)
    return "error", {} if code is None else {"refused": code}


def record_answer(
    audit: AuditLog,
    key: str,
    p: Principal,
    file_ids: Sequence[str],
    criteria: Mapping[str, object],
    result: JobResult,
    model: type[BaseModel],
    payload: object,
) -> None:
    """Record the answer of a job that makes no file, whatever it is, before anything of it goes out.

    Args:
        audit: The audit log.
        key: The tool, a key of the audit's operations.
        p: The caller, user and the chat folder of the files.
        file_ids: The files the job was given.
        criteria: The event's criteria, flags and counts.
        result: The executor's answer.
        model: The shape an ``ok`` answer must have to be sent on.
        payload: The answer as the route checks it against ``model``.

    Raises:
        AuditWriteError: The event could not be written; the answer must not go out.
    """
    outcome, more = judged(result, result.status == "ok" and _readable(model, payload))
    audit.record(key, p, file_ids=file_ids, outcome=outcome, criteria={**criteria, **more})


@dataclasses.dataclass(frozen=True)
class OutputKeeper:
    """Records a job of a tool that makes files before any of them is visible (:class:`~workbench.api.jobs.Keeper`).

    Attributes:
        audit: The audit log.
        p: The caller, user and the chat folder the files go to.
        tool: The tool, a key of the audit's operations.
        used: Ids of the files the job was given, named first in its event.
        decide: Given the answer and the ids planned for its outputs, the ids to keep, the outcome and the criteria.
    """

    audit: AuditLog
    p: Principal
    tool: str
    used: Sequence[str]
    decide: Callable[[JobResult, Sequence[str]], tuple[list[str], str, dict[str, object]]]

    def record(self, result: JobResult, planned: Sequence[str]) -> Sequence[str]:
        """Write the job's event, naming the files given and the outputs to keep, and say which those are.

        Args:
            result: The executor's answer.
            planned: The ids the job's outputs will have.

        Returns:
            The ids to keep.
        """
        keep, outcome, criteria = self.decide(result, planned)
        self.audit.record(self.tool, self.p, file_ids=[*self.used, *keep], outcome=outcome, criteria=criteria)
        return keep

    def again(self, ids: Sequence[str]) -> None:
        """Name outputs that did not end as the event said (not kept, or kept and then removed) in an ``error`` event.

        Args:
            ids: Their file ids.
        """
        self.audit.record(self.tool, self.p, file_ids=ids, outcome="error", criteria={"outputs": len(ids)})


def _join(*sentences: str | None) -> str | None:
    """Join sentences with a space, leaving out the missing ones.

    Args:
        sentences: The sentences.

    Returns:
        The text, or ``None`` if there is none.
    """
    return " ".join(sentence for sentence in sentences if sentence) or None


def _capped(text: str | None, limit: int) -> str | None:
    """Cut a text to the most characters a field of an answer takes.

    Args:
        text: The text, or ``None``.
        limit: The most characters.

    Returns:
        Its first ``limit`` characters, or ``None``.
    """
    return None if text is None else text[:limit]


def _with_paging(answer: DocumentRead, limit_kb: int) -> DocumentRead:
    """Add the sentence that tells the model how to go on, if the answer is cut and says where.

    Args:
        answer: The answer.
        limit_kb: The result limit in kilobytes.

    Returns:
        The answer with its hint extended, or as it is.
    """
    if not (answer.truncated and answer.next_part):
        return answer
    go_on = (
        f"The answer is cut at the result limit ({limit_kb} KB). "
        f'Call read_document again with part="{answer.next_part}" to continue.'
    )
    return answer.model_copy(update={"hint": _join(answer.hint, go_on)})


def _cut_part(shell: DocumentRead, part: DocumentPart, limit: int, limit_kb: int) -> DocumentPart | None:
    """Cut a part's text to the longest start that lets the whole answer fit.

    Args:
        shell: The answer without parts, as it will be sent.
        part: The part that does not fit.
        limit: The limit in bytes.
        limit_kb: The limit in kilobytes.

    Returns:
        The part with its text cut and marked, or ``None`` if not even the mark fits.
    """
    low, high = 0, len(part.text)
    best: DocumentPart | None = None
    while low <= high:
        middle = (low + high) // 2
        candidate = DocumentPart(label=part.label, text=part.text[:middle] + _CUT_MARK)
        if _json_size(_with_paging(shell.model_copy(update={"parts": [candidate]}), limit_kb)) <= limit:
            best, low = candidate, middle + 1
        else:
            high = middle - 1
    return best


def _within[M: BaseModel](answer: M, limit: int, *, keep_none: bool = False) -> M:
    """Shorten the hint, as a last resort, so that an answer whose parts or hits are cut as far as they go fits.

    Args:
        answer: The answer, already cut; a model with a ``hint``.
        limit: The limit in bytes.
        keep_none: Whether the answer sends its ``None`` fields too (:func:`_json_size`).

    Returns:
        The answer, with its hint cut to what fits if it was over the limit; as it is otherwise.
    """
    hint: str | None = getattr(answer, "hint", None)
    if _json_size(answer, keep_none=keep_none) <= limit or not hint:
        return answer
    low, high = 0, len(hint)
    best = answer.model_copy(update={"hint": None})
    while low <= high:
        middle = (low + high) // 2
        candidate = answer.model_copy(update={"hint": hint[:middle]})
        if _json_size(candidate, keep_none=keep_none) <= limit:
            best, low = candidate, middle + 1
        else:
            high = middle - 1
    return best


def _fit_read(answer: DocumentRead, limit_kb: int) -> DocumentRead:
    """Cut a read to the result limit, with whole parts: the first that does not fit is ``next_part``.

    The first part is never left out: if it alone is too long its text is cut and marked, so that
    paging always moves on.

    Args:
        answer: The validated answer.
        limit_kb: The result limit in kilobytes.

    Returns:
        The answer, within the limit as sent, with the sentence that says how to go on if it is cut.
    """
    limit = limit_kb * 1024
    whole = _with_paging(answer, limit_kb)
    if _json_size(whole) <= limit:
        return whole
    parts = answer.parts
    widest = max([part.label for part in parts] + [answer.next_part or ""], key=len)
    shell = answer.model_copy(update={"parts": [], "truncated": True, "next_part": widest})
    used = _json_size(_with_paging(shell, limit_kb))
    kept: list[DocumentPart] = []
    for part in parts:
        added = _json_size(part) + (1 if kept else 0)
        if used + added > limit:
            break
        used += added
        kept.append(part)
    following = parts[len(kept)].label if len(kept) < len(parts) else answer.next_part
    note: str | None = None
    if not kept and parts:
        following = parts[1].label if len(parts) > 1 else answer.next_part
        note = f"{parts[0].label} is longer than the result limit and is cut; its rest cannot be read by paging."
        shell = shell.model_copy(update={"hint": _join(answer.hint, note), "next_part": following})
        cut = _cut_part(shell, parts[0], limit, limit_kb)
        kept = [] if cut is None else [cut]
        following = following if cut is not None else parts[0].label
    final = answer.model_copy(
        update={"parts": kept, "truncated": True, "next_part": following, "hint": _join(answer.hint, note)}
    )
    return _within(_with_paging(final, limit_kb), limit)


def _fit_search(answer: SearchResult, limit_kb: int, notes: list[str]) -> SearchResult:
    """Cut a search to the result limit: hits are dropped from the end, then skipped files.

    Args:
        answer: The validated answer, with its hits already checked against the files that were searched.
        limit_kb: The result limit in kilobytes.
        notes: Sentences to add to the hint, such as how many files were left out of the search.

    Returns:
        The answer, within the limit as sent, with a sentence about what was left out.
    """
    limit = limit_kb * 1024
    hint = _join(answer.hint, *notes)
    truncated = answer.truncated or bool(notes)
    whole = answer.model_copy(update={"hint": hint, "truncated": truncated})
    if _json_size(whole) <= limit:
        return whole

    def cut_note(dropped: int) -> str:
        return (
            f"The answer is cut at the result limit ({limit_kb} KB): {dropped} further hits are not shown; "
            "narrow the query."
        )

    shell = answer.model_copy(update={"hits": [], "truncated": True, "hint": _join(hint, cut_note(len(answer.hits)))})
    used = _json_size(shell)
    kept: list[SearchHit] = []
    for hit in answer.hits:
        added = _json_size(hit) + (1 if kept else 0)
        if used + added > limit:
            break
        used += added
        kept.append(hit)
    final = answer.model_copy(
        update={"hits": kept, "truncated": True, "hint": _join(hint, cut_note(len(answer.hits) - len(kept)))}
    )
    skipped = list(final.skipped or [])
    while skipped and _json_size(final.model_copy(update={"skipped": skipped})) > limit:
        skipped.pop()
    return _within(final.model_copy(update={"skipped": skipped or None}), limit)


@router.post(
    "/tools/read_document",
    operation_id="read_document",
    dependencies=[Depends(require_tool("read_document"))],
    summary="Read a file of this conversation as text",
    description=(
        "Read one file of this conversation as text, each piece with a reference. Readable: PDF (text layer only, "
        "no OCR), DOCX, DOTX, XLSX, XLSM, CSV, TSV, PPTX, ODT, ODS, EML, HTML, JSON (shown indented), JSONL, "
        "NDJSON, TXT and MD. Give the file's path from list_files. Reading starts at part (the start if left out) "
        "and goes on until the result limit, so a long file is read in several calls: when truncated is true, "
        "call again with part set to next_part. "
        "Parts are pages ('page 3', 'pages 3-9'), slides ('slide 2'), paragraphs ('paragraph 40'), runs of table "
        "rows ('table 2, rows 30-60'; a search location such as 'table 2, row 350' opens there), ranges of up to "
        "200 sheet rows ('Sheet1!A201:F400', each line 'row: cell | cell'; a sheet's "
        "name starts at its first row) and lines ('lines 100-200'). Cite what you read by these references. "
        "pages_without_text lists pages or slides with no text layer (scans, pictures): this tool does no OCR, so "
        "tell the user those need OCR elsewhere, and never treat them as blank. Outlook .msg files cannot be read: "
        "ask the user to save them as .eml or PDF."
    ),
    response_model_exclude_none=True,
)
async def read_document(
    body: ReadDocumentRequest,
    p: PrincipalDep,
    settings: SettingsDep,
    store: StoreDep,
    audit: AuditDep,
    jobs: JobsDep,
) -> DocumentRead:
    """Read one of the user's files in the executor and answer with its text, cut to the result limit.

    The job runs in the chat folder the file lives in, whatever chat the request came from, with that one
    file as its input. The job is recorded by the file's id once it has answered and before anything of
    the answer goes out, a refusal as ``error`` with its code; neither the name, the part nor the text is
    recorded.

    Args:
        body: The path and where to start.
        p: The caller, user and chat.
        settings: The api's settings; ``result_max_kb`` caps the answer.
        store: The file store.
        audit: The audit log.
        jobs: The job queue.

    Returns:
        The parts read, the pages without text and how to go on.
    """
    rec = await run_in_threadpool(store.get, p, body.path)
    in_chat = dataclasses.replace(p, chat_id=rec.chat_id)
    args = {"path": rec.name, "part": body.part, "budget_s": max(1, settings.job_wall_s * 3 // 4)}
    _, result, _ = await jobs.submit(in_chat, "read_document", args, [rec.name])
    shown = {**result.result, "path": rec.virtual_path} if isinstance(result.result, dict) else None
    criteria = {"paged": body.part is not None}
    await run_in_threadpool(
        record_answer, audit, "read_document", in_chat, [rec.id], criteria, result, DocumentRead, shown
    )
    _payload(result, "file")
    return _fit_read(_parse(DocumentRead, shown), settings.result_max_kb)


@router.post(
    "/tools/search_files",
    operation_id="search_files",
    dependencies=[Depends(require_tool("search_files"))],
    summary="Search all files of this conversation",
    description=(
        "Search every file of this conversation for a text or, with regex true, a regular expression; case is "
        "ignored. Each hit has the file's path, the location in it (page 3, Sheet1!B7, paragraph 12, line 40) and "
        "a snippet of at most 200 characters; read the place with read_document. max_hits is 50 by default and at "
        "most 200. Files that cannot be searched are named in skipped (no text layer, encrypted, damaged) or "
        "counted in unsupported_files (images and other types): never tell the user something is not in the "
        "files without saying what could not be searched."
    ),
    response_model_exclude_none=True,
)
async def search_files(
    body: SearchFilesRequest,
    p: PrincipalDep,
    settings: SettingsDep,
    store: StoreDep,
    audit: AuditDep,
    jobs: JobsDep,
) -> SearchResult:
    """Search the chat's files in the executor and answer with the hits, cut to the result limit.

    Every file of the chat goes to the job, the newest 1000 if there are more (the protocol's limit), and
    the answer says so. The job is recorded by the ids of the files given to it, once it has answered
    and before anything of the answer goes out, a refusal as ``error`` with its code; the query, which may be
    a name, is not recorded.

    Args:
        body: The query, whether it is a regular expression, and how many hits to return.
        p: The caller, user and chat.
        settings: The api's settings; ``result_max_kb`` caps the answer.
        store: The file store.
        audit: The audit log.
        jobs: The job queue.

    Returns:
        The hits, and what could not be searched.
    """
    files = sorted(await run_in_threadpool(store.files_in_chat, p), key=lambda rec: (-rec.created_at, rec.name))
    if not files:
        return SearchResult(
            hits=[],
            truncated=False,
            files_searched=0,
            unsupported_files=0,
            hint="This conversation has no files, so there is nothing to search.",
        )
    chosen = files[:MAX_INPUT_FILES]
    max_hits = max(1, min(body.max_hits, MAX_HITS))
    args = {
        "query": body.query,
        "regex": body.regex,
        "max_hits": max_hits,
        "budget_s": max(1, settings.job_wall_s * 3 // 4),
    }
    _, result, _ = await jobs.submit(p, "search_files", args, [rec.name for rec in chosen])
    criteria = {"regex": body.regex, "max_hits": max_hits}
    await run_in_threadpool(
        record_answer,
        audit,
        "search_files",
        p,
        [rec.id for rec in chosen],
        criteria,
        result,
        SearchResult,
        result.result,
    )
    answer = _parse(SearchResult, _payload(result, "search"))
    sent = {rec.virtual_path for rec in chosen}
    answer = answer.model_copy(
        update={
            "hits": [hit for hit in answer.hits if hit.path in sent][:max_hits],
            "skipped": [skip for skip in answer.skipped or [] if skip.path in sent] or None,
        }
    )
    notes: list[str] = []
    if len(files) > len(chosen):
        notes.append(
            f"Searched the {len(chosen)} newest of {len(files)} files; the {len(files) - len(chosen)} oldest "
            "were not searched."
        )
    return _fit_search(answer, settings.result_max_kb, notes)


# --- run_python ------------------------------------------------------------------------------------------------

#: The most characters of code one call runs.
MAX_CODE: Final = 50_000


class RunPythonRequest(BaseModel):
    """Body of ``run_python``.

    Attributes:
        code: The Python to run, as a script.
    """

    code: str = Field(
        min_length=1,
        max_length=MAX_CODE,
        description=(
            "The Python code to run, as a script; at most 50000 characters. Print what you need to see, and "
            "write every result file into out/."
        ),
    )


class NotCompleted(HTTPException):
    """The code could not be completed: the job ran out of time, was stopped on a limit or left no result (422).

    Its detail is ``the code could not be completed: <reason>``, the reason a fixed text of the executor's.
    """

    def __init__(self, reason: str) -> None:
        """Say why.

        Args:
            reason: The executor's reason, cleaned.
        """
        super().__init__(status_code=422, detail=f"the code could not be completed: {reason}")


class CodeRun(BaseModel):
    """What the runner answers; checked before anything of it is sent on.

    Attributes:
        stdout: What the code printed.
        stderr: Its errors, its traceback, or the note that it timed out.
        truncated: Whether the runner cut either stream.
        hint: What the model should know, such as that only this chat's files are there.
    """

    stdout: str
    stderr: str
    truncated: bool
    hint: str | None = Field(default=None, max_length=MAX_HINT)


class PythonRun(BaseModel):
    """What ``run_python`` answers.

    Attributes:
        stdout: What the code printed.
        stderr: Its warnings and errors; a traceback ends with the error.
        files: The paths of the files it wrote into ``out/``, now in the workspace.
        truncated: Whether stdout or stderr was cut in the middle.
        job_id: The id of the run, the source of what it computed.
        hint: What to tell the user, or how to go on.
    """

    stdout: str = Field(description="What the code printed.")
    stderr: str = Field(description="Its warnings and errors; a traceback ends with the error.")
    files: list[str] = Field(description="The files it wrote into out/, now in the workspace: their paths.")
    truncated: bool = Field(description="True if stdout or stderr was cut in the middle to fit the result limit.")
    job_id: str = Field(description="The id of this run; cite it as the source of what the code computed.")
    hint: str | None = Field(default=None, max_length=MAX_HINT, description="What to tell the user, or how to go on.")


def _python_payload(result: JobResult) -> tuple[dict[str, Any], str | None]:
    """Take the runner's answer out of a job's result, or fail with what the model should be told.

    The code ran to an answer when the job is ``ok``, and also when its outputs broke the executor's limits:
    the job is then an ``error`` that still carries what the code printed, and no file is kept.

    Args:
        result: The executor's answer.

    Returns:
        The runner's answer, and the executor's note on the outputs, cleaned, if it has one; each door says in its
        own words what the note means for the files.

    Raises:
        NotCompleted: The job ran out of time, was stopped on a resource limit or left no result, as when the
            code ended its own process (422, with the executor's reason, a fixed text of its own).
        HTTPException: The runner failed with a code the api knows (its status), or in another way (500, with a
            fixed text: the runner's own may hold a path).
    """
    note = _clean(result.message) if result.message else None
    if result.status == "ok" or (result.status == "error" and "stdout" in result.result):
        return result.result, note
    if result.status == "error" and "error" in result.result:
        _payload(result, "code")  # raises, with the runner's code or the fixed text
    reason = _clean(result.message) or "the job failed"
    if result.status == "killed" and result.message == KILLED:  # the watchdog's own message names the limit
        reason += " (memory, CPU time, file size or processes)"
    raise NotCompleted(reason)


def python_args(code: str, wall_s: int) -> dict[str, Any]:
    """The runner's arguments: the code and its own time, 9/10 of the job's wall, so it can say it timed out.

    Args:
        code: The code.
        wall_s: The job's wall time in seconds.

    Returns:
        ``code`` and ``budget_s``.
    """
    return {"code": code, "budget_s": max(1, wall_s * 9 // 10)}


def read_code_run(result: JobResult) -> tuple[CodeRun, str | None]:
    """Read the answer of a ``run_python`` job, or fail with what the model should be told.

    Args:
        result: The executor's answer.

    Returns:
        What the code printed, and the executor's bare note on the outputs, if it has one. The job is ``ok``, or an
        ``error`` whose outputs broke the executor's limits, so that none of them is there.

    Raises:
        NotCompleted: The code could not be completed (:func:`_python_payload`).
        HTTPException: The runner refused or failed, or its answer is not of the shape the api sends on (500).
    """
    payload, note = _python_payload(result)
    return _parse(CodeRun, payload), note


def _kept_note(result: JobResult, note: str | None) -> str | None:
    """Say in the terminal's words what the executor's note means: which files in ``out/`` were not kept.

    Args:
        result: The executor's answer, as :func:`read_code_run` read it.
        note: The executor's bare note, or ``None``.

    Returns:
        The sentence, or ``None`` for an ``ok`` job without a note.
    """
    if result.status == "ok":
        return None if note is None else f"Not every file in out/ was kept: {note}."
    return f"No file in out/ was kept: {note or 'the outputs could not be kept'}."


def _readable(model: type[BaseModel], payload: object) -> bool:
    """Tell whether a runner's answer has the shape the api sends on.

    Args:
        model: The answer's model.
        payload: What the runner returned.

    Returns:
        Whether it validates.
    """
    try:
        model.model_validate(payload)
    except ValidationError:
        return False
    return True


def _middle(text: str, keep: int) -> str:
    """Keep the start and the end of a text, ``keep`` characters in all, around a mark of what is cut.

    Args:
        text: The text.
        keep: How many of its characters to keep.

    Returns:
        The text as it is if it is no longer, else its two ends and the mark.
    """
    if len(text) <= keep:
        return text
    front = keep // 2
    back = keep - front
    return f"{text[:front]}\n[… {len(text) - keep} characters cut …]\n{text[len(text) - back :]}"


def fit_python[M: BaseModel](answer: M, limit_kb: int, *, keep_none: bool = False) -> M:
    """Cut stdout and stderr in the middle, each to the same most characters, until the answer fits.

    What one stream does not need goes to the other. The files are never cut: they are what the code made.

    Args:
        answer: The answer: a model with ``stdout``, ``stderr`` and ``truncated``.
        limit_kb: The result limit in kilobytes.
        keep_none: Whether the answer sends its ``None`` fields too (:func:`_json_size`).

    Returns:
        The answer, within the limit as sent unless its files alone are over it.
    """
    limit = limit_kb * 1024
    if _json_size(answer, keep_none=keep_none) <= limit:
        return answer
    streams = answer.model_dump(include={"stdout", "stderr"})
    stdout, stderr = str(streams["stdout"]), str(streams["stderr"])

    def cut(keep: int) -> M:
        return answer.model_copy(
            update={"stdout": _middle(stdout, keep), "stderr": _middle(stderr, keep), "truncated": True}
        )

    best = cut(0)
    low, high = 0, max(len(stdout), len(stderr))
    while low <= high:
        keep = (low + high) // 2
        candidate = cut(keep)
        if _json_size(candidate, keep_none=keep_none) <= limit:
            best, low = candidate, keep + 1
        else:
            high = keep - 1
    return _within(best, limit, keep_none=keep_none)


@router.post(
    "/tools/run_python",
    operation_id="run_python",
    dependencies=[Depends(require_tool("run_python"))],
    summary="Run Python on the files of this conversation",
    description=(
        "Run Python code on this conversation's files and get back what it printed. The files are in the current "
        "folder, under their names as list_files gives them (also at their /workspace/<chat>/<name> paths), "
        "read-only. Write every result file directly into the out/ folder, not into a subfolder: each becomes a "
        "new file of the workspace, and files lists their paths (at most 20 files a run). Save charts as image "
        "files there, for example plt.savefig('out/chart.png'); nothing is shown inline, so show a file with "
        "display_file. Installed: pandas, numpy, scipy, matplotlib, openpyxl, xlsxwriter, python-docx, docxtpl, "
        "python-pptx, pypdf, odfpy, lxml, beautifulsoup4. To turn a data file (JSON, CSV, XLSX) into a table file, "
        "load it in code (json, pandas.json_normalize) and write it to out/ as .xlsx or .csv; do not copy its rows "
        "into write_table. There is no network and nothing can be installed. Use threads, not multiprocessing: "
        "process pools and multiprocessing do not work here. The code runs as a "
        "script with a time limit; an error or timeout is in stderr. stdout and stderr are cut in the middle when "
        "long (truncated is then true), so print summaries, not whole tables. Cite job_id as the source of figures "
        "the code computed."
    ),
    response_model_exclude_none=True,
)
async def run_python(
    body: RunPythonRequest,
    p: PrincipalDep,
    settings: SettingsDep,
    store: StoreDep,
    audit: AuditDep,
    jobs: JobsDep,
) -> PythonRun:
    """Run the model's code in the executor over the chat's files and answer with its output and new files.

    Every file of the chat goes to the job, the newest 1000 if there are more (the protocol's limit), except
    files below a folder named ``out``, which the executor never copies; the answer says what was left out. The
    run is recorded by the ids of the files it was given and the files it made, whenever it ran, before any of
    those files becomes visible and before the answer goes out; the code is never recorded.

    Args:
        body: The code.
        p: The caller, user and chat.
        settings: The api's settings; ``result_max_kb`` caps the answer, ``job_wall_s`` gives the time limit.
        store: The file store.
        audit: The audit log.
        jobs: The job queue.

    Returns:
        What the code printed, the files it made and the job's id.
    """
    files = sorted(await run_in_threadpool(store.files_in_chat, p), key=lambda rec: (-rec.created_at, rec.name))
    readable = [rec for rec in files if rec.name.split("/")[0] != OUT_DIR]
    chosen = readable[:MAX_INPUT_FILES]
    args = python_args(body.code, settings.job_wall_s)

    def decide(result: JobResult, planned: Sequence[str]) -> tuple[list[str], str, dict[str, object]]:
        # An answer the model cannot be given keeps no file: it would never learn of it.
        usable = result.status == "ok" and _readable(CodeRun, result.result)
        keep = list(planned) if usable else []
        outcome, more = judged(result, usable)
        return keep, outcome, {"outputs": len(keep), **more}

    keeper = OutputKeeper(audit, p, "run_python", [rec.id for rec in chosen], decide)
    job, result, made = await jobs.submit(p, "run_python", args, [rec.name for rec in chosen], keeper)
    run, note = read_code_run(result)
    notes: list[str | None] = [run.hint, _kept_note(result, note)]
    if len(readable) > len(chosen):
        notes.append(
            f"The code was given the {len(chosen)} newest of {len(readable)} files; the "
            f"{len(readable) - len(chosen)} oldest are not in its folder."
        )
    if len(files) > len(readable):
        hidden = len(files) - len(readable)
        notes.append(
            f"{hidden} {'file' if hidden == 1 else 'files'} in the folder out/ "
            f"{'is' if hidden == 1 else 'are'} not given to the code: out/ is where it writes."
        )
    answer = PythonRun(
        stdout=run.stdout,
        stderr=run.stderr,
        files=[rec.virtual_path for rec in made],
        truncated=run.truncated,
        job_id=job.id,
        hint=_capped(_join(*notes), MAX_HINT),
    )
    return fit_python(answer, settings.result_max_kb)
