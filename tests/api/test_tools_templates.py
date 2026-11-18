"""The ``inspect_template`` and ``fill_template`` tools on the MCP door, as Ghostwriter calls them.

The job runs the real runner in this process (:class:`Local`), in a folder made as the supervisor makes one from the
files the request names, so a filled file is a real one; a test that needs another answer scripts it. The runners
themselves are tested in ``tests/executor/test_templates.py``; here the api's part: which files go to the job, the
checks of the sources the api can make, the output's name and record, the audit event and the caps.
"""

import asyncio
import dataclasses
import io
import json
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, override

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api.conftest import CHAT, HG, PG, AuditLines, put, unrecorded
from tests.api.mcp_calls import (
    Headers,
    acall,
    answered,
    error,
    raw_call,
    refusal,
    refused,
    result,
    rpc,
    sent_size,
    unavailable,
)
from tests.api.test_jobs import FakeClient, KeepAll
from tests.fixtures import make_fixtures as mk
from tests.fixtures import make_templates as mt
from workbench.api.audit import AuditLog
from workbench.api.jobs import JobQueue, RequestTooLarge
from workbench.api.main import create_app
from workbench.api.metrics import Metrics
from workbench.api.routing import MAX_DEPTH
from workbench.api.store import Store
from workbench.config import Settings
from workbench.executor.preview import PREVIEW_TOTAL_S, previews_of
from workbench.executor.runners import RUNNERS, JobContext
from workbench.models import FileRecord, Principal
from workbench.protocol import (
    MAX_LINE,
    JobRequest,
    JobResult,
    Limits,
    OutputFile,
    TablePreview,
    decode_request,
    encode,
)

ASSISTANT: dict[str, Any] = {"kind": "assistant"}


class Local(FakeClient):
    """An executor that runs the real runner in this process, over a copy of the files a job names.

    A job that asks for previews (``req.previews``) gets those the executor's own reader gives of its outputs.

    Attributes:
        payload: When set, the answer instead of the runner's, with the fake's ``status`` and ``files``.
    """

    def __init__(self, data_dir: Path, jobs: Path) -> None:
        """Make job folders below ``jobs``."""
        super().__init__(data_dir)
        self._data = data_dir
        self._jobs = jobs
        self.payload: dict[str, Any] | None = None

    @override
    async def run(self, req: JobRequest, timeout_s: float) -> JobResult:
        """Copy the inputs in, run the runner, stage what it wrote into ``out/``."""
        if len(encode(req)) > MAX_LINE:
            raise RequestTooLarge("request too large")
        if self.payload is not None or self.status != "ok":
            base = await super().run(req, timeout_s)
            return base.model_copy(update={"result": self.payload or {}})
        self.requests.append(req)
        chat = self._data / "workspaces" / req.caller / req.user_id / req.chat_id
        work = self._jobs / req.job_id / "workspace" / req.chat_id
        (work / "out").mkdir(parents=True)
        for name in req.input_files:
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(chat / name, work / name)
        ctx = JobContext(work, work / "out", req.chat_id, req.max_chars, req.limits.file_mb)
        result = json.loads(json.dumps(RUNNERS[req.tool](json.loads(json.dumps(req.args)), ctx)))
        if set(result) == {"error"}:
            return JobResult(job_id=req.job_id, status="error", result=result, outputs=[], message="the tool failed")
        found = previews_of(work / "out", time.monotonic() + PREVIEW_TOTAL_S) if req.previews else {}
        outputs = []
        for made in sorted((work / "out").iterdir()):
            staged = self.staged(req.job_id, made.name)
            staged.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(made, staged)
            outputs.append(
                OutputFile(
                    name=made.name, staged_path=str(staged), size=staged.stat().st_size, preview=found.get(made.name)
                )
            )
        return JobResult(job_id=req.job_id, status="ok", result=result, outputs=outputs)


@pytest.fixture
def fake_executor(settings: Settings, tmp_path: Path) -> Local:
    """The in-process executor."""
    return Local(settings.data_dir, tmp_path / "jobs")


@pytest.fixture
def app(settings: Settings, store: Store, audit: AuditLog, fake_executor: Local) -> FastAPI:
    """The real app with a job queue over the in-process executor."""
    queue = JobQueue(settings, fake_executor, store, Metrics(settings.data_dir))
    return create_app(settings, store=store, audit=audit, jobs=queue)


def small(settings: Settings, store: Store, audit: AuditLog, executor: Local) -> TestClient:
    """A client on an app whose answers may be 1 KB."""
    limited = dataclasses.replace(settings, result_max_kb=1)
    queue = JobQueue(limited, executor, store, Metrics(settings.data_dir))
    return TestClient(create_app(limited, store=store, audit=audit, jobs=queue))


def stored(store: Store, tmp_path: Path, name: str, build: Callable[..., Path], **where: Any) -> FileRecord:
    """Build a file at test time and store it as an upload of the chat."""
    built = build(tmp_path / "built" / name) if not where else build(tmp_path / "built" / name, **where)
    return put(store, name, built.read_bytes(), p=PG)


@pytest.fixture(autouse=True)
def built(tmp_path: Path) -> None:
    """A folder to build files in."""
    (tmp_path / "built").mkdir()


def inspect(client: TestClient, path: str, headers: Headers = HG) -> httpx2.Response:
    """Call ``inspect_template`` as Ghostwriter does, on the MCP door."""
    return acall(client, "inspect_template", {"path": path}, headers=headers)


def fill(client: TestClient, path: str, values: dict[str, Any], headers: Headers = HG, **more: Any) -> httpx2.Response:
    """Call ``fill_template`` on the MCP door; a value given bare gets the assistant as its source."""
    given = {
        key: value if isinstance(value, dict) and "source" in value else {"value": value, "source": ASSISTANT}
        for key, value in values.items()
    }
    return acall(client, "fill_template", {"path": path, "values": given, **more}, headers=headers)


def events(audit_lines: AuditLines, tool: str) -> list[dict[str, Any]]:
    """The audit events of one tool."""
    return [e for e in audit_lines() if e["operation"] == f"tool.{tool}"]


def outputs(store: Store) -> list[FileRecord]:
    """The files tools made in Ghostwriter's chat."""
    return [rec for rec in store.files_in_chat(PG) if rec.kind == "output"]


def test_the_local_executor_reads_the_previews_a_job_asks_for(
    settings: Settings, store: Store, fake_executor: Local
) -> None:
    """The real reader fills the previews of the outputs of a job that asked, and only of that one."""
    queue = JobQueue(settings, fake_executor, store, Metrics(settings.data_dir))
    args = {"name": "counts.csv", "format": "csv", "columns": ["author", "posts"], "rows": [["user-a", 12]]}
    asked, unasked = KeepAll(), KeepAll()

    async def run() -> None:
        await queue.submit(PG, "write_table", args, [], asked, previews=True)
        await queue.submit(PG, "write_table", {**args, "name": "other.csv"}, [], unasked)

    asyncio.run(run())

    (result,) = asked.results
    assert [(o.name, o.preview) for o in result.outputs] == [
        ("counts.csv", TablePreview(columns=["author", "posts"], rows=[["user-a", "12"]], row_count=1, truncated=False))
    ]
    (result,) = unasked.results
    assert [(o.name, o.preview) for o in result.outputs] == [("other.csv", None)]


# --- inspect_template ---------------------------------------------------------------------------------------------


def test_inspect_answers_the_fields_and_records_a_query(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path, audit_lines: AuditLines
) -> None:
    """The template alone goes to the job; the answer names it by the store's path; one query event by its id."""
    form = stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    put(store, "other.txt", p=PG)
    body = answered(inspect(client, f"{CHAT}/form.docx"))
    assert body["path"] == f"{CHAT}/form.docx"
    assert body["template_kind"] == "content_controls"
    assert [f["name"] for f in body["fields"]] == ["customer", "report_date", "status", "approved", "summary"]
    assert body["truncated"] is False
    (req,) = fake_executor.requests
    assert (req.tool, req.input_files) == ("inspect_template", ["form.docx"])
    assert req.args == {"path": "form.docx", "budget_s": 86}
    (event,) = events(audit_lines, "inspect_template")
    assert (event["action"], event["target"]["ids"], event["outcome"]) == ("query", [form.id], "ok")


def test_inspect_none_carries_the_hint(client: TestClient, store: Store, tmp_path: Path) -> None:
    """A document without fields: kind none, and the hint for the user."""
    stored(store, tmp_path, "plain.docx", mk.make_docx)
    body = answered(inspect(client, f"{CHAT}/plain.docx"))
    assert (body["template_kind"], body["fields"]) == ("none", [])
    assert "version 1 cannot fill" in body["hint"]


@pytest.mark.parametrize("name", ["macro.docm", "macro.xlsm", "model.xltx", "notes.pdf"])
def test_other_files_are_refused_before_any_job(
    client: TestClient, store: Store, fake_executor: Local, name: str
) -> None:
    """Only .docx, .dotx and .xlsx; the answer names them, and nothing is sent."""
    put(store, name, b"PK", p=PG)
    for r in (inspect(client, f"{CHAT}/{name}"), fill(client, f"{CHAT}/{name}", {"x": "y"})):
        reason, detail = refusal(r)
        assert reason == "refused"
        assert detail.startswith("unsupported_template: ")
        for ext in (".docx", ".dotx", ".xlsx"):
            assert ext in detail
    assert fake_executor.requests == []


def test_another_users_template_is_not_found(client: TestClient, store: Store, tmp_path: Path) -> None:
    """A path outside the caller's own files is not found, as for every tool."""
    theirs = Principal("ghostwriter", "user-b", "chat-1")
    data = mt.make_controls_docx(tmp_path / "built" / "form.docx").read_bytes()
    store.add_upload(theirs, CHAT, "form.docx", io.BytesIO(data), max_bytes=10**7, quota_bytes=10**8, record=unrecorded)
    assert refusal(inspect(client, f"{CHAT}/form.docx")) == ("refused", "not found")
    assert refusal(fill(client, f"{CHAT}/form.docx", {"customer": "x"})) == ("refused", "not found")


def test_an_inspection_is_cut_at_the_result_limit(
    settings: Settings, store: Store, audit: AuditLog, fake_executor: Local, tmp_path: Path
) -> None:
    """The fields that fit, ``truncated``, and how many there are in all."""
    controls = [mt.text_control(f"field_{n:03d}") for n in range(60)]
    stored(store, tmp_path, "many.docx", mt.make_controls_body, controls=controls)
    with small(settings, store, audit, fake_executor) as client:
        body = answered(inspect(client, f"{CHAT}/many.docx"))
    assert sent_size(body) <= 1024
    assert body["truncated"] is True
    assert f"{len(body['fields'])} of 60 fields" in body["hint"]


# --- fill_template ------------------------------------------------------------------------------------------------


def test_fill_writes_a_new_file_and_records_a_modify(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path, audit_lines: AuditLines
) -> None:
    """The output is a file of the chat named after the template; the event names template, sources and output."""
    form = stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    pdf = stored(store, tmp_path, "q.pdf", lambda path: mk.make_pdf(path, ["one", "two"]))
    values = {
        "customer": {"value": "Invented Harbour Ltd", "source": {"kind": "user", "note": "said in the chat"}},
        "report_date": {"value": "2026-03-07", "source": {"kind": "file", "path": f"{CHAT}/q.pdf", "page": 2}},
        "approved": True,
    }
    answer = answered(fill(client, f"{CHAT}/form.docx", values))
    assert {key: value for key, value in answer.items() if key != "outputs"} == {
        "path": f"{CHAT}/form-filled.docx",
        "filled": 3,
        "unfilled": ["status", "summary"],
        "warnings": [],
    }
    (req,) = fake_executor.requests
    assert req.input_files == ["form.docx", "q.pdf"]
    assert req.args["output_name"] == "form-filled.docx"
    assert req.args["values"]["report_date"]["source"] == {
        "kind": "file",
        "path": "q.pdf",
        "virtual": f"{CHAT}/q.pdf",
        "page": 2,
    }
    (made,) = outputs(store)
    assert (made.name, made.job_id) == ("form-filled.docx", req.job_id)
    assert [output["id"] for output in answer["outputs"]] == [made.id]
    (event,) = events(audit_lines, "fill_template")
    assert event["action"] == "modify"
    assert event["target"]["ids"] == [form.id, pdf.id, made.id]
    assert event["criteria"] == {
        "caller": "ghostwriter",
        "filled_by_source": {"file": 1, "job": 0, "user": 1, "assistant": 1},
    }
    raw = json.dumps(audit_lines())
    for secret in ("Invented Harbour", "customer", "said in the chat", "q.pdf", "form"):
        assert secret not in raw, secret


def test_a_name_that_is_taken_gets_a_number_and_the_answer_names_the_stored_file(
    client: TestClient, store: Store, tmp_path: Path
) -> None:
    """The store adds the number; the model hears the name the file has."""
    stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    put(store, "form-filled.docx", p=PG)
    assert answered(fill(client, f"{CHAT}/form.docx", {"customer": "x"}))["path"] == f"{CHAT}/form-filled (1).docx"


def test_the_default_name_of_a_long_multibyte_template_is_cut_to_a_valid_name(
    client: TestClient, store: Store, tmp_path: Path
) -> None:
    """A name may have 255 bytes: 83 euro signs and .docx are 254, so the default name is cut by bytes, not length."""
    template = "€" * 83 + ".docx"
    put(store, template, mt.make_controls_docx(tmp_path / "built" / "form.docx").read_bytes(), p=PG)
    name = answered(fill(client, f"{CHAT}/{template}", {"customer": "x"}))["path"].rsplit("/", 1)[1]
    assert name == "€" * 81 + "-filled.docx"
    assert len(name.encode()) == 255


def test_a_form_field_that_could_not_be_filled_is_not_counted_as_filled(
    client: TestClient, store: Store, tmp_path: Path, audit_lines: AuditLines
) -> None:
    """A form field that runs over a paragraph break is unfilled, and the event does not count its source."""
    stored(store, tmp_path, "split.docx", mt.make_split_form_field_docx)
    values = {"name": {"value": "x", "source": {"kind": "user"}}, "city": "y"}
    body = answered(fill(client, f"{CHAT}/split.docx", values))
    assert (body["filled"], body["unfilled"]) == (1, ["name"])
    (event,) = events(audit_lines, "fill_template")
    assert event["criteria"]["filled_by_source"] == {"file": 0, "job": 0, "user": 0, "assistant": 1}


def test_the_runners_mirrored_bounds_are_the_apis() -> None:
    """The runner cuts to the api's bounds without importing the api; a change on one side must reach the other."""
    from workbench.api import tools, tools_templates
    from workbench.executor.runners import templates

    assert templates.MAX_NAME == tools_templates.MAX_NAME
    assert templates.WARNING_MAX == tools.MAX_HINT
    assert templates.LOCATION_MAX == tools_templates.MAX_LOCATION


def test_a_dotx_template_is_filled_into_a_docx(client: TestClient, store: Store, tmp_path: Path) -> None:
    """The default name and any name given end in .docx."""
    path = mt.as_dotx(mt.make_controls_docx(tmp_path / "built" / "a.docx"), tmp_path / "built" / "form.dotx")
    put(store, "form.dotx", path.read_bytes(), p=PG)
    assert answered(fill(client, f"{CHAT}/form.dotx", {"customer": "x"}))["path"] == f"{CHAT}/form-filled.docx"
    named = fill(client, f"{CHAT}/form.dotx", {"customer": "x"}, output_name="for-the-board.docx")
    assert answered(named)["path"] == f"{CHAT}/for-the-board.docx"
    refused_name = fill(client, f"{CHAT}/form.dotx", {"customer": "x"}, output_name="x.dotx")
    assert refusal(refused_name) == ("refused", "output_name must be a plain file name ending in .docx")


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("summary.xls", "refused"),
        ("summary", "refused"),
        ("../summary.xlsx", "refused"),
        ("a/b.xlsx", "refused"),
        ("", "refused"),
        ("x" * 300 + ".xlsx", "invalid_arguments"),
    ],
)
def test_an_output_name_must_be_a_plain_name_with_the_outputs_extension(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path, name: str, reason: str
) -> None:
    """Anything else is refused, past its bound already as an argument, and nothing runs."""
    stored(store, tmp_path, "summary.xlsx", mt.make_placeholder_xlsx)
    assert refused(fill(client, f"{CHAT}/summary.xlsx", {"total": 1}, output_name=name))["reason"] == reason
    assert fake_executor.requests == []


def test_a_workbook_is_filled_into_a_workbook(client: TestClient, store: Store, tmp_path: Path) -> None:
    """The default name keeps the extension; the warnings come through."""
    stored(store, tmp_path, "summary.xlsx", mt.make_placeholder_xlsx)
    body = answered(fill(client, f"{CHAT}/summary.xlsx", {"total": 1, "colour": "blue"}))
    assert body["path"] == f"{CHAT}/summary-filled.xlsx"
    assert body["unfilled"] == ["customer", "rows"]
    assert any("colour" in warning for warning in body["warnings"])


def test_sources_must_exist(client: TestClient, store: Store, fake_executor: Local, tmp_path: Path) -> None:
    """Each source the api or the job cannot find is refused, naming the field.

    A missing file, another user's or another chat's job, a job that is no finished run_python run, and a page past
    the end; none of the answers says whether a job exists for someone else.
    """
    stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    stored(store, tmp_path, "q.pdf", lambda path: mk.make_pdf(path, ["one", "two"]))
    other_chat_p = Principal("ghostwriter", "user-a", "chat-2")
    put(store, "elsewhere.pdf", directory="/workspace/chat-2", p=other_chat_p)
    mine = store.add_job(PG, "run_python")
    store.finish_job(mine.id, "ok")
    theirs = store.add_job(Principal("ghostwriter", "user-b", "chat-1"), "run_python")
    store.finish_job(theirs.id, "ok")
    other_chat = store.add_job(other_chat_p, "run_python")
    store.finish_job(other_chat.id, "ok")
    failed = store.add_job(PG, "run_python")
    store.finish_job(failed.id, "error")
    reading = store.add_job(PG, "read_document")
    store.finish_job(reading.id, "ok")

    def job(job_id: str) -> dict[str, Any]:
        return {"value": "x", "source": {"kind": "job", "job_id": job_id}}

    def file(path: str, **where: Any) -> dict[str, Any]:
        return {"value": "x", "source": {"kind": "file", "path": path, **where}}

    refused = {
        "missing file": file(f"{CHAT}/missing.pdf"),
        "another chat's file": file("/workspace/chat-2/elsewhere.pdf"),
        "outside the workspace": file("/etc/passwd"),
        "another user's job": job(theirs.id),
        "another chat's job": job(other_chat.id),
        "a failed job": job(failed.id),
        "not run_python": job(reading.id),
        "no such job": job("0" * 32),
        "page 99": file(f"{CHAT}/q.pdf", page=99),
    }
    answers: dict[str, str] = {}
    for case, value in refused.items():
        reason, detail = refusal(fill(client, f"{CHAT}/form.docx", {"customer": value}))
        assert reason == "refused", case
        assert detail.startswith("invalid_source: customer: "), (case, detail)
        answers[case] = detail
    shapes = {
        answers[case].replace(job_id, "<id>")
        for case, job_id in (("another user's job", theirs.id), ("no such job", "0" * 32))
    }
    assert len(shapes) == 1
    assert "page 99" in answers["page 99"]
    assert outputs(store) == []
    answered(fill(client, f"{CHAT}/form.docx", {"customer": job(mine.id)}))


def test_the_files_a_fill_names_go_to_the_job_once(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path
) -> None:
    """The template and each file source, by their paths below the chat folder."""
    stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    stored(store, tmp_path, "quarterly-figures.xlsx", mk.make_xlsx)
    cited = {"kind": "file", "path": f"{CHAT}/quarterly-figures.xlsx", "sheet": "Sheet1", "cell": "B2"}
    values = {"customer": {"value": "x", "source": cited}, "summary": {"value": "y", "source": cited}}
    answered(fill(client, f"{CHAT}/form.docx", values))
    assert fake_executor.requests[0].input_files == ["form.docx", "quarterly-figures.xlsx"]


def test_a_value_the_field_does_not_take_is_the_runners_refusal(
    client: TestClient, store: Store, tmp_path: Path
) -> None:
    """The runner's text, naming the field; no file is made."""
    stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    reason, detail = refusal(fill(client, f"{CHAT}/form.docx", {"status": "pending"}))
    assert reason == "refused"
    assert detail.startswith('invalid_value: status is a drop-down and takes one of: "open", "closed"')
    assert outputs(store) == []


def test_outputs_whose_fill_cannot_be_recorded_are_removed(
    client: TestClient, store: Store, tmp_path: Path, broken_audit: None
) -> None:
    """As with an upload: a file that could not be recorded does not stay."""
    stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    unavailable(fill(client, f"{CHAT}/form.docx", {"customer": "x"}))
    assert outputs(store) == []


def test_an_ok_answer_that_cannot_be_read_keeps_no_output_and_is_recorded_as_an_error(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path, audit_lines: AuditLines
) -> None:
    """The model is told the tool failed and never learns of the file, so it never becomes one; the job is an error."""
    form = stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    fake_executor.files = {"form-filled.docx": b"PK"}
    fake_executor.payload = {"filled": "all"}
    r = fill(client, f"{CHAT}/form.docx", {"customer": "x"})
    assert refusal(r) == ("refused", "the tool returned an answer that could not be read")
    assert outputs(store) == []
    (event,) = events(audit_lines, "fill_template")
    assert (event["action"], event["outcome"], event["target"]["ids"]) == ("modify", "error", [form.id])
    assert event["criteria"] == {"caller": "ghostwriter", "outputs": 0}


def test_a_fill_whose_output_was_not_kept_says_so(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path, audit_lines: AuditLines
) -> None:
    """An ok answer with no file kept (the executor dropped it) is not a success: the model is told why; an error."""
    stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    fake_executor.payload = {"filled": ["customer"], "unfilled": [], "warnings": []}
    fake_executor.message = "dropped 1 output: not the job's own file"
    reason, detail = refusal(fill(client, f"{CHAT}/form.docx", {"customer": "x"}))
    assert reason == "refused"
    assert "dropped 1 output" in detail
    (event,) = events(audit_lines, "fill_template")
    assert (event["outcome"], event["criteria"]["outputs"]) == ("error", 0)


def test_the_filled_file_is_recorded_before_it_is_a_file_of_the_chat(
    client: TestClient,
    store: Store,
    tmp_path: Path,
    audit_stream: io.StringIO,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the ``modify`` is written, the chat holds only the template; the new file it names is the one kept."""
    form = stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    seen: list[list[str]] = []
    write = audit_stream.write

    def look(text: str, /) -> int:
        if '"tool.fill_template"' in text:
            seen.append([rec.name for rec in store.files_in_chat(PG)])
        return write(text)

    monkeypatch.setattr(audit_stream, "write", look)
    answered(fill(client, f"{CHAT}/form.docx", {"customer": "x"}))
    assert seen == [["form.docx"]]
    (event,) = events(audit_lines, "fill_template")
    (new,) = outputs(store)
    assert event["target"]["ids"] == [form.id, new.id]


def test_unknown_names_the_model_sends_never_cost_the_filled_file(
    client: TestClient, store: Store, tmp_path: Path, audit_lines: AuditLines
) -> None:
    """Eight value names of 255 characters that are no field: one warning, within its bound; the file stays."""
    stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    unknown = {f"{n}" + "u" * 254: "y" for n in range(8)}
    body = answered(fill(client, f"{CHAT}/form.docx", {"customer": "x", **unknown}))
    assert all(len(warning) <= 2000 for warning in body["warnings"])
    assert [rec.name for rec in outputs(store)] == ["form-filled.docx"]
    assert len(events(audit_lines, "fill_template")) == 1


def test_a_runners_answer_over_the_apis_bounds_is_cut_never_a_500(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path
) -> None:
    """The backstop: an over-long warning is cut and the file kept; a field no value can name is left out."""
    stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    fake_executor.files = {"form-filled.docx": b"PK"}
    fake_executor.payload = {"filled": ["customer"], "unfilled": [], "warnings": ["w" * 5000]}
    assert answered(fill(client, f"{CHAT}/form.docx", {"customer": "x"}))["warnings"] == ["w" * 2000]
    assert [rec.name for rec in outputs(store)] == ["form-filled.docx"]

    def field(name: str, location: str = "body, paragraph 1") -> dict[str, Any]:
        return {"name": name, "kind": "placeholder", "type": "text", "options": None, "location": location}

    fake_executor.files = {}
    fake_executor.payload = {
        "template_kind": "placeholders",
        "fields": [field("n" * 400), field("short", "l" * 500)],
        "total": 2,
        "truncated": False,
        "hint": "h" * 3000,
    }
    body = answered(inspect(client, f"{CHAT}/form.docx"))
    assert [(f["name"], len(f["location"])) for f in body["fields"]] == [("short", 300)]
    assert len(body["hint"]) <= 2000


def test_a_busy_executor_is_executor_busy(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path
) -> None:
    """Nothing ran; retry later."""
    stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    fake_executor.status = "busy"
    assert refusal(fill(client, f"{CHAT}/form.docx", {"customer": "x"})) == ("executor_busy", "busy, retry later")
    assert refused(inspect(client, f"{CHAT}/form.docx"))["reason"] == "executor_busy"


def test_a_job_past_its_time_is_refused_with_its_reason(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path
) -> None:
    """The executor's reason, as for the reading tools."""
    stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    fake_executor.status = "timeout"
    fake_executor.message = "the job ran past its wall time of 120 s"
    assert refusal(fill(client, f"{CHAT}/form.docx", {"customer": "x"})) == (
        "refused",
        "the file could not be processed: the job ran past its wall time of 120 s",
    )


# --- the request ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {"path": f"{CHAT}/form.docx"},
        {"path": f"{CHAT}/form.docx", "values": {"x": {"value": 1}}},
        {"path": f"{CHAT}/form.docx", "values": {"x": {"value": 1, "source": {"kind": "rumour"}}}},
        {"path": f"{CHAT}/form.docx", "values": {"x": {"value": 1, "source": {"kind": "user", "note": "n" * 1001}}}},
        {"path": f"{CHAT}/form.docx", "values": {"n" * 256: {"value": 1, "source": ASSISTANT}}},
        {"path": f"{CHAT}/form.docx", "values": {f"f{n}": {"value": 1, "source": ASSISTANT} for n in range(1001)}},
        {"path": f"{CHAT}/form.docx", "values": {"x": {"value": 1, "source": {"kind": "file"}}}},
        {"path": f"{CHAT}/form.docx", "values": {"x": {"value": 1, "source": {"kind": "job"}}}},
    ],
    ids=["no values", "no source", "unknown kind", "long note", "long name", "1001 values", "no path", "no job id"],
)
def test_arguments_out_of_bounds_are_invalid_arguments_and_nothing_runs(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path, body: dict[str, Any]
) -> None:
    """At most 1000 values, names of 255 characters, notes of 1000; every value with a source of a known kind."""
    stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    assert refused(acall(client, "fill_template", body))["reason"] == "invalid_arguments"
    assert fake_executor.requests == []


def test_a_request_too_large_for_the_executor_is_too_large(client: TestClient, store: Store, tmp_path: Path) -> None:
    """The protocol's line bounds the rest."""
    stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    values = {f"f{n}": "x" * 2000 for n in range(600)}
    assert refusal(fill(client, f"{CHAT}/form.docx", values)) == ("too_large", "request too large")


def test_the_tools_are_listed_with_typed_parameters(client: TestClient) -> None:
    """Both are listed with a description for the model; every field of every argument model has an explicit type."""
    tools = {t["name"]: t for t in result(rpc(client, "tools/list", headers=HG))["tools"]}
    for name in ("inspect_template", "fill_template"):
        assert "Args:" not in tools[name]["description"]
    fill_description = tools["fill_template"]["description"]
    for phrase in ("inspect_template first", "every value needs a source", "job_id", "run_python"):
        assert phrase in fill_description, phrase
    assert "fill_template" in tools["inspect_template"]["description"]
    fill_schema = tools["fill_template"]["inputSchema"]
    bodies = [tools["inspect_template"]["inputSchema"], fill_schema, *fill_schema["$defs"].values()]
    assert len(bodies) >= 7
    for schema in bodies:
        for field, spec in schema["properties"].items():
            assert "type" in spec, (schema["title"], field)
            assert spec.get("description", "").strip(), (schema["title"], field)
    assert fill_schema["required"] == ["path", "values"]


# --- what a body may hold -----------------------------------------------------------------------------------------


def nested_json(depth: int) -> str:
    """A list holding a list, ``depth`` lists deep, a string at the bottom, as JSON text."""
    return "[" * depth + '"x"' + "]" * depth


def fill_body(value_json: str) -> str:
    """``fill_template``'s arguments for ``form.docx`` whose one value is the JSON text given: three levels above it."""
    value = f'{{"value": {value_json}, "source": {{"kind": "assistant"}}}}'
    return f'{{"path": "{CHAT}/form.docx", "values": {{"x": {value}}}}}'


def fill_call(client: TestClient, value_json: str) -> httpx2.Response:
    """Call ``fill_template`` with :func:`fill_body`'s arguments, two levels below the message: in its ``params``."""
    return raw_call(client, "fill_template", fill_body(value_json))


def too_deep(r: httpx2.Response) -> None:
    """Check the door's one answer to a message nested past the depth limit."""
    assert error(r, 400, -32600)["error"]["message"] == "request nested too deeply"


@pytest.mark.parametrize(
    ("depth", "past_the_parser"), [(300, False), (20_000, True)], ids=["past the serializer", "past the parser"]
)
def test_a_value_nested_too_deeply_is_refused_and_nothing_runs(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path, depth: int, past_the_parser: bool
) -> None:
    """A list of lists of lists was a 500 when the job was encoded; it is refused as read, with one short answer.

    20 000 levels are past Python's own JSON parser, which raises ``RecursionError`` before the depth is checked: the
    test checks that premise first, so a parser that one day takes that many fails here instead of passing unseen.
    """
    stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    body = fill_body(nested_json(depth))
    if past_the_parser:
        with pytest.raises(RecursionError):
            json.loads(body)
    too_deep(fill_call(client, nested_json(depth)))
    assert fake_executor.requests == []


def test_arguments_as_deep_as_allowed_reach_the_job(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path
) -> None:
    """The limit is the message's own depth, objects and arrays together: one level more is refused, this many is not.

    The arguments sit two levels below the message, in ``params``, so a value may nest two levels less than in a body.
    """
    stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    result(fill_call(client, nested_json(MAX_DEPTH - 5)))
    assert len(fake_executor.requests) == 1
    too_deep(fill_call(client, nested_json(MAX_DEPTH - 4)))


def test_a_request_as_deep_as_the_api_takes_is_one_the_executor_reads() -> None:
    """The protocol's reader takes 200 levels and pydantic writes 255: a body the api lets through goes both ways."""
    args = json.loads(nested_json(MAX_DEPTH - 1))
    req = JobRequest(
        job_id="0" * 32,
        tool="fill_template",
        args={"values": args},
        caller="ghostwriter",
        user_id="user-a",
        chat_id="chat-1",
        input_files=[],
        limits=Limits(wall_s=1, cpu_s=1, memory_mb=1, file_mb=1),
        max_chars=1,
    )
    assert decode_request(encode(req)) == req


def test_invalid_arguments_cut_what_they_name_of_the_request(
    client: TestClient, store: Store, fake_executor: Local
) -> None:
    """The field and the message are kept, cut where they would quote a long part of the request: a key, a tag."""
    key, tag = "harbour-key-" * 500, "harbour-tag-" * 500
    values = {key: {"value": 1, "source": ASSISTANT}, "x": {"value": 1, "source": {"kind": tag}}}
    r = acall(client, "fill_template", {"path": f"{CHAT}/form.docx", "values": values})
    structured = refused(r)
    assert structured["reason"] == "invalid_arguments"
    assert {error["type"] for error in structured["detail"]} == {"string_too_long", "union_tag_invalid"}
    assert len(result(r)["content"][0]["text"]) < 2000
    assert key[:400] not in r.text and tag[:400] not in r.text
    assert fake_executor.requests == []


#: What pydantic says of a source kind it does not know, around the kind itself: 107 characters without it.
UNKNOWN_KIND = (
    "Input tag '{}' found using 'kind' does not match any of the expected tags: 'file', 'job', 'user', 'assistant'"
)


@pytest.mark.parametrize("size", [300, 301])
def test_invalid_arguments_keep_300_characters_of_a_part_and_cut_the_301st(
    client: TestClient, fake_executor: Local, size: int
) -> None:
    """A location part or a message of 300 characters is whole; one more, and it is its first 300 and `` [cut]``."""
    key = "k" * size
    tag = "t" * (size - len(UNKNOWN_KIND.format("")))
    message = UNKNOWN_KIND.format(tag)
    assert len(message) == size
    values = {key: {"value": 1, "source": ASSISTANT}, "x": {"value": 1, "source": {"kind": tag}}}
    structured = refused(acall(client, "fill_template", {"path": f"{CHAT}/form.docx", "values": values}))
    assert structured["reason"] == "invalid_arguments"
    by_type = {error["type"]: error for error in structured["detail"]}
    shown_key = key if size == 300 else key[:300] + " [cut]"
    shown_message = message if size == 300 else message[:300] + " [cut]"
    assert by_type["string_too_long"]["loc"] == ["values", shown_key, "[key]"]
    assert by_type["union_tag_invalid"]["msg"] == shown_message
    assert fake_executor.requests == []


# --- refusals are recorded (R72) ---------------------------------------------------------------------------------


def test_an_inspection_refusal_quoting_the_template_is_recorded_by_its_code_alone(
    client: TestClient, store: Store, tmp_path: Path, audit_lines: AuditLines
) -> None:
    """The real runner quotes the line that does not compile; the event holds the code ``invalid_template`` only."""
    form = stored(store, tmp_path, "form.docx", mt.make_jinja_docx, extra="{% if salaries_of_user_b %}never closed")
    reason, detail = refusal(inspect(client, f"{CHAT}/form.docx"))
    assert reason == "refused"
    assert detail.startswith("invalid_template: ")
    assert "salaries_of_user_b" in detail
    (event,) = events(audit_lines, "inspect_template")
    assert (event["action"], event["outcome"], event["target"]["ids"]) == ("query", "error", [form.id])
    assert event["criteria"] == {"caller": "ghostwriter", "refused": "invalid_template"}
    assert "salaries" not in json.dumps(audit_lines())


def test_a_fill_refusal_quoting_the_drop_downs_entries_is_recorded_by_its_code_alone(
    client: TestClient, store: Store, tmp_path: Path, audit_lines: AuditLines
) -> None:
    """The real runner names the drop-down's entries; the event holds the template's id and ``invalid_value``."""
    form = stored(store, tmp_path, "form.docx", mt.make_controls_docx)
    reason, detail = refusal(fill(client, f"{CHAT}/form.docx", {"status": "pending"}))
    assert reason == "refused"
    assert '"open", "closed"' in detail
    assert outputs(store) == []
    (event,) = events(audit_lines, "fill_template")
    assert (event["action"], event["outcome"], event["target"]["ids"]) == ("modify", "error", [form.id])
    assert event["criteria"] == {"caller": "ghostwriter", "outputs": 0, "refused": "invalid_value"}
    assert "closed" not in json.dumps(audit_lines())


@pytest.mark.parametrize("tool", ["inspect_template", "fill_template"])
def test_a_template_refusal_whose_record_cannot_be_written_never_reaches_the_model(
    client: TestClient, store: Store, tmp_path: Path, broken_audit: None, tool: str
) -> None:
    """The audit failure is the answer, never the refusal's text."""
    stored(store, tmp_path, "form.docx", mt.make_jinja_docx, extra="{% if approved %}never closed")
    r = (
        inspect(client, f"{CHAT}/form.docx")
        if tool == "inspect_template"
        else fill(client, f"{CHAT}/form.docx", {"x": 1})
    )
    unavailable(r)
    assert "approved" not in r.text
