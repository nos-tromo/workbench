"""The ``run_python`` runner: the model's code, run as ``__main__`` in the job's working folder, answered in full.

Most tests call the runner in this process, in a folder laid out as the supervisor lays out a job
(``<data>/jobs/<id>/workspace/<chat>/`` with ``out/`` below it). The tests that fork run the job entry point
as its own process, as the supervisor does.
"""

import dis
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import tracemalloc
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import CodeType, FrameType
from typing import Any

import pytest

from workbench.executor import timer
from workbench.executor.runners import JobContext
from workbench.executor.runners.python import CodeTimeout, _Capture, run_python
from workbench.protocol import JobResult, encode

JOB_ID = "0123456789abcdef0123456789abcdef"
CHAT = "chat-1"
KEEP = 8 * 1024  # half of the default 16 KB result limit


@dataclass(frozen=True)
class Job:
    """A job folder as the supervisor makes it, with the process's cwd in its chat folder.

    Attributes:
        ctx: What the job entry point gives the runner.
        data: The stand-in for the data volume.
    """

    ctx: JobContext
    data: Path

    def put(self, name: str, data: bytes) -> None:
        """Place an input file, as the supervisor copies it in."""
        (self.ctx.work_dir / name).write_bytes(data)

    def run(self, code: str, **args: Any) -> dict[str, Any]:
        """Run ``code`` through the runner, with the arguments the api sends."""
        return run_python({"code": code, **args}, self.ctx)

    def outputs(self) -> list[str]:
        """The names in ``out/``, sorted."""
        return sorted(path.name for path in self.ctx.out_dir.iterdir())


@pytest.fixture
def tmp_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Job:
    """An empty job folder for ``chat-1``, the cwd while the test runs."""
    data = tmp_path / "data"
    data.mkdir()
    data = data.resolve()
    work = data / "jobs" / JOB_ID / "workspace" / CHAT
    (work / "out").mkdir(parents=True)
    (data / "jobs" / JOB_ID / "tmp").mkdir()
    monkeypatch.chdir(work)
    return Job(JobContext(work_dir=work, out_dir=work / "out", chat_id=CHAT, max_chars=16 * 1024, file_mb=50), data)


def test_pandas_reads_an_input_and_writes_an_output(tmp_job: Job) -> None:
    """The input is in the cwd under its name; a workbook written to ``out/`` is an output, named as written."""
    tmp_job.put("q.csv", b"quarter,figure\nq1,10\nq2,32\n")
    code = """
import pandas as pd
frame = pd.read_csv("q.csv")
frame.to_excel("out/r.xlsx", index=False)
print(frame["figure"].sum())
"""
    result = tmp_job.run(code)
    assert result == {"stdout": "42\n", "stderr": "", "truncated": False}
    assert tmp_job.outputs() == ["r.xlsx"]
    import openpyxl

    sheet = openpyxl.load_workbook(tmp_job.ctx.out_dir / "r.xlsx").active
    assert sheet is not None
    assert [[cell.value for cell in row] for row in sheet.iter_rows()] == [
        ["quarter", "figure"],
        ["q1", 10],
        ["q2", 32],
    ]


def test_plot_is_saved_as_a_file(tmp_job: Job) -> None:
    """A plot is a PNG in ``out/``, drawn by the non-interactive backend; nothing of it is in the answer."""
    code = """
import matplotlib
import matplotlib.pyplot as plt
plt.plot([1, 2, 3], [3, 1, 2])
plt.savefig("out/p.png")
print(matplotlib.get_backend())
"""
    result = tmp_job.run(code)
    assert result["stderr"] == ""
    assert result["stdout"].strip().lower() == "agg"
    assert "base64" not in json.dumps(result)
    assert tmp_job.outputs() == ["p.png"]
    assert (tmp_job.ctx.out_dir / "p.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


def test_output_is_capped(tmp_job: Job) -> None:
    """Each stream keeps at most half the result limit, its start and its end, and memory stays bounded."""
    result = tmp_job.run('print("start"); print("x" * 100_000); print("end")')
    assert result["truncated"] is True
    assert len(result["stdout"]) <= KEEP
    assert len(json.dumps(result["stdout"])) <= KEEP
    assert result["stdout"].startswith("start\nxxx")
    assert result["stdout"].endswith("xxx\nend\n")
    assert "characters cut" in result["stdout"]

    tracemalloc.start()
    try:
        flood = tmp_job.run(
            'import sys\nfor _ in range(10):\n    print("x" * 10**8)\n    sys.stderr.write("y" * 10**8)'
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 400 * 2**20  # one 100 MB string at a time, not the gigabyte printed
    assert flood["truncated"] is True
    assert len(flood["stdout"]) <= KEEP
    assert len(flood["stderr"]) <= KEEP


def test_non_ascii_output_is_capped_as_it_is_written(tmp_job: Job) -> None:
    """The cap counts the escaped JSON the job writes, so characters that escape long cannot overrun it."""
    result = tmp_job.run('print("\\U0001f600" * 20_000)')
    assert result["truncated"] is True
    assert len(json.dumps(result["stdout"])) <= KEEP


def test_exceptions_return_a_traceback_tail(tmp_job: Job) -> None:
    """The traceback starts in the code, shows its lines and ends with the error; the runner's frames are not in it."""
    result = tmp_job.run('print("before")\nx = 1\n1 / 0\n')
    assert result["stdout"] == "before\n"
    assert result["stderr"].rstrip().endswith("ZeroDivisionError: division by zero")
    assert 'File "<code>", line 3, in <module>' in result["stderr"]
    assert "1 / 0" in result["stderr"]
    assert "runners" not in result["stderr"]
    assert result["truncated"] is False


def test_a_syntax_error_is_reported_with_its_line(tmp_job: Job) -> None:
    """Code that does not compile answers with the syntax error, nothing else."""
    result = tmp_job.run("total = (1 +\n")
    assert "SyntaxError" in result["stderr"]
    assert "<code>" in result["stderr"]
    assert "runners" not in result["stderr"]


@pytest.mark.parametrize(
    ("code", "noted"),
    [
        ('import sys\nprint("before")\nsys.exit(3)', "SystemExit: 3"),
        ('print("before")\nraise SystemExit', "SystemExit"),
        ('print("before")\nraise KeyboardInterrupt', "KeyboardInterrupt"),
    ],
)
def test_an_exit_or_interrupt_still_answers(tmp_job: Job, code: str, noted: str) -> None:
    """``sys.exit()`` and ``KeyboardInterrupt`` end the code, not the job: what it printed comes back, with a note."""
    result = tmp_job.run(code)
    assert result["stdout"] == "before\n"
    assert result["stderr"].rstrip().endswith(noted)


def test_a_time_limit_ends_the_code_and_keeps_what_it_printed(tmp_job: Job) -> None:
    """Code that runs past its budget is stopped where it is, and the answer says it timed out."""
    began = time.monotonic()
    result = tmp_job.run('print("working")\nwhile True:\n    pass\n', budget_s=0.5)
    assert time.monotonic() - began < 5
    assert result["stdout"] == "working\n"
    assert "timed out" in result["stderr"]
    assert "wall time" in result["stderr"]
    assert 'File "<code>", line 3' in result["stderr"] or 'File "<code>", line 2' in result["stderr"]
    assert "workbench" not in result["stderr"]


def test_the_code_runs_as_main_in_a_fresh_namespace(tmp_job: Job) -> None:
    """``__name__`` is ``__main__``, the runner's globals are out of reach, and nothing carries over between runs."""
    first = tmp_job.run(
        "import sys\n"
        "print(__name__)\n"
        "print(sorted(name for name in globals() if not name.startswith('__')))\n"
        "print(sys.modules['__main__'].__dict__ is globals())\n"
        "left_over = 1\n"
    )
    assert first["stdout"].splitlines() == ["__main__", "['sys']", "True"]
    second = tmp_job.run("print('left_over' in globals(), 'ctx' in globals(), 'run_python' in globals())")
    assert second["stdout"] == "False False False\n"


def test_a_class_defined_in_the_code_pickles(tmp_job: Job) -> None:
    """``__main__`` is the code's own module while it runs, so its classes can be pickled into an output."""
    code = """
import pickle
class Row:
    pass
with open("out/row.pkl", "wb") as file:
    pickle.dump(Row(), file)
print("pickled")
"""
    assert tmp_job.run(code) == {"stdout": "pickled\n", "stderr": "", "truncated": False}


def test_the_process_is_left_as_it_was(tmp_job: Job) -> None:
    """Streams, ``__main__``, the cwd and the module's state are back after the code, whatever it changed."""
    main = sys.modules["__main__"]
    stdout, stderr = sys.stdout, sys.stderr
    code = """
import os, sys
os.chdir("out")
log = open("log.txt", "w")
sys.stdout = log
print("into the file")
log.close()
1 / 0
"""
    assert "ZeroDivisionError" in tmp_job.run(code)["stderr"]
    assert (sys.stdout, sys.stderr, sys.modules["__main__"]) == (stdout, stderr, main)
    assert Path.cwd() == tmp_job.ctx.work_dir
    assert (tmp_job.ctx.out_dir / "log.txt").read_text() == "into the file\n"


def test_paths_in_the_answer_are_virtual(tmp_job: Job) -> None:
    """The job folder never shows: the chat folder is ``/workspace/<chat>``, the rest of the volume placeholders."""
    code = """
import os, pathlib
import pandas as pd
here = pathlib.Path.cwd()
print(os.getcwd())
print(here.parent / "chat-2" / "a.csv")
print(here.parents[1] / "tmp" / "scratch")
print(str(here.parents[3] / "workspaces" / "open-webui" / "user-a") + " and " + str(here.parents[3]))
pd.read_csv(here / "missing.csv")
"""
    result = tmp_job.run(code)
    assert result["stdout"].splitlines() == [
        "/workspace/chat-1",
        "/workspace/chat-2/a.csv",
        "<job>/tmp/scratch",
        "<data> and <data>",
    ]
    assert "/workspace/chat-1/missing.csv" in result["stderr"]
    assert str(tmp_job.data) not in json.dumps(result)
    assert JOB_ID not in json.dumps(result)


def test_a_path_that_only_starts_like_the_job_folder_is_left_alone(tmp_job: Job) -> None:
    """``chat-10`` is not ``chat-1``, and a folder that merely shares the volume's name is not on it."""
    code = (
        'import pathlib\nhere = pathlib.Path.cwd()\nprint(str(here) + "0/x")\nprint(str(here.parents[3]) + "-other/x")'
    )
    result = tmp_job.run(code)
    assert result["stdout"].splitlines() == ["/workspace/chat-10/x", f"{tmp_job.data}-other/x"]


@pytest.mark.parametrize(
    "code",
    [
        'open("/workspace/chat-2/q.csv")',
        'import pandas as pd\npd.read_csv("/workspace/chat-2/q.csv")',
        'import os\nos.listdir("/workspace/chat-2")',
    ],
)
def test_another_chats_file_gets_the_hint(tmp_job: Job, code: str) -> None:
    """Asking for a file of another chat answers with why it is not there."""
    result = tmp_job.run(code)
    assert "FileNotFoundError" in result["stderr"]
    assert "only this chat's files are available" in result["hint"].lower()


def test_a_missing_file_of_this_chat_gets_no_hint(tmp_job: Job) -> None:
    """A wrong name in the chat's own folder is the code's mistake, not a boundary."""
    result = tmp_job.run('open("/workspace/chat-1/missing.csv")')
    assert "FileNotFoundError" in result["stderr"]
    assert "hint" not in result


@pytest.mark.skipif(not Path("/workspace").is_symlink(), reason="needs the executor image's /workspace link")
def test_absolute_virtual_paths_work_where_the_image_links_them(tmp_job: Job) -> None:
    """In the executor image ``/workspace/<chat>/x`` is the job's own copy of ``x``."""
    tmp_job.put("q.csv", b"quarter,figure\nq1,10\n")
    result = tmp_job.run('import pandas as pd\nprint(pd.read_csv("/workspace/chat-1/q.csv")["figure"].sum())')
    assert result["stdout"] == "10\n"


# --- as its own process, the way the supervisor runs a job ----------------------------------------------------


def _run_job(job: Job, code: str) -> dict[str, Any]:
    """Write a spec for ``code`` into the job folder, run the job entry point there, and read its result."""
    folder = job.ctx.work_dir.parents[1]
    spec = folder / "spec.json"
    spec.write_text(
        json.dumps({"tool": "run_python", "args": {"code": code}, "chat_id": CHAT, "max_chars": 16384, "file_mb": 50})
    )
    done = subprocess.run(
        [sys.executable, "-I", "-m", "workbench.executor.job", str(spec)],
        cwd=job.ctx.work_dir,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads((folder / "result.json").read_text())


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_a_child_the_code_forks_never_answers_for_the_job(tmp_job: Job) -> None:
    """Only the job's own process writes the result: a forked child that outlives it ends without one."""
    code = """
import os, time
if os.fork() == 0:
    time.sleep(0.5)
    print("child")
    raise SystemExit
print("parent")
"""
    result = _run_job(tmp_job, code)
    assert result["stdout"] == "parent\n"
    time.sleep(1)
    assert json.loads((tmp_job.ctx.work_dir.parents[1] / "result.json").read_text()) == result


def test_the_job_answers_when_the_code_exits(tmp_job: Job) -> None:
    """End to end: ``sys.exit`` in the code still leaves a well-formed result."""
    result = _run_job(tmp_job, 'import sys\nprint("done")\nsys.exit(0)')
    assert result["stdout"] == "done\n"
    assert result["stderr"].rstrip().endswith("SystemExit: 0")


def test_a_lone_surrogate_in_the_output_still_reaches_the_api(tmp_job: Job) -> None:
    """Halves of a surrogate pair printed apart are joined, a lone one is replaced: the result can be sent on."""
    result = tmp_job.run('print("\\ud800 and \\ud83d\\ude00")')
    assert result["stdout"] == "� and \U0001f600\n"
    line = encode(JobResult(job_id=JOB_ID, status="ok", result=result, outputs=[]))
    assert JobResult.model_validate_json(line).result == result


def test_many_small_writes_keep_the_start_and_the_end(tmp_job: Job) -> None:
    """A hundred thousand short lines: the first and the last come back, whole, around the mark."""
    result = tmp_job.run("for n in range(100_000):\n    print(n)")
    lines = result["stdout"].splitlines()
    assert lines[:3] == ["0", "1", "2"]
    assert lines[-2:] == ["99998", "99999"]
    assert result["truncated"] is True
    assert len(json.dumps(result["stdout"])) <= KEEP


# --- no real path survives a cut, wherever it falls -------------------------------------------------------------

PRINT_A_PATH = 'import pathlib\np = str(pathlib.Path("f.csv").resolve())\n'
VIRTUAL = f"/workspace/{CHAT}/f.csv"
MARK = re.compile(r"\n\[… (\d+) characters cut …\]\n")


def _fragments(text: str) -> list[str]:
    """The words of ``text`` that hold a path and are not ``VIRTUAL`` or a piece of it a cut left.

    A cut of the virtual path leaves its start or its end; anything else with a ``/`` in it is the job's real
    path, whole or in part.
    """
    return [word for word in text.split() if "/" in word and not (VIRTUAL.startswith(word) or VIRTUAL.endswith(word))]


def test_a_path_across_the_split_of_a_stream_below_twice_the_cap_is_rewritten(tmp_job: Job) -> None:
    """A stream that each end of the capture holds in part is kept whole; a real path across their split is rewritten.

    Each end keeps the cap and 4096 characters more (``KEEP + 4096``), so that is where a stream shorter than twice
    it is split in two. Every line puts the path across that split: rewriting the two halves apart, as the runner once
    did, leaves the real path in the answer.
    """
    real = str(tmp_job.ctx.work_dir / "f.csv")
    split = KEEP + 4096
    for start in range(split - len(real) + 1, split):
        assert start + len(real) + 102 < 2 * split  # the stream stays below twice the split: nothing is dropped
        line = f'print("x" * {start - 1} + " " + p + " " + "x" * 100)'
        stdout = tmp_job.run(PRINT_A_PATH + line)["stdout"]
        assert _fragments(stdout) == [], start
        assert VIRTUAL in stdout, start


@pytest.mark.parametrize("side", ["start", "end"])
def test_a_path_at_either_cut_edge_of_a_long_line_never_leaves_a_fragment(tmp_job: Job, side: str) -> None:
    """One line far over the cap, a real path at every place near where the start or the end is cut."""
    real = str(tmp_job.ctx.work_dir / "f.csv")
    for at in range(KEEP // 2 - len(real), KEEP + 4096 + len(real), 41):
        line = (
            f'print("x" * {at} + " " + p + " " + "x" * 60_000)'
            if side == "start"
            else f'print("x" * 60_000 + " " + p + " " + "x" * {at})'
        )
        result = tmp_job.run(PRINT_A_PATH + line)
        assert result["truncated"] is True
        assert _fragments(result["stdout"]) == [], at


def test_paths_packed_up_to_the_cut_never_leave_a_fragment(tmp_job: Job) -> None:
    """Rewriting shortens what is kept, so the text near a cut is shown; a path the cut went through is not in it."""
    real = str(tmp_job.ctx.work_dir / "f.csv")
    for lead in range(0, len(real) + 1, 3):
        line = f'print("x" * {lead} + " " + " ".join([p] * 400) + " " + "x" * {lead})'
        result = tmp_job.run(PRINT_A_PATH + line)
        assert result["truncated"] is True
        assert _fragments(result["stdout"]) == [], lead
        assert str(tmp_job.data) not in result["stdout"], lead


# --- writes from threads -----------------------------------------------------------------------------------------


def test_writes_from_many_threads_at_once_keep_exact_counts() -> None:
    """Every write is whole: the head and the tail are full, and what is dropped is counted to the character."""
    capture = _Capture(4096)
    errors: list[BaseException] = []

    def chatter() -> None:
        try:
            for _ in range(10_000):
                capture.write("abcd\n")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=chatter) for _ in range(8)]
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(interval)
    head, tail, dropped = capture.parts()
    assert errors == []
    assert capture.written == 8 * 10_000 * 5
    assert (len(head), len(tail)) == (4096, 4096)
    assert dropped == capture.written - 8192
    assert set(head + tail) == set("abcd\n")


def test_threads_printing_at_once_through_the_runner(tmp_job: Job) -> None:
    """Code that writes from eight threads gets no error from the capture, and the mark counts what is cut."""
    code = """
import sys, threading
sys.setswitchinterval(1e-6)
def chatter():
    for _ in range(20_000):
        sys.stdout.write("abcd\\n")
threads = [threading.Thread(target=chatter) for _ in range(8)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
"""
    interval = sys.getswitchinterval()
    try:
        result = tmp_job.run(code)
    finally:
        sys.setswitchinterval(interval)
    assert result["stderr"] == ""
    (cut,) = MARK.findall(result["stdout"])
    kept = MARK.sub("", result["stdout"])
    assert int(cut) + len(kept) == 8 * 20_000 * 5


# --- a signal in the middle of a write ----------------------------------------------------------------------------


def _dropping_code() -> tuple[CodeType, int]:
    """Find the capture's function that drops pieces off the end it keeps, and the instruction just after a drop."""
    for function in vars(_Capture).values():
        code = getattr(function, "__code__", None)
        if not isinstance(code, CodeType) or "popleft" not in code.co_names:
            continue
        instructions = list(dis.get_instructions(code))
        for index, instruction in enumerate(instructions):
            if instruction.opname == "LOAD_ATTR" and instruction.argval == "popleft":
                call = next(i for i in range(index + 1, len(instructions)) if instructions[i].opname == "CALL")
                return code, instructions[call + 1].offset
    pytest.fail("no function of the capture drops pieces off its tail")


@contextmanager
def after_a_drop(action: Callable[[FrameType], None], when: Callable[[], bool] = lambda: True) -> Iterator[list[int]]:
    """Run ``action`` once, just after the capture has dropped a piece off its tail, as a signal handled there would.

    ``action`` gets the frame of the capture's code; it runs the first time a drop happens while ``when()`` holds.

    Yields:
        A list that holds one entry once the action has run.
    """
    code, offset = _dropping_code()
    monitoring = sys.monitoring
    tool = next((t for t in (3, 4) if monitoring.get_tool(t) is None), None)
    if tool is None:
        pytest.skip("no free sys.monitoring tool id")
    ran: list[int] = []

    def step(_code: CodeType, at: int) -> None:
        if at == offset and not ran and when():
            ran.append(at)
            action(sys._getframe(1))

    monitoring.use_tool_id(tool, "test_run_python")
    try:
        monitoring.register_callback(tool, monitoring.events.INSTRUCTION, step)
        monitoring.set_local_events(tool, code, monitoring.events.INSTRUCTION)
        yield ran
    finally:
        monitoring.set_local_events(tool, code, 0)
        monitoring.register_callback(tool, monitoring.events.INSTRUCTION, None)
        monitoring.free_tool_id(tool)


@contextmanager
def alarm_held() -> Iterator[None]:
    """Hold ``SIGALRM`` back in this thread, so that the only place a deadline is handled is where a test lands it."""
    before = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, before)


def _counts_are_exact(capture: _Capture) -> None:
    """The sizes the capture counts are the sizes of what it holds."""
    assert capture._head_size == sum(map(len, capture._head))
    assert capture._tail_size == sum(map(len, capture._tail))


def test_a_timeout_just_after_a_drop_waits_until_the_write_is_whole() -> None:
    """A deadline handled just after a piece left the tail used to leave that piece counted in the tail's size.

    The next write then dropped pieces that were not there and raised ``IndexError``, so that the runner's own note
    that the code timed out ended as "the tool failed". The error now waits for the end of the write and comes
    just after it.
    """
    capture = _Capture(8)
    capture.write("a" * 20)
    whole = False
    with alarm_held(), pytest.raises(CodeTimeout):
        with timer.limit_until(time.monotonic() + 0.01, CodeTimeout):
            time.sleep(0.02)  # the deadline passes with its signal held back: the landing below is the only one
            with after_a_drop(lambda frame: timer._fire(signal.SIGALRM, frame)) as ran:
                capture.write("b" * 20)
            whole = True
            signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})
            time.sleep(2)  # the error comes now, once the signal is let through
    assert ran
    assert whole
    _counts_are_exact(capture)
    capture.write("c" * 3)  # the write that used to raise IndexError
    assert capture.parts() == ("a" * 8, "bbbbbccc", 43 - 16)
    _counts_are_exact(capture)


def test_a_signal_handler_that_prints_in_the_middle_of_a_write_follows_it() -> None:
    """A handler of the code's that prints while a write is half done: its text comes after that write's, counted."""
    capture = _Capture(8)
    capture.write("a" * 20)
    with after_a_drop(lambda _frame: print("handler", end="", file=capture)) as ran:
        capture.write("b" * 20)
    assert ran
    _counts_are_exact(capture)
    assert capture.written == 47
    assert capture.parts() == ("a" * 8, "bhandler", 47 - 16)


def test_a_timeout_in_the_middle_of_a_write_of_the_code_still_answers_that_it_timed_out(tmp_job: Job) -> None:
    """The deadline lands just after a drop in code that writes long lines: the answer is the code's timeout."""
    began = time.monotonic()

    def let_it_through(_frame: FrameType) -> None:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGALRM})  # the deadline's signal is handled here

    code = (
        "import sys, time\nend = time.monotonic() + 5\nwhile time.monotonic() < end:\n    sys.stderr.write('y' * 9**5)"
    )
    with alarm_held(), after_a_drop(let_it_through, when=lambda: time.monotonic() > began + 0.3) as ran:
        result = tmp_job.run(code, budget_s=0.2)
    assert ran
    assert result["truncated"] is True
    assert (
        result["stderr"]
        .rstrip()
        .endswith("timed out: the code was stopped after 0.2 s, its share of the job's wall time")
    )
