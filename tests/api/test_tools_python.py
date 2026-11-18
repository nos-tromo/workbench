"""The ``run_python`` tool on the MCP door, as Ghostwriter calls it, over a scripted executor.

The runner itself is tested in ``tests/executor/test_run_python.py`` and the real sandbox in
``tests/isolation``; here the executor answers what a test tells it to, and the api's part is checked:
which files go to the job, what is recorded, how outputs reach the answer, what is capped, and what the
model is told when the job could not finish.
"""

import asyncio
import contextlib
import dataclasses
import inspect
import io
import itertools
import json
from collections.abc import Callable, Coroutine, Sequence
from typing import Any, override

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from tests.api.conftest import CHAT, HG, PG, PG_SETUP, AuditLines, put, tick, unrecorded
from tests.api.mcp_calls import Headers, acall, answered, refusal, refused, result, rpc, sent_size, unavailable
from tests.api.test_tools_documents import Scripted
from workbench.api import tools, tools_output, tools_templates
from workbench.api.audit import AuditLog
from workbench.api.jobs import CallLimits, JobQueue, Keeper
from workbench.api.main import create_app
from workbench.api.metrics import Metrics
from workbench.api.store import Store
from workbench.config import Settings
from workbench.models import FileRecord, JobRecord, Principal
from workbench.protocol import JobResult


def ran(stdout: str = "", stderr: str = "", *, truncated: bool = False, **extra: Any) -> dict[str, Any]:
    """The runner's answer for code that printed ``stdout`` and ``stderr``."""
    return {"stdout": stdout, "stderr": stderr, "truncated": truncated, **extra}


@pytest.fixture
def fake_executor(settings: Settings) -> Scripted:
    """An executor that answers every job as code that printed nothing."""
    executor = Scripted(settings.data_dir)
    executor.payload = ran()
    return executor


@pytest.fixture
def app(settings: Settings, store: Store, audit: AuditLog, fake_executor: Scripted) -> FastAPI:
    """The real app with a job queue over the scripted executor."""
    queue = JobQueue(settings, fake_executor, store, Metrics(settings.data_dir))
    return create_app(settings, store=store, audit=audit, jobs=queue)


@pytest.fixture
def small(settings: Settings, store: Store, audit: AuditLog, fake_executor: Scripted) -> TestClient:
    """A client on an app whose answers may be 1 KB."""
    limited = dataclasses.replace(settings, result_max_kb=1)
    queue = JobQueue(limited, fake_executor, store, Metrics(settings.data_dir))
    return TestClient(create_app(limited, store=store, audit=audit, jobs=queue))


def run(client: TestClient, code: str = "print(1)", headers: Headers | None = None) -> httpx2.Response:
    """Call ``run_python`` as Ghostwriter does, on the MCP door."""
    return acall(client, "run_python", {"code": code}, **({} if headers is None else {"headers": headers}))


def outputs(store: Store) -> list[str]:
    """The names of the files tools made in Ghostwriter's chat."""
    return [rec.name for rec in store.files_in_chat(PG) if rec.kind == "output"]


def events(audit_lines: AuditLines) -> list[dict[str, Any]]:
    """The audit events of ``run_python``."""
    return [e for e in audit_lines() if e["operation"] == "tool.run_python"]


# --- what goes to the job, and what comes back ------------------------------------------------------------------


def test_a_run_records_combine_with_inputs_and_outputs(
    client: TestClient, store: Store, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """One ``combine`` event names every input and every output by id; the answer lists the outputs' paths."""
    a = put(store, "q.csv", b"quarter,figure\nq1,10\n", p=PG)
    b = put(store, "notes.txt", p=PG)
    fake_executor.files = {"p.png": b"\x89PNG", "r.xlsx": b"PK"}
    fake_executor.payload = ran("42\n")
    answer = answered(run(client, 'import pandas as pd\nprint(pd.read_csv("q.csv")["figure"].sum() + 32)'))
    (req,) = fake_executor.requests
    outputs = [rec for rec in store.files_in_chat(PG) if rec.kind == "output"]
    assert sorted(rec.name for rec in outputs) == ["p.png", "r.xlsx"]
    assert {key: value for key, value in answer.items() if key != "outputs"} == {
        "stdout": "42\n",
        "stderr": "",
        "files": [f"{CHAT}/p.png", f"{CHAT}/r.xlsx"],
        "truncated": False,
        "job_id": req.job_id,
        "hint": None,
    }
    (event,) = events(audit_lines)
    assert event["action"] == "combine"
    assert event["outcome"] == "ok"
    assert sorted(event["target"]["ids"]) == sorted([a.id, b.id, *(rec.id for rec in outputs)])
    assert event["target"]["count"] == 4
    assert event["target"]["container"] == "chat:chat-1"
    assert event["criteria"] == {"caller": "ghostwriter", "outputs": 2}
    raw = json.dumps(audit_lines())
    assert "pandas" not in raw
    assert "q.csv" not in raw


def test_every_file_of_the_chat_goes_to_the_job_newest_first(
    client: TestClient, store: Store, fake_executor: Scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All of this chat's files, in folders too, and nothing of another chat, user or caller; the code as it was sent.

    The code gets 9/10 of the job's wall less the five seconds the previews keep: (120 - 5) * 9 // 10.
    """
    tick(store, monkeypatch)
    put(store, "old.txt", p=PG)
    store.mkdir(PG, f"{CHAT}/reports")
    put(store, "q1.txt", directory=f"{CHAT}/reports", p=PG_SETUP)
    put(store, "new.docx", p=PG)
    put(store, "elsewhere.txt", directory="/workspace/chat-2", p=Principal("ghostwriter", "user-a", "chat-2"))
    theirs = Principal("ghostwriter", "user-b", "chat-1")
    store.add_upload(theirs, CHAT, "theirs.txt", io.BytesIO(b"x"), max_bytes=10, quota_bytes=100, record=unrecorded)
    put(store, "open-webuis.txt")
    answered(run(client, "print('hello')"))
    (req,) = fake_executor.requests
    assert req.tool == "run_python"
    assert req.input_files == ["new.docx", "reports/q1.txt", "old.txt"]
    assert req.args == {"code": "print('hello')", "budget_s": 103}
    assert (req.caller, req.user_id, req.chat_id) == ("ghostwriter", "user-a", "chat-1")
    assert req.max_chars == 16 * 1024


def test_the_job_id_is_the_job_record(client: TestClient, store: Store, fake_executor: Scripted) -> None:
    """The model cites ``job_id`` as the source of what the code computed; it is the job the store recorded."""
    job_id = answered(run(client))["job_id"]
    record = store.get_job(PG, job_id)
    assert (record.tool, record.status) == ("run_python", "ok")


def test_a_chat_without_files_still_runs(client: TestClient, fake_executor: Scripted, audit_lines: AuditLines) -> None:
    """Code that needs no input runs; the event names no file."""
    fake_executor.payload = ran("2\n")
    assert answered(run(client, "print(1 + 1)"))["stdout"] == "2\n"
    assert fake_executor.requests[0].input_files == []
    (event,) = events(audit_lines)
    assert "ids" not in event["target"]
    assert event["target"]["count"] == 0
    assert event["criteria"] == {"caller": "ghostwriter", "outputs": 0}


def test_files_in_a_folder_named_out_are_not_given_to_the_code(
    client: TestClient, store: Store, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """``out/`` is where the code writes; the executor never copies a file below it, so it is not sent or recorded."""
    store.mkdir(PG, f"{CHAT}/out")
    hidden = put(store, "kept.csv", directory=f"{CHAT}/out", p=PG_SETUP)
    shown = put(store, "a.csv", p=PG)
    body = answered(run(client))
    assert fake_executor.requests[0].input_files == ["a.csv"]
    assert "out/" in body["hint"]
    (event,) = events(audit_lines)
    assert event["target"]["ids"] == [shown.id]
    assert hidden.id not in json.dumps(audit_lines())


def test_over_1000_files_the_newest_1000_go_and_the_answer_says_so(
    client: TestClient, store: Store, fake_executor: Scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job protocol takes 1000 inputs; the oldest files are left out, and the answer says how many."""
    clock = itertools.count(1_000_000)
    monkeypatch.setattr(store, "_clock", lambda: float(next(clock)))
    for n in range(1005):
        put(store, f"f{n:04d}.txt", p=PG)
    body = answered(run(client))
    (req,) = fake_executor.requests
    assert len(req.input_files) == 1000
    assert (req.input_files[0], req.input_files[-1]) == ("f1004.txt", "f0005.txt")
    assert "1000 newest of 1005 files" in body["hint"]


def test_the_files_are_the_stores_paths(client: TestClient, store: Store, fake_executor: Scripted) -> None:
    """An output whose name is taken gets a number, and the answer names the file as the store keeps it."""
    put(store, "r.xlsx", p=PG)
    fake_executor.files = {"r.xlsx": b"new"}
    fake_executor.payload = ran(files=["/data/jobs/0123/workspace/chat-1/out/r.xlsx"])
    assert answered(run(client))["files"] == [f"{CHAT}/r (1).xlsx"]


def test_the_executors_note_on_outputs_is_passed_on(client: TestClient, fake_executor: Scripted) -> None:
    """Outputs the executor dropped are not silent: its message is in the hint."""
    fake_executor.message = "dropped 1 output: not the job's own file"
    body = answered(run(client))
    assert "dropped 1 output: not the job's own file" in body["hint"]


def test_the_runners_hint_is_passed_on(client: TestClient, fake_executor: Scripted) -> None:
    """The runner's hint about another chat's file reaches the model."""
    fake_executor.payload = ran(stderr="FileNotFoundError: x", hint="Only this chat's files are available.")
    assert answered(run(client))["hint"].startswith("Only this chat's files are available.")


def test_outputs_the_executor_refused_still_answer_with_what_the_code_printed(
    client: TestClient, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """Too many outputs keeps none of them; the code ran, so the model sees its output and why nothing was kept."""
    fake_executor.status = "error"
    fake_executor.message = "too many outputs: at most 20"
    fake_executor.payload = ran("made 30 files\n")
    body = answered(run(client))
    assert (body["stdout"], body["files"], body["outputs"]) == ("made 30 files\n", [], [])
    assert "too many outputs: at most 20" in body["hint"]
    (event,) = events(audit_lines)
    assert (event["outcome"], event["criteria"]["outputs"]) == ("error", 0)


@pytest.mark.parametrize(
    ("status", "message", "hint"),
    [
        (
            "ok",
            "dropped 1 output: not the job's own file",
            "Not every file in out/ was kept: dropped 1 output: not the job's own file.",
        ),
        ("error", "too many outputs: at most 20", "No file in out/ was kept: too many outputs: at most 20."),
        ("error", "", "No file in out/ was kept: the outputs could not be kept."),
    ],
)
def test_the_note_on_the_outputs_keeps_its_words(
    client: TestClient, fake_executor: Scripted, status: str, message: str, hint: str
) -> None:
    """The executor's note on the outputs reaches the model in the words it always had, byte for byte."""
    fake_executor.status = status
    fake_executor.message = message
    fake_executor.payload = ran("made files\n")
    assert answered(run(client))["hint"] == hint


@pytest.mark.parametrize(
    ("status", "message", "said"),
    [
        ("timeout", "the job ran past its wall time of 120 s", "wall time"),
        ("killed", "the job was stopped on a resource limit", "memory"),
    ],
)
def test_a_job_stopped_on_a_limit_is_not_completed_and_says_why(
    client: TestClient,
    store: Store,
    fake_executor: Scripted,
    audit_lines: AuditLines,
    status: str,
    message: str,
    said: str,
) -> None:
    """Nothing came back but the reason; the run is recorded all the same, as an error."""
    a = put(store, "q.csv", p=PG)
    fake_executor.status = status
    fake_executor.message = message
    reason, detail = refusal(run(client))
    assert reason == "not_completed"
    assert detail.startswith(f"the code could not be completed: {message}")
    assert said in detail
    (event,) = events(audit_lines)
    assert (event["outcome"], event["target"]["ids"], event["criteria"]["outputs"]) == ("error", [a.id], 0)


@pytest.mark.parametrize(
    ("error", "detail"),
    [
        ("read_timeout: the work ran past its time limit", "read_timeout: the work ran past its time limit"),
        ("KeyError: '/data/jobs/0123/spec.json'", "the tool failed"),
    ],
)
def test_a_runner_failure_is_its_code_or_a_fixed_text(
    client: TestClient, fake_executor: Scripted, error: str, detail: str
) -> None:
    """A failure the api knows is told; any other is a fixed text, since its own may hold a path."""
    fake_executor.status = "error"
    fake_executor.payload = {"error": error}
    assert refusal(run(client)) == ("refused", detail)


@pytest.mark.parametrize("message", ["the job failed", "the job left no readable result"])
def test_a_job_that_left_no_result_is_not_completed_with_the_executors_reason(
    client: TestClient, fake_executor: Scripted, message: str
) -> None:
    """Code that ended its own process or crashed it leaves no answer; the executor's fixed text says so."""
    fake_executor.status = "error"
    fake_executor.message = message
    fake_executor.payload = {}
    r = run(client, "import os\nos._exit(1)")
    assert refusal(r) == ("not_completed", f"the code could not be completed: {message}")


@pytest.mark.parametrize(
    "answer", [{}, {"stdout": 1, "stderr": "", "truncated": False}, {"stdout": "", "stderr": ""}, []]
)
def test_an_unreadable_job_answer_is_a_fixed_error(client: TestClient, fake_executor: Scripted, answer: Any) -> None:
    """The api does not forward what it cannot validate."""
    fake_executor.payload = answer
    assert refusal(run(client)) == ("refused", "the tool returned an answer that could not be read")


def test_a_busy_executor_is_executor_busy_and_nothing_is_recorded(
    client: TestClient, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """A job that never ran is a refusal to retry, and no event."""
    fake_executor.status = "busy"
    assert refusal(run(client)) == ("executor_busy", "busy, retry later")
    assert events(audit_lines) == []


def test_outputs_whose_use_cannot_be_recorded_are_removed(
    client: TestClient, store: Store, fake_executor: Scripted, broken_audit: None
) -> None:
    """As with an upload: a file that could not be recorded does not stay."""
    fake_executor.files = {"r.xlsx": b"PK"}
    unavailable(run(client))
    assert outputs(store) == []


def test_outputs_whose_id_continuation_cannot_be_recorded_are_removed(
    client: TestClient,
    store: Store,
    fake_executor: Scripted,
    audit_stream: io.StringIO,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run over 205 files names them in two events; when the second cannot be written, the run fails the same way."""
    for n in range(205):
        put(store, f"f{n:03d}.txt", p=PG)
    fake_executor.files = {"r.xlsx": b"PK"}
    write = audit_stream.write

    def no_continuation(text: str, /) -> int:
        if '.ids"' in text:
            raise OSError("synthetic audit stream failure")
        return write(text)

    monkeypatch.setattr(audit_stream, "write", no_continuation)
    unavailable(run(client))
    assert outputs(store) == []
    assert [e["target"]["count"] for e in events(audit_lines)] == [206]


def test_outputs_of_an_answer_that_cannot_be_read_are_removed_and_the_run_is_an_error(
    client: TestClient, store: Store, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """The model is told the tool failed and never learns of the files, so none of them stays."""
    a = put(store, "q.csv", p=PG)
    fake_executor.files = {"r.xlsx": b"PK"}
    fake_executor.payload = {"stdout": 1}
    assert refusal(run(client)) == ("refused", "the tool returned an answer that could not be read")
    assert outputs(store) == []
    (event,) = events(audit_lines)
    assert (event["outcome"], event["target"]["ids"], event["criteria"]["outputs"]) == ("error", [a.id], 0)


def test_an_output_of_an_answer_that_cannot_be_read_never_becomes_a_file(
    client: TestClient,
    store: Store,
    fake_executor: Scripted,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The run is recorded first, as an error that keeps no file, so its output is never moved into the chat."""
    fake_executor.files = {"r.xlsx": b"PK"}
    fake_executor.payload = {"stdout": 1}
    moved: list[str] = []
    monkeypatch.setattr(store, "register_output", lambda *args, **_: moved.append(args[2]))
    assert refusal(run(client)) == ("refused", "the tool returned an answer that could not be read")
    assert moved == []
    (event,) = events(audit_lines)
    assert (event["outcome"], event["target"].get("ids"), event["criteria"]["outputs"]) == ("error", None, 0)


def test_the_run_is_recorded_before_its_outputs_are_files_and_names_them(
    client: TestClient,
    store: Store,
    fake_executor: Scripted,
    audit_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the event is written, no output of the run is a file of the chat yet; then each is, by the id it names."""
    fake_executor.files = {"r.xlsx": b"PK", "chart.png": b"png"}
    seen: list[list[str]] = []
    write = audit_stream.write

    def look(text: str, /) -> int:
        if '"tool.run_python"' in text:
            seen.append([rec.name for rec in store.files_in_chat(PG)])
        return write(text)

    monkeypatch.setattr(audit_stream, "write", look)
    answered(run(client))
    assert seen == [[]]
    (event,) = [json.loads(line) for line in audit_stream.getvalue().splitlines() if "tool.run_python" in line]
    assert sorted(event["target"]["ids"]) == sorted(rec.id for rec in store.files_in_chat(PG))


def test_an_output_over_the_users_quota_is_not_kept_and_is_recorded_again(
    settings: Settings, store: Store, audit: AuditLog, audit_lines: AuditLines, fake_executor: Scripted
) -> None:
    """Outputs count against the workspace quota: one that does not fit is refused, named again by an error event."""
    small = dataclasses.replace(settings, workspace_max_mb=1)
    put(store, "q.csv", b"x" * (1024 * 1024 - 10), p=PG)
    fake_executor.files = {"fits.txt": b"12345", "too-big.bin": b"y" * 100}
    app = create_app(
        small, store=store, audit=audit, jobs=JobQueue(small, fake_executor, store, Metrics(small.data_dir))
    )
    with TestClient(app) as tight:
        body = answered(run(tight))
    assert body["files"] == [f"{CHAT}/fits.txt"]
    assert [output["name"] for output in body["outputs"]] == ["fits.txt"]
    assert "1 output could not be kept (quota)" in body["hint"]
    first, again = events(audit_lines)
    kept = {rec.name: rec.id for rec in store.files_in_chat(PG)}
    assert (first["outcome"], first["criteria"]["outputs"]) == ("ok", 2)
    assert kept["fits.txt"] in first["target"]["ids"]
    (lost,) = set(first["target"]["ids"]) - set(kept.values())
    assert (again["outcome"], again["target"]["ids"], again["criteria"]["outputs"]) == ("error", [lost], 1)


class CancelledOnReturn(JobQueue):
    """A queue whose caller is cancelled as ``submit`` returns, as when the client goes away just then."""

    @override
    async def submit(
        self,
        p: Principal,
        tool: str,
        args: dict[str, Any],
        input_files: Sequence[str],
        keeper: Keeper | None = None,
        *,
        call: CallLimits | None = None,
        previews: bool = False,
    ) -> tuple[JobRecord, JobResult, list[FileRecord]]:
        """Run the job, keep its files, then cancel the task that asked for it."""
        answer = await super().submit(p, tool, args, input_files, keeper, call=call, previews=previews)
        caller = asyncio.current_task()
        assert caller is not None
        caller.cancel()
        return answer


#: Each tool that makes files: its function, its arguments and an answer of its runner that keeps the file.
MAKERS: dict[str, tuple[Callable[..., Coroutine[Any, Any, Any]], BaseModel, dict[str, Any]]] = {
    "run_python": (tools.run_python, tools.RunPythonRequest(code="print(1)"), ran()),
    "fill_template": (
        tools_templates.fill_template,
        tools_templates.FillTemplateRequest.model_validate(
            {"path": f"{CHAT}/form.docx", "values": {"customer": {"value": "x", "source": {"kind": "assistant"}}}}
        ),
        {"filled": ["customer"], "unfilled": [], "warnings": []},
    ),
    "write_table": (
        tools_output.write_table,
        tools_output.WriteTableRequest(name="t", format="csv", columns=["a"], rows=[["1"]]),
        {},
    ),
    "write_document": (
        tools_output.write_document,
        tools_output.WriteDocumentRequest(name="r", format="md", markdown="# r"),
        {},
    ),
}


@pytest.mark.parametrize("tool", list(MAKERS))
def test_a_request_cancelled_as_its_job_returns_leaves_no_output_unrecorded(
    settings: Settings, store: Store, audit: AuditLog, audit_lines: AuditLines, tool: str
) -> None:
    """The job's file is recorded before it is in the workspace: a cancellation as ``submit`` returns finds it named."""
    function, body, payload = MAKERS[tool]
    put(store, "form.docx", b"PK", p=PG)
    executor = Scripted(settings.data_dir)
    executor.files = {"made.docx": b"PK"}
    executor.payload = payload
    queue = CancelledOnReturn(settings, executor, store, Metrics(settings.data_dir))
    audit.start({})

    async def call() -> None:
        given = {"body": body, "p": PG, "settings": settings, "store": store, "audit": audit, "jobs": queue}
        await function(**{name: given[name] for name in inspect.signature(function).parameters})

    with contextlib.suppress(asyncio.CancelledError):
        asyncio.run(call())
    made = [rec for rec in store.files_in_chat(PG) if rec.kind == "output"]
    named = {
        i for event in audit_lines() if event["operation"] == f"tool.{tool}" for i in event["target"].get("ids", [])
    }
    assert [rec.name for rec in made] == ["made.docx"]
    assert {rec.id for rec in made} <= named


def test_a_long_hint_from_the_runner_is_cut_never_a_500(client: TestClient, fake_executor: Scripted) -> None:
    """The runner's hint at its cap and the executor's note together are longer than a hint may be: cut to it."""
    fake_executor.message = "dropped 1 output: not the job's own file"
    fake_executor.payload = ran("ok\n", hint="h" * 2000)
    body = answered(run(client))
    assert body["stdout"] == "ok\n"
    assert body["hint"] == "h" * 2000


# --- the request -----------------------------------------------------------------------------------------------


def test_a_lone_surrogate_in_an_argument_reaches_the_job_as_a_replacement_character(
    client: TestClient, fake_executor: Scripted
) -> None:
    """JSON may carry the escape of a lone surrogate, which UTF-8 cannot hold: the job gets U+FFFD, never a 500."""
    r = rpc(client, "tools/call", {"name": "run_python", "arguments": {"code": 'print("a\ud800b")'}}, headers=HG)
    assert "\\ud800" in r.request.content.decode()
    answered(r)
    assert fake_executor.requests[0].args["code"] == 'print("a\ufffdb")'


def test_code_of_up_to_50000_characters_is_taken(client: TestClient, fake_executor: Scripted) -> None:
    """The limit is the request's; one character more is refused before anything runs."""
    answered(run(client, "#" * 50_000))
    assert refused(run(client, "#" * 50_001))["reason"] == "invalid_arguments"
    assert len(fake_executor.requests) == 1


@pytest.mark.parametrize("arguments", [{}, {"code": ""}, {"code": 1}, {"code": None}, {"code": ["print(1)"]}])
def test_arguments_that_are_not_valid_are_invalid_arguments(
    client: TestClient, fake_executor: Scripted, arguments: dict[str, Any]
) -> None:
    """The model's mistakes are told at once, and nothing runs."""
    assert refused(acall(client, "run_python", arguments))["reason"] == "invalid_arguments"
    assert fake_executor.requests == []


def test_run_python_is_listed_with_a_typed_code(client: TestClient) -> None:
    """A tool with its name, a description for the model, and one typed, bounded field."""
    tools = {t["name"]: t for t in result(rpc(client, "tools/list", headers=HG))["tools"]}
    tool = tools["run_python"]
    description = tool["description"]
    for phrase in ("out/", "thread", "multiprocessing", "job_id", "as a card in the chat"):
        assert phrase in description, phrase
    assert "Args:" not in description
    schema = tool["inputSchema"]
    assert schema["required"] == ["code"]
    code = schema["properties"]["code"]
    assert (code["type"], code["maxLength"], code["minLength"]) == ("string", 50_000, 1)
    assert code["description"].strip()


# --- the cap ---------------------------------------------------------------------------------------------------


def test_the_answer_is_cut_at_the_result_limit_keeping_both_ends(
    client: TestClient, store: Store, fake_executor: Scripted
) -> None:
    """Each stream as the runner may send it, together over the limit: both are cut in the middle, the error kept."""
    fake_executor.files = {f"chart-{n:02d}.png": b"\x89PNG" for n in range(20)}
    trace = "Traceback (most recent call last):\n" + "  frame\n" * 1000 + "ZeroDivisionError: division by zero\n"
    fake_executor.payload = ran("first line\n" + "o" * 8000 + "\nlast line\n", trace[-8192:], truncated=True)
    body = answered(run(client))
    assert sent_size(body) <= 16 * 1024
    assert body["truncated"] is True
    assert len(body["files"]) == 20
    assert body["stdout"].startswith("first line\n")
    assert body["stdout"].endswith("last line\n")
    assert body["stderr"].endswith("ZeroDivisionError: division by zero\n")
    assert "characters cut" in body["stdout"] + body["stderr"]


def test_an_answer_within_the_limit_is_not_cut(client: TestClient, fake_executor: Scripted) -> None:
    """Short streams pass as they are."""
    fake_executor.payload = ran("a\n", "b\n")
    body = answered(run(client))
    assert (body["stdout"], body["stderr"], body["truncated"]) == ("a\n", "b\n", False)


def test_the_cap_counts_bytes_of_the_json_not_characters(small: TestClient, fake_executor: Scripted) -> None:
    """Text that takes more than a byte a character is cut to the limit as sent."""
    fake_executor.payload = ran("ä€😀" * 400, "ü" * 400)
    with small:
        body = answered(run(small))
    assert sent_size(body) <= 1024
    assert body["truncated"] is True


# --- refusals are recorded (R72) ---------------------------------------------------------------------------------


def test_a_run_python_refusal_is_recorded_as_an_error_by_its_code_alone(
    client: TestClient, store: Store, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """A runner refusal of ``run_python`` is an ``error`` event with its code, never its text."""
    a = put(store, "q.csv", p=PG)
    fake_executor.status = "error"
    fake_executor.payload = {"error": "read_timeout: the work on 'salaries-of-user-b.csv' ran past its time limit"}
    assert refused(run(client))["reason"] == "refused"
    (event,) = events(audit_lines)
    assert (event["outcome"], event["target"]["ids"]) == ("error", [a.id])
    assert event["criteria"] == {"caller": "ghostwriter", "outputs": 0, "refused": "read_timeout"}
    assert "salaries" not in json.dumps(audit_lines())


def test_a_job_the_executor_refused_for_a_full_volume_is_told_and_recorded_as_a_refusal(
    client: TestClient, store: Store, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """R84: the executor's free-space floor stops or refuses the job; the model is told the volume is full."""
    a = put(store, "q.csv", p=PG)
    fake_executor.status = "error"
    fake_executor.payload = {"error": "storage_full: storage is full"}
    assert refusal(run(client)) == ("refused", "storage_full: storage is full")
    (event,) = events(audit_lines)
    assert (event["outcome"], event["target"]["ids"]) == ("error", [a.id])
    assert event["criteria"] == {"caller": "ghostwriter", "outputs": 0, "refused": "storage_full"}


def test_a_job_stopped_for_shared_memory_outside_any_jobs_count_is_told_and_recorded_as_a_refusal(
    client: TestClient, store: Store, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """R90: the executor stopped every running job; nothing was wrong with this call, so retry later."""
    a = put(store, "q.csv", p=PG)
    fake_executor.status = "error"
    fake_executor.payload = {"error": "hidden_memory: memory outside any job's count"}
    assert refusal(run(client)) == ("refused", "hidden_memory: memory outside any job's count")
    (event,) = events(audit_lines)
    assert (event["outcome"], event["target"]["ids"]) == ("error", [a.id])
    assert event["criteria"] == {"caller": "ghostwriter", "outputs": 0, "refused": "hidden_memory"}


def test_a_run_python_refusal_whose_record_cannot_be_written_never_reaches_the_model(
    client: TestClient, fake_executor: Scripted, broken_audit: None
) -> None:
    """The audit failure is the answer, never the refusal's text."""
    fake_executor.status = "error"
    fake_executor.payload = {"error": "read_timeout: the work on 'salaries-of-user-b.csv' ran past its time limit"}
    r = run(client)
    unavailable(r)
    assert "salaries" not in r.text
