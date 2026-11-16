"""The job protocol between the api and the executor: pydantic models, one JSON line per message.

The executor runs as root and builds paths from a request, so every request is checked here,
before anything reads it: no field it does not declare, ids that are safe folder names, input
paths that stay below the chat folder, and inline inputs that are each one plain file name.
"""

import re
from collections.abc import Sequence
from typing import Annotated, Any, Final, Literal, Protocol, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from workbench.paths import check_filename, check_id

MAX_LINE = 1_048_576
MAX_INPUT_FILES = 1000
#: Most entries a request carries inline.
MAX_INLINE_FILES: Final = 8
#: Most bytes, in UTF-8, the inline entries' texts hold together.
MAX_INLINE_BYTES: Final = 512 * 1024
#: The job's own output folder in its working folder: no input may take its name.
OUT_DIR: Final = "out"

#: Most rows below the header, columns, and characters of a cell, that a table preview holds.
PREVIEW_ROWS: Final = 5
PREVIEW_COLUMNS: Final = 20
PREVIEW_CELL: Final = 100

#: One cell of a table preview.
Cell = Annotated[str, StringConstraints(max_length=PREVIEW_CELL)]

#: The message of a job the kernel stopped on one of its limits (memory, CPU time, file size, processes); a job the
#: executor's watchdog stopped has a message that names the limit instead.
KILLED = "the job was stopped on a resource limit"

JOB_ID = re.compile(r"[0-9a-f]{32}")


class _Message(BaseModel):
    """A protocol message: a field it does not declare is refused, not ignored.

    A refusal's ``str()`` and ``repr()`` never quote the input; its ``errors()`` and ``json()`` still carry it unless
    called with ``include_input=False``.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


def check_input_path(path: str) -> str:
    """Accept a path below the chat folder: relative, each segment one plain file name.

    Args:
        path: An entry of ``JobRequest.input_files``, such as ``reports/summary.docx``.

    Returns:
        ``path``, unchanged.

    Raises:
        ValueError: The path is absolute, or a segment is empty, ``.``, ``..`` or not a valid
            file name (``workbench.paths.check_filename``).
    """
    if path.startswith("/"):
        raise ValueError("an input file is a path relative to the chat folder")
    for segment in path.split("/"):
        check_filename(segment)
    return path


def _check_inline_name(name: str) -> str:
    """Accept one plain file name that is not the job's output folder.

    Args:
        name: An inline input's name.

    Returns:
        ``name``, unchanged.

    Raises:
        ValueError: It is not a valid file name (``workbench.paths.check_filename``), or it is ``out``.
    """
    check_filename(name)
    if name == OUT_DIR:
        raise ValueError("an inline input cannot be named out")
    return name


class Limits(_Message):
    """What one job may use before the sandbox stops it.

    Attributes:
        wall_s: Wall-clock seconds the job may run.
        cpu_s: CPU seconds the job may use.
        memory_mb: Address space the job may map, in megabytes.
        file_mb: Largest single file the job may write, in megabytes.
        nproc: Processes the job's uid may have.
        nofile: Files the job may hold open.
    """

    wall_s: int = Field(gt=0)
    cpu_s: int = Field(gt=0)
    memory_mb: int = Field(gt=0)
    file_mb: int = Field(gt=0)
    nproc: int = Field(default=64, gt=0)
    nofile: int = Field(default=256, gt=0)


class InlineFile(_Message):
    """A text the request carries itself, written as one file into the job's working folder.

    Attributes:
        name: The file's name: one plain file name, not ``out``.
        content: Its text, written as UTF-8.
    """

    name: str
    content: str = Field(repr=False)

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        """Accept one plain file name that is not the job's output folder.

        Args:
            value: The name.

        Returns:
            ``value``, unchanged.

        Raises:
            ValueError: It is not a valid file name (``workbench.paths.check_filename``), or it is ``out``.
        """
        return _check_inline_name(value)


class NamedText(Protocol):
    """An inline input as its rules see it, whichever model carries it: a name and a text."""

    @property
    def name(self) -> str:
        """The file's name."""
        ...

    @property
    def content(self) -> str:
        """Its text."""
        ...


def check_inline_inputs(entries: Sequence[NamedText]) -> None:
    """Accept the inline inputs of one request: the protocol's rules, also for entries built without validation.

    The inline tool checks its arguments by these rules too, so the two cannot drift apart.

    Args:
        entries: The inline inputs.

    Raises:
        ValueError: There are more than ``MAX_INLINE_FILES``; a name is not one plain file name or is ``out``; a name
            is repeated; a text cannot be encoded as UTF-8; or the texts together are more than ``MAX_INLINE_BYTES``
            in UTF-8. The message never quotes a name or a text.
    """
    if len(entries) > MAX_INLINE_FILES:
        raise ValueError(f"at most {MAX_INLINE_FILES} inline inputs")
    names = [_check_inline_name(entry.name) for entry in entries]
    if len(set(names)) != len(names):
        raise ValueError("inline input names must be unique")
    try:
        size = sum(len(entry.content.encode("utf-8")) for entry in entries)
    except UnicodeEncodeError:
        raise ValueError("inline inputs must be text UTF-8 can hold") from None
    if size > MAX_INLINE_BYTES:
        raise ValueError(f"inline inputs hold more than {MAX_INLINE_BYTES // 1024} KiB together")


class JobRequest(_Message):
    """One job the api hands to the executor.

    The chat folder is ``<data_dir>/workspaces/<caller>/<user_id>/<chat_id>``.

    Attributes:
        job_id: Opaque id the result is matched by: 32 lowercase hex digits (``uuid4().hex``).
        tool: Name of the runner to run.
        args: Arguments for the runner.
        caller: Calling service whose user root holds the chat folder; a safe id.
        user_id: The caller's id for the user whose root holds the chat folder; a safe id.
        chat_id: Chat folder the job works on; a safe id.
        input_files: Paths relative to the chat folder, at most ``MAX_INPUT_FILES``; the
            executor copies only these.
        inline_inputs: Texts the request carries itself, at most ``MAX_INLINE_FILES``, unique by
            name and ``MAX_INLINE_BYTES`` in UTF-8 together; the executor writes each into the
            working folder. Never together with ``input_files``.
        limits: Resource limits for this job.
        max_chars: Largest result the runner may return, in characters (``result_max_kb * 1024``).
        previews: Whether the job reads a table preview of each csv and xlsx output once its runner has answered.
    """

    job_id: str
    tool: str
    args: dict[str, Any]
    caller: str
    user_id: str
    chat_id: str
    input_files: list[str] = Field(max_length=MAX_INPUT_FILES)
    inline_inputs: list[InlineFile] = Field(default_factory=list, max_length=MAX_INLINE_FILES)
    limits: Limits
    max_chars: int = Field(gt=0)
    previews: bool = False

    @field_validator("job_id")
    @classmethod
    def _check_job_id(cls, value: str) -> str:
        """Accept exactly 32 lowercase hex digits.

        Args:
            value: The job id.

        Returns:
            ``value``, unchanged.

        Raises:
            ValueError: It is anything else.
        """
        if JOB_ID.fullmatch(value) is None:
            raise ValueError("a job id is 32 lowercase hex digits")
        return value

    @field_validator("caller", "user_id", "chat_id")
    @classmethod
    def _check_ids(cls, value: str, info: ValidationInfo) -> str:
        """Accept an id that is safe as one folder name.

        Args:
            value: The caller, user or chat id.
            info: Which field is checked.

        Returns:
            ``value``, unchanged.

        Raises:
            ValueError: It is not a safe folder name (``workbench.paths.check_id``).
        """
        return check_id(value, what=(info.field_name or "id").replace("_", " "))

    @field_validator("input_files")
    @classmethod
    def _check_input_files(cls, value: list[str]) -> list[str]:
        """Accept only paths below the chat folder.

        Args:
            value: The input paths.

        Returns:
            ``value``, unchanged.

        Raises:
            ValueError: A path is not a plain relative path (``check_input_path``).
        """
        for path in value:
            check_input_path(path)
        return value

    @model_validator(mode="after")
    def _check_inline_inputs(self) -> Self:
        """Accept inline inputs of distinct names, within their bound, in a request that copies no input.

        Returns:
            The request, unchanged.

        Raises:
            ValueError: A name is repeated; a text cannot be encoded as UTF-8; the texts together
                are more than ``MAX_INLINE_BYTES`` in UTF-8; or the request also names
                ``input_files``.
        """
        check_inline_inputs(self.inline_inputs)
        if self.inline_inputs and self.input_files:
            raise ValueError("inline_inputs cannot come with input_files")
        return self


class TablePreview(_Message):
    """The start of a table a job wrote: its header and first rows, cut short, read inside the job.

    The job's own code could have written it, so only its shape and its caps are trusted.

    Attributes:
        columns: The header: the first record of a csv, or the first row with content of an xlsx's first sheet.
        rows: The rows below the header, at most ``PREVIEW_ROWS``.
        row_count: How many rows lie below the header, or ``None`` when they were not counted to the end.
        truncated: Whether a row, a column or a cell was left out or cut.
    """

    columns: list[Cell] = Field(max_length=PREVIEW_COLUMNS)
    rows: list[Annotated[list[Cell], Field(max_length=PREVIEW_COLUMNS)]] = Field(max_length=PREVIEW_ROWS)
    row_count: int | None = Field(ge=0)
    truncated: bool


class OutputFile(_Message):
    """A file a job wrote, staged for the api to move into the workspace.

    Attributes:
        name: Name the file had in the job's ``out/``.
        staged_path: Where the executor left it for the api.
        size: Size in bytes.
        preview: The table preview of a csv or xlsx output, when the request asked for previews and one was read.
    """

    name: str
    staged_path: str
    size: int = Field(ge=0)
    preview: TablePreview | None = None


class JobResult(_Message):
    """What the executor answers for one job.

    Attributes:
        job_id: The id of the request this answers.
        status: ``ok``, ``error`` (the runner or the process failed), ``timeout`` (wall time),
            ``killed`` (the kernel stopped it on a resource limit) or ``busy`` (no free slot).
        result: The runner's answer; ``{"error": ...}`` when it raised.
        outputs: Files the job wrote.
        message: Short human-readable reason for any status other than ``ok``; with ``ok``, a note
            such as how many outputs were dropped, or empty.
    """

    job_id: str
    status: Literal["ok", "error", "timeout", "killed", "busy"]
    result: dict[str, Any]
    outputs: list[OutputFile]
    message: str = ""


def encodable(value: Any) -> Any:
    """Replace every lone surrogate in the strings of a JSON value, its keys included, with U+FFFD.

    JSON reads the escape of a surrogate half that has no partner as a lone surrogate, which UTF-8 cannot hold, so a
    message that carries one could not be sent. A pair that is split over two code points stays one character.

    Args:
        value: A value as :func:`json.loads` returns it.

    Returns:
        The value, every string of it encodable as UTF-8.

    Raises:
        RecursionError: The value is nested too deeply to walk.
    """
    if isinstance(value, str):
        return value.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    if isinstance(value, list):
        return [encodable(item) for item in value]
    if isinstance(value, dict):
        return {encodable(key): encodable(item) for key, item in value.items()}
    return value


def encode(model: BaseModel) -> bytes:
    """Serialise a message as one JSON line.

    Args:
        model: The request or result to send.

    Returns:
        The JSON text, UTF-8 encoded, ending in a single newline.
    """
    return model.model_dump_json().encode() + b"\n"


def _check_length(line: bytes) -> None:
    """Refuse a line longer than ``MAX_LINE`` before anything parses it.

    Args:
        line: The received line.

    Raises:
        ValueError: The line is longer than ``MAX_LINE`` bytes.
    """
    if len(line) > MAX_LINE:
        raise ValueError(f"line too long: more than {MAX_LINE} bytes")


def decode_request(line: bytes) -> JobRequest:
    """Parse one request line.

    Args:
        line: One line as received, with or without its newline.

    Returns:
        The request.

    Raises:
        ValueError: The line is longer than ``MAX_LINE`` bytes, is not JSON, or is not a request.
    """
    _check_length(line)
    return JobRequest.model_validate_json(line)


def decode_result(line: bytes) -> JobResult:
    """Parse one result line.

    Args:
        line: One line as received, with or without its newline.

    Returns:
        The result.

    Raises:
        ValueError: The line is longer than ``MAX_LINE`` bytes, is not JSON, or is not a result.
    """
    _check_length(line)
    return JobResult.model_validate_json(line)
