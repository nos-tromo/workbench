"""The job entry point: one spec in, one ``result.json`` out, with errors as results."""

import ast
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from workbench.executor import job
from workbench.executor.runners import RUNNERS, JobContext
from workbench.executor.timer import TimeLimit
from workbench.protocol import MAX_LINE, PREVIEW_BUDGET, PREVIEW_CELL, PREVIEW_COLUMNS, PREVIEW_ROWS, TablePreview

SPEC = {"tool": "echo", "args": {"x": 1}, "chat_id": "chat-1", "max_chars": 16384, "file_mb": 50}


def _write_spec(directory: Path, **overrides: Any) -> Path:
    """Write a spec file into ``directory`` and return its path."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "spec.json"
    path.write_text(json.dumps({**SPEC, **overrides}))
    return path


def _run_job(spec: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the job exactly as the supervisor does, in isolated mode."""
    return subprocess.run(
        [sys.executable, "-I", "-m", "workbench.executor.job", str(spec)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_job_runs_a_registered_runner(tmp_path: Path) -> None:
    """The echo runner's answer lands in ``result.json`` next to the spec."""
    spec = _write_spec(tmp_path / "control")
    done = _run_job(spec, tmp_path)
    assert done.returncode == 0, done.stderr
    assert json.loads((tmp_path / "control" / "result.json").read_text()) == {"x": 1}
    assert not (tmp_path / "result.json").exists()


def test_job_turns_exceptions_into_errors(tmp_path: Path) -> None:
    """An unknown tool becomes an error result, not a crash."""
    spec = _write_spec(tmp_path, tool="no-such-tool")
    done = _run_job(spec, tmp_path)
    assert done.returncode == 0, done.stderr
    error = json.loads((tmp_path / "result.json").read_text())["error"]
    assert error.startswith("KeyError: ")
    assert "no-such-tool" in error


def test_a_raising_runner_is_reported_by_type_and_message(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Whatever a runner raises is reported as ``<Type>: <message>``."""

    def boom(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
        """Fail the way a tool would."""
        return {"ratio": 1 / args["x"]}

    monkeypatch.setitem(RUNNERS, "boom", boom)
    monkeypatch.chdir(tmp_path)
    spec = _write_spec(tmp_path, tool="boom", args={"x": 0})
    assert job.main(["job", str(spec)]) == 0
    assert json.loads((tmp_path / "result.json").read_text()) == {"error": "ZeroDivisionError: division by zero"}


def test_a_time_limit_that_escapes_a_runner_still_leaves_a_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timer error is not an ``Exception``: without its own clause the job would end with no ``result.json``."""

    class Late(TimeLimit):
        """A limit that fires after the runner's own handling is over."""

    def slip(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
        """Let a time limit out."""
        raise Late

    monkeypatch.setitem(RUNNERS, "slip", slip)
    monkeypatch.chdir(tmp_path)
    spec = _write_spec(tmp_path, tool="slip")
    assert job.main(["job", str(spec)]) == 0
    result = json.loads((tmp_path / "result.json").read_text())
    assert set(result) == {"error"}
    assert result["error"].startswith("read_timeout: ")


def test_a_read_that_is_out_of_time_is_a_read_timeout_result_in_a_job_process(tmp_path: Path) -> None:
    """Through the real entry point: a reader deadline that has passed is a ``read_timeout`` result, not a crash."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("hello\n", encoding="utf-8")
    spec = _write_spec(
        tmp_path / "control", tool="read_document", args={"path": "a.txt", "part": None, "budget_s": 0.0}
    )
    done = _run_job(spec, work)
    assert done.returncode == 0, done.stderr
    result = json.loads((tmp_path / "control" / "result.json").read_text())
    assert result["error"].startswith("read_timeout: ")


def test_a_time_limit_that_escapes_a_runner_is_a_result_in_a_job_process(tmp_path: Path) -> None:
    """In a fresh process, through the module's own entry point, a timer error no runner handles is a result.

    It ends in ``result.json`` and exit status 0 by way of the job's ``except TimeLimit``. The real runners handle
    their own limits, so a runner is registered that lets one out: the timer fires in its loop, in the job
    process, and nothing in the runner catches it.
    """
    spec = _write_spec(tmp_path / "control", tool="slip")
    script = (
        "import runpy, sys, time\n"
        "from workbench.executor.runners import RUNNERS\n"
        "from workbench.executor.timer import TimeLimit, limit_until\n"
        "class Late(TimeLimit):\n"
        "    pass\n"
        "def slip(args, ctx):\n"
        "    with limit_until(time.monotonic() + 0.05, Late):\n"
        "        while True:\n"
        "            pass\n"
        "RUNNERS['slip'] = slip\n"
        "sys.argv = ['job', sys.argv[1]]\n"
        "runpy.run_module('workbench.executor.job', run_name='__main__')\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", script, str(spec)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads((tmp_path / "control" / "result.json").read_text()) == {
        "error": "read_timeout: the work ran past its time limit"
    }


def test_a_result_that_is_not_json_becomes_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A runner that returns something JSON cannot hold still leaves a readable result."""
    monkeypatch.setitem(RUNNERS, "paths", lambda args, ctx: {"where": tmp_path})
    monkeypatch.chdir(tmp_path)
    spec = _write_spec(tmp_path, tool="paths")
    assert job.main(["job", str(spec)]) == 0
    assert json.loads((tmp_path / "result.json").read_text())["error"].startswith("TypeError: ")


@pytest.mark.parametrize("raised", [SystemExit(3), KeyboardInterrupt()])
def test_a_runner_that_exits_still_leaves_a_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raised: BaseException
) -> None:
    """``SystemExit`` and ``KeyboardInterrupt`` are not ``Exception`` either; the job answers all the same."""

    def leave(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
        """End the way code that calls ``sys.exit`` would."""
        raise raised

    monkeypatch.setitem(RUNNERS, "leave", leave)
    monkeypatch.chdir(tmp_path)
    spec = _write_spec(tmp_path, tool="leave")
    assert job.main(["job", str(spec)]) == 0
    assert json.loads((tmp_path / "result.json").read_text())["error"].startswith(f"{type(raised).__name__}")


def test_a_runner_answer_that_is_not_an_object_becomes_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The supervisor reads only a JSON object; a list would leave the job with no readable result."""

    def listing(args: dict[str, Any], ctx: JobContext) -> Any:
        """Answer with a list, as no runner may."""
        return ["a", "b"]

    monkeypatch.setitem(RUNNERS, "listing", listing)
    monkeypatch.chdir(tmp_path)
    spec = _write_spec(tmp_path, tool="listing")
    assert job.main(["job", str(spec)]) == 0
    assert json.loads((tmp_path / "result.json").read_text())["error"].startswith("TypeError: ")


def test_a_result_too_large_to_write_is_replaced_by_a_short_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that fails (a full volume, a file-size limit the code lowered) leaves a short error, not a torn file."""
    real_write_text = Path.write_text

    def write_text(self: Path, data: str, *args: Any, **kwargs: Any) -> int:
        if len(data) > 100:
            raise OSError(27, "File too large")
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setitem(RUNNERS, "big", lambda args, ctx: {"text": "x" * 1000})
    monkeypatch.chdir(tmp_path)
    spec = _write_spec(tmp_path, tool="big")
    monkeypatch.setattr(Path, "write_text", write_text)
    assert job.main(["job", str(spec)]) == 0
    monkeypatch.undo()
    assert json.loads((tmp_path / "result.json").read_text()) == {"error": "result_unwritable: OSError"}


def test_a_job_that_cannot_write_any_result_says_so_in_its_exit_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no result at all the supervisor answers from the exit status; the job does not claim success."""

    def refuse(self: Path, data: str, *args: Any, **kwargs: Any) -> int:
        raise OSError(28, "No space left on device")

    monkeypatch.chdir(tmp_path)
    spec = _write_spec(tmp_path)
    monkeypatch.setattr(Path, "write_text", refuse)
    assert job.main(["job", str(spec)]) == 1


def test_the_runner_gets_its_context_from_the_spec_and_the_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The working directory is the cwd, the outputs go to ``out/`` below it, the rest comes from the spec."""
    seen: list[JobContext] = []

    def spy(args: dict[str, Any], ctx: JobContext) -> dict[str, Any]:
        """Keep the context."""
        seen.append(ctx)
        return {}

    monkeypatch.setitem(RUNNERS, "spy", spy)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    spec = _write_spec(tmp_path / "control", tool="spy", chat_id="chat-7", max_chars=100, file_mb=3)
    assert job.main(["job", str(spec)]) == 0
    assert seen == [
        JobContext(work_dir=work.resolve(), out_dir=work.resolve() / "out", chat_id="chat-7", max_chars=100, file_mb=3)
    ]


COUNTS = "author,posts\nuser-a,12\n"


def _with_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Work in a folder whose ``out/`` holds ``t.csv``, as a tool would have left it; return the control folder."""
    work = tmp_path / "work"
    (work / "out").mkdir(parents=True)
    (work / "out" / "t.csv").write_text(COUNTS, encoding="utf-8")
    monkeypatch.chdir(work)
    return tmp_path / "control"


def test_previews_are_written_beside_the_result_when_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Asked, the job reads its outputs' previews once the runner has answered and writes them next to the result."""
    control = _with_output(tmp_path, monkeypatch)
    spec = _write_spec(control, previews=True)

    assert job.main(["job", str(spec)]) == 0

    assert json.loads((control / "result.json").read_text()) == {"x": 1}
    assert json.loads((control / "previews.json").read_text(encoding="utf-8")) == {
        "t.csv": {"columns": ["author", "posts"], "rows": [["user-a", "12"]], "row_count": 1, "truncated": False}
    }


@pytest.mark.parametrize("asked", [None, False], ids=["unsaid", "false"])
def test_no_previews_file_unless_asked(asked: bool | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A spec without the flag, as every job before the previews had, or with it false, reads no preview."""
    control = _with_output(tmp_path, monkeypatch)
    spec = _write_spec(control) if asked is None else _write_spec(control, previews=asked)

    assert job.main(["job", str(spec)]) == 0

    assert sorted(p.name for p in control.iterdir()) == ["result.json", "spec.json"]


@pytest.mark.parametrize("tool", ["refusing", "no-such-tool"])
def test_a_tool_that_failed_reads_no_preview(tool: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The supervisor keeps no output of a failed tool, so the job spends no time on their previews."""
    monkeypatch.setitem(RUNNERS, "refusing", lambda args, ctx: {"error": "invalid_value: refused"})
    control = _with_output(tmp_path, monkeypatch)
    spec = _write_spec(control, tool=tool, previews=True)

    assert job.main(["job", str(spec)]) == 0

    assert sorted(p.name for p in control.iterdir()) == ["result.json", "spec.json"]


class _Late(TimeLimit):
    """A time limit that lands in the previews."""


@pytest.mark.parametrize("raised", [RuntimeError("broken"), _Late(), KeyboardInterrupt()])
def test_a_preview_that_fails_leaves_the_result_alone(
    raised: BaseException, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever the previews raise, there is no previews file, and the result and the exit status are as before."""
    control = _with_output(tmp_path, monkeypatch)
    spec = _write_spec(control, previews=True)

    def fail(out_dir: Path, deadline: float) -> dict[str, Any]:
        raise raised

    monkeypatch.setattr(job, "previews_of", fail)

    assert job.main(["job", str(spec)]) == 0

    assert json.loads((control / "result.json").read_text()) == {"x": 1}
    assert not (control / "previews.json").exists()


def test_a_previews_file_cut_short_is_not_left_behind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A write that fails half-way (a full volume, the file-size limit) leaves no torn previews file."""
    control = _with_output(tmp_path, monkeypatch)
    spec = _write_spec(control, previews=True)
    real_write_text = Path.write_text

    def write_text(self: Path, data: str, *args: Any, **kwargs: Any) -> int:
        if self.name == "previews.json":
            real_write_text(self, data[:10], *args, **kwargs)
            raise OSError(28, "No space left on device")
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", write_text)

    assert job.main(["job", str(spec)]) == 0

    monkeypatch.undo()
    assert json.loads((control / "result.json").read_text()) == {"x": 1}
    assert not (control / "previews.json").exists()


def test_previews_that_would_pass_the_budget_are_left_off_and_the_file_stays_below_the_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Twenty near-maximal previews of control characters (6 bytes each, escaped) leave the first few by name."""
    work = tmp_path / "work"
    (work / "out").mkdir(parents=True)
    monkeypatch.chdir(work)
    cell = "\x01" * PREVIEW_CELL
    record = ",".join([cell] * PREVIEW_COLUMNS)
    names = [f"t-{n:02d}.csv" for n in range(20)]
    for name in names:
        (work / "out" / name).write_text("\n".join([record] * (PREVIEW_ROWS + 1)) + "\n", encoding="utf-8")
    control = tmp_path / "control"
    spec = _write_spec(control, previews=True)

    assert job.main(["job", str(spec)]) == 0

    raw = (control / "previews.json").read_bytes()
    written = json.loads(raw)
    assert len(raw) <= MAX_LINE
    assert list(written) == names[:3]
    assert (
        sum(len(TablePreview.model_validate(v).model_dump_json().encode()) for v in written.values()) <= PREVIEW_BUDGET
    )


def test_a_result_that_fell_back_to_the_short_error_reads_no_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The result could not be written, so the supervisor answers "the tool failed": no preview is worth the time."""
    real_write_text = Path.write_text

    def write_text(self: Path, data: str, *args: Any, **kwargs: Any) -> int:
        if self.name == "result.json" and len(data) > 100:
            raise OSError(27, "File too large")
        return real_write_text(self, data, *args, **kwargs)

    monkeypatch.setitem(RUNNERS, "big", lambda args, ctx: {"text": "x" * 1000})
    control = _with_output(tmp_path, monkeypatch)
    spec = _write_spec(control, tool="big", previews=True)
    monkeypatch.setattr(Path, "write_text", write_text)

    assert job.main(["job", str(spec)]) == 0

    monkeypatch.undo()
    assert json.loads((control / "result.json").read_text()) == {"error": "result_unwritable: OSError"}
    assert sorted(p.name for p in control.iterdir()) == ["result.json", "spec.json"]


def test_the_registered_runners() -> None:
    """Each tool task registers its own runners: ``echo`` proves the sandbox, the document tools read files."""
    assert set(RUNNERS) == {
        "echo",
        "read_document",
        "search_files",
        "run_python",
        "inspect_template",
        "fill_template",
        "write_table",
        "write_document",
        "write_presentation",
        "prepare_file",
        "convert_office",
        "measure_file",
    }


def test_the_job_reads_no_environment() -> None:
    """The spec is the job's only input: the module touches neither ``environ`` nor ``getenv``."""
    tree = ast.parse(Path(job.__file__).read_text())
    names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    names |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert not names & {"environ", "environb", "getenv", "getenvb"}
    assert not any(isinstance(n, ast.Import) and any(a.name == "os" for a in n.names) for n in ast.walk(tree))


def test_the_executor_loads_no_api_code() -> None:
    """The executor never imports the api package, or the api's dependencies come into the sandbox."""
    probe = "import sys, workbench.executor.job; print(sorted(m for m in sys.modules if m.startswith('workbench.api')))"
    done = subprocess.run([sys.executable, "-I", "-c", probe], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"
