"""The job protocol between the api and the executor: pydantic models, one JSON line per message.

The executor runs as root and builds paths from a request, so every request is checked here,
before anything reads it: no field it does not declare, ids that are safe folder names, and
input paths that stay below the chat folder.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from workbench.paths import check_filename, check_id

MAX_LINE = 1_048_576
MAX_INPUT_FILES = 1000

JOB_ID = re.compile(r"[0-9a-f]{32}")


class _Message(BaseModel):
    """A protocol message: a field it does not declare is refused, not ignored."""

    model_config = ConfigDict(extra="forbid")


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
        limits: Resource limits for this job.
        max_chars: Largest result the runner may return, in characters (``result_max_kb * 1024``).
    """

    job_id: str
    tool: str
    args: dict[str, Any]
    caller: str
    user_id: str
    chat_id: str
    input_files: list[str] = Field(max_length=MAX_INPUT_FILES)
    limits: Limits
    max_chars: int = Field(gt=0)

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


class OutputFile(_Message):
    """A file a job wrote, staged for the api to move into the workspace.

    Attributes:
        name: Name the file had in the job's ``out/``.
        staged_path: Where the executor left it for the api.
        size: Size in bytes.
    """

    name: str
    staged_path: str
    size: int = Field(ge=0)


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
