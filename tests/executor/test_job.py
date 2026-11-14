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


def test_the_registered_runners() -> None:
    """Each tool task registers its own runners: ``echo`` proves the sandbox, the document tools read files."""
    assert set(RUNNERS) == {"echo", "read_document", "search_files"}


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
