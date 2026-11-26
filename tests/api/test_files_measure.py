"""``POST /files/measure``: the length of a document in its reader's units, as Ghostwriter asks it after an upload.

The app runs the real ``measure_file`` runner in this process (:class:`~tests.api.test_tools_templates.Local`) over
files built at test time; every name, text and id is invented. Here the route's part: what it answers, what it records
and when, which jobs it makes and on which gate, and what it remembers of a file. The counts themselves are tested in
``tests/executor/test_measure.py``.
"""

import dataclasses
import io
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api.conftest import HD, HG, PG, AuditLines, put
from tests.api.test_tools_templates import Local
from tests.fixtures import make_fixtures as mk
from tests.fixtures import make_images as mi
from workbench.api.audit import AuditLog
from workbench.api.jobs import PREPARE_QUEUE_WAIT_S, JobQueue
from workbench.api.main import create_app
from workbench.api.measure import NOT_A_DOCUMENT, REFUSED_TEXT
from workbench.api.metrics import Metrics
from workbench.api.store import Store
from workbench.config import Settings
from workbench.executor.runners import documents
from workbench.models import FileRecord, Principal

PURPOSE = {"X-Audit-Purpose": "reason=ghostwriter+chat", "X-Request-ID": "req-1"}
HEADERS = {**HG, **PURPOSE}
P2 = Principal("ghostwriter", "user-a", "chat-2", confined=True)
NOT_FOUND = (404, {"detail": "not found"})
BUSY = (503, {"detail": "busy, retry later"})
ANSWER_KEYS = {"path", "kind", "unit", "total", "chars", "capped"}
TIMED_OUT = {"error": "read_timeout: reading the file took longer than 1 s; it is too large or too complex"}


@pytest.fixture
def executor(settings: Settings, tmp_path: Path) -> Local:
    """The in-process executor."""
    return Local(settings.data_dir, tmp_path / "jobs")


@pytest.fixture
def queue(settings: Settings, store: Store, executor: Local) -> JobQueue:
    """The job queue over it."""
    return JobQueue(settings, executor, store, Metrics(settings.data_dir))


@pytest.fixture
def app(settings: Settings, store: Store, audit: AuditLog, queue: JobQueue) -> FastAPI:
    """The real app with a job queue over the in-process executor."""
    return create_app(settings, store=store, audit=audit, jobs=queue)


@pytest.fixture
def built(tmp_path: Path) -> Path:
    """Where files are built before they are uploaded."""
    folder = tmp_path / "built"
    folder.mkdir()
    return folder


def measure(client: TestClient, path: str, headers: Mapping[str, Any] = HEADERS) -> Any:
    """Measure ``path``, as Ghostwriter does unless other headers are given."""
    return client.post("/files/measure", json={"path": path}, headers=headers)


def events_of(audit_lines: AuditLines, operation: str = "file.measure") -> list[dict[str, Any]]:
    """The audit events of one operation, in order."""
    return [line for line in audit_lines() if line["operation"] == operation]


def counts(r: Any) -> tuple[Any, ...]:
    """What an answer says of the document: its kind, unit, total, characters and whether it is capped."""
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == ANSWER_KEYS
    return body["kind"], body["unit"], body["total"], body["chars"], body["capped"]


def text(store: Store, name: str, content: str) -> FileRecord:
    """A text file of that content, uploaded to Ghostwriter's chat."""
    return put(store, name, content.encode(), p=PG)


# --- what a measure answers ----------------------------------------------------------------------------------------


def test_a_pdf_is_measured_and_recorded_as_a_query(
    client: TestClient, store: Store, executor: Local, audit_lines: AuditLines, built: Path
) -> None:
    """The answer holds its six keys; one ``query`` / ``file.measure`` event carries the purpose and request id."""
    name = "harbour-report.pdf"
    pages = ["First page text\nsecond line", None, "Quay list"]
    src = put(store, name, mk.make_pdf(built / name, pages).read_bytes(), p=PG)
    r = measure(client, src.virtual_path)

    assert (r.status_code, r.json()) == (
        200,
        {"path": src.virtual_path, "kind": "pdf", "unit": "page", "total": 3, "chars": 36, "capped": False},
    )
    (event,) = events_of(audit_lines)
    assert (event["action"], event["operation"], event["outcome"]) == ("query", "file.measure", "ok")
    assert event["target"]["ids"] == [src.id]
    assert event["purpose"] == {"reason": "ghostwriter chat"}
    assert event["request_id"] == "req-1"
    (job,) = executor.requests
    assert (job.tool, job.input_files) == ("measure_file", [src.name])


def test_a_word_document_with_a_table_is_measured_in_paragraphs(client: TestClient, store: Store, built: Path) -> None:
    """The table is one unit: a title, two paragraphs, the table and the closing paragraph make five."""
    src = put(store, "quarterly-review.docx", mk.make_docx(built / "quarterly-review.docx").read_bytes(), p=PG)

    assert counts(measure(client, src.virtual_path))[:3] == ("docx", "paragraph", 5)


def test_a_workbook_of_two_sheets_is_measured_in_rows_across_both(
    client: TestClient, store: Store, built: Path
) -> None:
    """The rows are numbered through the sheets in their order: six of the first and two of the second."""
    src = put(
        store, "quarterly-figures.xlsx", mk.make_xlsx(built / "quarterly-figures.xlsx", rows=5).read_bytes(), p=PG
    )

    assert counts(measure(client, src.virtual_path))[:3] == ("xlsx", "row", 8)


def test_text_and_json_are_measured_in_lines(client: TestClient, store: Store) -> None:
    """A text file counts its lines and their characters; a minified JSON file the lines of its indented form."""
    notes = text(store, "pier-notes.txt", "Pier\nQuay\nDock\nNo\n")
    berths = text(
        store, "berths.json", json.dumps({"berths": {"north": 1, "south": 2}, "open": 3}, separators=(",", ":"))
    )

    assert counts(measure(client, notes.virtual_path)) == ("text", "line", 4, 14, False)
    assert counts(measure(client, berths.virtual_path))[:3] == ("json", "line", 7)


def test_the_count_stops_at_the_cap(client: TestClient, store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the cap, the characters are the cap and ``capped`` is true; the total is still whole."""
    monkeypatch.setattr(documents, "MEASURE_CHARS_CAP", 10)
    notes = text(store, "pier-notes.txt", "Pier\nQuay\nDock\nNo\n")

    r = measure(client, notes.virtual_path)
    assert counts(r)[2:] == (4, 10, True)


# --- what is no document -------------------------------------------------------------------------------------------


def test_a_picture_or_a_clip_is_not_a_document(
    client: TestClient, store: Store, executor: Local, audit_lines: AuditLines, built: Path
) -> None:
    """Refused by its name, before any job and with no event: it has no text to measure."""
    photo = put(store, "quay-photo.jpg", mi.make_photo(built / "quay-photo.jpg").read_bytes(), p=PG)
    clip = put(store, "harbour-tour.mp4", b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16, p=PG)

    for source in (photo, clip):
        r = measure(client, source.virtual_path)
        assert (r.status_code, r.json()) == (422, {"detail": NOT_A_DOCUMENT})
    assert executor.requests == []
    assert events_of(audit_lines) == []


# --- what is remembered --------------------------------------------------------------------------------------------


def test_a_second_measure_is_answered_from_memory(
    client: TestClient, store: Store, executor: Local, audit_lines: AuditLines
) -> None:
    """The counts of a file are kept: the same call again runs no job and writes no event."""
    notes = text(store, "pier-notes.txt", "Pier\nQuay\nDock\nNo\n")
    first = measure(client, notes.virtual_path)
    second = measure(client, notes.virtual_path)

    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json() == first.json()
    assert len(executor.requests) == 1
    assert len(events_of(audit_lines)) == 1
    assert store.measure_of(notes) is not None


def test_a_refusal_is_remembered_and_a_timeout_is_not(
    client: TestClient, store: Store, executor: Local, audit_lines: AuditLines
) -> None:
    """What the file decides is refused again at once, recorded as remembered; running out of time is tried again."""
    ledger = put(store, "ledger.xls", b"\xd0\xcf\x11\xe0" + b"\x00" * 28, p=PG)
    first = measure(client, ledger.virtual_path)
    second = measure(client, ledger.virtual_path)

    assert (first.status_code, second.status_code) == (422, 422)
    assert first.json()["detail"].startswith("unsupported_format: .xls cannot be read here")
    assert second.json() == {"detail": REFUSED_TEXT["unsupported_format"]}  # the code's fixed text, not the reader's
    assert store.measure_of(ledger) == "unsupported_format"  # the code alone is kept
    assert len(executor.requests) == 1
    assert [
        (event["outcome"], event["criteria"]["refused"], event["criteria"].get("remembered"))
        for event in events_of(audit_lines)
    ] == [("error", "unsupported_format", None), ("error", "unsupported_format", True)]
    assert all(event["target"]["ids"] == [ledger.id] for event in events_of(audit_lines))

    notes = text(store, "pier-notes.txt", "Pier\nQuay\nDock\nNo\n")
    executor.status, executor.payload = "error", TIMED_OUT
    before = len(executor.requests)
    slow = [measure(client, notes.virtual_path) for _ in range(2)]

    assert [r.status_code for r in slow] == [422, 422]
    assert slow[0].json()["detail"].startswith("read_timeout: ")
    assert len(executor.requests) == before + 2
    assert store.measure_of(notes) is None
    executor.status, executor.payload = "ok", None
    assert counts(measure(client, notes.virtual_path))[2] == 4


def test_a_damaged_document_is_refused_again_from_memory_with_one_job(
    client: TestClient, store: Store, executor: Local, audit_lines: AuditLines
) -> None:
    """An ``unreadable_file`` is remembered at once, as an ``unsupported_format`` is: one job, two recorded refusals."""
    report = put(store, "quarterly-review.docx", b"PK\x03\x04 not really a document", p=PG)
    first = measure(client, report.virtual_path)
    second = measure(client, report.virtual_path)

    assert (first.status_code, second.status_code) == (422, 422)
    assert first.json()["detail"].startswith("unreadable_file: ")
    assert second.json() == {"detail": REFUSED_TEXT["unreadable_file"]}
    assert store.measure_of(report) == "unreadable_file"
    assert len(executor.requests) == 1
    assert [
        (event["outcome"], event["criteria"]["refused"], event["criteria"].get("remembered"))
        for event in events_of(audit_lines)
    ] == [("error", "unreadable_file", None), ("error", "unreadable_file", True)]
    assert all(event["target"]["ids"] == [report.id] for event in events_of(audit_lines))


def test_what_is_remembered_of_a_refusal_holds_no_part_of_the_name(
    client: TestClient, store: Store, executor: Local, audit_lines: AuditLines
) -> None:
    """A suffix with a dot and spaces is the reader's text on the first answer; the second and the memory hold none."""
    name = "Q3.Harbour expansion terms"
    terms = put(store, name, b"Pier\nQuay\n", p=PG)
    first = measure(client, terms.virtual_path)
    second = measure(client, terms.virtual_path)

    assert (first.status_code, second.status_code) == (422, 422)
    assert second.json() == {"detail": REFUSED_TEXT["unsupported_format"]}
    assert len(executor.requests) == 1
    for held in (second.text, repr(store.measure_of(terms)), repr(store._measures), json.dumps(events_of(audit_lines))):
        assert "harbour" not in held.lower() and "expansion" not in held.lower() and "Q3" not in held
    assert store.measure_of(terms) == "unsupported_format"


def test_a_remembered_refusal_that_cannot_be_recorded_is_not_answered(
    client: TestClient,
    store: Store,
    executor: Local,
    audit_lines: AuditLines,
    audit_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: a refusal answered from memory goes out only after its ``error`` is written."""
    ledger = put(store, "ledger.xls", b"\xd0\xcf\x11\xe0" + b"\x00" * 28, p=PG)
    assert measure(client, ledger.virtual_path).status_code == 422
    assert store.measure_of(ledger) == "unsupported_format"

    def refuse(_text: str, /) -> int:
        raise OSError("synthetic audit stream failure")

    monkeypatch.setattr(audit_stream, "write", refuse)
    r = measure(client, ledger.virtual_path)

    assert (r.status_code, r.json()) == (500, {"detail": "audit unavailable"})
    assert "cannot be read" not in r.text and REFUSED_TEXT["unsupported_format"] not in r.text
    assert len(executor.requests) == 1  # no job, either
    assert len(events_of(audit_lines)) == 1  # and no second event


def test_a_file_taken_back_is_forgotten(client: TestClient, store: Store, built: Path) -> None:
    """Its upload taken back, a file is not found, and the store holds nothing of its measure."""
    name = "harbour-report.pdf"
    src = put(store, name, mk.make_pdf(built / name, ["First page text"]).read_bytes(), p=PG)
    assert measure(client, src.virtual_path).status_code == 200
    assert store.measure_of(src) is not None

    assert client.delete("/files/upload", params={"path": src.virtual_path}, headers=HEADERS).status_code == 200
    r = measure(client, src.virtual_path)

    assert (r.status_code, r.json()) == NOT_FOUND
    assert store.measure_of(src) is None


# --- who may ask, and how ------------------------------------------------------------------------------------------


def test_measure_needs_a_purpose(
    settings: Settings,
    store: Store,
    audit: AuditLog,
    queue: JobQueue,
    executor: Local,
    audit_lines: AuditLines,
) -> None:
    """With ``query`` requiring a purpose, a measure without one is 428, recorded denied, and makes no job."""
    notes = text(store, "pier-notes.txt", "Pier\nQuay\nDock\nNo\n")
    required = dataclasses.replace(settings, require_purpose=frozenset({"query"}))
    with TestClient(create_app(required, store=store, audit=audit, jobs=queue)) as strict:
        r = measure(strict, notes.virtual_path, HG)
        assert (r.status_code, r.json()) == (428, {"detail": "a stated purpose is required"})
        (denied,) = events_of(audit_lines)
        assert (denied["outcome"], denied["criteria"]["refused"]) == ("denied", "purpose_required")
        assert executor.requests == []
        assert measure(strict, notes.virtual_path, HEADERS).status_code == 200


def test_another_chats_file_a_missing_file_or_a_derived_path_is_404(
    client: TestClient, store: Store, executor: Local, audit_lines: AuditLines, built: Path
) -> None:
    """Not found, each: no job and no event. A render's path is reached by no path."""
    other = put(store, "pier-notes.txt", b"Pier\n", directory="/workspace/chat-2", p=P2)
    photo = put(store, "quay-photo.jpg", mi.make_photo(built / "quay-photo.jpg").read_bytes(), p=PG)
    assert client.post("/files/prepare", params={"path": photo.virtual_path}, headers=HEADERS).status_code == 200
    (render,) = store.derived_of(photo)
    jobs = len(executor.requests)

    derived = f"/workspace/chat-1/.derived/{photo.id}/{render.name}"
    for path in (other.virtual_path, "/workspace/chat-1/missing.pdf", derived):
        r = measure(client, path)
        assert (r.status_code, r.json()) == NOT_FOUND
    assert len(executor.requests) == jobs
    assert events_of(audit_lines) == []


def test_docint_finds_no_measure_route(client: TestClient, store: Store, executor: Local) -> None:
    """The route is Ghostwriter's alone: docint's key answers 404, as on every other file route."""
    notes = text(store, "pier-notes.txt", "Pier\nQuay\nDock\nNo\n")
    r = measure(client, notes.virtual_path, HD)

    assert (r.status_code, r.json()) == NOT_FOUND
    assert executor.requests == []


def test_the_body_is_a_path_and_nothing_else(client: TestClient, store: Store, executor: Local) -> None:
    """A body without a path, with an empty one or with any other key does not validate, and makes no job."""
    notes = text(store, "pier-notes.txt", "Pier\nQuay\nDock\nNo\n")
    bodies: list[dict[str, Any]] = [{}, {"path": ""}, {"path": notes.virtual_path, "strips": 2}]
    for body in bodies:
        r = client.post("/files/measure", json=body, headers=HEADERS)
        assert r.status_code == 422, body
    assert client.post("/files/measure", params={"path": notes.virtual_path}, headers=HEADERS).status_code == 422
    assert executor.requests == []


# --- the queue -----------------------------------------------------------------------------------------------------


def test_a_measure_waits_on_the_callers_measure_gate(
    client: TestClient, store: Store, queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A measure waits up to as long as a prepare, on the measure gate, within the caller's wall and slots."""
    asked: list[tuple[str, str, int, int | None, list[str]]] = []
    real = queue.submit

    async def watched(p: Principal, tool: str, args: dict[str, Any], files: list[str], *more: Any, **named: Any) -> Any:
        call = named["call"]
        asked.append((tool, named["gate"], call.wait_s, call.slots, files))
        return await real(p, tool, args, files, *more, **named)

    monkeypatch.setattr(queue, "submit", watched)
    notes = text(store, "pier-notes.txt", "Pier\nQuay\nDock\nNo\n")
    assert measure(client, notes.virtual_path).status_code == 200

    assert asked == [("measure_file", "measure", PREPARE_QUEUE_WAIT_S, 4, [notes.name])]


def test_a_callers_wall_bounds_the_measures_budget(
    settings: Settings, store: Store, audit: AuditLog, executor: Local
) -> None:
    """A caller whose wall is below the global one gets a measure whose runner budget is inside its own wall."""
    narrow = dataclasses.replace(settings, ghostwriter_wall_s=30)
    notes = text(store, "pier-notes.txt", "Pier\nQuay\nDock\nNo\n")
    queue = JobQueue(narrow, executor, store, Metrics(narrow.data_dir))
    with TestClient(create_app(narrow, store=store, audit=audit, jobs=queue)) as client:
        assert measure(client, notes.virtual_path).status_code == 200

    (request,) = executor.requests
    assert 1 <= request.args["budget_s"] < 30


def test_a_busy_queue_is_503_and_not_remembered(client: TestClient, store: Store, executor: Local) -> None:
    """An executor with no free slot refuses the job: 503, nothing kept, and the next call, with room, answers."""
    notes = text(store, "pier-notes.txt", "Pier\nQuay\nDock\nNo\n")
    executor.status = "busy"
    r = measure(client, notes.virtual_path)

    assert (r.status_code, r.json()) == BUSY
    assert store.measure_of(notes) is None
    executor.status = "ok"
    assert counts(measure(client, notes.virtual_path))[2] == 4


# --- what is never sent --------------------------------------------------------------------------------------------


def test_an_answer_that_cannot_be_read_is_recorded_and_is_500(
    client: TestClient, store: Store, executor: Local, audit_lines: AuditLines
) -> None:
    """A runner that answers a unit its kind does not count is no answer: recorded as an error, sent as a 500."""
    notes = text(store, "pier-notes.txt", "Pier\nQuay\nDock\nNo\n")
    executor.payload = {"kind": "text", "unit": "page", "total": 4, "chars": 14, "capped": False}
    r = measure(client, notes.virtual_path)

    assert (r.status_code, r.json()) == (500, {"detail": "the tool returned an answer that could not be read"})
    (event,) = events_of(audit_lines)
    assert (event["outcome"], event["criteria"].get("refused")) == ("error", None)
    assert store.measure_of(notes) is None


def test_a_measure_that_cannot_be_recorded_is_not_answered(
    client: TestClient, store: Store, audit_lines: AuditLines, broken_audit: None
) -> None:
    """Fail closed: the counts leave only after their event is written."""
    notes = text(store, "pier-notes.txt", "Pier\nQuay\nDock\nNo\n")
    r = measure(client, notes.virtual_path)

    assert (r.status_code, r.json()) == (500, {"detail": "audit unavailable"})
    assert store.measure_of(notes) is None
