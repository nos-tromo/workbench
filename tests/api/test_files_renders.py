"""Ghostwriter's file routes for pictures and PDFs: ``POST /files/prepare`` and the views of a rendition.

The app runs the real ``prepare_file`` runner in this process (:class:`~tests.api.test_tools_templates.Local`) over
files built at test time; every name, text and id is invented. Here the routes' part: what each answers, what it
records and when, which jobs it makes, and that a rendition leaves the api with the hardening of every other view.
"""

import dataclasses
import io
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api.conftest import HG, PG, AuditLines, put
from tests.api.test_renders import capped, quay_pages
from tests.api.test_tools_templates import Local
from tests.fixtures import make_fixtures as mk
from tests.fixtures import make_images as mi
from workbench.api.audit import AuditLog
from workbench.api.hardening import FILE_HEADERS
from workbench.api.jobs import JobQueue
from workbench.api.main import create_app
from workbench.api.metrics import Metrics
from workbench.api.store import Conflict, QuotaExceeded, Store
from workbench.config import Settings
from workbench.executor.runners import RUNNERS
from workbench.models import DerivedRecord, FileRecord, Principal

PURPOSE = {"X-Audit-Purpose": "reason=ghostwriter+chat", "X-Request-ID": "req-1"}
HEADERS = {**HG, **PURPOSE}
P2 = Principal("ghostwriter", "user-a", "chat-2", confined=True)
NOT_FOUND = (404, {"detail": "not found"})
BUSY = (503, {"detail": "busy, retry later"})
MANIFEST_KEYS = {"path", "kind", "total", "pages_without_text", "renditions", "frames", "hint"}
RENDITION_KEYS = {"n", "width", "height", "mime", "bytes"}


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


@pytest.fixture
def photo(store: Store, built: Path) -> FileRecord:
    """A small photo, uploaded to Ghostwriter's chat."""
    return put(store, "quay-photo.jpg", mi.make_photo(built / "quay-photo.jpg").read_bytes(), p=PG)


def pdf(store: Store, built: Path, name: str, pages: list[str | None], *, password: str | None = None) -> FileRecord:
    """A PDF of those pages, uploaded to Ghostwriter's chat."""
    return put(store, name, mk.make_pdf(built / name, pages, password=password).read_bytes(), p=PG)


def prepare(client: TestClient, path: str, headers: Mapping[str, Any] = HEADERS) -> Any:
    """Prepare ``path``, as Ghostwriter does unless other headers are given."""
    return client.post("/files/prepare", params={"path": path}, headers=headers)


def rendition(
    client: TestClient,
    path: str,
    n: int,
    *,
    strips: int | None = None,
    strip: int | None = None,
    headers: Mapping[str, Any] = HEADERS,
) -> Any:
    """View rendition ``n`` of ``path``, with its strip count and strip if given."""
    params: dict[str, Any] = {"path": path, "rendition": n}
    if strips is not None:
        params["strips"] = strips
    if strip is not None:
        params["strip"] = strip
    return client.get("/files/view", params=params, headers=headers)


def events_of(audit_lines: AuditLines, operation: str) -> list[dict[str, Any]]:
    """The audit events of one operation, in order."""
    return [line for line in audit_lines() if line["operation"] == operation]


def hardened(r: Any) -> None:
    """Check that an answer carries the view hardening headers."""
    for name, value in FILE_HEADERS.items():
        assert r.headers[name] == value


# --- prepare -----------------------------------------------------------------------------------------------------


def test_prepare_answers_the_manifest_and_records_a_modify(
    client: TestClient, store: Store, audit_lines: AuditLines, built: Path
) -> None:
    """The manifest has all seven keys; one ``modify`` / ``file.prepare`` event carries the purpose and request id."""
    source = pdf(store, built, "harbour-report.pdf", ["Harbour figures 2026", None, "Quay list"])
    r = prepare(client, source.virtual_path)

    assert r.status_code == 200
    body = r.json()
    assert set(body) == MANIFEST_KEYS
    assert (body["path"], body["kind"], body["total"], body["pages_without_text"]) == (
        source.virtual_path,
        "pdf",
        3,
        [2],
    )
    assert (body["frames"], body["hint"]) == (None, None)
    assert [item["n"] for item in body["renditions"]] == [1, 2, 3]
    for item in body["renditions"]:
        assert set(item) == RENDITION_KEYS
        assert item["mime"] == "image/jpeg"
    (event,) = events_of(audit_lines, "file.prepare")
    assert (event["action"], event["outcome"]) == ("modify", "ok")
    assert event["purpose"] == {"reason": "ghostwriter chat"}
    assert event["request_id"] == "req-1"
    assert event["target"]["ids"][0] == source.id
    assert store.files_in_chat(PG) == [source]


def test_prepare_again_answers_from_the_renders_without_a_job_or_event(
    client: TestClient, photo: FileRecord, executor: Local, audit_lines: AuditLines
) -> None:
    """Idempotent: the second prepare answers the same manifest from the rows, with no job and no event."""
    first = prepare(client, photo.virtual_path)
    jobs, events = len(executor.requests), len(audit_lines())
    second = prepare(client, photo.virtual_path)

    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json() == first.json()
    assert first.json()["kind"] == "image" and first.json()["total"] == 1
    assert (jobs, len(executor.requests), len(audit_lines())) == (1, 1, events)


def test_prepare_of_a_word_file_is_kind_none_without_a_job(
    client: TestClient, store: Store, executor: Local, audit_lines: AuditLines, built: Path
) -> None:
    """Neither a picture nor a PDF: answered at once, with every key, no job and no event."""
    source = put(store, "harbour-notes.docx", mk.make_docx(built / "harbour-notes.docx").read_bytes(), p=PG)
    r = prepare(client, source.virtual_path)

    assert r.status_code == 200
    assert set(r.json()) == MANIFEST_KEYS
    assert (r.json()["kind"], r.json()["total"], r.json()["renditions"]) == ("none", 0, [])
    assert executor.requests == []
    assert events_of(audit_lines, "file.prepare") == []


def test_prepare_of_a_text_file_named_png_is_422_and_stores_nothing(
    client: TestClient, store: Store, audit_lines: AuditLines
) -> None:
    """Review Focus 2: a file that lies about its type is refused as unreadable, with no render kept."""
    source = put(store, "quay-photo.png", b"Quay list, not a picture", p=PG)
    r = prepare(client, source.virtual_path)

    assert r.status_code == 422
    assert r.json()["detail"].startswith("unreadable_file:")
    assert store.derived_of(source) == []
    (event,) = events_of(audit_lines, "file.prepare")
    assert event["outcome"] == "error"
    assert event["criteria"]["refused"] == "unreadable_file"


def test_prepare_needs_a_purpose(
    settings: Settings,
    store: Store,
    audit: AuditLog,
    queue: JobQueue,
    executor: Local,
    audit_lines: AuditLines,
    photo: FileRecord,
) -> None:
    """With ``modify`` requiring a purpose, a prepare without one is 428, recorded denied, and makes no job."""
    required = dataclasses.replace(settings, require_purpose=frozenset({"modify"}))
    with TestClient(create_app(required, store=store, audit=audit, jobs=queue)) as strict:
        r = prepare(strict, photo.virtual_path, HG)
        assert (r.status_code, r.json()) == (428, {"detail": "a stated purpose is required"})
        (denied,) = events_of(audit_lines, "file.prepare")
        assert (denied["outcome"], denied["criteria"]["refused"]) == ("denied", "purpose_required")
        assert executor.requests == []
        assert prepare(strict, photo.virtual_path, HEADERS).status_code == 200


def test_prepare_of_another_chats_file_is_404(
    client: TestClient, store: Store, executor: Local, audit_lines: AuditLines, built: Path
) -> None:
    """The user's file in another chat is not found: no job, no event."""
    other = put(
        store,
        "quay-photo.jpg",
        mi.make_photo(built / "quay-photo.jpg").read_bytes(),
        directory="/workspace/chat-2",
        p=P2,
    )
    for path in (other.virtual_path, "/workspace/chat-1/missing.jpg"):
        r = prepare(client, path)
        assert (r.status_code, r.json()) == NOT_FOUND
    assert executor.requests == []
    assert events_of(audit_lines, "file.prepare") == []


def test_prepare_has_no_body(client: TestClient, photo: FileRecord, executor: Local) -> None:
    """The request bound holds a prepare: a declared 3 MiB body is refused before the route runs."""
    r = client.post(
        "/files/prepare", params={"path": photo.virtual_path}, content=b"x" * (3 * 1024 * 1024), headers=HEADERS
    )
    assert (r.status_code, r.json()) == (413, {"detail": "request too large"})
    assert executor.requests == []


def test_prepare_wants_a_path(client: TestClient, executor: Local) -> None:
    """A prepare without ``path`` does not validate, and makes no job."""
    assert client.post("/files/prepare", headers=HEADERS).status_code == 422
    assert executor.requests == []


# --- a rendition -------------------------------------------------------------------------------------------------


def test_a_rendition_is_served_with_the_view_hardening(
    client: TestClient, store: Store, audit_lines: AuditLines, built: Path
) -> None:
    """One page: ``image/jpeg``, the three hardening headers, ``inline``, and one view event naming file and render."""
    source = pdf(store, built, "harbour-report.pdf", quay_pages(3))
    r = rendition(client, source.virtual_path, 3)

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(b"\xff\xd8\xff")
    assert r.headers["content-length"] == str(len(r.content))
    assert r.headers["content-disposition"] == "inline; filename*=UTF-8''harbour-report-page-3.jpg"
    hardened(r)
    page = next(row for row in store.derived_of(source) if row.ordinal == 3)
    (event,) = events_of(audit_lines, "file.view")
    assert (event["action"], event["outcome"]) == ("disclose", "ok")
    assert event["target"]["ids"] == [source.id, page.id]
    assert event["purpose"] == {"reason": "ghostwriter chat"}
    assert event["request_id"] == "req-1"


def test_the_names_of_renditions(client: TestClient, store: Store, built: Path, photo: FileRecord) -> None:
    """The stem of the source, the page and the strip, quoted; a picture whole is its stem alone."""
    scan = pdf(store, built, "Quay list ü.pdf", quay_pages(1))
    named = {
        (photo.virtual_path, 1, None, None): "quay-photo.jpg",
        (photo.virtual_path, 1, 2, 1): "quay-photo-strip-1-of-2.jpg",
        (scan.virtual_path, 1, None, None): "Quay%20list%20%C3%BC-page-1.jpg",
        (scan.virtual_path, 1, 3, 2): "Quay%20list%20%C3%BC-page-1-strip-2-of-3.jpg",
    }
    for (path, n, strips, strip), expected in named.items():
        r = rendition(client, path, n, strips=strips, strip=strip)
        assert r.status_code == 200
        assert r.headers["content-disposition"] == f"inline; filename*=UTF-8''{expected}"


def test_a_png_picture_is_served_as_png(client: TestClient, store: Store, built: Path, audit_lines: AuditLines) -> None:
    """A palette PNG that fits is kept as PNG, and named so; the render is one of its own, named by the event."""
    source = put(store, "quay-chart.png", mi.make_palette_png(built / "quay-chart.png").read_bytes(), p=PG)
    r = rendition(client, source.virtual_path, 1)

    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert r.headers["content-disposition"] == "inline; filename*=UTF-8''quay-chart.png"
    (render,) = store.derived_of(source)
    (event,) = events_of(audit_lines, "file.view")
    assert event["target"]["ids"] == [source.id, render.id]


def test_a_rendition_past_the_first_pages_is_rendered_on_demand(
    client: TestClient, store: Store, executor: Local, built: Path
) -> None:
    """Page 21 of 22 is one job; asking again makes none."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(22))
    assert prepare(client, source.virtual_path).status_code == 200
    prepared = len(executor.requests)

    first = rendition(client, source.virtual_path, 21)
    assert first.status_code == 200
    assert len(executor.requests) == prepared + 1
    again = rendition(client, source.virtual_path, 21)
    assert again.content == first.content
    assert len(executor.requests) == prepared + 1
    assert [item["n"] for item in prepare(client, source.virtual_path).json()["renditions"]][-1] == 21


def test_strips_are_cut_once_and_served(client: TestClient, photo: FileRecord, executor: Local) -> None:
    """Both strips of a page are one job; the two bodies differ."""
    assert prepare(client, photo.virtual_path).status_code == 200
    prepared = len(executor.requests)
    top = rendition(client, photo.virtual_path, 1, strips=2, strip=1)
    bottom = rendition(client, photo.virtual_path, 1, strips=2, strip=2)

    assert (top.status_code, bottom.status_code) == (200, 200)
    assert top.content != bottom.content
    assert len(executor.requests) == prepared + 1
    hardened(top)


@pytest.mark.parametrize(
    ("strips", "strip"),
    [(None, 2), (2, None), (2, 3), (1, 2)],
    ids=["strip-alone", "strips-alone", "strip-past-strips", "one-strip-is-strip-1"],
)
def test_bad_strip_pairs_are_422(
    client: TestClient,
    photo: FileRecord,
    executor: Local,
    audit_lines: AuditLines,
    strips: int | None,
    strip: int | None,
) -> None:
    """``strip`` alone, ``strips`` alone, ``strip`` past ``strips``: refused before any job or event."""
    before = len(audit_lines())
    r = rendition(client, photo.virtual_path, 1, strips=strips, strip=strip)

    assert (r.status_code, r.json()) == (422, {"detail": "strip is 1 to strips"})
    assert executor.requests == []
    assert len(audit_lines()) == before
    hardened(r)


def test_strips_alone_of_one_and_a_strip_of_one_are_a_whole_picture(client: TestClient, photo: FileRecord) -> None:
    """``strips=1`` with no strip, or with strip 1, is the whole render."""
    whole = rendition(client, photo.virtual_path, 1)
    for strip in (None, 1):
        r = rendition(client, photo.virtual_path, 1, strips=1, strip=strip)
        assert (r.status_code, r.content) == (200, whole.content)


@pytest.mark.parametrize(
    ("case", "token"),
    [
        ("text-named-png", "unreadable_file"),
        ("protected-pdf", "unreadable_file"),
        ("timeout", "read_timeout"),
        ("unsupported", "unsupported_format"),
    ],
)
def test_prepare_422_details_start_with_their_token(
    client: TestClient, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch, case: str, token: str
) -> None:
    """The three tokens a caller may branch on come before the first ``:`` of a 422, on a prepare and on a rendition.

    A text file named ``.png`` and a password-protected PDF are ``unreadable_file``; a runner that ran out of time is
    ``read_timeout``; a format it cannot draw is ``unsupported_format``. Nothing is kept.
    """
    if case == "text-named-png":
        source = put(store, "quay-photo.png", b"Quay list, not a picture", p=PG)
    elif case == "protected-pdf":
        source = pdf(store, built, "harbour-report.pdf", ["Harbour figures 2026"], password="invented-secret")
    else:
        source = put(store, "quay-photo.jpg", mi.make_photo(built / "quay-photo.jpg").read_bytes(), p=PG)
        refusal = {"error": f"{token}: invented reason"}
        monkeypatch.setitem(RUNNERS, "prepare_file", lambda _args, _ctx: refusal)

    for r in (prepare(client, source.virtual_path), rendition(client, source.virtual_path, 1)):
        assert r.status_code == 422
        assert r.json()["detail"].partition(":")[0] == token
    assert store.derived_of(source) == []


def test_a_rendition_out_of_range_or_of_a_word_file_is_404(
    client: TestClient, store: Store, built: Path, executor: Local, audit_lines: AuditLines
) -> None:
    """A page past the last, or any rendition of a file that is not shown, is not found, hardened and unrecorded."""
    scan = pdf(store, built, "quay-list.pdf", quay_pages(2))
    word = put(store, "harbour-notes.docx", mk.make_docx(built / "harbour-notes.docx").read_bytes(), p=PG)
    assert prepare(client, scan.virtual_path).status_code == 200
    jobs = len(executor.requests)
    for path, n in ((scan.virtual_path, 3), (word.virtual_path, 1), (scan.virtual_path + "x", 1)):
        r = rendition(client, path, n)
        assert (r.status_code, r.json()) == NOT_FOUND
        hardened(r)
    assert len(executor.requests) == jobs
    assert events_of(audit_lines, "file.view") == []
    for bad in (0, -1):
        r = rendition(client, scan.virtual_path, bad)
        assert r.status_code == 422
        hardened(r)


def test_without_rendition_the_view_is_as_before(
    client: TestClient, photo: FileRecord, store: Store, executor: Local, audit_lines: AuditLines
) -> None:
    """The file itself: its bytes and type, ``inline`` under its own name, one event naming it alone, and no job."""
    r = client.get("/files/view", params={"path": photo.virtual_path}, headers=HEADERS)

    with store.open_file(PG, photo.virtual_path)[1] as src:
        assert r.content == src.read()
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["content-disposition"] == "inline; filename*=UTF-8''quay-photo.jpg"
    hardened(r)
    (event,) = events_of(audit_lines, "file.view")
    assert event["target"]["ids"] == [photo.id]
    assert executor.requests == []


def test_a_derived_path_is_not_found(client: TestClient, photo: FileRecord, store: Store) -> None:
    """A render is reached through its rendition alone: its path is not found, and the chat's list does not show it."""
    assert prepare(client, photo.virtual_path).status_code == 200
    (render,) = store.derived_of(photo)
    path = f"/workspace/chat-1/.derived/{photo.id}/{render.name}"
    for headers in (HEADERS, HG):
        r = client.get("/files/view", params={"path": path}, headers=headers)
        assert (r.status_code, r.json()) == NOT_FOUND
        hardened(r)
    listing = client.get("/files/list", params={"directory": "/workspace/chat-1"}, headers=HG).json()
    assert [entry["name"] for entry in listing["entries"]] == ["quay-photo.jpg"]
    r = client.get("/files/list", params={"directory": "/workspace/chat-1/.derived"}, headers=HG)
    assert (r.status_code, r.json()) == NOT_FOUND


# --- the loss of a render ----------------------------------------------------------------------------------------


def test_a_page_past_the_render_cap_is_507(
    client: TestClient, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch, audit_lines: AuditLines
) -> None:
    """Review Focus 5: a page past the cap is 507 on the route, never a 500, and the pages rendered are still served."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(22))
    assert prepare(client, source.virtual_path).status_code == 200
    capped(store, monkeypatch, sum(row.size for row in store.derived_of(source)))

    r = rendition(client, source.virtual_path, 21)
    assert (r.status_code, r.json()) == (507, {"detail": "the renders of this file are at their limit"})
    hardened(r)
    assert events_of(audit_lines, "file.view") == []
    again = rendition(client, source.virtual_path, 1)
    assert (again.status_code, again.content[:3]) == (200, b"\xff\xd8\xff")


def refusing(error: Exception) -> Callable[..., DerivedRecord]:
    """A ``register_derived`` that refuses every render it is given with ``error``."""

    def register(*_: Any, **__: Any) -> DerivedRecord:
        raise error

    return register


def test_a_page_the_quota_refuses_is_507_with_the_quota_text(
    client: TestClient, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store's quota is the user's, not the source's cap: its own text, and the same 507."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(22))
    assert prepare(client, source.virtual_path).status_code == 200
    monkeypatch.setattr(store, "register_derived", refusing(QuotaExceeded("workspace quota exceeded")))

    r = rendition(client, source.virtual_path, 21)
    assert (r.status_code, r.json()) == (507, {"detail": "workspace quota exceeded"})
    hardened(r)


def test_a_page_that_exists_already_is_a_500(
    client: TestClient, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A render the store holds already is a loss the route cannot explain: a 500 of its own text, hardened."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(22))
    assert prepare(client, source.virtual_path).status_code == 200
    monkeypatch.setattr(store, "register_derived", refusing(Conflict("render exists")))

    r = rendition(client, source.virtual_path, 21)
    assert (r.status_code, r.json()) == (500, {"detail": "the renders of this file could not be kept"})
    hardened(r)


def test_a_page_the_job_did_not_make_is_a_500(
    client: TestClient, store: Store, built: Path, executor: Local, audit_lines: AuditLines
) -> None:
    """A job that answers ``ok`` without the page asked for leaves no row: ``internal error``, and no view event."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(22))
    assert prepare(client, source.virtual_path).status_code == 200
    executor.payload = {
        "kind": "pdf",
        "total": 22,
        "pages_without_text": [],
        "frames": None,
        "hint": None,
        "renders": [],
    }

    r = rendition(client, source.virtual_path, 21)
    assert (r.status_code, r.json()) == (500, {"detail": "internal error"})
    hardened(r)
    assert events_of(audit_lines, "file.view") == []


def test_a_render_whose_bytes_are_wrong_is_a_500(
    client: TestClient, store: Store, photo: FileRecord, settings: Settings, audit_lines: AuditLines
) -> None:
    """A render overwritten with text is not read back: ``internal error``, hardened, and no view event."""
    assert prepare(client, photo.virtual_path).status_code == 200
    (render,) = store.derived_of(photo)
    where = settings.data_dir / "workspaces" / render.caller / render.user_id / render.chat_id / render.relative_path
    where.write_bytes(b"Quay list, not a picture")

    r = rendition(client, photo.virtual_path, 1)
    assert (r.status_code, r.json()) == (500, {"detail": "internal error"})
    hardened(r)
    assert events_of(audit_lines, "file.view") == []


def test_a_view_that_cannot_be_recorded_sends_no_picture(
    client: TestClient,
    photo: FileRecord,
    audit_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: the render is made and kept, but its bytes leave only after the view event is written."""
    assert prepare(client, photo.virtual_path).status_code == 200
    seen: list[str] = []
    real = audit_stream.write

    def refuse(text: str, /) -> int:
        if '"file.view"' in text:
            seen.append(text)
            raise OSError("synthetic audit stream failure")
        return real(text)

    monkeypatch.setattr(audit_stream, "write", refuse)
    r = rendition(client, photo.virtual_path, 1)

    assert (r.status_code, r.json()) == (500, {"detail": "audit unavailable"})
    assert len(seen) == 1
    hardened(r)


# --- the queue -----------------------------------------------------------------------------------------------------


def test_a_busy_queue_is_503(client: TestClient, photo: FileRecord, executor: Local, store: Store) -> None:
    """An executor with no free slot refuses the job: 503 on a prepare and on a rendition, with nothing kept."""
    executor.status = "busy"
    prepared = prepare(client, photo.virtual_path)
    viewed = rendition(client, photo.virtual_path, 1)

    assert (prepared.status_code, prepared.json()) == BUSY
    assert (viewed.status_code, viewed.json()) == BUSY
    hardened(viewed)
    assert store.derived_of(photo) == []


def test_without_a_queue_a_prepare_is_busy_and_a_plain_view_still_answers(
    settings: Settings, store: Store, audit: AuditLog, photo: FileRecord
) -> None:
    """An app with no job queue refuses a prepare and a rendition as busy; the file itself is still viewed."""
    with TestClient(create_app(settings, store=store, audit=audit)) as client:
        prepared = prepare(client, photo.virtual_path)
        assert (prepared.status_code, prepared.json()) == BUSY
        viewed = rendition(client, photo.virtual_path, 1)
        assert (viewed.status_code, viewed.json()) == BUSY
        hardened(viewed)
        assert client.get("/files/view", params={"path": photo.virtual_path}, headers=HEADERS).status_code == 200


def test_each_route_waits_for_its_own_turn_on_its_own_gate(
    client: TestClient, store: Store, queue: JobQueue, monkeypatch: pytest.MonkeyPatch, built: Path
) -> None:
    """A prepare waits up to 120 s on the prepare gate; a page made for a view waits 5 s on the user's own turn.

    Both are within the caller's wall and slots.
    """
    asked: list[tuple[int, int, int | None, str]] = []
    real = queue.submit_renders

    async def watched(*args: Any, **more: Any) -> Any:
        call = more["call"]
        asked.append((call.wall_s, call.wait_s, call.slots, more["gate"]))
        return await real(*args, **more)

    monkeypatch.setattr(queue, "submit_renders", watched)
    source = pdf(store, built, "quay-list.pdf", quay_pages(21))
    assert prepare(client, source.virtual_path).status_code == 200
    assert rendition(client, source.virtual_path, 21).status_code == 200

    assert asked == [(120, 120, 2, "prepare"), (120, 5, 2, "user")]


def test_a_callers_wall_bounds_the_runners_budget(
    settings: Settings, store: Store, audit: AuditLog, executor: Local, photo: FileRecord, built: Path
) -> None:
    """A caller whose wall is below the global one gets jobs whose runner budget is inside its own wall."""
    narrow = dataclasses.replace(settings, ghostwriter_wall_s=30)
    scan = pdf(store, built, "quay-list.pdf", quay_pages(22))
    queue = JobQueue(narrow, executor, store, Metrics(narrow.data_dir))
    with TestClient(create_app(narrow, store=store, audit=audit, jobs=queue)) as client:
        assert prepare(client, photo.virtual_path).status_code == 200
        assert rendition(client, scan.virtual_path, 21).status_code == 200

    assert len(executor.requests) == 3  # the photo's prepare, the scan's prepare, its page 21
    budgets = [request.args["budget_s"] for request in executor.requests]
    assert all(1 <= budget < 30 for budget in budgets)
