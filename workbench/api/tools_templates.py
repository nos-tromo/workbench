"""The template tools, ``inspect_template`` and ``fill_template``, two rows of :mod:`workbench.api.tool_table`.

A template is a file of the chat: a Word ``.docx`` or ``.dotx`` (content controls, legacy form fields, ``{{ }}``
placeholders) or an Excel ``.xlsx`` (``{{ }}`` cells and ``{{ table:<name> }}`` anchors). A PowerPoint ``.pptx`` or
``.potx`` is a slide master: ``inspect_template`` lists its layouts, and ``fill_template`` refuses it, since a deck is
built in it with ``write_presentation``. The runners (:mod:`workbench.executor.runners.templates`) open the file in
the executor; here the api checks what it alone knows before a job is sent: that the template is a template, that the
output's name is a plain name with the output's extension, that each ``file`` source is a file of the template's chat
and each ``job`` source a finished ``run_python`` run of the user in that chat. The job is given the template and
every file a source names, so it can check a page, sheet or cell in its copy.

``inspect_template`` is recorded as a ``query`` of the template. ``fill_template`` is recorded as a ``modify`` before
its file becomes visible, by the ids of the template, of every file source and of the new file, with how many fields
each kind of source filled; never a value, a field's name or a job's content. A new file whose event cannot be written
is never kept. Both record a job that refused, by the refusal's code: a template's line or a drop-down's entries can
be in its message.
"""

import posixpath
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal, Self

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, BeforeValidator, Field, StringConstraints, model_validator
from pydantic.json_schema import JsonDict

from workbench.api.audit import AuditLog
from workbench.api.store import NotFound, Store
from workbench.api.tools import (
    MAX_HINT,
    OutputKeeper,
    _capped,
    _clean,
    _join,
    _json_size,
    _parse,
    _payload,
    _readable,
    _within,
    judged,
    record_answer,
)
from workbench.config import Settings
from workbench.decks import MAX_LAYOUT_NAME
from workbench.models import FileRecord, Principal
from workbench.paths import MAX_FILENAME, MAX_FILENAME_BYTES, PathError, check_filename
from workbench.protocol import MAX_INPUT_FILES, JobResult

if TYPE_CHECKING:  # tools_mcp imports this module, through tool_table
    from workbench.api.tools_mcp import CallJobs

#: The templates a fill takes, by extension, and the extension of what they are filled into.
TEMPLATE_FILES: Final = {".docx": ".docx", ".dotx": ".docx", ".xlsx": ".xlsx"}

#: The slide masters, which ``inspect_template`` lists the layouts of and a deck is built in.
SLIDE_MASTERS: Final = frozenset({".pptx", ".potx"})

#: Most values one fill takes; the longest field name and user's note.
MAX_VALUES: Final = 1000
MAX_NAME: Final = 255
MAX_NOTE: Final = 1000

#: The kinds of source, in the order the audit event counts them.
SOURCE_KINDS: Final = ("file", "job", "user", "assistant")

_NOT_A_TEMPLATE: Final = "templates are .docx and .dotx Word files and .xlsx workbooks"
_NOT_INSPECTABLE: Final = "templates are .docx and .dotx Word files, .xlsx workbooks and .pptx and .potx slide masters"
_A_MASTER: Final = "unsupported_template: a slide master is filled with write_presentation"
_INTEGER: Final[JsonDict] = {"type": "integer"}
_STRING: Final[JsonDict] = {"type": "string"}
_OBJECT: Final[JsonDict] = {"type": "object"}
_ANY: Final[JsonDict] = {"type": ["string", "number", "boolean", "array", "object"]}
_PATH: Final = "The file's absolute path, for example /workspace/<chat>/report.docx, as list_files gives it."

#: The longest place an answer names.
MAX_LOCATION: Final = 300


def _cut_to(limit: int) -> Callable[[object], object]:
    """Make a validator that cuts a text to its bound instead of refusing it.

    A runner's answer that is too long in one place must never cost the model the rest of it, or a file that was
    filled: this is the api's backstop behind the runner's own bounds.

    Args:
        limit: The most characters.

    Returns:
        The validator; anything but a string passes as it is.
    """

    def cut(value: object) -> object:
        return value[:limit] if isinstance(value, str) else value

    return cut


def _nameable(fields: object) -> object:
    """Leave out the fields a value could never be given for: their names are longer than a value's name may be.

    Args:
        fields: The runner's fields.

    Returns:
        The others; anything but a list passes as it is.
    """
    if not isinstance(fields, list):
        return fields
    return [
        f for f in fields if not (isinstance(f, dict) and isinstance(f.get("name"), str) and len(f["name"]) > MAX_NAME)
    ]


class InspectTemplateRequest(BaseModel):
    """Body of ``inspect_template``.

    Attributes:
        path: The template's absolute path, or ``None`` for the plain slide master.
    """

    path: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description=f"{_PATH} Leave it out for the plain slide master.",
        json_schema_extra=_STRING,
    )


class TemplateField(BaseModel):
    """One field of a template.

    Attributes:
        name: What ``fill_template`` takes its value under.
        kind: ``content_control``, ``form_field``, ``placeholder`` or ``table``.
        type: What it takes.
        options: A drop-down's entries.
        location: Where it sits.
    """

    name: str = Field(max_length=MAX_NAME, description="The field's name: give its value under it.")
    kind: Literal["content_control", "form_field", "placeholder", "table"] = Field(description="What it is.")
    type: Literal["text", "rich_text", "date", "dropdown", "checkbox", "table"] = Field(
        description=(
            "What it takes: text and rich_text a string or a number, date YYYY-MM-DD, dropdown one of options, "
            "checkbox true or false, table a list of rows."
        )
    )
    options: list[str] | None = Field(description="A dropdown's entries, exactly as the value must be written.")
    location: Annotated[str, BeforeValidator(_cut_to(MAX_LOCATION))] = Field(
        max_length=MAX_LOCATION, description="Where it sits: body, paragraph 3; body, table 2, row 4; B7."
    )


class LayoutPlace(BaseModel):
    """One place of a slide layout, which one block of a slide fills.

    Attributes:
        role: What it takes: text, content (text, a table, a chart or a picture), picture, table or chart.
        lines: About how many lines fit.
        chars: About how many characters fit on a line.
    """

    role: Literal["text", "content", "picture", "table", "chart"] = Field(
        description="What it takes: text, content (text, a table, a chart or a picture), picture, table or chart."
    )
    lines: int = Field(description="About how many lines fit at the font size the place inherits.")
    chars: int = Field(description="About how many characters fit on a line.")


class SlideLayout(BaseModel):
    """One layout of a slide master.

    Attributes:
        number: Its number, from 1, over all the master's layouts.
        name: Its name, as the file has it.
        title: Whether it has a title.
        subtitle: Whether it has a subtitle.
        places: Its places in reading order: top, then left.
        footer: Whether it has a date, footer or slide number.
    """

    number: int = Field(description="Its number, from 1: a slide names its layout by it.")
    name: Annotated[str, BeforeValidator(_cut_to(MAX_LAYOUT_NAME))] = Field(
        max_length=MAX_LAYOUT_NAME, description="Its name, as the file has it; it may be empty or shared."
    )
    title: bool = Field(description="Whether it has a title.")
    subtitle: bool = Field(description="Whether it has a subtitle.")
    places: list[LayoutPlace] = Field(description="Its places in reading order, top then left; a block fills each.")
    footer: bool = Field(description="Whether it has a date, a footer or a slide number.")


class TemplateInfo(BaseModel):
    """What ``inspect_template`` answers.

    Attributes:
        path: The template's path; ``None`` for the plain slide master.
        template_kind: The kind of template, ``none``, or ``slide_master``.
        fields: Its fields, each name once; empty for a slide master.
        layouts: A slide master's layouts; ``None`` for any other template.
        slides: The example slides a slide master has; ``None`` for any other template.
        truncated: Whether fields or layouts were left out to keep the answer within the result limit.
        hint: What to tell the user, or what is not listed.
    """

    path: str | None = Field(description="The template's path; null for the plain slide master.")
    template_kind: Literal["content_controls", "form_fields", "placeholders", "mixed", "none", "slide_master"] = Field(
        description=(
            "The kind of fields it has; none: version 1 cannot fill it, and hint says what it needs; slide_master: a "
            ".pptx or .potx, whose layouts are listed in layouts."
        )
    )
    fields: list[TemplateField] = Field(
        description="Its fields, in document order, each name once; empty for a slide master."
    )
    layouts: list[SlideLayout] | None = Field(
        default=None, description="A slide master's layouts, numbered from 1; null for any other template."
    )
    slides: int | None = Field(
        default=None,
        description="The example slides the file already has; a deck leaves them out. Null for any other template.",
    )
    truncated: bool = Field(default=False, description="True if some fields or layouts are not listed.")
    hint: str | None = Field(default=None, max_length=MAX_HINT, description="What to tell the user, or how to go on.")


class _Inspected(BaseModel):
    """What the inspecting runner answers, of a document or of a slide master; checked before anything is sent on.

    Attributes:
        template_kind: The kind of template.
        fields: The fields it lists; a slide master's answer has none.
        layouts: The layouts of a slide master it lists; any other answer has none.
        slides: The example slides of a slide master.
        total: How many fields, or for a slide master layouts, there are.
        truncated: Whether it listed fewer.
        hint: What the model should know.
    """

    template_kind: Literal["content_controls", "form_fields", "placeholders", "mixed", "none", "slide_master"]
    fields: Annotated[list[TemplateField], BeforeValidator(_nameable)] = Field(default_factory=list)
    layouts: list[SlideLayout] | None = None
    slides: int | None = Field(default=None, ge=0)
    total: int = Field(ge=0)
    truncated: bool
    hint: Annotated[str | None, BeforeValidator(_cut_to(MAX_HINT))] = Field(default=None, max_length=MAX_HINT)

    @model_validator(mode="after")
    def _of_its_kind(self) -> Self:
        """Check that the answer holds what its kind lists: layouts and slides for a slide master, and no others.

        Returns:
            The answer.

        Raises:
            ValueError: A slide master without layouts or slides, or another kind with either.
        """
        given = (self.layouts is not None, self.slides is not None)
        if given != ((True, True) if self.template_kind == "slide_master" else (False, False)):
            raise ValueError("layouts and slides belong to a slide master alone")
        return self


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
        unfilled: The fields left without a value: given none, or one that could not be written where they are.
        warnings: What the model should tell the user.
    """

    path: str = Field(description="The new file's path; the user sees it as a card in the chat.")
    filled: int = Field(description="How many fields were filled.")
    unfilled: list[str] = Field(
        description=(
            "The fields left without a value: given none (they are empty, or keep their placeholder), or one that "
            "could not be written where the field is (a warning says why)."
        )
    )
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
    warnings: list[Annotated[str, BeforeValidator(_cut_to(MAX_HINT)), StringConstraints(max_length=MAX_HINT)]]


def _output_extension(name: str, *, inspecting: bool = False) -> str:
    """Tell what a template is filled into, by its name.

    Args:
        name: The template's name below its chat folder.
        inspecting: Whether the file is inspected, not filled: a slide master is a template then.

    Returns:
        ``.docx`` or ``.xlsx``; for a slide master being inspected, its own extension.

    Raises:
        HTTPException: It is not a template, or it is a slide master and is to be filled (422).
    """
    ext = posixpath.splitext(name)[1].lower()
    if ext in SLIDE_MASTERS:
        if inspecting:
            return ext
        raise HTTPException(status_code=422, detail=_A_MASTER)
    filled = TEMPLATE_FILES.get(ext)
    if filled is None:
        shown = ext or "a file without an extension"
        done, taken = ("inspected", _NOT_INSPECTABLE) if inspecting else ("filled", _NOT_A_TEMPLATE)
        raise HTTPException(
            status_code=422, detail=_clean(f"unsupported_template: {shown} cannot be {done} here; {taken}")
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
        suffix = f"-filled{ext}"
        stem = posixpath.splitext(posixpath.basename(template))[0][: MAX_FILENAME - len(suffix)]
        stem = stem.encode()[: MAX_FILENAME_BYTES - len(suffix.encode())].decode(errors="ignore")  # whole characters
        return check_filename(f"{stem}{suffix}")
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


def _source_spec(
    store: Store, p: Principal, chat_id: str, source: Source, label: str
) -> tuple[dict[str, Any], FileRecord | None]:
    """Check one source as far as the api can, and write it as the runner takes it.

    Args:
        store: The file store.
        p: The caller and user.
        chat_id: The chat the job runs in: a job source must be a run of it.
        source: The source.
        label: What the refusal names: a field, or ``slide N, source M``.

    Returns:
        The source for the runner (a file source by its path below the chat folder, with its virtual path for the
        notes or the appendix), and the file a file source names, or ``None``.

    Raises:
        HTTPException: A file source is not a file of the request's chat, or a job source is not a finished
            ``run_python`` run of the user in that chat (422, naming ``label`` and never whether a job exists for
            someone else).
    """
    if isinstance(source, FileSource):
        try:
            rec = store.get(p, source.path)
        except (NotFound, PathError):
            rec = None
        if rec is None:
            raise _refused(label, f"{source.path} is not a file of this conversation")
        spec: dict[str, Any] = {"kind": "file", "path": rec.name, "virtual": rec.virtual_path}
        spec.update(
            (key, value)
            for key, value in (("page", source.page), ("sheet", source.sheet), ("cell", source.cell))
            if value is not None
        )
        return spec, rec
    if isinstance(source, JobSource):
        try:
            job = store.get_job(p, source.job_id)
        except NotFound:
            job = None
        if job is None or (job.tool, job.chat_id, job.status) != ("run_python", chat_id, "ok"):
            raise _refused(
                label, f"{source.job_id} is not the job_id of a run_python run of this conversation that finished"
            )
        return {"kind": "job", "job_id": job.id}, None
    if isinstance(source, UserSource):
        return ({"kind": "user"} if source.note is None else {"kind": "user", "note": source.note}), None
    return {"kind": "assistant"}, None


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
        HTTPException: A source is refused (:func:`_source_spec`), naming the field; or the sources name more files
            than a job takes.
    """
    sent: dict[str, Any] = {}
    inputs: dict[str, None] = {template.name: None}
    ids: dict[str, None] = {}
    for name, entry in values.items():
        spec, rec = _source_spec(store, p, template.chat_id, entry.source, name)
        if rec is not None:
            inputs[rec.name] = None
            ids[rec.id] = None
        sent[name] = {"value": entry.value, "source": spec}
    if len(inputs) > MAX_INPUT_FILES:
        raise HTTPException(status_code=422, detail=f"the sources name more than {MAX_INPUT_FILES - 1} files")
    return sent, list(inputs), list(ids)


def _fit_inspection(answer: TemplateInfo, total: int, limit_kb: int) -> TemplateInfo:
    """Keep as many fields, or a slide master's layouts, as fit within the result limit, and say how many of how many.

    Args:
        answer: The answer, with the fields or layouts the runner listed.
        total: How many fields, or layouts, the template has.
        limit_kb: The result limit in kilobytes.

    Returns:
        The answer within the limit as sent.
    """
    limit = limit_kb * 1024
    key = "fields" if answer.layouts is None else "layouts"
    listed: list[Any] = answer.fields if answer.layouts is None else answer.layouts

    def told(kept: list[Any]) -> TemplateInfo:
        if len(kept) >= total:
            return answer.model_copy(update={key: kept})
        note = (
            f"Only {len(kept)} of {total} {key} are listed: the answer is cut at the result limit ({limit_kb} KB). "
            "Plan with these, and ask the user about the rest."
        )
        hint = _capped(_join(answer.hint, note), MAX_HINT)
        return answer.model_copy(update={key: kept, "truncated": True, "hint": hint})

    whole = told(listed)
    if _json_size(whole) <= limit:
        return whole
    kept: list[Any] = []
    for item in listed:
        if _json_size(told([*kept, item])) > limit:
            break
        kept.append(item)
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


async def inspect_template(
    body: InspectTemplateRequest,
    p: Principal,
    settings: Settings,
    store: Store,
    audit: AuditLog,
    jobs: "CallJobs",
) -> TemplateInfo:
    """List a template's fields, or a slide master's layouts, in the executor and answer, cut to the result limit.

    The job runs in the chat folder the template lives in, with that one file as its input; without a path it lists
    the plain master's layouts, a job with no input. The job is recorded by the template's id, or by none, once it has
    answered and before anything of the answer goes out, a refusal as ``error`` with its code; never a layout's name,
    which is the template's content.

    Args:
        body: The template's path, or none for the plain master.
        p: The caller, user and chat.
        settings: The api's settings; ``result_max_kb`` caps the answer.
        store: The file store.
        audit: The audit log.
        jobs: The queue, within the call's limits.

    Returns:
        The kind of template and its fields, or a slide master's layouts and example slides.
    """
    budget: dict[str, Any] = {"budget_s": max(1, settings.job_wall_s * 3 // 4)}
    if body.path is None:
        rec = None
        _, result, _ = await jobs.submit(p, "inspect_template", budget, [])
    else:
        rec = await run_in_threadpool(store.get, p, body.path)
        _output_extension(rec.name, inspecting=True)
        _, result, _ = await jobs.submit(p, "inspect_template", {"path": rec.name, **budget}, [rec.name])
    used = [] if rec is None else [rec.id]
    await run_in_threadpool(record_answer, audit, "inspect_template", p, used, {}, result, _Inspected, result.result)
    found = _parse(_Inspected, _payload(result, "file"))
    answer = TemplateInfo(
        path=None if rec is None else rec.virtual_path,
        template_kind=found.template_kind,
        fields=found.fields,
        layouts=found.layouts,
        slides=found.slides,
        truncated=found.truncated,
        hint=found.hint,
    )
    listed = found.fields if found.layouts is None else found.layouts
    return _fit_inspection(answer, max(found.total, len(listed)), settings.result_max_kb)


async def fill_template(
    body: FillTemplateRequest,
    p: Principal,
    settings: Settings,
    store: Store,
    audit: AuditLog,
    jobs: "CallJobs",
) -> FilledTemplate:
    """Fill a template in the executor into a new file of its chat, and answer with the file and what was filled.

    Args:
        body: The template, the values with their sources, the new file's name and whether to add the sources.
        p: The caller, user and chat.
        settings: The api's settings; ``result_max_kb`` caps the answer.
        store: The file store.
        audit: The audit log.
        jobs: The queue, within the call's limits.

    Returns:
        The new file's path, how many fields were filled, which were not, and warnings.

    Raises:
        HTTPException: The template, the output name or a source is refused (422); the job's answer cannot be read,
            or its file was not kept (500).
    """
    rec = await run_in_threadpool(store.get, p, body.path)
    ext = _output_extension(rec.name)
    output = _output_name(rec.name, ext, body.output_name)
    values, inputs, source_ids = await run_in_threadpool(_sources, store, p, rec, body.values)
    args = {
        "path": rec.name,
        "values": values,
        "output_name": output,
        "include_sources": body.include_sources,
        "budget_s": max(1, settings.job_wall_s * 3 // 4),
    }
    used = list(dict.fromkeys([rec.id, *source_ids]))

    def counted(done: _Filled) -> dict[str, int]:
        counts = dict.fromkeys(SOURCE_KINDS, 0)
        for name in dict.fromkeys(done.filled):
            if name in body.values:
                counts[body.values[name].source.kind] += 1
        return counts

    def decide(result: JobResult, planned: Sequence[str]) -> tuple[list[str], str, dict[str, object]]:
        # One new file, of an answer the model can be given; otherwise nothing is kept, the model never learning of it.
        if result.status == "ok" and _readable(_Filled, result.result) and len(planned) == 1:
            return list(planned), "ok", {"filled_by_source": counted(_Filled.model_validate(result.result))}
        outcome, more = judged(result, False)
        return [], outcome, {"outputs": 0, **more}

    keeper = OutputKeeper(audit, p, "fill_template", used, decide)
    _, result, made = await jobs.submit(p, "fill_template", args, inputs, keeper)
    done = _parse(_Filled, _payload(result, "file"))
    if len(made) != 1:
        note = _clean(result.message) or "the job made no file"
        raise HTTPException(status_code=500, detail=f"the filled file could not be kept: {note}")
    (new,) = made
    filled = sum(counted(done).values())
    answer = FilledTemplate(path=new.virtual_path, filled=filled, unfilled=done.unfilled, warnings=done.warnings)
    return _fit_fill(answer, settings.result_max_kb)
