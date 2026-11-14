"""The template tools, ``inspect_template`` and ``fill_template``, on the router of :mod:`workbench.api.tools`.

A template is a file of the chat: a Word ``.docx`` or ``.dotx`` (content controls, legacy form fields, ``{{ }}``
placeholders) or an Excel ``.xlsx`` (``{{ }}`` cells and ``{{ table:<name> }}`` anchors). The runners
(:mod:`workbench.executor.runners.templates`) open it in the executor; here the api checks what it alone knows before
a job is sent: that the template is a template, that the output's name is a plain name with the output's extension,
that each ``file`` source is a file of the template's chat and each ``job`` source a finished ``run_python`` run of
the user in that chat. The job is given the template and every file a source names, so it can check a page, sheet
or cell in its copy.

``inspect_template`` is recorded as a ``query`` of the template. ``fill_template`` is recorded as a ``modify`` once
its file is kept, by the ids of the template, of every file source and of the new file, with how many fields each
kind of source filled; never a value, a field's name or a job's content. A new file whose event cannot be written is
removed again.
"""

import dataclasses
import posixpath
from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, StringConstraints
from pydantic.json_schema import JsonDict

from workbench.api.audit import AuditLog
from workbench.api.deps import AuditDep, JobsDep, PrincipalDep, SettingsDep, StoreDep
from workbench.api.store import NotFound, Store
from workbench.api.tools import (
    MAX_HINT,
    _clean,
    _join,
    _json_size,
    _parse,
    _payload,
    _readable,
    _remove_outputs,
    _within,
    router,
)
from workbench.models import FileRecord, Principal
from workbench.paths import PathError, check_filename
from workbench.protocol import MAX_INPUT_FILES
from workbench.utils.audit_event import AuditWriteError

#: The templates, by extension, and the extension of what they are filled into.
OUTPUTS: Final = {".docx": ".docx", ".dotx": ".docx", ".xlsx": ".xlsx"}

#: Most values one fill takes; the longest field name and user's note.
MAX_VALUES: Final = 1000
MAX_NAME: Final = 255
MAX_NOTE: Final = 1000

#: The kinds of source, in the order the audit event counts them.
SOURCE_KINDS: Final = ("file", "job", "user", "assistant")

_NOT_A_TEMPLATE: Final = "templates are .docx and .dotx Word files and .xlsx workbooks"
_INTEGER: Final[JsonDict] = {"type": "integer"}
_STRING: Final[JsonDict] = {"type": "string"}
_OBJECT: Final[JsonDict] = {"type": "object"}
_ANY: Final[JsonDict] = {"type": ["string", "number", "boolean", "array", "object"]}
_PATH: Final = "The file's absolute path, for example /workspace/<chat>/report.docx, as list_files gives it."


class InspectTemplateRequest(BaseModel):
    """Body of ``inspect_template``.

    Attributes:
        path: The template's absolute path.
    """

    path: str = Field(min_length=1, max_length=1024, description=_PATH)


class TemplateField(BaseModel):
    """One field of a template.

    Attributes:
        name: What ``fill_template`` takes its value under.
        kind: ``content_control``, ``form_field``, ``placeholder`` or ``table``.
        type: What it takes.
        options: A drop-down's entries.
        location: Where it sits.
    """

    name: str = Field(max_length=MAX_NAME + 45, description="The field's name: give its value under it.")
    kind: Literal["content_control", "form_field", "placeholder", "table"] = Field(description="What it is.")
    type: Literal["text", "rich_text", "date", "dropdown", "checkbox", "table"] = Field(
        description=(
            "What it takes: text and rich_text a string or a number, date YYYY-MM-DD, dropdown one of options, "
            "checkbox true or false, table a list of rows."
        )
    )
    options: list[str] | None = Field(description="A dropdown's entries, exactly as the value must be written.")
    location: str = Field(max_length=300, description="Where it sits: body, paragraph 3; body, table 2, row 4; B7.")


class TemplateInfo(BaseModel):
    """What ``inspect_template`` answers.

    Attributes:
        path: The template's path.
        template_kind: The kind of template, or ``none``.
        fields: Its fields, each name once.
        truncated: Whether fields were left out to keep the answer within the result limit.
        hint: What to tell the user, or what is not listed.
    """

    path: str = Field(description="The template's path.")
    template_kind: Literal["content_controls", "form_fields", "placeholders", "mixed", "none"] = Field(
        description="The kind of fields it has; none: version 1 cannot fill it, and hint says what it needs."
    )
    fields: list[TemplateField] = Field(description="Its fields, in document order, each name once.")
    truncated: bool = Field(default=False, description="True if some fields are not listed.")
    hint: str | None = Field(default=None, max_length=MAX_HINT, description="What to tell the user, or how to go on.")


class _Inspected(BaseModel):
    """What the inspecting runner answers; checked before anything of it is sent on.

    Attributes:
        template_kind: The kind of template.
        fields: The fields it lists.
        total: How many fields there are.
        truncated: Whether it listed fewer.
        hint: What the model should know.
    """

    template_kind: Literal["content_controls", "form_fields", "placeholders", "mixed", "none"]
    fields: list[TemplateField]
    total: int = Field(ge=0)
    truncated: bool
    hint: str | None = Field(default=None, max_length=MAX_HINT)


class FileSource(BaseModel):
    """A value read in a file of the conversation.

    Attributes:
        kind: ``file``.
        path: The file's absolute path.
        page: The page of a PDF, or the slide of a presentation.
        sheet: The sheet of a workbook.
        cell: The cell of that sheet, such as ``B7``.
    """

    kind: Literal["file"] = Field(description="file: the value was read in a file of this conversation.")
    path: str = Field(min_length=1, max_length=1024, description=_PATH)
    page: int | None = Field(
        default=None, description="The page of a PDF or the slide of a PPTX it is on.", json_schema_extra=_INTEGER
    )
    sheet: str | None = Field(
        default=None, max_length=100, description="The sheet of an XLSX or ODS it is on.", json_schema_extra=_STRING
    )
    cell: str | None = Field(
        default=None, max_length=20, description="The cell of that sheet, such as B7.", json_schema_extra=_STRING
    )


class JobSource(BaseModel):
    """A value a ``run_python`` run computed.

    Attributes:
        kind: ``job``.
        job_id: The run's ``job_id``.
    """

    kind: Literal["job"] = Field(description="job: the value was computed by a run_python run.")
    job_id: str = Field(min_length=1, max_length=64, description="The job_id run_python answered with.")


class UserSource(BaseModel):
    """A value the user stated.

    Attributes:
        kind: ``user``.
        note: Where or how the user said it.
    """

    kind: Literal["user"] = Field(description="user: the user stated the value in the chat.")
    note: str | None = Field(
        default=None, max_length=MAX_NOTE, description="Where or how the user said it.", json_schema_extra=_STRING
    )


class AssistantSource(BaseModel):
    """A value the model wrote itself.

    Attributes:
        kind: ``assistant``.
    """

    kind: Literal["assistant"] = Field(description="assistant: you wrote the value yourself.")


Source = FileSource | JobSource | UserSource | AssistantSource


class FieldValue(BaseModel):
    """A field's value and where it comes from.

    Attributes:
        value: The value.
        source: Its source.
    """

    value: Any = Field(
        description=(
            "The value, in the field's type: a string or a number, YYYY-MM-DD, one of a dropdown's options, true or "
            "false, or a list of rows for a table."
        ),
        json_schema_extra=_ANY,
    )
    source: Annotated[Source, Field(discriminator="kind")] = Field(
        description="Where the value comes from: kind file, job, user or assistant.", json_schema_extra=_OBJECT
    )


class FillTemplateRequest(BaseModel):
    """Body of ``fill_template``.

    Attributes:
        path: The template's absolute path.
        values: The values by field name.
        output_name: The new file's name.
        include_sources: Whether the new file ends with its sources.
    """

    path: str = Field(min_length=1, max_length=1024, description=_PATH)
    values: dict[Annotated[str, StringConstraints(min_length=1, max_length=MAX_NAME)], FieldValue] = Field(
        min_length=1,
        max_length=MAX_VALUES,
        description="Each field's name, as inspect_template lists it, with its value and source; at most 1000.",
    )
    output_name: str | None = Field(
        default=None,
        max_length=200,
        description="The new file's name, ending in .docx (for a .docx or .dotx) or .xlsx; <name>-filled by default.",
        json_schema_extra=_STRING,
    )
    include_sources: bool = Field(default=True, description="End the new file with a Sources table or sheet.")


class FilledTemplate(BaseModel):
    """What ``fill_template`` answers.

    Attributes:
        path: The new file's path.
        filled: How many fields were filled.
        unfilled: The fields left without a value.
        warnings: What the model should tell the user.
    """

    path: str = Field(description="The new file's path; show it with display_file.")
    filled: int = Field(description="How many fields were filled.")
    unfilled: list[str] = Field(description="The fields given no value; they are empty, or keep their placeholder.")
    warnings: list[str] = Field(description="What to tell the user about the fill.")


class _Filled(BaseModel):
    """What the filling runner answers; checked before anything of it is sent on.

    Attributes:
        filled: The names of the fields filled.
        unfilled: The names of the fields left unfilled.
        warnings: What the model should know.
    """

    filled: list[str]
    unfilled: list[str]
    warnings: list[Annotated[str, StringConstraints(max_length=MAX_HINT)]]


def _output_extension(name: str) -> str:
    """Tell what a template is filled into, by its name.

    Args:
        name: The template's name below its chat folder.

    Returns:
        ``.docx`` or ``.xlsx``.

    Raises:
        HTTPException: It is not a template (422).
    """
    ext = posixpath.splitext(name)[1].lower()
    filled = OUTPUTS.get(ext)
    if filled is None:
        shown = ext or "a file without an extension"
        raise HTTPException(
            status_code=422, detail=_clean(f"unsupported_template: {shown} cannot be filled here; {_NOT_A_TEMPLATE}")
        )
    return filled


def _output_name(template: str, ext: str, wanted: str | None) -> str:
    """Name the new file: as asked, or after the template.

    Args:
        template: The template's name below its chat folder.
        ext: The new file's extension.
        wanted: The name the model asked for, or ``None``.

    Returns:
        A plain file name ending in ``ext``; ``<stem>-filled<ext>`` by default.

    Raises:
        HTTPException: The name asked for is not a plain file name, or does not end in ``ext`` (422).
    """
    if wanted is None:
        stem = posixpath.splitext(posixpath.basename(template))[0][: 200 - len(f"-filled{ext}")]
        return check_filename(f"{stem}-filled{ext}")
    try:
        name = check_filename(wanted)
    except PathError:
        name = ""
    if not name.lower().endswith(ext) or len(name) <= len(ext):
        raise HTTPException(status_code=422, detail=f"output_name must be a plain file name ending in {ext}")
    return name


def _refused(name: str, why: str) -> HTTPException:
    """Refuse a value's source, naming the field.

    Args:
        name: The field.
        why: What is wrong.

    Returns:
        The 422 to raise.
    """
    return HTTPException(status_code=422, detail=_clean(f"invalid_source: {name}: {why}"))


def _sources(
    store: Store, p: Principal, template: FileRecord, values: Mapping[str, FieldValue]
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Check the sources the api can check, and write the values as the runner takes them.

    Args:
        store: The file store.
        p: The caller and user.
        template: The template's record.
        values: The values by field name.

    Returns:
        The values for the runner (a file source by its path below the chat folder, with its virtual path for the
        appendix), the job's input files (the template first) and the ids of the source files.

    Raises:
        HTTPException: A file source is not a file of the template's chat, or a job source is not a finished
            ``run_python`` run of the user in that chat (422, naming the field and never whether a job exists for
            someone else); or the sources name more files than a job takes.
    """
    sent: dict[str, Any] = {}
    inputs: dict[str, None] = {template.name: None}
    ids: dict[str, None] = {}
    for name, entry in values.items():
        source = entry.source
        if isinstance(source, FileSource):
            try:
                rec = store.get(p, source.path)
            except (NotFound, PathError):
                rec = None
            if rec is None or rec.chat_id != template.chat_id:
                raise _refused(name, f"{source.path} is not a file of this conversation")
            inputs[rec.name] = None
            ids[rec.id] = None
            spec: dict[str, Any] = {"kind": "file", "path": rec.name, "virtual": rec.virtual_path}
            spec.update(
                (key, value)
                for key, value in (("page", source.page), ("sheet", source.sheet), ("cell", source.cell))
                if value is not None
            )
        elif isinstance(source, JobSource):
            try:
                job = store.get_job(p, source.job_id)
            except NotFound:
                job = None
            if job is None or (job.tool, job.chat_id, job.status) != ("run_python", template.chat_id, "ok"):
                raise _refused(
                    name, f"{source.job_id} is not the job_id of a run_python run of this conversation that finished"
                )
            spec = {"kind": "job", "job_id": job.id}
        elif isinstance(source, UserSource):
            spec = {"kind": "user"} if source.note is None else {"kind": "user", "note": source.note}
        else:
            spec = {"kind": "assistant"}
        sent[name] = {"value": entry.value, "source": spec}
    if len(inputs) > MAX_INPUT_FILES:
        raise HTTPException(status_code=422, detail=f"the sources name more than {MAX_INPUT_FILES - 1} files")
    return sent, list(inputs), list(ids)


def _record_fill(
    audit: AuditLog, store: Store, p: Principal, file_ids: list[str], counts: dict[str, int], output: FileRecord
) -> None:
    """Record a fill by the ids of its files and how many fields each kind of source filled.

    As with an upload, a file whose making cannot be recorded does not stay: the new file is removed again.

    Args:
        audit: The audit log.
        store: The file store.
        p: The caller, user and the template's chat.
        file_ids: The template's, the source files' and the new file's ids.
        counts: The fields filled, by kind of source.
        output: The new file.

    Raises:
        AuditWriteError: The event could not be written.
    """
    try:
        audit.record("fill_template", p, file_ids=file_ids, criteria={"filled_by_source": counts})
    except AuditWriteError:
        _remove_outputs(store, [output])  # the audit failure is the answer
        raise


def _fit_inspection(answer: TemplateInfo, total: int, limit_kb: int) -> TemplateInfo:
    """Keep as many fields as fit within the result limit, and say how many of how many are listed.

    Args:
        answer: The answer, with the fields the runner listed.
        total: How many fields the template has.
        limit_kb: The result limit in kilobytes.

    Returns:
        The answer within the limit as sent.
    """
    limit = limit_kb * 1024

    def told(kept: list[TemplateField]) -> TemplateInfo:
        if len(kept) >= total:
            return answer.model_copy(update={"fields": kept})
        note = (
            f"Only {len(kept)} of {total} fields are listed: the answer is cut at the result limit ({limit_kb} KB). "
            "Plan with these, and ask the user about the rest."
        )
        return answer.model_copy(update={"fields": kept, "truncated": True, "hint": _join(answer.hint, note)})

    whole = told(answer.fields)
    if _json_size(whole) <= limit:
        return whole
    kept: list[TemplateField] = []
    for field in answer.fields:
        if _json_size(told([*kept, field])) > limit:
            break
        kept.append(field)
    return _within(told(kept), limit)


def _fit_fill(answer: FilledTemplate, limit_kb: int) -> FilledTemplate:
    """Cut the lists of a fill's answer from their ends until it fits the result limit.

    Args:
        answer: The answer.
        limit_kb: The result limit in kilobytes.

    Returns:
        The answer within the limit as sent, with a warning that says what is not listed.
    """
    limit = limit_kb * 1024
    if _json_size(answer) <= limit:
        return answer
    warnings, unfilled = list(answer.warnings), list(answer.unfilled)

    def cut() -> FilledTemplate:
        left = len(answer.warnings) - len(warnings), len(answer.unfilled) - len(unfilled)
        note = (
            f"The answer is cut at the result limit ({limit_kb} KB): {left[1]} unfilled fields and {left[0]} warnings "
            "are not listed."
        )
        return answer.model_copy(update={"warnings": [*warnings, note], "unfilled": unfilled})

    while _json_size(cut()) > limit and warnings:
        warnings.pop()
    while _json_size(cut()) > limit and unfilled:
        unfilled.pop()
    return cut()


@router.post(
    "/tools/inspect_template",
    operation_id="inspect_template",
    summary="List the fields of a report template",
    description=(
        "List the fields of a report template in this conversation, so you can plan the fill with the user before "
        "you call fill_template. Templates are Word .docx and .dotx files with content controls or legacy form "
        "fields, Word .docx files with {{ field }} placeholders (loops and conditions too), and Excel .xlsx files "
        "with {{ field }} cells and {{ table:name }} cells where a table grows downwards. Each field has its name "
        "(give its value under it), kind, type (text, rich_text, date, dropdown with its options, checkbox, table) "
        "and location. template_kind none means version 1 cannot fill the file: tell the user what it needs, as "
        "hint says. A very long list is cut: truncated is then true."
    ),
    response_model_exclude_none=True,
)
async def inspect_template(
    body: InspectTemplateRequest,
    p: PrincipalDep,
    settings: SettingsDep,
    store: StoreDep,
    audit: AuditDep,
    jobs: JobsDep,
) -> TemplateInfo:
    """List a template's fields in the executor and answer with them, cut to the result limit.

    The job runs in the chat folder the template lives in, with that one file as its input. The use is recorded by
    the template's id once the job has answered and before the fields go out.

    Args:
        body: The template's path.
        p: The caller, user and chat.
        settings: The api's settings; ``result_max_kb`` caps the answer.
        store: The file store.
        audit: The audit log.
        jobs: The job queue.

    Returns:
        The kind of template and its fields.
    """
    rec = await run_in_threadpool(store.get, p, body.path)
    _output_extension(rec.name)
    in_chat = dataclasses.replace(p, chat_id=rec.chat_id)
    args = {"path": rec.name, "budget_s": max(1, settings.job_wall_s * 3 // 4)}
    _, result, _ = await jobs.submit(in_chat, "inspect_template", args, [rec.name])
    found = _parse(_Inspected, _payload(result, "file"))
    await run_in_threadpool(audit.record, "inspect_template", in_chat, file_ids=[rec.id])
    answer = TemplateInfo(
        path=rec.virtual_path,
        template_kind=found.template_kind,
        fields=found.fields,
        truncated=found.truncated,
        hint=found.hint,
    )
    return _fit_inspection(answer, max(found.total, len(found.fields)), settings.result_max_kb)


@router.post(
    "/tools/fill_template",
    operation_id="fill_template",
    summary="Fill a report template into a new file",
    description=(
        "Fill a report template of this conversation into a new file; the template itself is not changed, and a "
        ".dotx becomes a .docx. Call inspect_template first and agree the values with the user. values maps each "
        "field's name to its value and its source, and every value needs a source: kind file (the path of a file of "
        "this conversation as list_files gives it, with page for a PDF page or PPTX slide, or sheet and cell such as "
        "B7 for a workbook), job (the job_id of the run_python run that computed it: every computed figure cites "
        "its job_id), user (the user said it; note says where) or assistant (you wrote it). Types: text and "
        "rich_text take a string or a number, date YYYY-MM-DD, dropdown one of its options, checkbox true or false, "
        "table a list of rows (each a list of values in a workbook). Fields without a value are listed in unfilled. "
        "The new file ends with a Sources table (a Sources sheet in a workbook) naming each value's source; "
        "include_sources false leaves it out. Show the result with display_file."
    ),
    response_model_exclude_none=True,
)
async def fill_template(
    body: FillTemplateRequest,
    p: PrincipalDep,
    settings: SettingsDep,
    store: StoreDep,
    audit: AuditDep,
    jobs: JobsDep,
) -> FilledTemplate:
    """Fill a template in the executor into a new file of its chat, and answer with the file and what was filled.

    Args:
        body: The template, the values with their sources, the new file's name and whether to add the sources.
        p: The caller, user and chat.
        settings: The api's settings; ``result_max_kb`` caps the answer.
        store: The file store.
        audit: The audit log.
        jobs: The job queue.

    Returns:
        The new file's path, how many fields were filled, which were not, and warnings.

    Raises:
        HTTPException: The template, the output name or a source is refused (422); the job's answer cannot be read,
            or its file was not kept (500).
    """
    rec = await run_in_threadpool(store.get, p, body.path)
    ext = _output_extension(rec.name)
    output = _output_name(rec.name, ext, body.output_name)
    in_chat = dataclasses.replace(p, chat_id=rec.chat_id)
    values, inputs, source_ids = await run_in_threadpool(_sources, store, p, rec, body.values)
    args = {
        "path": rec.name,
        "values": values,
        "output_name": output,
        "include_sources": body.include_sources,
        "budget_s": max(1, settings.job_wall_s * 3 // 4),
    }
    _, result, made = await jobs.submit(in_chat, "fill_template", args, inputs)
    if result.status == "ok" and (not _readable(_Filled, result.result) or len(made) != 1):
        # Nothing the model can be told of may stay.
        await run_in_threadpool(_remove_outputs, store, made)
        if _readable(_Filled, result.result):
            note = _clean(result.message) or "the job made no file"
            raise HTTPException(status_code=500, detail=f"the filled file could not be kept: {note}")
    done = _parse(_Filled, _payload(result, "file"))
    (new,) = made
    filled = [name for name in dict.fromkeys(done.filled) if name in body.values]
    counts = dict.fromkeys(SOURCE_KINDS, 0)
    for name in filled:
        counts[body.values[name].source.kind] += 1
    file_ids = list(dict.fromkeys([rec.id, *source_ids, new.id]))
    await run_in_threadpool(_record_fill, audit, store, in_chat, file_ids, counts, new)
    answer = FilledTemplate(path=new.virtual_path, filled=len(filled), unfilled=done.unfilled, warnings=done.warnings)
    return _fit_fill(answer, settings.result_max_kb)
