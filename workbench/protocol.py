"""The job protocol between the api and the executor: pydantic models, one JSON line per message."""

from typing import Any, Literal

from pydantic import BaseModel, Field

MAX_LINE = 1_048_576


class Limits(BaseModel):
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


class JobRequest(BaseModel):
    """One job the api hands to the executor.

    Attributes:
        job_id: Opaque id the result is matched by.
        tool: Name of the runner to run.
        args: Arguments for the runner.
        chat_id: Chat folder the job works on.
        input_files: Paths relative to the chat folder; the executor copies only these.
        limits: Resource limits for this job.
        max_chars: Largest result the runner may return, in characters (``result_max_kb * 1024``).
    """

    job_id: str
    tool: str
    args: dict[str, Any]
    chat_id: str
    input_files: list[str]
    limits: Limits
    max_chars: int


class OutputFile(BaseModel):
    """A file a job wrote, staged for the api to move into the workspace.

    Attributes:
        name: Name the file had in the job's ``out/``.
        staged_path: Where the executor left it for the api.
        size: Size in bytes.
    """

    name: str
    staged_path: str
    size: int


class JobResult(BaseModel):
    """What the executor answers for one job.

    Attributes:
        job_id: The id of the request this answers.
        status: ``ok``, ``error`` (the runner or the process failed), ``timeout`` (wall time),
            ``killed`` (the kernel stopped it on a resource limit) or ``busy`` (no free slot).
        result: The runner's answer; ``{"error": ...}`` when it raised.
        outputs: Files the job wrote.
        message: Short human-readable reason for any status other than ``ok``.
    """

    job_id: str
    status: Literal["ok", "error", "timeout", "killed", "busy"]
    result: dict[str, Any]
    outputs: list[OutputFile]
    message: str = ""


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
