"""The ``view_file`` tool on the MCP door, as Ghostwriter calls it.

The jobs run the real ``prepare_file`` runner in this process (:class:`~tests.api.test_tools_templates.Local`) over
files built at test time; every name, text and id is invented. The renders themselves are tested in
``tests/executor`` and the api's reading of them in ``tests/api/test_renders.py``; here the tool's part: which pages and
how many images one answer shows, that block ``i`` is ``shown[i]``, what the model is told of a page that could not be
shown, and that the event is written before an image leaves.
"""

import base64
import dataclasses
import io
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from tests.api.conftest import HD, HG, PG, AuditLines, put
from tests.api.mcp_calls import Headers, acall, answered, refusal, result, rpc, unavailable
from tests.api.test_renders import capped, quay_pages
from tests.api.test_tools_templates import Local
from tests.fixtures import make_fixtures as mk
from tests.fixtures import make_images as mi
from workbench.api import tools_views
from workbench.api.audit import AuditLog
from workbench.api.jobs import JobQueue
from workbench.api.main import create_app
from workbench.api.metrics import Metrics
from workbench.api.renders import MAX_SHOWN_BYTES, NOT_SHOWN
from workbench.api.store import Conflict, QuotaExceeded, Store
from workbench.config import Settings
from workbench.executor.runners import RUNNERS
from workbench.models import DerivedRecord, FileRecord, Principal

ANSWER_KEYS = {"path", "kind", "total", "shown", "truncated", "next_part", "hint", "outputs"}
SHOWN_KEYS = {"label", "n", "width", "height", "mime"}
LIMIT = "the renders of this file are at their limit"
NAMES = [
    "fill_template",
    "inspect_template",
    "list_files",
    "read_document",
    "run_python",
    "search_files",
    "view_file",
    "write_document",
    "write_presentation",
    "write_table",
]


@pytest.fixture
def fake_executor(settings: Settings, tmp_path: Path) -> Local:
    """The in-process executor."""
    return Local(settings.data_dir, tmp_path / "jobs")


@pytest.fixture
def app(settings: Settings, store: Store, audit: AuditLog, fake_executor: Local) -> FastAPI:
    """The real app with a job queue over the in-process executor."""
    queue = JobQueue(settings, fake_executor, store, Metrics(settings.data_dir))
    return create_app(settings, store=store, audit=audit, jobs=queue)


@pytest.fixture
def built(tmp_path: Path) -> Path:
    """Where files are built before they are uploaded."""
    folder = tmp_path / "built"
    folder.mkdir()
    return folder


def photo(store: Store, built: Path, name: str = "quay-photo.jpg") -> FileRecord:
    """A small photo, uploaded to Ghostwriter's chat."""
    return put(store, name, mi.make_photo(built / name).read_bytes(), p=PG)


def pdf(store: Store, built: Path, name: str, pages: list[str | None], *, password: str | None = None) -> FileRecord:
    """A PDF of those pages, uploaded to Ghostwriter's chat."""
    return put(store, name, mk.make_pdf(built / name, pages, password=password).read_bytes(), p=PG)


def view(
    client: TestClient,
    path: str,
    part: str | None = None,
    strips: int | None = None,
    headers: Headers = HG,
) -> httpx2.Response:
    """Call ``view_file`` as Ghostwriter does, on the MCP door."""
    arguments: dict[str, Any] = {"path": path}
    if part is not None:
        arguments["part"] = part
    if strips is not None:
        arguments["strips"] = strips
    return acall(client, "view_file", arguments, headers=headers)


def seen(r: httpx2.Response) -> dict[str, Any]:
    """The result of a call that ran: its content blocks and its structured answer."""
    answer = result(r)
    assert answer["isError"] is False, answer
    return answer


def images(answer: dict[str, Any]) -> list[dict[str, Any]]:
    """The image blocks of a result: every block after its text."""
    first, *rest = answer["content"]
    assert first["type"] == "text"
    assert all(block["type"] == "image" for block in rest)
    return rest


def raw(block: dict[str, Any]) -> bytes:
    """The bytes of an image block."""
    return base64.b64decode(block["data"], validate=True)


def events(audit_lines: AuditLines) -> list[dict[str, Any]]:
    """The ``view_file`` events written so far."""
    return [event for event in audit_lines() if event["operation"] == "tool.view_file"]


def file_of(settings: Settings, rec: DerivedRecord) -> Path:
    """Where a render lies on the volume."""
    return settings.data_dir / "workspaces" / rec.caller / rec.user_id / rec.chat_id / rec.relative_path


def whole_renders(store: Store, source: FileRecord) -> dict[int, DerivedRecord]:
    """The whole renders of a source, by page."""
    return {rec.ordinal: rec for rec in store.derived_of(source) if rec.strips == 1}


# --- what is shown -----------------------------------------------------------------------------------------------


def test_view_file_shows_an_image_to_the_assistant(
    client: TestClient, store: Store, built: Path, audit_lines: AuditLines
) -> None:
    """The text comes first, then one image block for the assistant alone; the answer has the contract's keys."""
    source = photo(store, built)
    answer = seen(view(client, source.virtual_path))

    text, image = answer["content"]
    assert text["type"] == "text"
    assert (image["type"], image["mimeType"], image["annotations"]) == (
        "image",
        "image/jpeg",
        {"audience": ["assistant"]},
    )
    data = raw(image)
    assert data[:3] == b"\xff\xd8\xff"
    assert len(data) < 768 * 1024
    structured = answer["structuredContent"]
    assert set(structured) == ANSWER_KEYS
    assert structured["outputs"] == []
    assert {key: structured[key] for key in ("path", "kind", "total", "truncated", "next_part", "hint")} == {
        "path": source.virtual_path,
        "kind": "image",
        "total": 1,
        "truncated": False,
        "next_part": None,
        "hint": None,
    }
    (shown,) = structured["shown"]
    assert set(shown) == SHOWN_KEYS
    assert (shown["label"], shown["n"], shown["mime"]) == ("image", 1, "image/jpeg")
    assert Image.open(io.BytesIO(data)).size == (shown["width"], shown["height"])
    assert json.loads(text["text"]) == structured  # the text is the structured answer, as every tool's is
    (event,) = events(audit_lines)
    (render,) = store.derived_of(source)
    assert event["target"]["ids"] == [source.id, render.id]


def test_view_file_shows_pages_1_to_4_by_default(
    client: TestClient, store: Store, built: Path, audit_lines: AuditLines
) -> None:
    """Left out, ``part`` is the first four pages of a PDF, whatever more it has."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(6))
    answer = seen(view(client, source.virtual_path))

    structured = answer["structuredContent"]
    assert [shown["label"] for shown in structured["shown"]] == ["page 1", "page 2", "page 3", "page 4"]
    assert [shown["n"] for shown in structured["shown"]] == [1, 2, 3, 4]
    assert (structured["kind"], structured["total"], structured["truncated"], structured["next_part"]) == (
        "pdf",
        6,
        False,
        None,
    )
    assert len(images(answer)) == 4
    (event,) = events(audit_lines)
    assert event["criteria"] == {"caller": "ghostwriter", "images": 4, "strips": 1, "paged": False}


def test_a_part_shows_the_pages_it_names(client: TestClient, store: Store, built: Path) -> None:
    """``page 3``, ``3`` and ``Pages 2-3`` read as the plain grammar says; a window past the end stops at the end."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(6))
    for part, pages in (("page 3", [3]), ("3", [3]), ("Pages 2-3", [2, 3]), ("pages 5-9", [5, 6])):
        answer = seen(view(client, source.virtual_path, part))
        assert [shown["n"] for shown in answer["structuredContent"]["shown"]] == pages, part
        assert len(images(answer)) == len(pages)


def test_view_file_caps_at_8_images(client: TestClient, store: Store, built: Path, audit_lines: AuditLines) -> None:
    """Twelve pages asked for give eight, and ``next_part`` names the rest."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(12))
    answer = seen(view(client, source.virtual_path, "pages 1-12"))

    structured = answer["structuredContent"]
    assert len(images(answer)) == 8
    assert [shown["n"] for shown in structured["shown"]] == list(range(1, 9))
    assert (structured["truncated"], structured["next_part"]) == (True, "pages 9-12")
    again = seen(view(client, source.virtual_path, structured["next_part"]))
    assert [shown["n"] for shown in again["structuredContent"]["shown"]] == [9, 10, 11, 12]
    assert again["structuredContent"]["truncated"] is False
    first, second = events(audit_lines)
    assert first["criteria"] == {"caller": "ghostwriter", "images": 8, "strips": 1, "paged": True}
    assert second["criteria"]["images"] == 4


def test_pages_past_what_one_answer_takes_are_not_rendered(
    client: TestClient, store: Store, built: Path, fake_executor: Local
) -> None:
    """Only the pages one answer may show are made on demand: the rest wait for the call ``next_part`` names."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(30))
    answer = seen(view(client, source.virtual_path, "page 1"))  # the prepare makes pages 1-20
    assert len(images(answer)) == 1
    made = len(fake_executor.requests)

    answer = seen(view(client, source.virtual_path, "pages 19-30"))

    structured = answer["structuredContent"]
    assert [shown["n"] for shown in structured["shown"]] == list(range(19, 27))
    assert (structured["truncated"], structured["next_part"]) == (True, "pages 27-30")
    (job,) = fake_executor.requests[made:]
    assert job.args["pages"] == [21, 22, 23, 24, 25, 26]
    assert max(rec.ordinal for rec in store.derived_of(source)) == 26


def test_the_last_page_that_does_not_fit_is_named_alone(client: TestClient, store: Store, built: Path) -> None:
    """A rest of one page is ``page N``, not ``pages N-N``."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(9))
    answer = seen(view(client, source.virtual_path, "pages 1-9"))

    assert (answer["structuredContent"]["truncated"], answer["structuredContent"]["next_part"]) == (True, "page 9")


def test_view_file_caps_at_2_5_mib(
    client: TestClient, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Renders of about 700 KiB each: three fit in 2.5 MiB, a fourth does not, and the page it is ends the answer."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(6))
    real = tools_views.read_render
    size = 700 * 1024

    def padded(store: Store, rec: DerivedRecord) -> bytes | None:
        data = real(store, rec)
        return None if data is None else data + bytes(size - len(data))

    monkeypatch.setattr(tools_views, "read_render", padded)
    answer = seen(view(client, source.virtual_path, "pages 1-6"))

    blocks = images(answer)
    structured = answer["structuredContent"]
    assert len(blocks) == len(structured["shown"]) == 3
    assert sum(len(raw(block)) for block in blocks) == 3 * size <= MAX_SHOWN_BYTES
    assert (structured["truncated"], structured["next_part"]) == (True, "pages 4-6")


@pytest.mark.parametrize(("strips", "pages", "rest"), [(2, 4, "pages 5-6"), (3, 2, "pages 3-6")])
def test_strips_cap_the_pages_of_one_answer(
    client: TestClient, store: Store, built: Path, strips: int, pages: int, rest: str
) -> None:
    """With ``k`` strips a page is ``k`` images, so eight images are ``8 // k`` whole pages."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(6))
    answer = seen(view(client, source.virtual_path, "pages 1-6", strips))

    structured = answer["structuredContent"]
    assert len(images(answer)) == len(structured["shown"]) == pages * strips
    assert {shown["n"] for shown in structured["shown"]} == set(range(1, pages + 1))
    assert (structured["truncated"], structured["next_part"]) == (True, rest)


def test_strips_come_as_k_images_per_page(
    client: TestClient, store: Store, built: Path, audit_lines: AuditLines
) -> None:
    """A page cut in two is two images, in order, each labelled; the event counts them and names the strips."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(3))
    answer = seen(view(client, source.virtual_path, "page 2", 2))

    structured = answer["structuredContent"]
    assert [shown["label"] for shown in structured["shown"]] == ["page 2, strip 1 of 2", "page 2, strip 2 of 2"]
    assert [shown["n"] for shown in structured["shown"]] == [2, 2]
    assert len(images(answer)) == 2
    (event,) = events(audit_lines)
    assert event["criteria"] == {"caller": "ghostwriter", "images": 2, "strips": 2, "paged": True}
    assert len(event["target"]["ids"]) == 3  # the source and its two strips


def test_a_picture_is_cut_into_strips_too(client: TestClient, store: Store, built: Path) -> None:
    """A picture cut in three is three images of it, each labelled; its one page is ``n`` 1."""
    source = photo(store, built)
    answer = seen(view(client, source.virtual_path, strips=3))

    structured = answer["structuredContent"]
    assert [shown["label"] for shown in structured["shown"]] == [f"image, strip {i} of 3" for i in (1, 2, 3)]
    assert {shown["n"] for shown in structured["shown"]} == {1}
    assert len(images(answer)) == 3
    assert (structured["truncated"], structured["next_part"]) == (False, None)


def test_image_blocks_follow_shown_in_order(client: TestClient, store: Store, built: Path, settings: Settings) -> None:
    """Block ``i`` is the render ``shown[i]`` names: the same page, width and height, whatever order it was made in."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(3))
    # Page 3 first: the rows are made in the order a call asks, not the order of the pages.
    assert seen(view(client, source.virtual_path, "page 3"))["isError"] is False
    answer = seen(view(client, source.virtual_path, "pages 1-3"))

    by_bytes = {file_of(settings, rec).read_bytes(): rec for rec in whole_renders(store, source).values()}
    assert len(by_bytes) == 3
    shown = answer["structuredContent"]["shown"]
    assert [item["n"] for item in shown] == [1, 2, 3]
    for item, block in zip(shown, images(answer), strict=True):
        rec = by_bytes[raw(block)]
        assert (rec.ordinal, rec.width, rec.height, rec.mime) == (
            item["n"],
            item["width"],
            item["height"],
            item["mime"],
        )
        assert block["mimeType"] == item["mime"]


def test_view_file_shows_a_chart_run_python_made(
    client: TestClient, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A picture a tool wrote is a file of the chat like any other: it is shown, and as a PNG, as it was written."""

    def chart(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        bars = Image.new("RGB", (320, 200), "white")
        bars.paste(Image.new("RGB", (60, 120), "navy"), (40, 70))
        bars.save(ctx.out_dir / "chart.png")
        return {"stdout": "", "stderr": "", "truncated": False}

    monkeypatch.setitem(RUNNERS, "run_python", chart)
    ran = answered(acall(client, "run_python", {"code": "pass"}))
    (path,) = ran["files"]
    answer = seen(view(client, path))

    (image,) = images(answer)
    assert image["mimeType"] == "image/png"
    assert raw(image)[:8] == b"\x89PNG\r\n\x1a\n"
    assert answer["structuredContent"]["shown"][0]["label"] == "image"


def test_view_file_on_a_word_file_says_read_it(
    client: TestClient, store: Store, built: Path, fake_executor: Local, audit_lines: AuditLines
) -> None:
    """A Word file is not shown: one text block, no job, and a call that is recorded all the same."""
    source = put(store, "harbour-notes.docx", mk.make_docx(built / "harbour-notes.docx").read_bytes(), p=PG)
    answer = seen(view(client, source.virtual_path))

    assert len(answer["content"]) == 1
    assert answer["structuredContent"] == {
        "path": source.virtual_path,
        "kind": "none",
        "total": 0,
        "shown": [],
        "truncated": False,
        "next_part": None,
        "hint": NOT_SHOWN,
        "outputs": [],
    }
    assert fake_executor.requests == []
    (event,) = events(audit_lines)
    assert (event["action"], event["outcome"], event["target"]["ids"]) == ("query", "ok", [source.id])
    assert event["criteria"] == {"caller": "ghostwriter", "images": 0, "strips": 1, "paged": False}


# --- what is recorded --------------------------------------------------------------------------------------------


def test_view_file_is_recorded_before_the_images_go(
    client: TestClient,
    store: Store,
    built: Path,
    audit_stream: io.StringIO,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The event names the source and each render shown; with the chain broken the call fails and no image is sent.

    The renders exist first, so the failure is the call's own event, not the prepare's.
    """
    source = pdf(store, built, "quay-list.pdf", quay_pages(3))
    seen(view(client, source.virtual_path, "pages 1-3"))
    renders = whole_renders(store, source)
    (event,) = events(audit_lines)
    assert (event["action"], event["operation"], event["outcome"]) == ("query", "tool.view_file", "ok")
    assert event["target"]["ids"] == [source.id, *(renders[n].id for n in (1, 2, 3))]
    assert event["target"]["count"] == 4
    assert event["target"]["container"] == "chat:chat-1"

    def refuse(_text: str, /) -> int:
        raise OSError("synthetic audit stream failure")

    monkeypatch.setattr(audit_stream, "write", refuse)
    r = view(client, source.virtual_path, "pages 1-3")
    unavailable(r)
    assert "image" not in r.text


def test_a_call_that_stated_no_purpose_is_refused_before_anything_is_shown(
    settings: Settings, store: Store, audit: AuditLog, fake_executor: Local, built: Path, audit_lines: AuditLines
) -> None:
    """``query`` is an action that needs a purpose by default: a call without one is denied and shows nothing."""
    required = dataclasses.replace(settings, require_purpose=frozenset({"query"}))
    queue = JobQueue(required, fake_executor, store, Metrics(settings.data_dir))
    source = photo(store, built)
    with TestClient(create_app(required, store=store, audit=audit, jobs=queue)) as client:
        reason, _ = refusal(view(client, source.virtual_path))
        assert reason == "purpose_required"
        assert fake_executor.requests == []
        (event,) = events(audit_lines)
        assert event["outcome"] == "denied"


# --- a page that cannot be shown ---------------------------------------------------------------------------------


def test_a_tampered_render_is_skipped_with_a_hint(
    client: TestClient, store: Store, built: Path, settings: Settings, audit_lines: AuditLines
) -> None:
    """A render that is not what its row says is left out and named; the blocks still follow ``shown``."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(4))
    seen(view(client, source.virtual_path, "pages 1-4"))
    renders = whole_renders(store, source)
    file_of(settings, renders[2]).write_bytes(b"not a picture, whatever its row says")

    answer = seen(view(client, source.virtual_path, "pages 1-4"))
    structured = answer["structuredContent"]
    assert [shown["n"] for shown in structured["shown"]] == [1, 3, 4]
    assert structured["hint"] == "page 2 could not be shown"
    assert (structured["truncated"], structured["next_part"]) == (False, None)
    for item, block in zip(structured["shown"], images(answer), strict=True):
        assert raw(block) == file_of(settings, renders[item["n"]]).read_bytes()
    _, second = events(audit_lines)
    assert second["target"]["ids"] == [source.id, renders[1].id, renders[3].id, renders[4].id]
    assert second["criteria"]["images"] == 3
    assert "not a picture" not in json.dumps(audit_lines())


def test_a_page_past_the_render_cap_is_named_in_the_hint(
    client: TestClient, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch, audit_lines: AuditLines
) -> None:
    """Review Focus 5: a window wholly past the cap is not an error: no image, the limit named, the call recorded.

    Nothing more can be made, so the rest of the window past what one answer takes is not offered as ``next_part``.
    """
    source = pdf(store, built, "quay-list.pdf", quay_pages(30))
    seen(view(client, source.virtual_path))
    capped(store, monkeypatch, sum(row.size for row in store.derived_of(source)))

    answer = seen(view(client, source.virtual_path, "pages 21-30"))

    assert len(answer["content"]) == 1
    structured = answer["structuredContent"]
    assert structured["shown"] == []
    assert structured["hint"] == f"{LIMIT}; pages after 20 cannot be shown"
    assert (structured["truncated"], structured["next_part"]) == (False, None)
    *_, event = events(audit_lines)
    assert event["target"]["ids"] == [source.id]
    assert event["criteria"] == {"caller": "ghostwriter", "images": 0, "strips": 1, "paged": True}
    assert [
        shown["n"] for shown in seen(view(client, source.virtual_path, "page 20"))["structuredContent"]["shown"]
    ] == [20]


def test_the_pages_before_the_cap_are_still_shown(
    client: TestClient, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A window that begins before the cap shows its pages that exist and names each that could not be made."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(22))
    seen(view(client, source.virtual_path))
    capped(store, monkeypatch, sum(row.size for row in store.derived_of(source)))

    answer = seen(view(client, source.virtual_path, "pages 19-22"))

    structured = answer["structuredContent"]
    assert [shown["n"] for shown in structured["shown"]] == [19, 20]
    assert len(images(answer)) == 2
    assert structured["hint"] == "page 21 could not be shown page 22 could not be shown"


def test_a_page_the_quota_refuses_is_named_not_failed(
    client: TestClient, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch, audit_lines: AuditLines
) -> None:
    """The user's quota, on the page made on demand: the page is named, and the call is not a tool error."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(22))
    seen(view(client, source.virtual_path))

    def refuse(*_: Any, **__: Any) -> DerivedRecord:
        raise QuotaExceeded("workspace quota exceeded")

    monkeypatch.setattr(store, "register_derived", refuse)
    answer = seen(view(client, source.virtual_path, "page 21"))

    structured = answer["structuredContent"]
    assert (len(answer["content"]), structured["shown"]) == (1, [])
    assert structured["hint"] == "page 21 could not be shown"
    assert (structured["truncated"], structured["next_part"]) == (False, None)
    *_, event = events(audit_lines)
    assert event["criteria"]["images"] == 0


def test_a_render_that_cannot_be_kept_for_any_other_reason_is_named_too(
    client: TestClient, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store's own 500, ``could not be kept``, is a page named in the hint, as the quota is."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(22))
    seen(view(client, source.virtual_path))

    def refuse(*_: Any, **__: Any) -> DerivedRecord:
        raise Conflict("render exists")

    monkeypatch.setattr(store, "register_derived", refuse)
    answer = seen(view(client, source.virtual_path, "pages 21-22"))

    assert answer["structuredContent"]["hint"] == "page 21 could not be shown page 22 could not be shown"


def test_a_rest_after_a_page_that_could_not_be_shown_is_still_named(
    client: TestClient, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pages past what one answer takes are ``next_part`` whether or not the pages before them were shown."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(22))
    seen(view(client, source.virtual_path))

    def refuse(*_: Any, **__: Any) -> DerivedRecord:
        raise QuotaExceeded("workspace quota exceeded")

    monkeypatch.setattr(store, "register_derived", refuse)
    answer = seen(view(client, source.virtual_path, "pages 21-22", 3))

    structured = answer["structuredContent"]
    assert structured["shown"] == []
    assert structured["hint"] == "page 21 could not be shown page 22 could not be shown"


# --- what is refused ---------------------------------------------------------------------------------------------


def test_view_file_of_another_chat_or_a_derived_path_is_not_found(
    client: TestClient, store: Store, built: Path
) -> None:
    """Another chat's file, a render's own path, and a file that does not exist are one answer."""
    other = put(
        store,
        "elsewhere.jpg",
        mi.make_photo(built / "elsewhere.jpg").read_bytes(),
        directory="/workspace/chat-2",
        p=Principal("ghostwriter", "user-a", "chat-2", confined=True),
    )
    source = photo(store, built)
    seen(view(client, source.virtual_path))
    (render,) = store.derived_of(source)

    for path in (
        other.virtual_path,
        f"/workspace/chat-1/.derived/{source.id}/{render.name}",
        "/workspace/chat-1/missing.jpg",
    ):
        assert refusal(view(client, path)) == ("refused", "not found"), path
    assert refusal(view(client, source.virtual_path, headers={**HG, "X-User-Id": "user-b"})) == ("refused", "not found")


def test_a_part_that_is_not_a_page_is_refused(client: TestClient, store: Store, built: Path) -> None:
    """The grammar is pages only; a page past the end is a ``no_such_part`` that says how many there are."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(3))
    still = photo(store, built)

    assert refusal(view(client, source.virtual_path, "slide 2")) == (
        "refused",
        "no_such_part: give a page as 'page 3', or pages as 'pages 2-5'",
    )
    assert refusal(view(client, source.virtual_path, "page 4")) == (
        "refused",
        "no_such_part: this document has 3 pages",
    )
    assert refusal(view(client, source.virtual_path, "pages 3-2"))[0] == "refused"
    assert refusal(view(client, still.virtual_path, "page 2")) == ("refused", "no_such_part: this document has 1 page")
    assert [shown["n"] for shown in seen(view(client, still.virtual_path, "page 1"))["structuredContent"]["shown"]] == [
        1
    ]
    assert [shown["n"] for shown in seen(view(client, still.virtual_path, "1"))["structuredContent"]["shown"]] == [1]


@pytest.mark.parametrize("strips", [0, 4, -1])
def test_strips_are_one_to_three(client: TestClient, store: Store, built: Path, strips: int) -> None:
    """Other counts do not validate."""
    source = photo(store, built)

    assert refusal(view(client, source.virtual_path, strips=strips))[0] == "invalid_arguments"


def test_a_pdf_that_cannot_be_opened_is_refused_by_what_it_is(client: TestClient, store: Store, built: Path) -> None:
    """Review Focus 3: a password-protected PDF is the runner's refusal, short, and no render is kept."""
    source = pdf(store, built, "sealed.pdf", quay_pages(2), password="invented-secret")
    reason, text = refusal(view(client, source.virtual_path))

    assert reason == "refused"
    assert text.startswith("unreadable_file")
    assert store.derived_of(source) == []


# --- what the model is told --------------------------------------------------------------------------------------


def tools_of(client: TestClient, headers: Headers) -> list[dict[str, Any]]:
    """The tools a caller is offered."""
    return result(rpc(client, "tools/list", headers=headers))["tools"]


def test_the_schema_stays_small(client: TestClient) -> None:
    """The tool's schema and description are told to the model on every turn: they stay within 3000 characters."""
    (tool,) = [t for t in tools_of(client, HG) if t["name"] == "view_file"]

    assert len(json.dumps(tool["inputSchema"])) + len(tool["description"]) <= 3000
    assert 600 <= len(tool["description"]) <= 1000


def test_ghostwriter_is_offered_view_file(client: TestClient) -> None:
    """Ghostwriter's ten tools, sorted by name; docint's list is ``run_python`` alone."""
    assert [tool["name"] for tool in tools_of(client, HG)] == NAMES
    assert [tool["name"] for tool in tools_of(client, HD)] == ["run_python"]


def test_the_description_says_what_the_tool_does_not_show(client: TestClient) -> None:
    """The model is told which files are not pictures, and to read them, and not to describe what it has not seen."""
    (tool,) = [t for t in tools_of(client, HG) if t["name"] == "view_file"]
    description = tool["description"]

    for phrase in ("read them with read_document", "next_part", "at most 8", "never describe an image you have not"):
        assert phrase.lower() in description.lower(), phrase
    properties = tool["inputSchema"]["properties"]
    assert properties["part"]["type"] == "string" and properties["strips"]["type"] == "integer"
    assert tool["inputSchema"]["required"] == ["path"]
