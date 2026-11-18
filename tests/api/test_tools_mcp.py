"""Ghostwriter's workspace tools on the MCP door: the eight tools, their outputs and previews, its chat and its limits.

The executor runs the real runners in this process (:class:`~tests.api.test_tools_templates.Local`); a test that needs
another answer scripts it. The wire is ``tests/api/test_mcp.py``'s, sent with Ghostwriter's headers.
"""

import concurrent.futures
import dataclasses
import inspect
import io
import json
import logging
import sqlite3
from functools import partial
from pathlib import Path
from typing import Any, Final, cast

import httpx2
import jsonschema
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api import test_tools_templates as template_tests
from tests.api.conftest import HD, HG, PG, AuditLines, H, put, unrecorded
from tests.api.test_jobs import sample, until
from tests.api.test_mcp import Headers, error, refused, result, rpc
from tests.api.test_tools_documents import Scripted
from tests.api.test_tools_python import ran
from tests.api.test_tools_templates import Local
from tests.callers import EIGHT
from tests.conftest import scaled
from tests.fixtures import make_templates as mt
from workbench.api.audit import OPERATIONS, AuditLog
from workbench.api.auth import CallerRegistry
from workbench.api.jobs import Busy, CallLimits, JobQueue
from workbench.api.main import create_app
from workbench.api.mcp_wire import INSTRUCTIONS, WORKSPACE_INSTRUCTIONS
from workbench.api.metrics import Metrics
from workbench.api.openapi import tool_openapi, tool_routes
from workbench.api.store import Store
from workbench.api.tools_mcp import PREVIEW_RESERVE_S
from workbench.config import Settings
from workbench.models import FileRecord, Principal

#: The fixtures of the template tools' tests: the real app over the in-process executor.
app = template_tests.app
fake_executor = template_tests.fake_executor

P2: Final = Principal("ghostwriter", "user-a", "chat-2", confined=True)
CHAT: Final = "/workspace/chat-1"
MIB: Final = 1024 * 1024
TABLE: Final[dict[str, Any]] = {
    "name": "counts",
    "format": "csv",
    "columns": ["author", "posts"],
    "rows": [["user-a", 12], ["user-b", 7]],
}
PREVIEW: Final[dict[str, Any]] = {
    "columns": ["author", "posts"],
    "rows": [["user-a", "12"], ["user-b", "7"]],
    "row_count": 2,
    "truncated": False,
}
CSV: Final = b"author,posts\nuser-a,12\nuser-b,7\n"
PNG: Final = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
#: Every parameter a tool route may take: what the MCP door's adapter passes by name.
KNOWN: Final = frozenset({"body", "p", "settings", "store", "audit", "jobs"})
#: Ghostwriter's headers when it states a purpose and a request id.
PURPOSE: Final = {**HG, "X-Audit-Purpose": "reason=ghostwriter+chat", "X-Request-ID": "req-1"}
#: Valid arguments for each tool the purpose rule covers, with the action its event is recorded under.
GUARDED: Final[dict[str, tuple[str, dict[str, Any]]]] = {
    "read_document": ("query", {"path": f"{CHAT}/q.csv"}),
    "search_files": ("query", {"query": "user-a"}),
    "run_python": ("combine", {"code": "print(1)"}),
    "inspect_template": ("query", {"path": f"{CHAT}/form.docx"}),
    "fill_template": (
        "modify",
        {"path": f"{CHAT}/form.docx", "values": {"customer": {"value": "x", "source": {"kind": "assistant"}}}},
    ),
    "write_table": ("collect", TABLE),
    "write_document": ("collect", {"name": "notes", "format": "md", "markdown": "# Hafen"}),
}


def acall(
    client: TestClient, tool: str, arguments: dict[str, Any] | None = None, *, headers: Headers = HG, **options: Any
) -> httpx2.Response:
    """Call a tool on the MCP door as Ghostwriter does, on the modern revision."""
    return rpc(client, "tools/call", {"name": tool, "arguments": arguments or {}}, headers=headers, **options)


def keep(store: Store, p: Principal, name: str, data: bytes = CSV) -> FileRecord:
    """Store ``data`` as ``name`` in ``p``'s own chat, without going through the api."""
    return store.add_upload(
        p, f"/workspace/{p.chat_id}", name, io.BytesIO(data), max_bytes=MIB, quota_bytes=10 * MIB, record=unrecorded
    )


def without_previews(structured: dict[str, Any]) -> dict[str, Any]:
    """The structured answer with each output's ``preview`` left out."""
    return {**structured, "outputs": [{k: v for k, v in o.items() if k != "preview"} for o in structured["outputs"]]}


def tool_events(audit_lines: AuditLines) -> list[dict[str, Any]]:
    """The audit events of the tools."""
    return [e for e in audit_lines() if e["operation"].startswith("tool.")]


def job_rows(settings: Settings) -> list[tuple[str, str, str]]:
    """Every job record of the volume's database: its caller, chat and tool."""
    db = sqlite3.connect(settings.data_dir / "workbench.db")
    try:
        return [tuple(row) for row in db.execute("SELECT caller, chat_id, tool FROM jobs").fetchall()]
    finally:
        db.close()


def spy_submit(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record the keywords of every ``JobQueue.submit`` of the app, which still runs."""
    queue: JobQueue = app.state.jobs
    real = queue.submit
    seen: list[dict[str, Any]] = []

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(queue, "submit", spy)
    return seen


# --- tools/list and server/discover --------------------------------------------------------------------------------


def test_the_ghostwriter_lists_its_eight_tools_in_name_order(client: TestClient) -> None:
    """The eight workspace tools, by name; each takes an object and answers its REST model plus ``outputs``."""
    tools = result(rpc(client, "tools/list", headers=HG))["tools"]
    assert [t["name"] for t in tools] == sorted(EIGHT)
    for tool in tools:
        assert set(tool) == {"name", "title", "description", "inputSchema", "outputSchema"}
        assert tool["inputSchema"]["type"] == "object", tool["name"]
        assert tool["outputSchema"]["type"] == "object", tool["name"]
        assert "outputs" in tool["outputSchema"]["properties"], tool["name"]
    (no_arguments,) = [t["inputSchema"] for t in tools if t["name"] == "list_files"]
    assert no_arguments["properties"] == {} and no_arguments["additionalProperties"] is False


def test_each_tool_has_its_own_schemas(client: TestClient) -> None:
    """Every tool is bound to its own models: no two of the eight share an input or an output schema."""
    tools = {t["name"]: t for t in result(rpc(client, "tools/list", headers=HG))["tools"]}
    assert tools["list_files"]["inputSchema"]["properties"] == {}
    assert {"name", "rows"} <= set(tools["write_table"]["inputSchema"]["properties"])
    for key in ("inputSchema", "outputSchema"):
        schemas = {json.dumps(tool[key], sort_keys=True) for tool in tools.values()}
        assert len(schemas) == len(EIGHT), key


def test_display_file_is_nowhere_in_ghostwriters_tools(client: TestClient) -> None:
    """The list answers, and names no display_file anywhere; a call of it is an unknown tool, never a 500."""
    listed = rpc(client, "tools/list", headers=HG)
    assert "display_file" not in json.dumps(result(listed))
    body = error(acall(client, "display_file", {"path": f"{CHAT}/x"}), 400, -32602)
    assert body["error"]["message"] == "unknown tool"


def test_discover_tells_each_form_its_own_instructions(client: TestClient) -> None:
    """Ghostwriter, with workspace files, gets the workspace's instructions; docint, inline, version 2's."""
    assert result(rpc(client, "server/discover", headers=HG))["instructions"] == WORKSPACE_INSTRUCTIONS
    assert result(rpc(client, "server/discover", headers=HD))["instructions"] == INSTRUCTIONS


# --- the result ----------------------------------------------------------------------------------------------------


def test_a_call_answers_the_rest_model_plus_outputs(client: TestClient, store: Store) -> None:
    """The REST answer with its null fields, and the new file by reference: id, path, name, type, size, preview."""
    answer = result(acall(client, "write_table", TABLE))
    rec = store.get(PG, f"{CHAT}/counts.csv")
    assert answer["isError"] is False and [b["type"] for b in answer["content"]] == ["text"]
    assert answer["structuredContent"] == {
        "path": rec.virtual_path,
        "rows": 2,
        "hint": None,
        "outputs": [
            {
                "id": rec.id,
                "path": rec.virtual_path,
                "name": "counts.csv",
                "mime": "text/csv",
                "size": rec.size,
                "preview": PREVIEW,
            }
        ],
    }


def test_every_answer_holds_to_the_output_schema_it_lists(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path
) -> None:
    """Each tool's structured answer is valid against its outputSchema from tools/list, its null fields kept.

    The template's text controls have no options, so inspect_template answers ``options: null`` for them.
    """
    schemas = {t["name"]: t["outputSchema"] for t in result(rpc(client, "tools/list", headers=HG))["tools"]}
    form = keep(store, PG, "form.docx", mt.make_controls_docx(tmp_path / "form.docx").read_bytes())
    keep(store, PG, "q.csv")
    assistant = {"kind": "assistant"}
    calls: list[tuple[str, dict[str, Any]]] = [
        ("list_files", {}),
        ("read_document", {"path": f"{CHAT}/q.csv"}),
        ("search_files", {"query": "user-a"}),
        ("inspect_template", {"path": form.virtual_path}),
        ("fill_template", {"path": form.virtual_path, "values": {"customer": {"value": "x", "source": assistant}}}),
        ("write_table", TABLE),
        ("write_document", {"name": "notes", "format": "md", "markdown": "# Hafen"}),
    ]
    answers = {tool: result(acall(client, tool, arguments)) for tool, arguments in calls}
    fake_executor.payload = ran("ok")
    fake_executor.files = {"chart.png": PNG, "counts.csv": CSV}
    fake_executor.previews = {"counts.csv": PREVIEW}
    answers["run_python"] = result(acall(client, "run_python", {"code": "print('ok')"}))
    assert set(answers) == EIGHT
    for tool, answer in answers.items():
        assert answer["isError"] is False, tool
        validator = jsonschema.Draft202012Validator(schemas[tool])
        assert [e.message for e in validator.iter_errors(answer["structuredContent"])] == [], tool
    (customer,) = [f for f in answers["inspect_template"]["structuredContent"]["fields"] if f["name"] == "customer"]
    assert customer["options"] is None


def test_the_text_block_leaves_the_previews_out(client: TestClient) -> None:
    """The text is the structured answer as compact JSON without each output's preview, and nothing else changes."""
    answer = result(acall(client, "write_table", TABLE))
    (text,) = answer["content"]
    structured = answer["structuredContent"]
    assert structured["outputs"][0]["preview"] == PREVIEW
    assert json.loads(text["text"]) == without_previews(structured)
    assert text["text"] == json.dumps(without_previews(structured), ensure_ascii=False, separators=(",", ":"))


def test_a_tool_that_writes_nothing_has_empty_outputs(client: TestClient, store: Store) -> None:
    """``list_files`` answers the chat's files and no outputs."""
    rec = keep(store, PG, "quarterly-figures.csv")
    answer = result(acall(client, "list_files"))
    assert answer["isError"] is False
    assert [f["path"] for f in answer["structuredContent"]["files"]] == [rec.virtual_path]
    assert answer["structuredContent"]["outputs"] == []


def test_one_call_is_one_job_and_one_event(
    client: TestClient, settings: Settings, store: Store, audit_lines: AuditLines
) -> None:
    """The preview is read inside the call's one job: no second job, no second event."""
    assert result(acall(client, "write_table", TABLE))["isError"] is False
    rec = store.get(PG, f"{CHAT}/counts.csv")
    assert job_rows(settings) == [("ghostwriter", "chat-1", "write_table")]
    (event,) = tool_events(audit_lines)
    assert (event["operation"], event["outcome"], event["target"]["ids"]) == ("tool.write_table", "ok", [rec.id])


def test_run_pythons_outputs_are_every_file_it_wrote(settings: Settings, store: Store, audit: AuditLog) -> None:
    """Each file the code wrote is an output, in the order made, with the preview its table has and none for a chart."""
    executor = Scripted(settings.data_dir)
    executor.payload = ran("ok")
    executor.files = {"chart.png": PNG, "counts.csv": CSV}
    executor.previews = {"counts.csv": PREVIEW}
    queue = JobQueue(settings, executor, store, Metrics(settings.data_dir))
    with TestClient(create_app(settings, store=store, audit=audit, jobs=queue)) as client:
        answer = result(acall(client, "run_python", {"code": "print('ok')"}))
    structured = answer["structuredContent"]
    outputs = structured["outputs"]
    assert [o["name"] for o in outputs] == ["chart.png", "counts.csv"]
    assert [o["mime"] for o in outputs] == ["image/png", "text/csv"]
    assert [o["preview"] for o in outputs] == [None, PREVIEW]
    assert [o["size"] for o in outputs] == [len(PNG), len(CSV)]
    assert structured["files"] == [o["path"] for o in outputs]
    assert [o["id"] for o in outputs] == [store.get(PG, o["path"]).id for o in outputs]
    assert executor.requests[0].previews is True


def test_a_taken_name_comes_back_numbered_with_its_own_preview(client: TestClient) -> None:
    """The model writes counts.csv twice: the second is counts (1).csv, with its own id and its own preview."""
    first = result(acall(client, "write_table", TABLE))["structuredContent"]["outputs"][0]
    second = result(acall(client, "write_table", {**TABLE, "rows": [["user-c", 3]]}))["structuredContent"]
    (output,) = second["outputs"]
    assert output["name"] == "counts (1).csv"
    assert output["path"] == second["path"] and output["path"].endswith("/counts (1).csv")
    assert output["id"] != first["id"]
    assert output["preview"]["rows"] == [["user-c", "3"]]
    assert first["preview"] == PREVIEW


def test_output_names_come_back_as_written(client: TestClient) -> None:
    """Spaces, umlauts and a hash come back exactly in the name and the path, and the view finds the file by it."""
    arguments = {"name": "Übersicht März #1", "format": "md", "markdown": "# Hafen"}
    (output,) = result(acall(client, "write_document", arguments))["structuredContent"]["outputs"]
    assert output["name"] == "Übersicht März #1.md"
    assert output["path"] == f"{CHAT}/Übersicht März #1.md"
    viewed = client.get("/files/view", params={"path": output["path"]}, headers=HG)
    assert viewed.status_code == 200 and b"# Hafen" in viewed.content


# --- the chat ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("read_document", {"path": "/workspace/chat-2/q.csv"}),
        ("inspect_template", {"path": "/workspace/chat-2/t.docx"}),
        ("read_document", {"path": "/workspace/chat-1/../chat-2/q.csv"}),
        (
            "fill_template",
            {
                "path": "/workspace/chat-2/t.docx",
                "values": {"customer": {"value": "x", "source": {"kind": "assistant"}}},
            },
        ),
    ],
)
def test_another_chats_path_is_refused_as_not_found(
    client: TestClient,
    store: Store,
    fake_executor: Local,
    audit_lines: AuditLines,
    tool: str,
    arguments: dict[str, Any],
) -> None:
    """The user's file in another chat is not found, by any spelling: no job is sent and nothing is recorded."""
    keep(store, P2, "q.csv")
    keep(store, P2, "t.docx", b"PK")
    r = acall(client, tool, arguments)
    assert refused(r) == {"reason": "refused"}
    assert result(r)["content"][0]["text"] == "not found"
    assert fake_executor.requests == []
    assert tool_events(audit_lines) == []


def test_a_source_in_another_chat_is_refused(
    client: TestClient, store: Store, fake_executor: Local, audit_lines: AuditLines, tmp_path: Path
) -> None:
    """A fill of this chat's template refuses a file or a job of another chat as a value's source, and sends no job.

    The same fill with a file of this chat as the source fills the template.
    """
    template = keep(store, PG, "form.docx", mt.make_controls_docx(tmp_path / "form.docx").read_bytes())
    keep(store, PG, "q.csv")
    keep(store, P2, "q.csv")
    job = store.add_job(P2, "run_python")
    store.finish_job(job.id, "ok")

    def fill(source: dict[str, Any]) -> httpx2.Response:
        values = {"customer": {"value": "user-a", "source": source}}
        return acall(client, "fill_template", {"path": template.virtual_path, "values": values})

    for source in ({"kind": "file", "path": "/workspace/chat-2/q.csv"}, {"kind": "job", "job_id": job.id}):
        assert refused(fill(source)) == {"reason": "refused"}, source
    assert fake_executor.requests == []
    assert tool_events(audit_lines) == []
    filled = result(fill({"kind": "file", "path": f"{CHAT}/q.csv"}))
    assert filled["isError"] is False
    assert [o["name"] for o in filled["structuredContent"]["outputs"]] == ["form-filled.docx"]


def test_open_webuis_files_are_not_ghostwriters(client: TestClient, store: Store, fake_executor: Local) -> None:
    """Open WebUI's file in a chat of the same name and user is not Ghostwriter's."""
    put(store, "q.csv", CSV)
    r = acall(client, "read_document", {"path": f"{CHAT}/q.csv"})
    assert refused(r) == {"reason": "refused"}
    assert result(r)["content"][0]["text"] == "not found"
    assert fake_executor.requests == []


# --- the limits ----------------------------------------------------------------------------------------------------


def test_ghostwriters_call_holds_its_limits(
    client: TestClient, app: FastAPI, store: Store, fake_executor: Local, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its wall, its five-second wait and its two slots; the job keeps the wall, the runner leaves the preview 5 s.

    The CPU limit is the job's wall, never less, so a preview never meets it before the wall.
    """
    seen = spy_submit(app, monkeypatch)
    keep(store, PG, "q.csv")
    fake_executor.payload = ran()
    assert result(acall(client, "run_python", {"code": "print(1)"}))["isError"] is False
    fake_executor.payload = None
    assert result(acall(client, "read_document", {"path": f"{CHAT}/q.csv"}))["isError"] is False
    assert seen == [{"call": CallLimits(120, 5, 2), "previews": True}] * 2
    python, read = fake_executor.requests
    for req in (python, read):
        assert (req.limits.wall_s, req.limits.cpu_s, req.previews) == (120, 120, True)
    assert python.args["budget_s"] == (120 - PREVIEW_RESERVE_S) * 9 // 10 == 103
    assert read.args["budget_s"] == (120 - PREVIEW_RESERVE_S) * 3 // 4 == 86


def test_a_lower_caller_wall_is_the_jobs_wall(
    settings: Settings, store: Store, audit: AuditLog, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a 30 s wall of its own, the job has 30 s and the code 9/10 of the 25 s left after the preview's 5."""
    lower = dataclasses.replace(settings, ghostwriter_wall_s=30)
    executor = Scripted(settings.data_dir)
    executor.payload = ran()
    lowered = create_app(
        lower, store=store, audit=audit, jobs=JobQueue(lower, executor, store, Metrics(lower.data_dir))
    )
    seen = spy_submit(lowered, monkeypatch)
    with TestClient(lowered) as client:
        assert result(acall(client, "run_python", {"code": "print(1)"}))["isError"] is False
    assert seen == [{"call": CallLimits(30, 5, 2), "previews": True}]
    (req,) = executor.requests
    assert (req.limits.wall_s, req.limits.cpu_s) == (30, 30)
    assert req.args["budget_s"] == (30 - PREVIEW_RESERVE_S) * 9 // 10 == 22


def test_ghostwriters_waiting_calls_are_capped_across_many_users(
    settings: Settings, store: Store, audit: AuditLog
) -> None:
    """Ghostwriter's users are pseudonyms each: 2 run, 8 wait (2 * 16 // 4), the 11th is refused at once.

    Open WebUI's user still runs meanwhile, on a slot Ghostwriter's cap leaves free.
    """
    executor = Scripted(settings.data_dir)
    executor.payload = ran()
    executor.hold = {f"ghostwriter/user-{n}" for n in range(10)}
    queue = JobQueue(settings, executor, store, Metrics(settings.data_dir))
    with TestClient(create_app(settings, store=store, audit=audit, jobs=queue)) as client:
        portal = client.portal
        assert portal is not None
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            calls = [
                pool.submit(acall, client, "run_python", {"code": "print(1)"}, headers={**HG, "X-User-Id": f"user-{n}"})
                for n in range(10)
            ]
            try:
                held = partial(
                    until,
                    lambda: executor.running == 2 and sample(queue.metrics, "workbench_queue_length") == 8,
                    seconds=scaled(5),
                )
                portal.call(held)
                eleventh = acall(client, "run_python", {"code": "print(1)"}, headers={**HG, "X-User-Id": "user-10"})
                assert refused(eleventh) == {"reason": "queue_full"}
                other = client.post("/tools/run_python", json={"code": "print(1)"}, headers=H)
                assert other.status_code == 200, other.text
                assert executor.running == 2
            finally:
                portal.call(executor.gate.set)
            answers = [result(done.result(timeout=scaled(10))) for done in calls]
    assert [answer["isError"] for answer in answers] == [False] * 10
    assert sorted(req.caller for req in executor.requests) == ["ghostwriter"] * 10 + ["open-webui"]


def test_the_terminal_door_asks_for_no_previews_and_no_caller_cap(
    client: TestClient, app: FastAPI, store: Store, fake_executor: Local, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Open WebUI's write_table submits with neither a call's limits nor previews, and answers no outputs."""
    seen = spy_submit(app, monkeypatch)
    r = client.post("/tools/write_table", json=TABLE, headers=H)
    assert r.status_code == 200, r.text
    assert seen == [{}]
    (req,) = fake_executor.requests
    assert (req.previews, req.limits.wall_s) == (False, 120)
    assert r.json() == {"path": f"{CHAT}/counts.csv", "rows": 2}


@pytest.mark.parametrize("reason", ["queue_full", "wait_timeout", "executor_busy"])
def test_a_queue_refusal_is_an_error_result_with_its_token(
    client: TestClient, app: FastAPI, monkeypatch: pytest.MonkeyPatch, reason: Any
) -> None:
    """The queue's own token, and the one short text for every refusal of the queue."""

    async def refuse(*_: object, **__: object) -> Any:
        raise Busy(reason)

    monkeypatch.setattr(app.state.jobs, "submit", refuse)
    r = acall(client, "write_table", TABLE)
    assert refused(r) == {"reason": reason}
    assert result(r)["content"][0]["text"] == "busy, retry later"


def test_without_a_queue_a_job_is_executor_busy_and_the_list_still_answers(
    client: TestClient, app: FastAPI, store: Store
) -> None:
    """No queue runs no job; ``list_files`` needs none."""
    app.state.jobs = None
    assert refused(acall(client, "write_table", TABLE)) == {"reason": "executor_busy"}
    keep(store, PG, "quarterly-figures.csv")
    assert result(acall(client, "list_files"))["structuredContent"]["outputs"] == []


def test_arguments_that_do_not_validate_are_invalid_arguments(client: TestClient, fake_executor: Local) -> None:
    """A table without its format, columns and rows, and an argument list_files does not take: nothing runs."""
    assert refused(acall(client, "write_table", {"name": "x"}))["reason"] == "invalid_arguments"
    assert refused(acall(client, "list_files", {"path": "/workspace/chat-2"}))["reason"] == "invalid_arguments"
    assert fake_executor.requests == []


# --- the purpose and the request id -------------------------------------------------------------------------------


def strict_client(settings: Settings, store: Store, audit: AuditLog, executor: Local, *actions: str) -> TestClient:
    """A client on an app that requires a stated purpose for ``actions``."""
    required = dataclasses.replace(settings, require_purpose=frozenset(actions))
    queue = JobQueue(required, executor, store, Metrics(required.data_dir))
    return TestClient(create_app(required, store=store, audit=audit, jobs=queue))


def test_the_guard_covers_every_recorded_workspace_tool() -> None:
    """Only ``list_files`` is recorded nowhere; ``GUARDED`` holds the other seven, each under its recorded action."""
    assert set(EIGHT) - set(OPERATIONS) == {"list_files"}
    assert {tool: action for tool, (action, _) in GUARDED.items()} == {
        tool: OPERATIONS[tool][0] for tool in EIGHT - {"list_files"}
    }


@pytest.mark.parametrize("tool", sorted(GUARDED))
def test_a_stated_purpose_and_request_id_reach_the_tool_event(
    client: TestClient,
    store: Store,
    audit_lines: AuditLines,
    caplog: pytest.LogCaptureFixture,
    fake_executor: Local,
    tmp_path: Path,
    tool: str,
) -> None:
    """Both come as the headers state them, the purpose as the parser normalizes it; no log line holds it."""
    caplog.set_level(logging.DEBUG)
    keep(store, PG, "q.csv")
    keep(store, PG, "form.docx", mt.make_controls_docx(tmp_path / "form.docx").read_bytes())
    if tool == "run_python":
        fake_executor.payload = ran("1")
    _, arguments = GUARDED[tool]
    assert result(acall(client, tool, arguments, headers=PURPOSE))["isError"] is False
    (event,) = tool_events(audit_lines)
    assert event["operation"] == f"tool.{tool}" and event["outcome"] == "ok"
    assert event["purpose"] == {"reason": "ghostwriter chat"}
    assert event["request_id"] == "req-1"
    assert event["actor"] == {"user": "user-a", "via": "service"}
    assert event["target"]["container"] == "chat:chat-1"
    assert event["criteria"]["caller"] == "ghostwriter"
    assert "ghostwriter chat" not in caplog.text and "ghostwriter+chat" not in caplog.text


def test_a_call_without_a_purpose_or_request_id_records_neither(client: TestClient, audit_lines: AuditLines) -> None:
    """No header, no field: nothing is made up, as no rule requires one here."""
    assert result(acall(client, "write_table", TABLE))["isError"] is False
    (event,) = tool_events(audit_lines)
    assert "purpose" not in event and "request_id" not in event


@pytest.mark.parametrize("tool", sorted(GUARDED))
def test_a_missing_required_purpose_is_denied_before_any_job(
    settings: Settings,
    store: Store,
    audit: AuditLog,
    audit_lines: AuditLines,
    fake_executor: Local,
    tool: str,
) -> None:
    """One ``denied`` event with no target, the refusal as an error result, and the executor is never asked."""
    action, arguments = GUARDED[tool]
    keep(store, PG, "q.csv")
    with strict_client(settings, store, audit, fake_executor, action) as client:
        answer = result(acall(client, tool, arguments, headers={**HG, "X-Request-ID": "req-1"}))
    assert answer["isError"] is True and answer["structuredContent"] == {"reason": "purpose_required"}
    assert answer["content"] == [{"type": "text", "text": "a stated purpose is required"}]
    event = tool_events(audit_lines)[-1]
    assert (event["action"], event["operation"], event["outcome"]) == (action, f"tool.{tool}", "denied")
    assert "target" not in event and "purpose" not in event
    assert event["request_id"] == "req-1"
    assert event["criteria"] == {"caller": "ghostwriter", "refused": "purpose_required", "container": "chat:chat-1"}
    assert len(tool_events(audit_lines)) == 1
    assert fake_executor.requests == []
    assert job_rows(settings) == []


def test_an_empty_purpose_is_none(
    settings: Settings, store: Store, audit: AuditLog, audit_lines: AuditLines, fake_executor: Local
) -> None:
    """A purpose header with neither a case reference nor a reason is no purpose: the denied event has none."""
    with strict_client(settings, store, audit, fake_executor, "collect") as client:
        answer = result(acall(client, "write_table", TABLE, headers={**HG, "X-Audit-Purpose": "case_ref=+&reason="}))
    assert answer["structuredContent"] == {"reason": "purpose_required"}
    assert fake_executor.requests == []
    assert "purpose" not in tool_events(audit_lines)[-1]


@pytest.mark.parametrize("purpose", ["reason=ghostwriter+chat", "case_ref=case-0001"])
def test_a_stated_purpose_meets_the_requirement(
    settings: Settings, store: Store, audit: AuditLog, audit_lines: AuditLines, fake_executor: Local, purpose: str
) -> None:
    """A reason or a case reference is a purpose: the job runs, and its event is ``ok``."""
    with strict_client(settings, store, audit, fake_executor, "collect") as client:
        answer = result(acall(client, "write_table", TABLE, headers={**HG, "X-Audit-Purpose": purpose}))
    assert answer["isError"] is False
    (event,) = tool_events(audit_lines)
    assert event["outcome"] == "ok" and event["purpose"]
    assert len(fake_executor.requests) == 1


def test_list_files_needs_no_purpose(settings: Settings, store: Store, audit: AuditLog, fake_executor: Local) -> None:
    """It is recorded nowhere, so no rule can cover it: with every action required it still answers."""
    everything = ("query", "combine", "modify", "collect", "disclose")
    rec = keep(store, PG, "q.csv")
    with strict_client(settings, store, audit, fake_executor, *everything) as client:
        answer = result(acall(client, "list_files"))
    assert answer["isError"] is False
    assert [f["path"] for f in answer["structuredContent"]["files"]] == [rec.virtual_path]


def test_the_terminal_door_keeps_its_rule(
    settings: Settings, store: Store, audit: AuditLog, audit_lines: AuditLines, fake_executor: Local
) -> None:
    """Open WebUI states no purpose: its read_document answers 200 whatever the rule says, and nothing is denied."""
    put(store, "q.csv", CSV)
    with strict_client(settings, store, audit, fake_executor, "query") as client:
        r = client.post("/tools/read_document", json={"path": f"{CHAT}/q.csv"}, headers=H)
    assert r.status_code == 200, r.text
    assert [e for e in audit_lines() if e["outcome"] == "denied"] == []


# --- the routes both doors run -------------------------------------------------------------------------------------


def test_every_mcp_tool_route_takes_only_known_parameters(app: FastAPI) -> None:
    """The adapter passes each parameter by its name, so a route of the eight may take no other."""
    endpoints = {route.operation_id: route.endpoint for route in tool_routes(app)}
    for name in EIGHT:
        endpoint = endpoints[name]
        assert endpoint is not None and set(inspect.signature(endpoint).parameters) <= KNOWN, name


def test_the_terminal_document_is_unchanged(app: FastAPI) -> None:
    """Open WebUI's nine operations keep their texts: the MCP door's wording is a copy."""
    doc = tool_openapi(app, cast(CallerRegistry, app.state.callers).get("open-webui"))
    operations = [operation for path in doc["paths"].values() for operation in path.values()]
    assert len(operations) == 9
    assert doc["paths"]["/tools/write_table"]["post"]["description"].endswith("Show the result with display_file.")
    path = doc["components"]["schemas"]["WrittenTable"]["properties"]["path"]
    assert path["description"].endswith("show it with display_file.")
