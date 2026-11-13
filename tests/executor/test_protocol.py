"""The job protocol: one JSON line per message, refused above a size cap."""

import pytest
from pydantic import ValidationError

from workbench.protocol import (
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


def _request() -> JobRequest:
    """A request with synthetic content."""
    return JobRequest(
        job_id="job-1",
        tool="echo",
        args={"x": 1, "text": "line one\nline two"},
        chat_id="chat-1",
        input_files=["quarterly-figures.xlsx", "reports/summary.docx"],
        limits=LIMITS,
        max_chars=16384,
    )


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
