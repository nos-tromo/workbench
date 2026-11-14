"""The ``run_python`` tool through the api, over a scripted executor.

The runner itself is tested in ``tests/executor/test_run_python.py`` and the real sandbox in
``tests/isolation``; here the executor answers what a test tells it to, and the api's part is checked:
which files go to the job, what is recorded, how outputs reach the answer, what is capped, and what the
model is told when the job could not finish.
"""

import dataclasses
import io
import itertools
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api.conftest import KEY
from tests.api.test_files import CHAT, ME, AuditLines, H, put, tick
from tests.api.test_tools_documents import Scripted
from workbench.api.audit import AuditLog
from workbench.api.jobs import JobQueue
from workbench.api.main import create_app
from workbench.api.metrics import Metrics
from workbench.api.openapi import TOOL_TAG
from workbench.api.store import Store
from workbench.config import Settings
from workbench.models import Principal


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


def run(client: TestClient, code: str = "print(1)", headers: dict[str, str] = H) -> Any:
    """Call ``run_python`` the way Open WebUI does."""
    return client.post("/tools/run_python", json={"code": code}, headers=headers)


def events(audit_lines: AuditLines) -> list[dict[str, Any]]:
    """The audit events of ``run_python``."""
    return [e for e in audit_lines() if e["operation"] == "tool.run_python"]


# --- what goes to the job, and what comes back ------------------------------------------------------------------


def test_route_records_combine_with_inputs_and_outputs(
    client: TestClient, store: Store, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """One ``combine`` event names every input and every output by id; the answer lists the outputs' paths."""
    a = put(store, "q.csv", b"quarter,figure\nq1,10\n")
    b = put(store, "notes.txt")
    fake_executor.files = {"p.png": b"\x89PNG", "r.xlsx": b"PK"}
    fake_executor.payload = ran("42\n")
    r = run(client, 'import pandas as pd\nprint(pd.read_csv("q.csv")["figure"].sum() + 32)')
    assert r.status_code == 200
    (req,) = fake_executor.requests
    outputs = [rec for rec in store.files_in_chat(ME) if rec.kind == "output"]
    assert sorted(rec.name for rec in outputs) == ["p.png", "r.xlsx"]
    assert r.json() == {
        "stdout": "42\n",
        "stderr": "",
        "files": [f"{CHAT}/p.png", f"{CHAT}/r.xlsx"],
        "truncated": False,
        "job_id": req.job_id,
    }
    (event,) = events(audit_lines)
    assert event["action"] == "combine"
    assert event["outcome"] == "ok"
    assert sorted(event["target"]["ids"]) == sorted([a.id, b.id, *(rec.id for rec in outputs)])
    assert event["target"]["count"] == 4
    assert event["target"]["container"] == "chat:chat-1"
    assert event["criteria"] == {"caller": "open-webui", "outputs": 2}
    raw = json.dumps(audit_lines())
    assert "pandas" not in raw
    assert "q.csv" not in raw


def test_every_file_of_the_chat_goes_to_the_job_newest_first(
    client: TestClient, store: Store, fake_executor: Scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All of this chat's files, in folders too, and nothing of another chat or user; the code as it was sent."""
    tick(store, monkeypatch)
    put(store, "old.txt")
    store.mkdir(ME, f"{CHAT}/reports")
    put(store, "q1.txt", directory=f"{CHAT}/reports")
    put(store, "new.docx")
    put(store, "elsewhere.txt", directory="/workspace/chat-2")
    theirs = Principal("open-webui", "user-b", "chat-1")
    store.add_upload(theirs, CHAT, "theirs.txt", io.BytesIO(b"x"), max_bytes=10, quota_bytes=100)
    assert run(client, "print('hello')").status_code == 200
    (req,) = fake_executor.requests
    assert req.tool == "run_python"
    assert req.input_files == ["new.docx", "reports/q1.txt", "old.txt"]
    assert req.args == {"code": "print('hello')", "budget_s": 108}
    assert (req.caller, req.user_id, req.chat_id) == ("open-webui", "user-a", "chat-1")
    assert req.max_chars == 16 * 1024


def test_the_job_id_is_the_job_record(client: TestClient, store: Store, fake_executor: Scripted) -> None:
    """The model cites ``job_id`` as the source of what the code computed; it is the job the store recorded."""
    job_id = run(client).json()["job_id"]
    record = store.get_job(ME, job_id)
    assert (record.tool, record.status) == ("run_python", "ok")


def test_a_chat_without_files_still_runs(client: TestClient, fake_executor: Scripted, audit_lines: AuditLines) -> None:
    """Code that needs no input runs; the event names no file."""
    fake_executor.payload = ran("2\n")
    assert run(client, "print(1 + 1)").json()["stdout"] == "2\n"
    assert fake_executor.requests[0].input_files == []
    (event,) = events(audit_lines)
    assert "ids" not in event["target"]
    assert event["target"]["count"] == 0
    assert event["criteria"] == {"caller": "open-webui", "outputs": 0}


def test_files_in_a_folder_named_out_are_not_given_to_the_code(
    client: TestClient, store: Store, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """``out/`` is where the code writes; the executor never copies a file below it, so it is not sent or recorded."""
    store.mkdir(ME, f"{CHAT}/out")
    hidden = put(store, "kept.csv", directory=f"{CHAT}/out")
    shown = put(store, "a.csv")
    body = run(client).json()
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
        put(store, f"f{n:04d}.txt")
    body = run(client).json()
    (req,) = fake_executor.requests
    assert len(req.input_files) == 1000
    assert (req.input_files[0], req.input_files[-1]) == ("f1004.txt", "f0005.txt")
    assert "1000 newest of 1005 files" in body["hint"]


def test_the_files_are_the_stores_paths(client: TestClient, store: Store, fake_executor: Scripted) -> None:
    """An output whose name is taken gets a number, and the answer names the file as the store keeps it."""
    put(store, "r.xlsx")
    fake_executor.files = {"r.xlsx": b"new"}
    fake_executor.payload = ran(files=["/data/jobs/0123/workspace/chat-1/out/r.xlsx"])
    assert run(client).json()["files"] == [f"{CHAT}/r (1).xlsx"]


def test_the_executors_note_on_outputs_is_passed_on(client: TestClient, fake_executor: Scripted) -> None:
    """Outputs the executor dropped are not silent: its message is in the hint."""
    fake_executor.message = "dropped 1 output: not the job's own file"
    body = run(client).json()
    assert "dropped 1 output: not the job's own file" in body["hint"]


def test_the_runners_hint_is_passed_on(client: TestClient, fake_executor: Scripted) -> None:
    """The runner's hint about another chat's file reaches the model."""
    fake_executor.payload = ran(stderr="FileNotFoundError: x", hint="Only this chat's files are available.")
    assert run(client).json()["hint"].startswith("Only this chat's files are available.")


def test_outputs_the_executor_refused_still_answer_with_what_the_code_printed(
    client: TestClient, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """Too many outputs keeps none of them; the code ran, so the model sees its output and why nothing was kept."""
    fake_executor.status = "error"
    fake_executor.message = "too many outputs: at most 20"
    fake_executor.payload = ran("made 30 files\n")
    r = run(client)
    assert r.status_code == 200
    body = r.json()
    assert (body["stdout"], body["files"]) == ("made 30 files\n", [])
    assert "too many outputs: at most 20" in body["hint"]
    (event,) = events(audit_lines)
    assert (event["outcome"], event["criteria"]["outputs"]) == ("error", 0)


@pytest.mark.parametrize(
    ("status", "message", "said"),
    [
        ("timeout", "the job ran past its wall time of 120 s", "wall time"),
        ("killed", "the job was stopped on a resource limit", "memory"),
    ],
)
def test_a_job_stopped_on_a_limit_is_a_422_that_says_why(
    client: TestClient,
    store: Store,
    fake_executor: Scripted,
    audit_lines: AuditLines,
    status: str,
    message: str,
    said: str,
) -> None:
    """Nothing came back but the reason; the run is recorded all the same, as an error."""
    a = put(store, "q.csv")
    fake_executor.status = status
    fake_executor.message = message
    r = run(client)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail.startswith(f"the code could not be completed: {message}")
    assert said in detail
    (event,) = events(audit_lines)
    assert (event["outcome"], event["target"]["ids"], event["criteria"]["outputs"]) == ("error", [a.id], 0)


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        ("read_timeout: the work ran past its time limit", 422, "read_timeout: the work ran past its time limit"),
        ("KeyError: '/data/jobs/0123/spec.json'", 500, "the tool failed"),
    ],
)
def test_a_runner_failure_is_its_code_or_a_fixed_text(
    client: TestClient, fake_executor: Scripted, error: str, status: int, detail: str
) -> None:
    """A failure the api knows is told; any other is a fixed text, since its own may hold a path."""
    fake_executor.status = "error"
    fake_executor.payload = {"error": error}
    r = run(client)
    assert (r.status_code, r.json()) == (status, {"detail": detail})


@pytest.mark.parametrize("message", ["the job failed", "the job left no readable result"])
def test_a_job_that_left_no_result_is_a_422_with_the_executors_reason(
    client: TestClient, fake_executor: Scripted, message: str
) -> None:
    """Code that ended its own process or crashed it leaves no answer; the executor's fixed text says so."""
    fake_executor.status = "error"
    fake_executor.message = message
    fake_executor.payload = {}
    r = run(client, "import os\nos._exit(1)")
    assert (r.status_code, r.json()) == (422, {"detail": f"the code could not be completed: {message}"})


@pytest.mark.parametrize(
    "answer", [{}, {"stdout": 1, "stderr": "", "truncated": False}, {"stdout": "", "stderr": ""}, []]
)
def test_an_unreadable_job_answer_is_a_fixed_error(client: TestClient, fake_executor: Scripted, answer: Any) -> None:
    """The api does not forward what it cannot validate."""
    fake_executor.payload = answer
    r = run(client)
    assert (r.status_code, r.json()) == (500, {"detail": "the tool returned an answer that could not be read"})


def test_a_busy_executor_is_a_503_and_nothing_is_recorded(
    client: TestClient, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """A job that never ran is a refusal to retry, and no event."""
    fake_executor.status = "busy"
    assert run(client).json() == {"detail": "busy, retry later"}
    assert events(audit_lines) == []


def test_outputs_whose_use_cannot_be_recorded_are_removed(
    client: TestClient, store: Store, fake_executor: Scripted, broken_audit: None
) -> None:
    """As with an upload: a file that could not be recorded does not stay."""
    fake_executor.files = {"r.xlsx": b"PK"}
    r = run(client)
    assert (r.status_code, r.json()) == (500, {"detail": "audit unavailable"})
    assert [rec for rec in store.files_in_chat(ME) if rec.kind == "output"] == []


# --- the request -----------------------------------------------------------------------------------------------


def test_code_of_up_to_50000_characters_is_taken(client: TestClient, fake_executor: Scripted) -> None:
    """The limit is the request's; one character more is refused before anything runs."""
    assert run(client, "#" * 50_000).status_code == 200
    assert run(client, "#" * 50_001).status_code == 422
    assert len(fake_executor.requests) == 1


@pytest.mark.parametrize("body", [{}, {"code": ""}, {"code": 1}, {"code": None}, {"code": ["print(1)"]}])
def test_a_body_that_is_not_valid_is_a_422(client: TestClient, fake_executor: Scripted, body: Any) -> None:
    """The model's mistakes are told at once, and nothing runs."""
    assert client.post("/tools/run_python", json=body, headers=H).status_code == 422
    assert fake_executor.requests == []


def test_the_tool_needs_the_callers_key(client: TestClient) -> None:
    """No key, no tool."""
    assert client.post("/tools/run_python", json={"code": "print(1)"}).status_code == 401
    wrong = {"Authorization": "Bearer wrong", "X-User-Id": "user-a"}
    assert client.post("/tools/run_python", json={"code": "print(1)"}, headers=wrong).status_code == 401


def test_run_python_is_in_the_schema_with_a_typed_code(client: TestClient) -> None:
    """A tool with its operationId, a description for the model, and one typed, bounded field."""
    doc = client.get("/openapi.json", headers={"Authorization": f"Bearer {KEY}"}).json()
    operation = doc["paths"]["/tools/run_python"]["post"]
    assert operation["operationId"] == "run_python"
    assert operation["tags"] == [TOOL_TAG]
    description = operation["description"]
    for phrase in ("out/", "thread", "multiprocessing", "job_id", "display_file"):
        assert phrase in description, phrase
    assert "Args:" not in description
    reference = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
    schema = doc["components"]["schemas"][reference.rsplit("/", 1)[1]]
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
    r = run(client)
    assert len(r.content) <= 16 * 1024
    body = r.json()
    assert body["truncated"] is True
    assert len(body["files"]) == 20
    assert body["stdout"].startswith("first line\n")
    assert body["stdout"].endswith("last line\n")
    assert body["stderr"].endswith("ZeroDivisionError: division by zero\n")
    assert "characters cut" in body["stdout"] + body["stderr"]


def test_an_answer_within_the_limit_is_not_cut(client: TestClient, fake_executor: Scripted) -> None:
    """Short streams pass as they are."""
    fake_executor.payload = ran("a\n", "b\n")
    body = run(client).json()
    assert (body["stdout"], body["stderr"], body["truncated"]) == ("a\n", "b\n", False)


def test_the_cap_counts_bytes_of_the_json_not_characters(small: TestClient, fake_executor: Scripted) -> None:
    """Text that takes more than a byte a character is cut to the limit as sent."""
    fake_executor.payload = ran("ä€😀" * 400, "ü" * 400)
    with small:
        r = run(small)
    assert r.status_code == 200
    assert len(r.content) <= 1024
    assert r.json()["truncated"] is True
