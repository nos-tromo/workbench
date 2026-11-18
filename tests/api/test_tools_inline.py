"""Inline ``run_python``: the data comes with the call, the outputs go back in the answer, and nothing is kept.

The request, the job and the result are spec §3.4, the audit event §3.6. The executor is the scripted one of
``tests/api/test_tools_python.py``; every call runs on the app's own loop, as the MCP door's would.
"""

import base64
import dataclasses
import json
import logging
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tests.api import test_tools_python as python_tests
from tests.api.conftest import DOCINT_KEY, GHOSTWRITER_KEY, AuditLines
from tests.api.test_tools_documents import Scripted
from tests.api.test_tools_python import ran
from tests.callers import GHOSTWRITER_TOOLS
from workbench import protocol
from workbench.api.audit import AuditLog
from workbench.api.auth import CallerRegistry
from workbench.api.jobs import Busy, CallLimits, ExecutorClient, JobQueue, RequestTooLarge, SkippedOutput
from workbench.api.main import create_app
from workbench.api.metrics import Metrics
from workbench.api.registry import INLINE_TOOLS, PurposeRequired, Registry, ToolEnv, ToolOutcome
from workbench.api.store import Store
from workbench.api.tools import MAX_CODE, NotCompleted
from workbench.api.tools_inline import (
    INLINE_SPECS,
    MAX_DATA,
    MAX_DATA_BYTES,
    InlinePythonRequest,
    _skip_hint,
    input_schema,
    run_python_inline,
)
from workbench.config import Settings
from workbench.models import Principal
from workbench.protocol import JOB_ID, InlineFile
from workbench.utils.audit_event import AuditWriteError, Purpose

#: The fixtures of ``run_python``'s tests: the real app over the scripted executor.
app = python_tests.app
fake_executor = python_tests.fake_executor

PASSAGES = {"name": "passages.json", "content": '[{"n": 1, "text": "synthetic harbour notes", "author": "user-a"}]'}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
COUNTS = b"author,posts\nuser-a,12\nuser-b,7\n"
CODE = "import pandas as pd\nprint(len(pd.read_json('passages.json')), 'authors')"
DOCINT = Principal("docint", "a" * 64, "default")
INLINE_OPERATION = "tool.run_python.inline"


def body(**fields: Any) -> InlinePythonRequest:
    """A request as the door validates it for docint, whose wall cap is 60 s; ``code`` is :data:`CODE` unless given."""
    return InlinePythonRequest.model_validate({"code": CODE, **fields}, context={"wall_cap": 60})


def env(
    client: TestClient, p: Principal = DOCINT, purpose: Purpose | None = None, request_id: str | None = None
) -> ToolEnv:
    """What the door hands the tool for docint: the app's collaborators and what the call's headers say."""
    state = cast(FastAPI, client.app).state
    callers: CallerRegistry = state.callers
    return ToolEnv(
        p=p,
        caller=callers.get("docint"),
        settings=state.settings,
        store=state.store,
        audit=state.audit,
        jobs=state.jobs,
        purpose=purpose,
        request_id=request_id,
    )


def call(client: TestClient, request: InlinePythonRequest, tool_env: ToolEnv | None = None) -> ToolOutcome:
    """Run the tool on the app's loop, as the door does."""
    portal = client.portal
    assert portal is not None
    return portal.call(run_python_inline, request, tool_env or env(client))


def inline_events(audit_lines: AuditLines) -> list[dict[str, Any]]:
    """The audit events of inline ``run_python``."""
    return [e for e in audit_lines() if e["operation"] == INLINE_OPERATION]


# --- §3.4, the request -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("code", ["", "x" * (MAX_CODE + 1)])
def test_code_is_1_to_50000_characters(code: str) -> None:
    """Empty code or one character past 50 000 is refused; 50 000 pass."""
    with pytest.raises(ValidationError):
        body(code=code)
    assert len(body(code="x" * MAX_CODE).code) == 50_000


@pytest.mark.parametrize("name", ["passages.json", "a.csv", "B-1.txt", "x_y.md", "a" + "b" * 99 + ".json"])
def test_these_data_names_pass(name: str) -> None:
    """A letter or digit, then up to 99 of letters, digits, ``_``, ``.`` and ``-``, then one of four suffixes."""
    assert body(data=[{"name": name, "content": ""}]).data[0].name == name


@pytest.mark.parametrize(
    "name", [".a.json", "-a.csv", "a.xlsx", "a.JSON", "a/b.csv", "out", "a" + "b" * 100 + ".json", "ü.csv", "a.csv\n"]
)
def test_these_data_names_are_refused(name: str) -> None:
    """Hidden, dashed, another type, an upper-case suffix, a path, no suffix, too long, not ASCII, a newline."""
    with pytest.raises(ValidationError):
        body(data=[{"name": name, "content": ""}])


def test_data_names_are_unique() -> None:
    """Two entries of one name would be one file."""
    with pytest.raises(ValidationError, match="unique"):
        body(data=[PASSAGES, {**PASSAGES, "content": "[]"}])


def test_at_most_eight_entries() -> None:
    """Eight pass, a ninth is refused."""
    entries = [{"name": f"p{n}.json", "content": "[]"} for n in range(MAX_DATA + 1)]
    assert len(body(data=entries[:MAX_DATA]).data) == 8
    with pytest.raises(ValidationError):
        body(data=entries)


def test_all_data_together_is_at_most_512_kib_in_utf8() -> None:
    """The bound counts UTF-8 bytes of all entries together: 512 KiB of two-byte letters pass, one byte more not."""
    full = {"name": "passages.json", "content": "ü" * (256 * 1024)}
    assert MAX_DATA_BYTES == 512 * 1024
    assert body(data=[full]).data[0].content == full["content"]
    with pytest.raises(ValidationError, match="512 KiB"):
        body(data=[full, {"name": "notes.txt", "content": "x"}])


def test_the_data_bounds_are_the_protocols(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool refuses what the job's protocol would refuse, by the protocol's own rules, so the two cannot drift."""
    assert (MAX_DATA, MAX_DATA_BYTES) == (protocol.MAX_INLINE_FILES, protocol.MAX_INLINE_BYTES)
    monkeypatch.setattr(protocol, "MAX_INLINE_BYTES", 4)
    with pytest.raises(ValidationError):
        body(data=[{"name": "notes.txt", "content": "12345"}])


def test_data_utf8_cannot_hold_is_refused() -> None:
    """A lone surrogate could not be written as a file, so it is refused with the request."""
    with pytest.raises(ValidationError, match="UTF-8"):
        body(data=[{"name": "notes.txt", "content": "a\ud800b"}])


@pytest.mark.parametrize("limit", [4, 61, 0, -1])
def test_time_limit_outside_5_to_the_cap_is_refused_never_clamped(limit: int) -> None:
    """A client's error stays visible: outside 5 to the caller's cap is refused; 5, 60 and none pass."""
    with pytest.raises(ValidationError):
        body(time_limit_s=limit)
    assert [body(time_limit_s=5).time_limit_s, body(time_limit_s=60).time_limit_s, body().time_limit_s] == [5, 60, None]


@pytest.mark.parametrize("limit", ["30", 30.0, True])
def test_time_limit_is_an_integer_as_sent(limit: object) -> None:
    """The schema says integer: a string, a float or a boolean is refused, never converted."""
    with pytest.raises(ValidationError):
        body(time_limit_s=limit)


def test_a_time_limit_without_the_callers_cap_is_refused() -> None:
    """Validated without the door's context, a time limit cannot be checked, so it is refused."""
    with pytest.raises(ValidationError, match="wall cap"):
        InlinePythonRequest.model_validate({"code": CODE, "time_limit_s": 30})
    assert InlinePythonRequest.model_validate({"code": CODE}).time_limit_s is None


def test_unknown_fields_are_refused() -> None:
    """At the top level and in an entry."""
    with pytest.raises(ValidationError):
        body(timeout=30)
    with pytest.raises(ValidationError):
        body(data=[{**PASSAGES, "mime": "application/json"}])


def test_a_refusal_never_quotes_the_data() -> None:
    """The refusal's text names where and why, never the data a log could pick up."""
    marker = "zz-synthetic-marker"
    with pytest.raises(ValidationError) as refused:
        body(data=[{"name": "a.json", "content": marker}, {"name": "a.json", "content": marker}])
    assert marker not in str(refused.value)
    assert marker not in repr(refused.value)


def test_the_schema_gives_time_limit_the_callers_range(settings: Settings) -> None:
    """``time_limit_s`` is an integer from 5 to the caller's cap, not required; only ``code`` is; no other field."""
    docint = CallerRegistry.from_settings(settings).get("docint")
    schema = input_schema(docint)
    limit = schema["properties"]["time_limit_s"]
    assert (limit["type"], limit["minimum"], limit["maximum"]) == ("integer", 5, 60)
    assert "anyOf" not in limit and limit["description"]
    assert schema["required"] == ["code"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"code", "data", "time_limit_s"}
    assert f"at most {MAX_DATA} entries and {MAX_DATA_BYTES // 1024} KiB" in schema["properties"]["data"]["description"]
    assert f"at most {MAX_CODE} characters" in schema["properties"]["code"]["description"]
    assert input_schema(dataclasses.replace(docint, wall_s=30))["properties"]["time_limit_s"]["maximum"] == 30


# --- §3.4, the job -----------------------------------------------------------------------------------------------


def test_the_data_goes_inline_and_no_file_goes_in(
    client: TestClient, fake_executor: Scripted, settings: Settings, store: Store
) -> None:
    """The data travels in the request; nothing is copied from a workspace, and no workspace exists for docint."""
    call(client, body(data=[PASSAGES]))
    (req,) = fake_executor.requests
    assert req.inline_inputs == [InlineFile(**PASSAGES)]
    assert (req.input_files, req.caller, req.user_id, req.chat_id) == ([], "docint", "a" * 64, "default")
    assert not (settings.data_dir / "workspaces" / "docint").exists()
    assert store.count_files() == 0


@pytest.mark.parametrize(("limit", "job_wall", "wall"), [(None, 120, 60), (30, 120, 30), (None, 40, 40)])
def test_the_wall_is_the_least_of_the_limit_the_cap_and_the_setting(
    client: TestClient, fake_executor: Scripted, settings: Settings, limit: int | None, job_wall: int, wall: int
) -> None:
    """The job runs for the least of the call's limit, the caller's cap and the job wall; the code gets 9/10 of it."""
    fields = {} if limit is None else {"time_limit_s": limit}
    tool_env = dataclasses.replace(env(client), settings=dataclasses.replace(settings, job_wall_s=job_wall))
    call(client, body(**fields), tool_env)
    (req,) = fake_executor.requests
    assert req.limits.wall_s == wall
    assert req.args == {"code": CODE, "budget_s": wall * 9 // 10}


def test_the_call_waits_five_seconds_and_holds_the_callers_slots(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The door's call waits at most MCP_QUEUE_WAIT_S for its turn, within docint's 60 s and 2 slots."""
    queue: JobQueue = app.state.jobs
    real = queue.submit_inline
    seen: list[CallLimits] = []

    async def spy(*args: Any) -> Any:
        seen.append(args[-1])
        return await real(*args)

    monkeypatch.setattr(queue, "submit_inline", spy)
    call(client, body())
    assert seen == [CallLimits(60, 5, 2)]


# --- §3.4, the result --------------------------------------------------------------------------------------------


def test_the_result_has_the_shape_of_the_spec(client: TestClient, fake_executor: Scripted) -> None:
    """The structured answer with its Nones, a chart as an image for the user, a table as a resource with its text."""
    fake_executor.payload = ran("2 authors")
    fake_executor.files = {"chart.png": PNG, "counts.csv": COUNTS}
    outcome = call(client, body(data=[PASSAGES]))
    structured = outcome.structured
    job_id = fake_executor.requests[0].job_id
    assert set(structured) == {"stdout", "stderr", "truncated", "job_id", "files", "hint", "reason"}
    assert (structured["stdout"], structured["stderr"], structured["truncated"]) == ("2 authors", "", False)
    assert (structured["hint"], structured["reason"]) == (None, None)
    assert structured["job_id"] == job_id and JOB_ID.fullmatch(job_id)
    assert structured["files"] == [
        {"name": "chart.png", "mime": "image/png", "bytes": len(PNG), "returned": True},
        {"name": "counts.csv", "mime": "text/csv", "bytes": len(COUNTS), "returned": True},
    ]
    assert outcome.blocks == [
        {
            "type": "image",
            "data": base64.b64encode(PNG).decode(),
            "mimeType": "image/png",
            "annotations": {"audience": ["user"]},
        },
        {
            "type": "resource",
            "resource": {
                "uri": f"workbench://jobs/{job_id}/out/counts.csv",
                "mimeType": "text/csv",
                "text": COUNTS.decode(),
            },
        },
    ]


def test_files_lists_what_did_not_come_back_too(client: TestClient, fake_executor: Scripted) -> None:
    """Every output is listed: those that came back first, in return order, then the rest by name, typed if known."""
    fake_executor.files = {"x.xlsx": b"PK", "counts.csv": COUNTS, "t.csv": "Müller\n".encode("latin-1")}
    outcome = call(client, body())
    assert outcome.structured["files"] == [
        {"name": "counts.csv", "mime": "text/csv", "bytes": len(COUNTS), "returned": True},
        {"name": "t.csv", "mime": "text/csv", "bytes": 7, "returned": False},
        {"name": "x.xlsx", "mime": "application/octet-stream", "bytes": 2, "returned": False},
    ]
    assert [block["resource"]["uri"].rsplit("/", 1)[1] for block in outcome.blocks] == ["counts.csv"]


@pytest.mark.parametrize(
    ("staged", "phrase"),
    [
        ({"x.xlsx": b"PK"}, "only .png, .csv, .txt, .md and .json come back"),
        ({"big.png": PNG + b"\x00" * (512 * 1024)}, "plt.savefig(..., dpi=100)"),
        ({"t.csv": "Müller".encode("latin-1")}, "not being UTF-8"),
        ({"t.csv": b"a,b\n1,x\x01y\n"}, "holding control characters"),
    ],
)
def test_the_hint_says_why_a_file_did_not_come_back(
    client: TestClient, fake_executor: Scripted, staged: dict[str, bytes], phrase: str
) -> None:
    """The hint names how many did not come back and what to do about it."""
    fake_executor.files = staged
    hint = call(client, body()).structured["hint"]
    assert hint.startswith("1 ")
    assert phrase in hint


@pytest.mark.parametrize(
    ("reason", "name", "phrase"),
    [
        ("type", "x.xlsx", "only .png, .csv, .txt, .md and .json come back"),
        ("size", "chart.png", "save it smaller, for example plt.savefig(..., dpi=100)"),
        ("size", "long.txt", "a text comes back up to 256 KiB"),
        ("count", "i.csv", "at most 8 files come back"),
        ("total", "c.csv", "1 MiB in all"),
        ("encoding", "t.csv", "not being UTF-8: write text files as UTF-8"),
        ("control", "t.csv", "holding control characters: write text without them"),
        ("signature", "fake.png", "is not a PNG"),
        ("unreadable", "gone.csv", "could not be read"),
    ],
)
def test_each_skip_reason_has_one_sentence_with_its_count(reason: Any, name: str, phrase: str) -> None:
    """One sentence per reason that occurred, naming how many files it kept back."""
    (sentence,) = _skip_hint([SkippedOutput(name, 1, reason), SkippedOutput("2-" + name, 1, reason)])
    assert sentence.startswith("2 ")
    assert phrase in sentence


def test_a_control_character_is_not_blamed_on_the_encoding() -> None:
    """Both kinds in one answer give two sentences, each with its own count; only the first names UTF-8."""
    skipped = [SkippedOutput("a.csv", 1, "encoding"), SkippedOutput("b.csv", 1, "control")]
    skipped.append(SkippedOutput("c.csv", 1, "control"))
    encoding, control = _skip_hint(skipped)
    assert (encoding.split()[0], control.split()[0]) == ("1", "2")
    assert "UTF-8" in encoding and "UTF-8" not in control


def test_the_hint_joins_the_runners_and_the_executors_notes(client: TestClient, fake_executor: Scripted) -> None:
    """The runner's hint, the executor's note on outputs, then a sentence per reason, as one hint."""
    fake_executor.payload = ran(hint="The code ran on the data given.")
    fake_executor.message = "dropped 1 output: not the job's own file"
    fake_executor.files = {"x.xlsx": b"PK", "y.html": b"<p>"}
    hint = call(client, body()).structured["hint"]
    assert hint.startswith("The code ran on the data given. ")
    assert "dropped 1 output: not the job's own file" in hint
    assert hint.endswith("only .png, .csv, .txt, .md and .json come back.")
    assert "2 files" in hint


@pytest.mark.parametrize(
    ("status", "message", "said"),
    [
        (
            "ok",
            "dropped 1 output: not the job's own file",
            "Not every file in out/ came back: dropped 1 output: not the job's own file.",
        ),
        ("error", "too many outputs: at most 20", "No file in out/ came back: too many outputs: at most 20."),
        ("error", "", "No file in out/ came back: the outputs could not be returned."),
    ],
)
def test_the_executors_note_says_what_did_not_come_back(
    client: TestClient, fake_executor: Scripted, status: str, message: str, said: str
) -> None:
    """Nothing is kept on this door, so the executor's note on the outputs says that a file did not come back."""
    fake_executor.status = status
    fake_executor.message = message
    fake_executor.payload = ran("made files")
    hint = call(client, body()).structured["hint"]
    assert hint == said
    assert "kept" not in hint


def test_a_uri_holds_the_name_percent_encoded(client: TestClient, fake_executor: Scripted) -> None:
    """A name a model writes, with spaces, umlauts, ``#`` and ``%``, comes back as written and percent-encoded."""
    fake_executor.files = {"Übersicht März #1.csv": COUNTS, "50% share.csv": COUNTS}
    outcome = call(client, body())
    uris = [block["resource"]["uri"] for block in outcome.blocks]
    assert uris[0].endswith("/out/50%25%20share.csv")
    assert uris[1].endswith("/out/%C3%9Cbersicht%20M%C3%A4rz%20%231.csv")
    assert [f["name"] for f in outcome.structured["files"]] == ["50% share.csv", "Übersicht März #1.csv"]


def test_stdout_and_stderr_stay_within_the_result_limit(client: TestClient, fake_executor: Scripted) -> None:
    """Both streams are cut in the middle until the structured answer, as sent with its Nones, fits 16 KiB."""
    fake_executor.payload = ran("x" * 100_000, "ü" * 10_000)
    structured = call(client, body()).structured
    assert structured["truncated"] is True
    assert len(json.dumps(structured, separators=(",", ":"), ensure_ascii=False).encode()) <= 16 * 1024
    assert "characters cut" in structured["stdout"]


def test_code_that_could_not_complete_raises_not_completed(
    client: TestClient, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """A job past its wall time is the 422 the workspace tool raises too, and its event says ``not_completed``."""
    fake_executor.status = "timeout"
    fake_executor.message = "the job ran past its wall time of 60 s"
    with pytest.raises(NotCompleted) as refused:
        call(client, body())
    assert refused.value.status_code == 422
    assert refused.value.detail == "the code could not be completed: the job ran past its wall time of 60 s"
    (event,) = inline_events(audit_lines)
    assert (event["outcome"], event["criteria"]["refused"]) == ("error", "not_completed")


def test_a_runner_refusal_raises_its_http_exception(
    client: TestClient, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """A full volume is the runner's refusal with its own status, not ``not_completed``; its event says ``refused``."""
    fake_executor.status = "error"
    fake_executor.payload = {"error": "storage_full: storage is full"}
    with pytest.raises(HTTPException) as refused:
        call(client, body())
    assert not isinstance(refused.value, NotCompleted)
    assert (refused.value.status_code, refused.value.detail) == (507, "storage_full: storage is full")
    (event,) = inline_events(audit_lines)
    assert (event["outcome"], event["criteria"]["refused"]) == ("error", "refused")


def test_an_answer_that_cannot_be_read_returns_nothing_and_is_recorded_so(
    client: TestClient, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """The runner's answer is not of its shape: a 500, and the event counts no output as returned."""
    fake_executor.payload = {"stdout": 1}
    fake_executor.files = {"counts.csv": COUNTS}
    with pytest.raises(HTTPException) as refused:
        call(client, body())
    assert refused.value.status_code == 500
    (event,) = inline_events(audit_lines)
    assert event["outcome"] == "error"
    assert {key: event["criteria"][key] for key in ("outputs", "dropped", "refused")} == {
        "outputs": 0,
        "dropped": 1,
        "refused": "refused",
    }


def test_escape_heavy_data_is_too_large_never_a_500(
    settings: Settings, store: Store, audit: AuditLog, audit_lines: AuditLines, tmp_path: Path
) -> None:
    """512 KiB of quotes passes the request's bound and doubles on the line: too large, no event, nothing left."""
    queue = JobQueue(settings, ExecutorClient(tmp_path / "none.sock"), store, Metrics(settings.data_dir))
    data = [{"name": f"p{n}.json", "content": '"' * (64 * 1024)} for n in range(MAX_DATA)]
    with TestClient(create_app(settings, store=store, audit=audit, jobs=queue)) as client:
        with pytest.raises(RequestTooLarge):
            call(client, body(data=data))
    assert inline_events(audit_lines) == []
    staging = settings.data_dir / "staging"
    assert not staging.exists() or list(staging.iterdir()) == []
    assert store.count_files() == 0
    (job_id,) = store.due_jobs(float("inf"))
    record = store.find_job(job_id)
    assert record is not None and record.status == "error"


def test_without_a_queue_the_executor_is_busy(client: TestClient, audit_lines: AuditLines) -> None:
    """An app without a job queue runs nothing: the queue's own refusal, and nothing recorded."""
    with pytest.raises(Busy) as refused:
        call(client, body(), dataclasses.replace(env(client), jobs=None))
    assert refused.value.reason == "executor_busy"
    assert inline_events(audit_lines) == []


def test_nothing_is_registered_and_no_workspace_is_made(
    client: TestClient, fake_executor: Scripted, settings: Settings, store: Store
) -> None:
    """The outputs come back in the answer, and no file row, no workspace and no staging folder stay."""
    fake_executor.files = {"counts.csv": COUNTS, "chart.png": PNG}
    outcome = call(client, body(data=[PASSAGES]))
    assert len(outcome.blocks) == 2
    assert store.count_files() == 0
    assert not (settings.data_dir / "workspaces" / "docint").exists()
    assert not (settings.data_dir / "staging" / outcome.structured["job_id"]).exists()


def test_the_inline_specs_are_the_inline_tools(app: FastAPI) -> None:
    """The start check's set names exactly the inline specs, and docint finds them in the app's registry."""
    assert {spec.name for spec in INLINE_SPECS} == INLINE_TOOLS
    assert all(spec.form == "inline" for spec in INLINE_SPECS)
    registry: Registry = app.state.registry
    assert registry.for_caller(cast(CallerRegistry, app.state.callers).get("docint")) == list(INLINE_SPECS)
    (spec,) = INLINE_SPECS
    for phrase in ("current folder", "read-only", "out/", "CSV", "PNG", "512 KiB", "network", "installed"):
        assert phrase in spec.description, phrase


# --- §3.6, the audit ---------------------------------------------------------------------------------------------


def test_the_call_is_recorded_as_combine_on_inline_data(
    client: TestClient, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """By reference only: the user's pseudonym, the count of entries, the session, the job and how many came back."""
    fake_executor.files = {"chart.png": PNG, "counts.csv": COUNTS}
    stated = env(client, purpose=Purpose(reason="docint chat"), request_id="req-1")
    outcome = call(client, body(data=[PASSAGES]), stated)
    (event,) = inline_events(audit_lines)
    assert (event["action"], event["operation"], event["outcome"]) == ("combine", INLINE_OPERATION, "ok")
    assert event["actor"] == {"user": "a" * 64, "via": "service"}
    assert event["target"] == {"kind": "inline_data", "count": 1, "container": "chat:default"}
    assert event["criteria"] == {
        "caller": "docint",
        "job": outcome.structured["job_id"],
        "input_bytes": len(PASSAGES["content"].encode()),
        "outputs": 2,
        "dropped": 0,
    }
    assert event["purpose"] == {"reason": "docint chat"}
    assert event["request_id"] == "req-1"


def test_the_container_is_the_session(client: TestClient, fake_executor: Scripted, audit_lines: AuditLines) -> None:
    """The session docint names is the event's container."""
    call(client, body(), env(client, p=dataclasses.replace(DOCINT, chat_id="b" * 64)))
    (event,) = inline_events(audit_lines)
    assert event["target"]["container"] == "chat:" + "b" * 64


def test_the_outputs_that_did_not_come_back_are_counted_as_dropped(
    client: TestClient, fake_executor: Scripted, audit_lines: AuditLines
) -> None:
    """Returned and dropped, by count alone."""
    fake_executor.files = {"counts.csv": COUNTS, "x.xlsx": b"PK", "fake.png": b"GIF89a"}
    call(client, body())
    (event,) = inline_events(audit_lines)
    assert (event["criteria"]["outputs"], event["criteria"]["dropped"]) == (1, 2)


def test_never_recorded(
    client: TestClient, fake_executor: Scripted, audit_lines: AuditLines, caplog: pytest.LogCaptureFixture
) -> None:
    """The data, a data name, the code, stdout, an output's name and a refusal's text reach no audit line or log."""
    marker = "zz-synthetic-marker"
    caplog.set_level(logging.DEBUG)
    fake_executor.payload = ran(f"{marker} stdout", f"{marker} stderr")
    fake_executor.files = {f"{marker}-out.csv": b"a\n"}
    data = [{"name": f"{marker}.json", "content": f'["{marker} content"]'}]
    call(client, body(code=f"print('{marker} code')", data=data))
    fake_executor.files = {}
    fake_executor.status = "error"
    fake_executor.payload = {"error": f"read_timeout: the work on {marker} ran past its time limit"}
    with pytest.raises(HTTPException):
        call(client, body(data=data))
    assert len(inline_events(audit_lines)) == 2
    assert marker not in json.dumps(audit_lines())
    assert marker not in caplog.text


def test_nothing_goes_out_when_the_event_cannot_be_written(
    broken_audit: None, client: TestClient, fake_executor: Scripted, settings: Settings
) -> None:
    """Fail closed: the audit failure is the answer, and the outputs are gone with the staging folder."""
    fake_executor.files = {"counts.csv": COUNTS}
    with pytest.raises(AuditWriteError):
        call(client, body(data=[PASSAGES]))
    job_id = fake_executor.requests[0].job_id
    assert not (settings.data_dir / "staging" / job_id).exists()


def strict(client: TestClient, settings: Settings, purpose: Purpose | None = None) -> ToolEnv:
    """Docint's environment on an api that requires a purpose for ``combine``."""
    required = dataclasses.replace(settings, require_purpose=frozenset({"combine"}))
    return dataclasses.replace(env(client, purpose=purpose, request_id="req-1"), settings=required)


def test_a_missing_required_purpose_is_denied_without_a_target_and_runs_nothing(
    client: TestClient, fake_executor: Scripted, settings: Settings, audit_lines: AuditLines
) -> None:
    """As docint records it: one ``denied`` event, no target, the session in the criteria; no job starts."""
    with pytest.raises(PurposeRequired):
        call(client, body(data=[PASSAGES]), strict(client, settings))
    last = audit_lines()[-1]
    assert (last["action"], last["operation"], last["outcome"]) == ("combine", INLINE_OPERATION, "denied")
    assert "target" not in last
    assert last["criteria"] == {"caller": "docint", "refused": "purpose_required", "container": "chat:default"}
    assert last["request_id"] == "req-1"
    assert fake_executor.requests == []


@pytest.mark.parametrize("purpose", [Purpose(reason="docint chat"), Purpose(case_ref="case-0001")])
def test_a_stated_purpose_meets_the_requirement(
    client: TestClient, fake_executor: Scripted, settings: Settings, audit_lines: AuditLines, purpose: Purpose
) -> None:
    """A reason or a case reference is a purpose: the job runs and its event carries it."""
    call(client, body(), strict(client, settings, purpose))
    (event,) = inline_events(audit_lines)
    assert (event["outcome"], event["purpose"]) == ("ok", purpose.to_dict())
    assert len(fake_executor.requests) == 1


def test_an_empty_purpose_is_none(client: TestClient, fake_executor: Scripted, settings: Settings) -> None:
    """A purpose with neither field is not one."""
    with pytest.raises(PurposeRequired):
        call(client, body(), strict(client, settings, Purpose()))
    assert fake_executor.requests == []


def test_config_audit_lists_the_callers_and_the_purpose_rule(
    settings: Settings, store: Store, audit: AuditLog, audit_lines: AuditLines
) -> None:
    """Each caller's name, door (the MCP door, every caller's), tools, wall cap and slots, and the purpose rule."""
    required = dataclasses.replace(settings, require_purpose=frozenset({"query", "disclose", "combine"}))
    with TestClient(create_app(required, store=store, audit=audit)):
        pass
    (config,) = [e for e in audit_lines() if e["operation"] == "config.audit"]
    assert config["criteria"]["callers"] == [
        {"name": "docint", "door": "mcp", "tools": ["run_python"], "wall_s": 60, "slots": 2},
        {"name": "ghostwriter", "door": "mcp", "tools": sorted(GHOSTWRITER_TOOLS), "wall_s": 120, "slots": 2},
    ]
    assert config["criteria"]["require_purpose"] == ["combine", "disclose", "query"]
    raw = json.dumps(audit_lines())
    assert DOCINT_KEY not in raw
    assert GHOSTWRITER_KEY not in raw
