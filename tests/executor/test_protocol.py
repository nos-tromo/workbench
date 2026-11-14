"""The job protocol: one JSON line per message, refused above a size cap."""

import json
from typing import Any

import pytest
from pydantic import ValidationError

from workbench.protocol import (
    MAX_INPUT_FILES,
    MAX_LINE,
    JobRequest,
    JobResult,
    Limits,
    OutputFile,
    decode_request,
    decode_result,
    encode,
)

LIMITS = Limits(wall_s=120, cpu_s=120, memory_mb=1536, file_mb=50)
JOB_ID = "0123456789abcdef0123456789abcdef"


def _fields(**overrides: Any) -> dict[str, Any]:
    """The fields of a request with synthetic content, with ``overrides`` applied."""
    return {
        "job_id": JOB_ID,
        "tool": "echo",
        "args": {"x": 1, "text": "line one\nline two"},
        "caller": "open-webui",
        "user_id": "user-a",
        "chat_id": "chat-1",
        "input_files": ["quarterly-figures.xlsx", "reports/summary.docx"],
        "limits": LIMITS,
        "max_chars": 16384,
        **overrides,
    }


def _request() -> JobRequest:
    """A request with synthetic content."""
    return JobRequest(**_fields())


def _result() -> JobResult:
    """A result with one output file."""
    return JobResult(
        job_id="job-1",
        status="ok",
        result={"rows": 3},
        outputs=[OutputFile(name="summary.xlsx", staged_path="job-1/out/summary.xlsx", size=2048)],
    )


def test_limits_default_the_process_and_file_caps() -> None:
    """A job gets 64 processes and 256 open files unless the caller says otherwise."""
    assert (LIMITS.nproc, LIMITS.nofile) == (64, 256)


@pytest.mark.parametrize("field", ["wall_s", "cpu_s", "memory_mb", "file_mb", "nproc", "nofile"])
def test_limits_must_be_positive(field: str) -> None:
    """A zero limit would stop the job before it starts, so it is refused."""
    values = {"wall_s": 1, "cpu_s": 1, "memory_mb": 1, "file_mb": 1, "nproc": 1, "nofile": 1, field: 0}
    with pytest.raises(ValidationError):
        Limits(**values)


def test_protocol_round_trip() -> None:
    """A request and a result decode to what was encoded."""
    request, result = _request(), _result()
    assert decode_request(encode(request)) == request
    assert decode_result(encode(result)) == result


def test_encode_is_one_line() -> None:
    """Newlines inside values are escaped, so one message is exactly one line."""
    line = encode(_request())
    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1


def test_result_message_defaults_to_empty() -> None:
    """A result without a message has an empty one."""
    assert JobResult(job_id="job-1", status="busy", result={}, outputs=[]).message == ""


def test_oversized_line_is_refused() -> None:
    """A line above ``MAX_LINE`` is refused before it is parsed."""
    line = b" " * (MAX_LINE + 1)
    with pytest.raises(ValueError, match="too long"):
        decode_request(line)
    with pytest.raises(ValueError, match="too long"):
        decode_result(line)


def test_a_line_of_exactly_the_cap_is_accepted() -> None:
    """The cap itself is allowed, the byte after it is not."""
    padding = MAX_LINE - len(encode(_result()))
    line = encode(_result()).rstrip(b"\n") + b" " * padding + b"\n"
    assert len(line) == MAX_LINE
    assert decode_result(line) == _result()
    with pytest.raises(ValueError, match="too long"):
        decode_result(line + b" ")


def test_malformed_lines_are_value_errors() -> None:
    """Broken JSON, a wrong shape and an unknown status all raise ``ValueError``."""
    with pytest.raises(ValueError):
        decode_request(b"not json\n")
    with pytest.raises(ValueError):
        decode_request(b'{"job_id": "job-1"}\n')
    with pytest.raises(ValueError):
        decode_result(b'{"job_id": "job-1", "status": "weird", "result": {}, "outputs": []}\n')


def test_a_request_names_the_user_root_its_chat_folder_lies_in() -> None:
    """The executor finds the chat folder below ``<caller>/<user-id>``, so the request carries both."""
    request = _request()
    assert (request.caller, request.user_id, request.chat_id) == ("open-webui", "user-a", "chat-1")


@pytest.mark.parametrize(
    "model",
    [
        lambda: Limits.model_validate({**LIMITS.model_dump(), "rlimit_stack": 1}),
        lambda: JobRequest.model_validate({**_fields(limits=LIMITS.model_dump()), "uid": 0}),
        lambda: OutputFile.model_validate({"name": "r.csv", "staged_path": "/data/staging/r.csv", "size": 1, "x": 1}),
        lambda: JobResult.model_validate({"job_id": JOB_ID, "status": "ok", "result": {}, "outputs": [], "x": 1}),
    ],
    ids=["limits", "request", "output", "result"],
)
def test_unknown_fields_are_refused(model: Any) -> None:
    """Every message refuses a field it does not declare, so nothing unvetted reaches the executor."""
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model()


@pytest.mark.parametrize(
    "job_id",
    ["", "job-1", JOB_ID.upper(), JOB_ID[:-1], JOB_ID + "0", JOB_ID + "\n", "../" + JOB_ID[3:], "g" * 32],
)
def test_a_job_id_is_32_lowercase_hex_digits(job_id: str) -> None:
    """The job id becomes a folder name on the root side, so it is exactly a ``uuid4().hex``."""
    with pytest.raises(ValidationError):
        JobRequest(**_fields(job_id=job_id))


@pytest.mark.parametrize("field", ["caller", "user_id", "chat_id"])
@pytest.mark.parametrize("value", ["", "..", "a/b", "-x", ".hidden", "a\x00b", "x" * 129, "chat..1"])
def test_the_ids_of_the_folder_are_safe_folder_names(field: str, value: str) -> None:
    """Caller, user and chat ids each become one folder name, checked as the api checks them."""
    with pytest.raises(ValidationError):
        JobRequest(**_fields(**{field: value}))


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../other-chat/q.csv",
        "reports/../../q.csv",
        "./q.csv",
        "reports//q.csv",
        "reports/",
        "",
        "q\x00.csv",
        "x" * 201,
        "reports/" + "ü" * 128,
    ],
    ids=[
        "absolute",
        "dotdot",
        "inner-dotdot",
        "dot",
        "empty-segment",
        "trailing-slash",
        "empty",
        "nul",
        "long",
        "utf8",
    ],
)
def test_input_files_are_plain_relative_paths(path: str) -> None:
    """An input is a path below the chat folder: no ``..``, no absolute path, every segment a plain name."""
    with pytest.raises(ValidationError):
        JobRequest(**_fields(input_files=["q.csv", path]))


def test_nested_input_files_are_accepted() -> None:
    """A file inside a folder of the chat is named by its path below the chat folder."""
    request = JobRequest(**_fields(input_files=["reports/2025/summary.docx", "Übersicht März.xlsx"]))
    assert request.input_files == ["reports/2025/summary.docx", "Übersicht März.xlsx"]


def test_at_most_a_thousand_input_files() -> None:
    """A request lists at most ``MAX_INPUT_FILES`` inputs."""
    names = [f"f{n}.csv" for n in range(MAX_INPUT_FILES)]
    assert len(JobRequest(**_fields(input_files=names)).input_files) == MAX_INPUT_FILES == 1000
    with pytest.raises(ValidationError):
        JobRequest(**_fields(input_files=[*names, "one-more.csv"]))


@pytest.mark.parametrize("max_chars", [0, -1])
def test_max_chars_must_be_positive(max_chars: int) -> None:
    """A result of no characters is no result."""
    with pytest.raises(ValidationError):
        JobRequest(**_fields(max_chars=max_chars))


def test_an_output_size_is_never_negative() -> None:
    """Sizes count bytes."""
    assert OutputFile(name="r.csv", staged_path="/data/staging/r.csv", size=0).size == 0
    with pytest.raises(ValidationError):
        OutputFile.model_validate({"name": "r.csv", "staged_path": "/data/staging/r.csv", "size": -1})


def test_a_refused_request_line_is_a_value_error() -> None:
    """``decode_request`` refuses an unsafe request as it refuses a malformed one."""
    line = json.dumps({**_fields(limits=LIMITS.model_dump()), "chat_id": "../x"}).encode()
    with pytest.raises(ValueError):
        decode_request(line)
