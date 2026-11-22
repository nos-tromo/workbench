"""The ``view_file`` tool on the MCP door, as Ghostwriter calls it.

The jobs run the real ``prepare_file`` runner in this process (:class:`~tests.api.test_tools_templates.Local`) over
files built at test time; every name, text and id is invented. The renders themselves are tested in
``tests/executor`` and the api's reading of them in ``tests/api/test_renders.py``; here the tool's part: which pages and
how many images one answer shows, that block ``i`` is ``shown[i]``, what the model is told of a page that could not be
shown, and that the event is written before an image leaves.
"""

import asyncio
import base64
import contextlib
import dataclasses
import io
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from tests.api.conftest import HD, HG, PG, AuditLines, put
from tests.api.mcp_calls import Headers, acall, answered, message, refusal, result, rpc, unavailable
from tests.api.test_files_renders import deck
from tests.api.test_renders import HeldConversion, capped, quay_pages
from tests.api.test_tools_templates import Local
from tests.conftest import FakeOffice, scaled
from tests.fixtures import make_fixtures as mk
from tests.fixtures import make_images as mi
from tests.fixtures import make_templates as mt
from workbench.api import tools_mcp, tools_views
from workbench.api.audit import AuditLog
from workbench.api.auth import MCP_PATH
from workbench.api.jobs import JobQueue
from workbench.api.main import create_app
from workbench.api.metrics import Metrics
from workbench.api.renders import MAX_SHOWN_BYTES, NOT_SHOWN
from workbench.api.store import Conflict, QuotaExceeded, Store
from workbench.config import Settings
from workbench.executor.runners import RUNNERS
from workbench.models import DerivedRecord, FileRecord, Principal

ANSWER_KEYS = {"path", "kind", "unit", "total", "shown", "truncated", "next_part", "hint", "outputs"}
SHOWN_KEYS = {"label", "n", "width", "height", "mime"}
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
    """Left out, ``part`` is the first four pages of a PDF, whatever more it has; the hint names the pages not shown.

    Final review I3a: ``truncated`` stays "cut for room", so the sentence is what tells the model of the rest.
    """
    source = pdf(store, built, "quay-list.pdf", quay_pages(30))
    answer = seen(view(client, source.virtual_path))

    structured = answer["structuredContent"]
    assert [shown["label"] for shown in structured["shown"]] == ["page 1", "page 2", "page 3", "page 4"]
    assert [shown["n"] for shown in structured["shown"]] == [1, 2, 3, 4]
    assert (structured["kind"], structured["total"], structured["truncated"], structured["next_part"]) == (
        "pdf",
        30,
        False,
        None,
    )
    assert structured["hint"] == 'Pages 5-30 are not shown; call view_file with part "pages 5-8".'
    assert len(images(answer)) == 4
    (event,) = events(audit_lines)
    assert event["criteria"] == {"caller": "ghostwriter", "images": 4, "strips": 1, "paged": False}


@pytest.mark.parametrize(
    ("total", "said"),
    [
        (4, None),
        (5, 'Page 5 is not shown; call view_file with part "page 5".'),
        (6, 'Pages 5-6 are not shown; call view_file with part "pages 5-6".'),
    ],
)
def test_the_default_window_names_only_the_pages_it_leaves_out(
    client: TestClient, store: Store, built: Path, total: int, said: str | None
) -> None:
    """A PDF of four pages is shown whole; one of five or six names its rest, and the window that shows it."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(total))

    assert seen(view(client, source.virtual_path))["structuredContent"]["hint"] == said


def test_a_window_cut_for_room_names_both_its_rest_and_the_pages_after_it(
    client: TestClient, store: Store, built: Path
) -> None:
    """Three strips a page: the default window is cut after two pages, and pages 5 on are named as well."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(10))
    structured = seen(view(client, source.virtual_path, strips=3))["structuredContent"]

    assert (structured["truncated"], structured["next_part"]) == (True, "pages 3-4")
    assert structured["hint"] == 'Pages 5-10 are not shown; call view_file with part "pages 5-8".'


def test_a_named_part_says_nothing_of_the_pages_after_it(client: TestClient, store: Store, built: Path) -> None:
    """The model chose the window: the pages it did not ask for are not named."""
    source = pdf(store, built, "quay-list.pdf", quay_pages(30))

    assert seen(view(client, source.virtual_path, "pages 1-4"))["structuredContent"]["hint"] is None


def test_a_picture_with_several_frames_says_only_the_first_is_shown(
    client: TestClient, store: Store, built: Path
) -> None:
    """Final review I3b: an animated GIF is shown as its first frame, and the model is told so."""
    source = put(store, "harbour-cranes.gif", mi.make_gif(built / "harbour-cranes.gif", frames=3).read_bytes(), p=PG)
    answer = seen(view(client, source.virtual_path))

    assert len(images(answer)) == 1
    assert answer["structuredContent"]["hint"] == "Only the first of 3 frames is shown."


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


def test_view_file_says_a_spreadsheet_is_not_shown(
    client: TestClient, store: Store, built: Path, fake_executor: Local, audit_lines: AuditLines
) -> None:
    """A spreadsheet is not shown: one text block, no job, and a call that is recorded all the same."""
    source = put(store, "quarterly-figures.xlsx", mk.make_xlsx(built / "quarterly-figures.xlsx").read_bytes(), p=PG)
    answer = seen(view(client, source.virtual_path))

    assert len(answer["content"]) == 1
    assert answer["structuredContent"] == {
        "path": source.virtual_path,
        "kind": "none",
        "unit": None,
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


def test_the_unit_is_null_for_a_picture_and_page_for_a_pdf(client: TestClient, store: Store, built: Path) -> None:
    """The contract: ``unit`` counts what ``total`` counts, and a picture has none."""
    still = photo(store, built)
    source = pdf(store, built, "quay-list.pdf", quay_pages(2))

    assert seen(view(client, still.virtual_path))["structuredContent"]["unit"] is None
    assert seen(view(client, source.virtual_path))["structuredContent"]["unit"] == "page"


# --- Office files: the pages of a document, the slides of a deck --------------------------------------------------


def test_view_file_shows_the_slides_of_a_deck(
    client: TestClient, store: Store, built: Path, fake_office: FakeOffice, audit_lines: AuditLines
) -> None:
    """A deck is converted once and drawn; its slides are labelled as slides, and the rest is named by slide."""
    source = deck(store, built, 6)
    answer = seen(view(client, source.virtual_path))

    structured = answer["structuredContent"]
    assert set(structured) == ANSWER_KEYS
    assert (structured["kind"], structured["unit"], structured["total"]) == ("office", "slide", 6)
    assert [shown["label"] for shown in structured["shown"]] == ["slide 1", "slide 2", "slide 3", "slide 4"]
    assert [shown["n"] for shown in structured["shown"]] == [1, 2, 3, 4]
    assert len(images(answer)) == 4
    assert (structured["truncated"], structured["next_part"]) == (False, None)
    assert structured["hint"] == 'Slides 5-6 are not shown; call view_file with part "slides 5-6".'
    assert fake_office.calls == 1
    (event,) = events(audit_lines)
    renders = whole_renders(store, source)
    assert event["target"]["ids"] == [source.id, *(renders[n].id for n in (1, 2, 3, 4))]
    assert event["criteria"] == {"caller": "ghostwriter", "images": 4, "strips": 1, "paged": False}


def test_view_file_pages_a_deck_by_slides(
    client: TestClient, store: Store, built: Path, fake_office: FakeOffice
) -> None:
    """Twelve slides asked for give eight, ``next_part`` names the rest by slide, and the slide grammar is taken."""
    source = deck(store, built, 12)
    answer = seen(view(client, source.virtual_path, "slides 1-12"))

    structured = answer["structuredContent"]
    assert len(images(answer)) == 8
    assert [shown["n"] for shown in structured["shown"]] == list(range(1, 9))
    assert (structured["truncated"], structured["next_part"]) == (True, "slides 9-12")
    again = seen(view(client, source.virtual_path, structured["next_part"]))["structuredContent"]
    assert [shown["label"] for shown in again["shown"]] == [f"slide {n}" for n in range(9, 13)]
    assert again["truncated"] is False
    for part, slides in (("slide 3", [3]), ("3", [3]), ("Slides 2-3", [2, 3]), ("2-3", [2, 3])):
        shown = seen(view(client, source.virtual_path, part))["structuredContent"]["shown"]
        assert [item["n"] for item in shown] == slides, part
    assert fake_office.calls == 1


def test_view_file_refuses_a_page_of_a_deck(
    client: TestClient, store: Store, built: Path, fake_office: FakeOffice
) -> None:
    """A deck counts slides: ``page 2`` is not of its grammar, and a slide past the end names how many there are."""
    source = deck(store, built, 3)

    assert refusal(view(client, source.virtual_path, "page 2")) == (
        "refused",
        "no_such_part: give a slide as 'slide 3', or slides as 'slides 2-5'",
    )
    assert refusal(view(client, source.virtual_path, "slide 4")) == (
        "refused",
        "no_such_part: this document has 3 slides",
    )


def test_view_file_passes_on_the_refusal_of_a_deck_without_slides(
    client: TestClient, store: Store, built: Path, fake_office: FakeOffice
) -> None:
    """Review Focus 4: a slide master with layouts and no slides is refused in the conversion's own words.

    ``view_file`` passes the 422 on, with nothing converted and nothing kept, whichever part is asked for.
    """
    master = mt.as_potx(mt.make_master(built / "harbour-master.pptx", examples=0), built / "harbour-master.potx")
    source = put(store, "harbour-master.potx", master.read_bytes(), p=PG)
    words = "unsupported_format: the deck has no slides; inspect_template lists the layouts of a slide master"

    assert refusal(view(client, source.virtual_path)) == ("refused", words)
    assert refusal(view(client, source.virtual_path, "slide 1")) == ("refused", words)
    assert store.derived_of(source) == []
    assert fake_office.calls == 0


def test_view_file_shows_a_word_file_as_pages(
    client: TestClient, store: Store, built: Path, fake_office: FakeOffice
) -> None:
    """A Word file is converted and drawn as pages, labelled as a PDF's are; it takes the page grammar alone."""
    source = put(store, "harbour-notes.docx", mk.make_docx(built / "harbour-notes.docx").read_bytes(), p=PG)
    answer = seen(view(client, source.virtual_path, "page 1"))

    structured = answer["structuredContent"]
    assert (structured["kind"], structured["unit"], structured["total"]) == ("office", "page", 3)
    assert [shown["label"] for shown in structured["shown"]] == ["page 1"]
    assert len(images(answer)) == 1
    assert structured["hint"] is None
    assert fake_office.decks == [False]
    assert refusal(view(client, source.virtual_path, "slide 1")) == (
        "refused",
        "no_such_part: give a page as 'page 3', or pages as 'pages 2-5'",
    )


def test_view_file_names_a_slide_count_that_differs(
    client: TestClient, store: Store, built: Path, fake_office: FakeOffice
) -> None:
    """Review Focus 2: a build that leaves a slide out draws fewer pictures; the hint says their numbers may be off."""
    fake_office.drop = {2}
    source = deck(store, built, 3)
    structured = seen(view(client, source.virtual_path))["structuredContent"]

    assert (structured["total"], [shown["label"] for shown in structured["shown"]]) == (2, ["slide 1", "slide 2"])
    assert structured["hint"] == (
        "The deck has 3 slides and 2 were drawn, so a picture's number may not be its slide's."
    )


def test_view_file_shows_a_deck_written_in_the_chat(
    client: TestClient, fake_office: FakeOffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deck a tool wrote is a file of the chat like any other: it is converted and shown by slide."""

    def writes_a_deck(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
        mk.make_deck(ctx.out_dir / "harbour-deck.pptx", 2)
        return {"stdout": "", "stderr": "", "truncated": False}

    monkeypatch.setitem(RUNNERS, "run_python", writes_a_deck)
    (path,) = answered(acall(client, "run_python", {"code": "pass"}))["files"]
    answer = seen(view(client, path))

    structured = answer["structuredContent"]
    assert (structured["kind"], structured["unit"]) == ("office", "slide")
    assert [shown["label"] for shown in structured["shown"]] == ["slide 1", "slide 2"]
    assert len(images(answer)) == 2
    assert fake_office.calls == 1


# --- a deck its prepare converts, and a deck refused once ---------------------------------------------------------

#: Ghostwriter's file routes state a purpose and a request id, as its MCP calls may.
ROUTE_HEADERS = {**HG, "X-Audit-Purpose": "reason=ghostwriter+chat", "X-Request-ID": "req-1"}
#: Ghostwriter's user B and the chat of theirs, as the store's own calls name them.
THEIRS = ("ghostwriter", "user-b", "chat-1")


@pytest.fixture
def anyio_backend() -> str:
    """Run the async tests on asyncio, the loop the api runs on."""
    return "asyncio"


@contextlib.asynccontextmanager
async def door(settings: Settings, store: Store, audit: AuditLog, queue: JobQueue) -> AsyncIterator[httpx2.AsyncClient]:
    """The real app over ``queue``, its audit chain open, reached from the test's own loop, so calls can overlap."""
    app = create_app(settings, store=store, audit=audit, jobs=queue)
    audit.start({"retention_days": settings.retention_days})
    try:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://workbench-api") as http:
            yield http
    finally:
        audit.close()


async def mcp_call(http: httpx2.AsyncClient, tool: str, arguments: dict[str, Any]) -> httpx2.Response:
    """Call a tool on the MCP door as Ghostwriter does."""
    body, headers = message("tools/call", {"name": tool, "arguments": arguments}, headers=HG)
    sent = {name: value if isinstance(value, str) else value.decode() for name, value in headers.items() if value}
    return await http.post(MCP_PATH, content=body, headers=sent)


async def prepare_route(http: httpx2.AsyncClient, source: FileRecord) -> httpx2.Response:
    """Prepare a file as Ghostwriter does right after its upload."""
    return await http.post("/files/prepare", params={"path": source.virtual_path}, headers=ROUTE_HEADERS)


@pytest.mark.anyio
async def test_view_file_waits_for_the_prepare_that_converts_its_deck(
    settings: Settings,
    store: Store,
    audit: AuditLog,
    built: Path,
    fake_office: FakeOffice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Final review: the model views a deck while the prepare of its attachment converts it.

    The view waits for that prepare, longer than a call waits for its own turn (lowered here to none), and answers the
    slides it drew: never busy, and with no second conversion.
    """
    monkeypatch.setattr(tools_mcp, "MCP_QUEUE_WAIT_S", 0)
    executor = HeldConversion(settings.data_dir, built.parent / "held-conversions")
    queue = JobQueue(settings, executor, store, Metrics(settings.data_dir))
    source = deck(store, built, 3)
    async with door(settings, store, audit, queue) as http:
        preparing = asyncio.create_task(prepare_route(http, source))
        await executor.converting.wait()
        viewing = asyncio.create_task(mcp_call(http, "view_file", {"path": source.virtual_path}))
        await asyncio.sleep(0.2)
        assert not viewing.done()
        executor.release.set()
        prepared, viewed = await asyncio.gather(preparing, viewing)

    assert prepared.status_code == 200
    answer = seen(viewed)
    assert [shown["label"] for shown in answer["structuredContent"]["shown"]] == ["slide 1", "slide 2", "slide 3"]
    assert len(images(answer)) == 3
    assert fake_office.calls == 1
    assert [req.tool for req in executor.requests] == ["convert_office", "prepare_file"]


@pytest.mark.anyio
async def test_view_file_waits_for_a_sources_lock_no_longer_than_its_bound(
    settings: Settings,
    store: Store,
    audit: AuditLog,
    built: Path,
    fake_office: FakeOffice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lock held past ``SOURCE_LOCK_WAIT_S`` (lowered here) still answers busy, ``wait_timeout``, as a refusal."""
    monkeypatch.setattr(tools_mcp, "SOURCE_LOCK_WAIT_S", 0.2)
    executor = HeldConversion(settings.data_dir, built.parent / "held-conversions")
    queue = JobQueue(settings, executor, store, Metrics(settings.data_dir))
    source = deck(store, built, 3)
    async with door(settings, store, audit, queue) as http:
        preparing = asyncio.create_task(prepare_route(http, source))
        await executor.converting.wait()
        viewed = await asyncio.wait_for(mcp_call(http, "view_file", {"path": source.virtual_path}), scaled(2))
        executor.release.set()
        assert (await preparing).status_code == 200

    assert refusal(viewed) == ("wait_timeout", "busy, retry later")


@pytest.mark.anyio
async def test_view_file_of_an_unprepared_deck_waits_out_another_users_conversion(
    settings: Settings,
    store: Store,
    audit: AuditLog,
    built: Path,
    fake_office: FakeOffice,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ruling R19: a deck no one prepared is converted by its view, after user B's conversion: never busy meanwhile.

    The view waits for the conversion gate past its own turn's wait (lowered here to none), as for a source's lock.
    """
    monkeypatch.setattr(tools_mcp, "MCP_QUEUE_WAIT_S", 0)
    executor = HeldConversion(settings.data_dir, built.parent / "held-conversions")
    queue = JobQueue(settings, executor, store, Metrics(settings.data_dir))
    theirs = put(store, "quay-deck.pptx", mk.make_deck(built / "quay-deck.pptx", 3).read_bytes(), p=Principal(*THEIRS))
    mine = deck(store, built, 3)
    async with door(settings, store, audit, queue) as http:
        preparing = asyncio.create_task(
            http.post(
                "/files/prepare",
                params={"path": theirs.virtual_path},
                headers={**ROUTE_HEADERS, "X-User-Id": THEIRS[1]},
            )
        )
        await executor.converting.wait()
        viewing = asyncio.create_task(mcp_call(http, "view_file", {"path": mine.virtual_path}))
        await asyncio.sleep(0.2)
        assert not viewing.done()
        executor.release.set()
        prepared, viewed = await asyncio.gather(preparing, viewing)

    assert prepared.status_code == 200
    answer = seen(viewed)
    assert [shown["label"] for shown in answer["structuredContent"]["shown"]] == ["slide 1", "slide 2", "slide 3"]
    assert [(req.user_id, req.tool) for req in executor.requests] == [
        ("user-b", "convert_office"),
        ("user-b", "prepare_file"),
        ("user-a", "convert_office"),
        ("user-a", "prepare_file"),
    ]
    assert executor.peak == 1


def test_view_file_of_a_refused_deck_answers_the_remembered_refusal(
    client: TestClient, store: Store, built: Path, fake_executor: Local, fake_office: FakeOffice
) -> None:
    """Final review: a deck its prepare could not convert is refused by every view after, at once, with no job."""
    fake_office.ceiling = 0
    source = deck(store, built, 2)
    words = "unreadable_file: the file holds more or larger pictures than can be drawn within the memory limit"
    prepared = client.post("/files/prepare", params={"path": source.virtual_path}, headers=ROUTE_HEADERS)
    assert (prepared.status_code, prepared.json()) == (422, {"detail": words})
    calls = fake_office.calls

    assert refusal(view(client, source.virtual_path)) == ("refused", words)
    assert refusal(view(client, source.virtual_path, "slide 2")) == ("refused", words)
    again = client.post("/files/prepare", params={"path": source.virtual_path}, headers=ROUTE_HEADERS)
    assert (again.status_code, again.json()) == (422, {"detail": words})
    assert [req.tool for req in fake_executor.requests] == ["convert_office"]
    assert fake_office.calls == calls


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
    assert structured["hint"] == "Page 2 could not be shown."
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
    assert structured["hint"] == "The renders of this file are at their limit, so pages 21-30 cannot be shown."
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
    assert structured["hint"] == "Pages 21, 22 could not be shown."


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
    assert structured["hint"] == "Page 21 could not be shown."
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

    assert answer["structuredContent"]["hint"] == "Pages 21, 22 could not be shown."


def test_a_rest_after_a_page_that_could_not_be_shown_is_still_named(
    client: TestClient, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pages past what one answer takes are ``next_part`` whether or not the pages before them were shown.

    Three strips a page: one answer takes pages 21 and 22, which the quota refuses; 23 and 24 are the rest.
    """
    source = pdf(store, built, "quay-list.pdf", quay_pages(24))
    seen(view(client, source.virtual_path))

    def refuse(*_: Any, **__: Any) -> DerivedRecord:
        raise QuotaExceeded("workspace quota exceeded")

    monkeypatch.setattr(store, "register_derived", refuse)
    answer = seen(view(client, source.virtual_path, "pages 21-24", 3))

    structured = answer["structuredContent"]
    assert structured["shown"] == []
    assert (structured["truncated"], structured["next_part"]) == (True, "pages 23-24")
    assert structured["hint"] == "Pages 21, 22 could not be shown."


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


def test_a_shown_image_is_described_as_a_page_or_a_slide(client: TestClient) -> None:
    """A deck's images are labelled ``slide 3``: the answer's schema says so, for both tools that show images."""
    for name in ("view_file", "read_document"):
        (tool,) = [t for t in tools_of(client, HG) if t["name"] == name]
        shown = tool["outputSchema"]["$defs"]["ShownImage"]
        assert "'slide 3'" in shown["properties"]["label"]["description"], name
        assert shown["properties"]["n"]["description"].startswith("The page or slide"), name
        assert "``slide 3``" in shown["description"], name


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
