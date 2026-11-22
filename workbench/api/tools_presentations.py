"""The presentation tool, ``write_presentation``, a row of :mod:`workbench.api.tool_table`.

A deck is built in a slide master of the chat, a ``.pptx`` or ``.potx``, or without one in the plain master the
executor makes, into one new ``.pptx``: each slide on a layout of the master, its blocks in the layout's places, its
sources in its speaker notes. The runner (:mod:`workbench.executor.runners.presentations`) builds it in the executor;
here the api checks what it can before a job is sent, so a refusal names its place and costs no job. It first reads a
block in a shape models write by mistake as the block they meant (:func:`_read_as_meant`), then checks that the master
is a slide master, the new file's name, the deck's bounds (:func:`workbench.decks.deck_problem`, which the runner
checks again), that each picture is a PNG, JPEG or GIF file of the chat within its bytes, and each source as
``fill_template`` checks one. The job is given the master, if there is one, every picture and every file a source
names, a picture by its path below the chat folder and its virtual path.

It is recorded as a ``modify`` before the new file becomes visible, by the ids of the master (if there is one), the
pictures, the source files and the new file, with how many slides, tables, charts and pictures the deck has and how
many sources of each kind; never a slide's text, a layout's name or a file's name. A deck whose event cannot be written
is never kept, and a job that refused is recorded by the refusal's code alone: a layout's name can be in its message.
"""

import json
import posixpath
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Annotated, Any, Final, Literal

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    PlainValidator,
    StrictFloat,
    StrictInt,
    StringConstraints,
    Tag,
    model_validator,
)
from pydantic.json_schema import JsonDict, JsonValue
from pydantic_core import PydanticCustomError

from workbench.api.audit import AuditLog
from workbench.api.store import NotFound, Store
from workbench.api.tools import MAX_HINT, OutputKeeper, _clean, _json_size, _parse, _payload, _readable, judged
from workbench.api.tools_output import MAX_NAME, _output_name
from workbench.api.tools_templates import SLIDE_MASTERS, SOURCE_KINDS, Source, _cut_to, _source_spec
from workbench.config import Settings
from workbench.decks import IMAGE_TYPES, MAX_IMAGE_BYTES, MAX_IMAGES_BYTES, BlockKind, block_kind, deck_problem
from workbench.models import FileRecord, Principal
from workbench.paths import PathError
from workbench.protocol import MAX_INPUT_FILES, JobResult, encodable

if TYPE_CHECKING:  # tools_mcp imports this module, through tool_table
    from workbench.api.tools_mcp import CallJobs

_NOT_A_MASTER: Final = "a deck is built in a .pptx or .potx slide master"
_MB: Final = 1024 * 1024


def _terse(schema: JsonDict) -> None:
    """Keep a request model's schema short, as Ghostwriter sends it on every round: its fields' descriptions alone.

    The model's title and docstring, its fields' titles, and the OpenAPI ``discriminator`` of a list of sources (JSON
    Schema reads its ``oneOf``) are left out. A field that is another model gets ``"type": "object"`` beside its
    ``$ref``, so every field states its type, as every other tool's do.

    Args:
        schema: The model's schema, rewritten in place.
    """
    schema.pop("title", None)
    schema.pop("description", None)
    properties = schema.get("properties")
    for field in properties.values() if isinstance(properties, dict) else ():
        if isinstance(field, dict):
            field.pop("title", None)
            if "$ref" in field:
                field.setdefault("type", "object")
            items = field.get("items")
            if isinstance(items, dict):
                items.pop("discriminator", None)


#: A request model: an unknown key is refused, naming it; its schema is short.
_REQUEST: Final = ConfigDict(extra="forbid", json_schema_extra=_terse)


def _shown(**fixed: JsonValue) -> Callable[[JsonDict], None]:
    """Make a field's schema plainer than the one generated: the keys given, with no ``anyOf`` and no default.

    An optional field is shown as its type alone (leaving it out is what ``required`` already says), and a list of
    values that may be of several types as one list of types.

    Args:
        fixed: The keys of the field's schema to set, such as ``type`` or ``items``.

    Returns:
        What rewrites the field's schema in place.
    """

    def plain(schema: JsonDict) -> None:
        schema.pop("anyOf", None)
        schema.pop("default", None)
        schema.update(fixed)

    return plain


_STRING: Final = _shown(type="string")


def _cell(value: object) -> object:
    """Take a table's cell as it came: a string, a number, a bool or ``None``, each kept as its own type.

    One check for every type, so that a bad cell is one error, not one for each type a cell may be.

    Args:
        value: The cell.

    Returns:
        The cell, unchanged.

    Raises:
        PydanticCustomError: It is none of those (``invalid_cell``).
    """
    if value is None or isinstance(value, str | int | float):  # a bool is an int
        return value
    raise PydanticCustomError("invalid_cell", "a cell is a string, a number, true, false or null")


#: A cell of a deck's table; the field's schema says its types.
TableCell = Annotated[str | int | float | bool | None, PlainValidator(_cell)]


class TableBlock(BaseModel):
    """A native table.

    Attributes:
        table: The rows, the first the header.
    """

    model_config = _REQUEST

    table: list[list[TableCell]] = Field(
        description="Rows of cells; the first is the header.",
        json_schema_extra=_shown(items={"type": "array", "items": {"type": ["string", "number", "boolean", "null"]}}),
    )


class ChartSpec(BaseModel):
    """A native chart, in the theme's colours.

    Attributes:
        type: ``bar`` (horizontal bars), ``column`` (vertical bars), ``line`` or ``pie``.
        categories: The labels along the axis, or a pie's slices.
        series: Each series' name and a value per category.
    """

    model_config = _REQUEST

    type: Literal["bar", "column", "line", "pie"] = Field(description="bar: horizontal bars; column: vertical bars.")
    categories: list[str | StrictInt | StrictFloat] = Field(
        description="Axis labels, or a pie's slices.", json_schema_extra=_shown(items={"type": ["string", "number"]})
    )
    series: dict[str, list[StrictFloat | None]] = Field(
        description="Each series' name to a value per category, or null; a pie has one.",
        json_schema_extra=_shown(additionalProperties={"type": "array", "items": {"type": ["number", "null"]}}),
    )


class ChartBlock(BaseModel):
    """A native chart.

    Attributes:
        chart: The chart.
    """

    model_config = _REQUEST

    chart: ChartSpec = Field(description="The chart.")


class ImageBlock(BaseModel):
    """A picture of the conversation.

    Attributes:
        image: The picture's absolute path.
        alt: Its alternative text.
    """

    model_config = _REQUEST

    image: str = Field(
        min_length=1, max_length=1024, description="The path of a PNG, JPEG or GIF of this conversation."
    )
    alt: str | None = Field(default=None, description="Its alternative text.", json_schema_extra=_STRING)


def _block_kind(value: object) -> BlockKind | None:
    """Tell a block's kind, as the deck's rules tell it: text, or the one key of an object; or a model's own.

    Args:
        value: A block as it came, or a block model, as it is serialised.

    Returns:
        ``text``, ``table``, ``chart`` or ``image``; ``None`` for anything else, which is refused as ``invalid_block``.
    """
    if isinstance(value, TableBlock):
        return "table"
    if isinstance(value, ChartBlock):
        return "chart"
    if isinstance(value, ImageBlock):
        return "image"
    return block_kind(value)


#: One entry of a slide's content: Markdown text, a table, a chart or a picture. Its kind picks the model, so a bad
#: block is told what is wrong with it as that kind alone.
Block = Annotated[
    Annotated[str, Tag("text")]
    | Annotated[TableBlock, Tag("table")]
    | Annotated[ChartBlock, Tag("chart")]
    | Annotated[ImageBlock, Tag("image")],
    Discriminator(
        _block_kind,
        custom_error_type="invalid_block",
        custom_error_message="a block is Markdown text, or an object with exactly one of table, chart or image",
    ),
]


#: The keys of a block a model wrapped around its content, as inspect_template names a place's role.
_WRAPPER_KEYS: Final = frozenset({"text", "role"})
#: A picture written as the old description wrote it, not as JSON: ``{image: /workspace/chat/chart.png}``.
_PSEUDO_IMAGE: Final = re.compile(r"\{\s*image\s*:\s*(?P<path>[^{}]+?)\s*\}")
#: A line break a model wrote out (a backslash and an n, escaped twice) where a break belongs: not after another
#: backslash, and before a list's next item or another such break.
_WRITTEN_BREAK: Final = re.compile(r"(?<!\\)\\n(?=[ \t]*(?:[-*+][ \t]|\d+[.)][ \t]|\\n))")
#: Every written-out break not after a backslash, once a text is known to hold them.
_ANY_WRITTEN_BREAK: Final = re.compile(r"(?<!\\)\\n")


def _parsed(text: str) -> object:
    """Read the JSON value a string holds, as the request reader reads a body.

    Args:
        text: The string.

    Returns:
        The value, every lone surrogate in it replaced (:func:`~workbench.protocol.encodable`); ``None`` when the
        string holds no JSON or a value nested too deeply to read.
    """
    try:
        return encodable(json.loads(text))
    except (ValueError, RecursionError):
        return None


def _unwrapped(block: object) -> object:
    """Take an object of ``text`` and, perhaps, ``role`` as what its ``text`` holds; any other block as it came.

    Args:
        block: A block as it came.

    Returns:
        The block meant: the role of a place, as inspect_template lists it, is dropped, since a block's order picks
        its place.
    """
    if isinstance(block, dict) and "text" in block and set(block) <= _WRAPPER_KEYS:
        return block["text"]
    return block


def _with_breaks(text: str) -> str:
    """Give text whose line breaks a model wrote out real ones; any other text as it came.

    Only text without a real break inside is read so, and only when a written-out break stands where a break
    belongs: before a list's next item or another one. A Windows path, a formula or a backslash-n in prose stays. Once
    one break is found, every written-out break not after a backslash is one, so a text that mixes a path with
    breaks written out loses the path's backslash-n: rare, and the price of reading the common case.

    Args:
        text: Markdown text, or speaker notes.

    Returns:
        The text meant.
    """
    if "\n" in text.strip() or _WRITTEN_BREAK.search(text) is None:
        return text
    return _ANY_WRITTEN_BREAK.sub("\n", text)


def _read_as_meant(block: object) -> object:
    """Take a block in a shape models write by mistake as the block they meant; any other block as it came.

    The shapes, each found in a model's first call:

    - an object of ``text`` and, perhaps, ``role`` (:func:`_unwrapped`): what ``text`` holds, text or a block;
    - a string that holds such an object or a block as JSON, or ``{image: <path>}``, the old description's notation:
      that block;
    - Markdown text with its line breaks written out (:func:`_with_breaks`): the text with line breaks.

    Anything else, an object with a key beside ``text`` and ``role`` too, is left to the block's own check.

    Args:
        block: One entry of a slide's content, as it came.

    Returns:
        The block meant.
    """
    block = _unwrapped(block)
    if not isinstance(block, str):
        return block
    stripped = block.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        parsed = _unwrapped(_parsed(stripped))
        if isinstance(parsed, dict) and block_kind(parsed) in ("table", "chart", "image"):
            return parsed
        if isinstance(parsed, str):
            return _with_breaks(parsed)
        pseudo = _PSEUDO_IMAGE.fullmatch(stripped)
        if pseudo is not None:
            return {"image": pseudo.group("path").strip("\"'")}
    return _with_breaks(block)


class SlideSpec(BaseModel):
    """One slide.

    Attributes:
        layout: The layout's number or name; ``None`` for the first layout that takes the slide.
        title: The slide's title.
        subtitle: Its subtitle.
        content: Its blocks, one a place of the layout.
        notes: Its speaker notes.
        sources: Where its content comes from.
    """

    model_config = _REQUEST

    layout: StrictInt | str | None = Field(
        default=None,
        description="Its number or name, as inspect_template lists it; left out, the first that takes the slide.",
        json_schema_extra=_shown(type=["integer", "string"]),
    )
    title: str | None = Field(default=None, description="Its title.", json_schema_extra=_STRING)
    subtitle: str | None = Field(default=None, description="Its subtitle.", json_schema_extra=_STRING)
    content: list[Block] = Field(default_factory=list, description="One block a place of the layout, in its order.")
    notes: str | None = Field(default=None, description="Its speaker notes.", json_schema_extra=_STRING)
    sources: list[Annotated[Source, Field(discriminator="kind")]] = Field(
        default_factory=list, description="Where its content comes from."
    )

    @model_validator(mode="before")
    @classmethod
    def _as_meant(cls, data: object) -> object:
        """Read a slide as the model meant it: its content a list of blocks read as meant, its notes with breaks.

        Content given as one block, or as a list written in a string, is that list; each block is read by
        :func:`_read_as_meant`, and the notes by :func:`_with_breaks`.

        Args:
            data: The slide as it came.

        Returns:
            The slide as meant; anything but an object as it came.
        """
        if not isinstance(data, dict):
            return data
        slide = dict(data)
        content = slide.get("content")
        if isinstance(content, str) and content.strip().startswith("["):
            listed = _parsed(content.strip())
            content = listed if isinstance(listed, list) else content
        if isinstance(content, (str, dict)):
            content = [content]
        if isinstance(content, list):
            slide["content"] = [_read_as_meant(block) for block in content]
        notes = slide.get("notes")
        if isinstance(notes, str):
            slide["notes"] = _with_breaks(notes)
        return slide


class WritePresentationRequest(BaseModel):
    """Body of ``write_presentation``.

    Attributes:
        path: The slide master's absolute path; ``None`` for the plain master.
        name: The new file's name.
        slides: The slides, in order.
        include_sources: Whether the speaker notes end with each slide's sources.
    """

    model_config = _REQUEST

    path: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
        description="The slide master's path, as list_files gives it; leave it out for the plain master.",
        json_schema_extra=_shown(type="string", minLength=1, maxLength=1024),
    )
    name: str = Field(min_length=1, max_length=MAX_NAME, description="The new file's name; .pptx is added if missing.")
    slides: list[SlideSpec] = Field(description="The slides, in order.")
    include_sources: bool = Field(default=True, description="Put each slide's sources in its speaker notes.")


class BuiltPresentation(BaseModel):
    """What ``write_presentation`` answers.

    Attributes:
        path: The new file's path.
        slides: How many slides the deck has.
        warnings: What to tell the user.
    """

    path: str = Field(description="The new file's path; the user sees it as a card in the chat.")
    slides: int = Field(description="How many slides the deck has.")
    warnings: list[str] = Field(description="What to tell the user about the deck, such as text that may not fit.")


class _Built(BaseModel):
    """What the building runner answers; checked before anything of it is sent on.

    Attributes:
        slides: How many slides it built.
        warnings: What the model should know.
    """

    slides: int = Field(ge=1)
    warnings: list[Annotated[str, BeforeValidator(_cut_to(MAX_HINT)), StringConstraints(max_length=MAX_HINT)]]


def _master(rec: FileRecord) -> None:
    """Check that a file is a slide master, by its name.

    Args:
        rec: The file's record.

    Raises:
        HTTPException: It is not a ``.pptx`` or ``.potx`` (422).
    """
    ext = posixpath.splitext(rec.name)[1].lower()
    if ext not in SLIDE_MASTERS:
        shown = ext or "a file without an extension"
        raise HTTPException(
            status_code=422, detail=_clean(f"unsupported_template: {_NOT_A_MASTER}; {shown} is not one")
        )


def _invalid(where: str, why: str) -> HTTPException:
    """Refuse a block, naming it.

    Args:
        where: ``slide N, block M``.
        why: What is wrong.

    Returns:
        The 422 to raise.
    """
    return HTTPException(status_code=422, detail=_clean(f"invalid_value: {where}: {why}"))


def _picture(store: Store, p: Principal, path: str, where: str) -> FileRecord:
    """Find a picture among the chat's files: a PNG, JPEG or GIF of at most :data:`MAX_IMAGE_BYTES`.

    Args:
        store: The file store.
        p: The caller, user and chat.
        path: The picture's absolute path, as the model gave it.
        where: ``slide N, block M``.

    Returns:
        Its record.

    Raises:
        HTTPException: It is no file of this chat, not a picture by its name, or too large (422, never a 404).
    """
    try:
        rec = store.get(p, path)
    except (NotFound, PathError):
        raise _invalid(where, f"{path} is not a file of this conversation") from None
    if posixpath.splitext(rec.name)[1].lower() not in IMAGE_TYPES:
        raise _invalid(where, "an image is a PNG, JPEG or GIF file")
    if rec.size > MAX_IMAGE_BYTES:
        raise _invalid(where, f"{rec.virtual_path} is larger than {MAX_IMAGE_BYTES // _MB} MB")
    return rec


def _slides(
    store: Store, p: Principal, chat_id: str, slides: Sequence[SlideSpec], dumped: Sequence[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[FileRecord], list[FileRecord]]:
    """Find every picture and check every source, and write the slides as the runner takes them.

    Args:
        store: The file store.
        p: The caller, user and chat.
        chat_id: The master's chat, where the job runs.
        slides: The slides as validated.
        dumped: The same slides as JSON, without their ``None`` fields.

    Returns:
        The slides for the runner (a picture and a file source by their paths below the chat folder, each with its
        virtual path), the pictures and the source files, each once, in the order first met.

    Raises:
        HTTPException: A picture or a source is refused (422), or all pictures together are larger than
            :data:`MAX_IMAGES_BYTES`, an image used twice counting twice, as the runner counts.
    """
    sent: list[dict[str, Any]] = []
    pictures: dict[str, FileRecord] = {}
    files: dict[str, FileRecord] = {}
    total = 0
    for number, (slide, raw) in enumerate(zip(slides, dumped, strict=True), 1):
        content: list[Any] = []
        for index, (block, given) in enumerate(zip(slide.content, raw.get("content", []), strict=True), 1):
            if not isinstance(block, ImageBlock):
                content.append(given)
                continue
            where = f"slide {number}, block {index}"
            rec = _picture(store, p, block.image, where)
            total += rec.size
            if total > MAX_IMAGES_BYTES:
                raise _invalid(where, f"the deck's images take more than {MAX_IMAGES_BYTES // _MB} MB together")
            pictures.setdefault(rec.id, rec)
            picture = {"image": rec.name, "virtual": rec.virtual_path}
            content.append(picture if block.alt is None else {**picture, "alt": block.alt})
        cited: list[dict[str, Any]] = []
        for index, source in enumerate(slide.sources, 1):
            spec, named = _source_spec(store, p, chat_id, source, f"slide {number}, source {index}")
            if named is not None:
                files.setdefault(named.id, named)
            cited.append(spec)
        sent.append({**raw, "content": content, "sources": cited})
    return sent, list(pictures.values()), list(files.values())


def _criteria(slides: Sequence[SlideSpec]) -> dict[str, object]:
    """Count what the deck holds, for its event: never a text, a name or a value.

    Args:
        slides: The slides.

    Returns:
        ``slides``, ``tables``, ``charts``, ``images`` (each picture block, a picture used twice counted twice) and
        ``sources_by_kind``, keyed by every kind of source.
    """
    blocks = [block for slide in slides for block in slide.content]
    by_kind = dict.fromkeys(SOURCE_KINDS, 0)
    for slide in slides:
        for source in slide.sources:
            by_kind[source.kind] += 1
    return {
        "slides": len(slides),
        "tables": sum(isinstance(block, TableBlock) for block in blocks),
        "charts": sum(isinstance(block, ChartBlock) for block in blocks),
        "images": sum(isinstance(block, ImageBlock) for block in blocks),
        "sources_by_kind": by_kind,
    }


def _fit_built(answer: BuiltPresentation, limit_kb: int) -> BuiltPresentation:
    """Cut the warnings from their end until the answer fits the result limit, and say how many are not listed.

    Args:
        answer: The answer.
        limit_kb: The result limit in kilobytes.

    Returns:
        The answer within the limit as sent.
    """
    limit = limit_kb * 1024
    if _json_size(answer) <= limit:
        return answer
    warnings = list(answer.warnings)

    def cut() -> BuiltPresentation:
        left = len(answer.warnings) - len(warnings)
        note = f"The answer is cut at the result limit ({limit_kb} KB): {left} warnings are not listed."
        return answer.model_copy(update={"warnings": [*warnings, note]})

    while _json_size(cut()) > limit and warnings:
        warnings.pop()
    return cut()


async def write_presentation(
    body: WritePresentationRequest,
    p: Principal,
    settings: Settings,
    store: Store,
    audit: AuditLog,
    jobs: "CallJobs",
) -> BuiltPresentation:
    """Build a deck in a slide master in the executor, into a new ``.pptx`` of its chat, and answer with it.

    Without a master the deck is built in the plain one, which the executor makes
    (:func:`workbench.executor.presentations.master.plain_master`), in the call's chat.

    Args:
        body: The master, or none for the plain master; the new file's name, the slides and whether the notes end
            with the sources.
        p: The caller, user and chat.
        settings: The api's settings; ``job_wall_s`` gives the time limit, ``result_max_kb`` caps the answer.
        store: The file store.
        audit: The audit log.
        jobs: The queue, within the call's limits.

    Returns:
        The new file's path, how many slides it has, and warnings.

    Raises:
        HTTPException: The master, the name, a bound, a picture or a source is refused (422, before any job); the
            runner refused (its 422); the job's answer cannot be read, or its file was not kept (500).
    """
    rec = None if body.path is None else await run_in_threadpool(store.get, p, body.path)
    if rec is not None:
        _master(rec)
    name = _output_name(body.name, "pptx")
    dumped: list[dict[str, Any]] = body.model_dump(mode="json", exclude_none=True)["slides"]
    problem = await run_in_threadpool(deck_problem, dumped)
    if problem is not None:
        raise HTTPException(status_code=422, detail=f"invalid_value: {problem}")
    given: list[FileRecord] = [] if rec is None else [rec]
    chat_id = p.chat_id if rec is None else rec.chat_id
    slides, pictures, files = await run_in_threadpool(_slides, store, p, chat_id, body.slides, dumped)
    inputs = list(dict.fromkeys(file.name for file in [*given, *pictures, *files]))
    if len(inputs) > MAX_INPUT_FILES:
        raise HTTPException(status_code=422, detail=f"the slides name more than {MAX_INPUT_FILES - len(given)} files")
    args: dict[str, Any] = {"path": rec.name} if rec is not None else {}
    args |= {
        "name": name,
        "slides": slides,
        "include_sources": body.include_sources,
        "budget_s": max(1, settings.job_wall_s * 3 // 4),
    }
    used = list(dict.fromkeys(file.id for file in [*given, *pictures, *files]))
    criteria = _criteria(body.slides)

    def decide(result: JobResult, planned: Sequence[str]) -> tuple[list[str], str, dict[str, object]]:
        # One new file, of an answer the model can be given; otherwise nothing is kept, the model never learning of it.
        if result.status == "ok" and _readable(_Built, result.result) and len(planned) == 1:
            return list(planned), "ok", criteria
        outcome, more = judged(result, False)
        return [], outcome, {"outputs": 0, **more}

    keeper = OutputKeeper(audit, p, "write_presentation", used, decide)
    _, result, made = await jobs.submit(p, "write_presentation", args, inputs, keeper)
    done = _parse(_Built, _payload(result, "file"))
    if len(made) != 1:
        note = _clean(result.message) or "the job made no file"
        raise HTTPException(status_code=500, detail=f"the deck could not be kept: {note}")
    (new,) = made
    answer = BuiltPresentation(path=new.virtual_path, slides=done.slides, warnings=done.warnings)
    return _fit_built(answer, settings.result_max_kb)
