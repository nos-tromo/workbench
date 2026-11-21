"""The ``write_presentation`` tool on the MCP door, as Ghostwriter calls it.

The job runs the real runner in this process (:class:`~tests.api.test_tools_templates.Local`), so a deck is a real one;
a test that needs another answer scripts it (:class:`~tests.api.test_tools_documents.Scripted`). The runner and the
deck's building are tested in ``tests/executor/test_presentations.py``; here the api's part: the master, the name and
the bounds checked before a job, the pictures and the sources found in the chat, what the job is given, the event and
what it never holds, and the answer.
"""

import asyncio
import io
import json
from pathlib import Path
from typing import Any, Final

import httpx2
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pptx import Presentation

from tests.api import test_tools_templates as template_tests
from tests.api.conftest import CALL, CHAT, HG, PG, PG_SETUP, AuditLines, put
from tests.api.mcp_calls import acall, answered, refusal, refused, result, rpc, unavailable
from tests.api.test_jobs import FakeClient
from tests.api.test_tools_documents import Scripted
from tests.api.test_tools_templates import Local, events, outputs, stored
from tests.callers import GHOSTWRITER_TOOLS
from tests.fixtures import make_fixtures as mk
from tests.fixtures import make_templates as mt
from workbench.api.audit import AuditLog
from workbench.api.jobs import JobQueue
from workbench.api.main import create_app
from workbench.api.metrics import Metrics
from workbench.api.store import Store, new_file_id
from workbench.api.tools_mcp import CallJobs
from workbench.api.tools_presentations import WritePresentationRequest, write_presentation
from workbench.config import Settings
from workbench.models import FileRecord, Principal

#: The fixtures of the template tools' tests: the real app over the in-process executor.
app = template_tests.app
fake_executor = template_tests.fake_executor
built = template_tests.built

MIB: Final = 1024 * 1024
OTHER_CHAT: Final = Principal("ghostwriter", "user-a", "chat-2")
CATEGORIES: Final = ["Invented north pier", "Invented south pier"]
#: Every text of the deck the tests write: none of it may be in an event.
SECRETS: Final = (
    "Invented harbour review",
    "Invented dredging figures",
    "Invented quay walls",
    "Invented berth count",
    "Invented north pier",
    "Invented tonnes",
    "Invented speaker note",
    "Invented picture of the basin",
    "Comparison",
    "chart.png",
    "harbour-report",
    "master.pptx",
    "harbour-deck",
)


def call(client: TestClient, arguments: dict[str, Any]) -> httpx2.Response:
    """Call ``write_presentation`` as Ghostwriter does, on the MCP door."""
    return acall(client, "write_presentation", arguments, headers=HG)


def deck(*slides: dict[str, Any], path: str | None = f"{CHAT}/master.pptx", **more: Any) -> dict[str, Any]:
    """The arguments of a deck named ``harbour-deck`` in ``master.pptx``, or in the given ``path``."""
    return {"path": path, "name": "harbour-deck", "slides": list(slides), **more}


def png_output(store: Store, tmp_path: Path, name: str = "chart.png") -> FileRecord:
    """A picture a ``run_python`` run made: an output of the chat, kept as the queue keeps one."""
    job = store.add_job(PG, "run_python")
    staged = tmp_path / "staging" / name
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(mk._png(16))
    return store.register_output(PG_SETUP, staged, name, job.id, file_id=new_file_id(), quota_bytes=10**12)


def raised(
    settings: Settings, store: Store, audit: AuditLog, executor: FakeClient, arguments: dict[str, Any]
) -> HTTPException:
    """Run the tool's function itself, which must refuse: the exception it raised."""
    body = WritePresentationRequest.model_validate(arguments)
    jobs = CallJobs(JobQueue(settings, executor, store, Metrics(settings.data_dir)), CALL)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(write_presentation(body, PG, settings, store, audit, jobs))
    return caught.value


# --- a deck ---------------------------------------------------------------------------------------------------------


def test_a_deck_is_written_and_recorded_as_a_modify(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path, audit_lines: AuditLines
) -> None:
    """One new ``.pptx`` of the chat; the job is given the master, the picture and the source file; one ``modify``.

    The event names the master, the picture, the source file and the new deck by id, and counts the slides, tables,
    charts, images and sources by kind; nothing of the deck's text, a layout's name or a file's name is in it.
    """
    master = stored(store, tmp_path, "master.pptx", mt.make_master)
    pdf = stored(store, tmp_path, "harbour-report.pdf", lambda path: mk.make_pdf(path, ["one", "two"]))
    picture = png_output(store, tmp_path)
    slides = [
        {
            "layout": 2,
            "title": "Invented harbour review",
            "content": [{"image": picture.virtual_path, "alt": "Invented picture of the basin"}],
            "notes": "Invented speaker note",
            "sources": [{"kind": "file", "path": pdf.virtual_path, "page": 2}, {"kind": "assistant"}],
        },
        {
            "layout": "Comparison",
            "title": "Invented dredging figures",
            "content": [
                "Invented quay walls",
                "Invented berth count",
                {"table": [["Pier", "Tonnes"], ["Invented north pier", 12]]},
                {"chart": {"type": "column", "categories": CATEGORIES, "series": {"Invented tonnes": [12, 7]}}},
            ],
        },
    ]
    answer = answered(call(client, deck(*slides)))
    (new,) = [rec for rec in outputs(store) if rec.name.endswith(".pptx")]
    assert new.name == "harbour-deck.pptx"
    assert {key: value for key, value in answer.items() if key != "outputs"} == {
        "path": f"{CHAT}/harbour-deck.pptx",
        "slides": 2,
        "warnings": [],
    }
    assert [output["id"] for output in answer["outputs"]] == [new.id]
    (req,) = fake_executor.requests
    assert (req.tool, req.input_files) == ("write_presentation", ["master.pptx", "chart.png", "harbour-report.pdf"])
    assert {key: value for key, value in req.args.items() if key != "slides"} == {
        "path": "master.pptx",
        "name": "harbour-deck.pptx",
        "include_sources": True,
        "budget_s": 86,
    }
    first, second = req.args["slides"]
    assert first["content"] == [
        {"image": "chart.png", "virtual": picture.virtual_path, "alt": "Invented picture of the basin"}
    ]
    assert first["sources"] == [
        {"kind": "file", "path": "harbour-report.pdf", "virtual": pdf.virtual_path, "page": 2},
        {"kind": "assistant"},
    ]
    assert second["content"][:2] == ["Invented quay walls", "Invented berth count"]
    assert second["sources"] == []
    _, data = store.open_file(PG, new.virtual_path)
    with data:
        assert len(Presentation(io.BytesIO(data.read())).slides) == 2
    (event,) = events(audit_lines, "write_presentation")
    assert (event["action"], event["outcome"]) == ("modify", "ok")
    assert event["target"]["ids"] == [master.id, picture.id, pdf.id, new.id]
    assert event["criteria"] == {
        "caller": "ghostwriter",
        "slides": 2,
        "tables": 1,
        "charts": 1,
        "images": 1,
        "sources_by_kind": {"file": 1, "job": 0, "user": 0, "assistant": 1},
    }
    raw = json.dumps(audit_lines())
    for secret in SECRETS:
        assert secret not in raw, secret


def test_a_potx_master_gives_a_pptx_and_include_sources_reaches_the_job(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path
) -> None:
    """A ``.potx`` is a master too; the deck is a ``.pptx``, and ``include_sources`` goes to the job as given."""

    def build(target: Path) -> Path:
        return mt.as_potx(mt.make_master(tmp_path / "built" / "source.pptx"), target)

    stored(store, tmp_path, "master.potx", build)
    arguments = deck({"layout": 1, "title": "Invented harbour review"}, path=f"{CHAT}/master.potx")
    answer = answered(call(client, {**arguments, "name": "harbour-deck.pptx", "include_sources": False}))
    assert answer["path"] == f"{CHAT}/harbour-deck.pptx"
    (req,) = fake_executor.requests
    assert req.args["include_sources"] is False


def test_the_files_go_to_the_job_once_and_the_event_names_each_once(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path, audit_lines: AuditLines
) -> None:
    """A picture on two slides and a source cited twice: each file is given and named once, in the order first met."""
    master = stored(store, tmp_path, "master.pptx", mt.make_master)
    picture = png_output(store, tmp_path)
    figures = stored(store, tmp_path, "quarterly-figures.xlsx", mk.make_xlsx)
    cited = {"kind": "file", "path": figures.virtual_path, "sheet": "Sheet1", "cell": "B2"}
    slide = {"layout": 2, "content": [{"image": picture.virtual_path}], "sources": [cited, {"kind": "user"}]}
    answered(call(client, deck(slide, slide)))
    (req,) = fake_executor.requests
    assert req.input_files == ["master.pptx", "chart.png", "quarterly-figures.xlsx"]
    (event,) = events(audit_lines, "write_presentation")
    (new,) = [rec for rec in outputs(store) if rec.name.endswith(".pptx")]
    assert event["target"]["ids"] == [master.id, picture.id, figures.id, new.id]
    assert event["criteria"]["images"] == 2
    assert event["criteria"]["sources_by_kind"] == {"file": 2, "job": 0, "user": 2, "assistant": 0}


def test_a_job_source_must_be_a_finished_run_python_run_of_the_chat(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path, audit_lines: AuditLines
) -> None:
    """As in ``fill_template``: another chat's run is refused, naming slide and source; the chat's own is cited."""
    stored(store, tmp_path, "master.pptx", mt.make_master)
    mine = store.add_job(PG, "run_python")
    store.finish_job(mine.id, "ok")
    theirs = store.add_job(OTHER_CHAT, "run_python")
    store.finish_job(theirs.id, "ok")
    cited = {"layout": 1, "title": "x", "sources": [{"kind": "assistant"}, {"kind": "job", "job_id": theirs.id}]}
    reason, detail = refusal(call(client, deck(cited)))
    assert reason == "refused"
    assert detail.startswith("invalid_source: slide 1, source 2: ")
    assert fake_executor.requests == []
    answered(call(client, deck({"layout": 1, "title": "x", "sources": [{"kind": "job", "job_id": mine.id}]})))
    (req,) = fake_executor.requests
    assert req.args["slides"][0]["sources"] == [{"kind": "job", "job_id": mine.id}]
    (event,) = events(audit_lines, "write_presentation")
    assert event["criteria"]["sources_by_kind"] == {"file": 0, "job": 1, "user": 0, "assistant": 0}


def test_a_deck_without_a_master_is_built_in_the_plain_one(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path, audit_lines: AuditLines
) -> None:
    """Without a path: no master is looked up or sent, the job gets the picture and the source, the event names them."""
    pdf = stored(store, tmp_path, "harbour-report.pdf", lambda path: mk.make_pdf(path, ["one", "two"]))
    picture = png_output(store, tmp_path)
    slide = {
        "layout": "Picture with Caption",
        "title": "Invented harbour review",
        "content": [{"image": picture.virtual_path}, "Invented quay walls"],
        "sources": [{"kind": "file", "path": pdf.virtual_path, "page": 1}],
    }
    arguments = {key: value for key, value in deck(slide).items() if key != "path"}
    answer = answered(call(client, arguments))
    (new,) = [rec for rec in outputs(store) if rec.name.endswith(".pptx")]
    assert (answer["path"], answer["slides"], answer["warnings"]) == (f"{CHAT}/harbour-deck.pptx", 1, [])
    (req,) = fake_executor.requests
    assert req.input_files == ["chart.png", "harbour-report.pdf"]
    assert "path" not in req.args
    _, data = store.open_file(PG, new.virtual_path)
    with data:
        built_deck = Presentation(io.BytesIO(data.read()))
    assert (built_deck.slide_width, built_deck.slide_height) == (12192000, 6858000)
    (event,) = events(audit_lines, "write_presentation")
    assert (event["action"], event["outcome"]) == ("modify", "ok")
    assert event["target"]["ids"] == [picture.id, pdf.id, new.id]
    assert event["criteria"]["images"] == 1


def test_a_null_path_is_the_plain_master(client: TestClient, fake_executor: Local) -> None:
    """``path: null`` is the same as no path."""
    answered(call(client, deck({"layout": 1, "title": "Invented harbour review"}, path=None)))
    (req,) = fake_executor.requests
    assert (req.input_files, "path" in req.args) == ([], False)


# --- refusals before a job ------------------------------------------------------------------------------------------


def _slides(count: int) -> list[dict[str, Any]]:
    return [{"layout": 2, "title": "x"} for _ in range(count)]


REFUSED: Final[dict[str, tuple[dict[str, Any], str]]] = {
    "a .docx as master": (
        deck({"layout": 1}, path=f"{CHAT}/form.docx"),
        "unsupported_template: a deck is built in a .pptx or .potx slide master; .docx is not one",
    ),
    "a name ../x": ({**deck({"layout": 1}), "name": "../x"}, "name: a plain file name without /"),
    "31 slides": ({**deck(), "slides": _slides(31)}, "invalid_value: a deck has 1 to 30 slides"),
    "a table of 31 rows": (
        deck({"layout": 2, "content": [{"table": [["h"], *[["x"]] * 30]}]}),
        "invalid_value: slide 1, block 1: a table has 1 to 30 rows with its header",
    ),
    "an image that is no file of the chat": (
        deck({"layout": 1}, {"layout": 2, "content": [{"image": f"{CHAT}/missing.png"}]}),
        f"invalid_value: slide 2, block 1: {CHAT}/missing.png is not a file of this conversation",
    ),
    "an image of another chat": (
        deck({"layout": 2, "content": [{"image": "/workspace/chat-2/elsewhere.png"}]}),
        "invalid_value: slide 1, block 1: /workspace/chat-2/elsewhere.png is not a file of this conversation",
    ),
    "a .svg image": (
        deck({"layout": 2, "content": [{"image": f"{CHAT}/drawing.svg"}]}),
        "invalid_value: slide 1, block 1: an image is a PNG, JPEG or GIF file",
    ),
    "an 11 MiB image": (
        deck({"layout": 4, "content": ["x", {"image": f"{CHAT}/large.png"}]}),
        f"invalid_value: slide 1, block 2: {CHAT}/large.png is larger than 10 MB",
    ),
    "an image used four times": (
        deck(*[{"layout": 4, "content": [{"image": f"{CHAT}/nine.png"}, {"image": f"{CHAT}/nine.png"}]}] * 2),
        "invalid_value: slide 2, block 2: the deck's images take more than 30 MB together",
    ),
    "a source file of another chat": (
        deck({"layout": 1, "sources": [{"kind": "file", "path": "/workspace/chat-2/elsewhere.pdf"}]}),
        "invalid_source: slide 1, source 1: /workspace/chat-2/elsewhere.pdf is not a file of this conversation",
    ),
}


@pytest.mark.parametrize("case", list(REFUSED))
def test_refusals_cost_no_job(
    client: TestClient,
    settings: Settings,
    store: Store,
    audit: AuditLog,
    fake_executor: Local,
    tmp_path: Path,
    audit_lines: AuditLines,
    case: str,
) -> None:
    """A 422 that names the slide and the block, or the source, where it applies; no job runs, nothing is recorded."""
    stored(store, tmp_path, "master.pptx", mt.make_master)
    put(store, "form.docx", b"PK")
    put(store, "drawing.svg", b"<svg/>")
    put(store, "large.png", b"\0" * (11 * MIB))
    put(store, "nine.png", b"\0" * (9 * MIB))
    put(store, "elsewhere.png", directory="/workspace/chat-2", p=OTHER_CHAT)
    put(store, "elsewhere.pdf", directory="/workspace/chat-2", p=OTHER_CHAT)
    arguments, wanted = REFUSED[case]
    reason, detail = refusal(call(client, arguments))
    assert reason == "refused"
    assert detail.startswith(wanted), detail
    exc = raised(settings, store, audit, fake_executor, arguments)
    assert (exc.status_code, exc.detail) == (422, detail)
    assert fake_executor.requests == []
    assert events(audit_lines, "write_presentation") == []
    assert outputs(store) == []


def test_another_users_master_is_not_found(client: TestClient, store: Store, tmp_path: Path) -> None:
    """A path outside the caller's own files is not found, as for every tool."""
    data = mt.make_master(tmp_path / "built" / "master.pptx").read_bytes()
    put(store, "master.pptx", data, p=Principal("ghostwriter", "user-b", "chat-1"))
    assert refusal(call(client, deck({"layout": 1}))) == ("refused", "not found")


@pytest.mark.parametrize(
    "arguments",
    [
        deck({"layout": 1, "colour": "blue"}),
        deck({"layout": 2, "content": [{"table": [["x"]], "chart": {}}]}),
        deck({"layout": 2, "content": [{"table": [["x"]], "alt": "x"}]}),
        deck({"layout": 2, "content": [{"image": f"{CHAT}/x.png", "caption": "x"}]}),
        deck({"layout": 2, "content": [{"chart": {"type": "pie", "categories": ["a"], "series": {}, "x": 1}}]}),
        deck({"layout": 2, "content": [{"chart": {"type": "area", "categories": ["a"], "series": {"s": [1]}}}]}),
        deck({"layout": 2, "content": [{"chart": {"type": "pie", "categories": [True], "series": {"s": [1]}}}]}),
        deck({"layout": 2, "content": [{"chart": {"type": "pie", "categories": ["a"], "series": {"s": ["1"]}}}]}),
        deck({"layout": True}),
        deck({"layout": 2, "content": [7]}),
        deck({"layout": 2}, theme="dark"),
        deck({"layout": 2, "content": [{"image": "/" + "x" * 1024 + ".png"}]}),
        {**deck({"layout": 2}), "path": "/" + "x" * 1024 + ".pptx"},
        {**deck({"layout": 2}), "path": ""},
        {"path": f"{CHAT}/master.pptx", "slides": [{"layout": 2}]},
    ],
    ids=[
        "unknown slide key",
        "a table and a chart",
        "alt on a table",
        "unknown image key",
        "unknown chart key",
        "unknown chart type",
        "a true category",
        "a value as text",
        "a true layout",
        "a number as a block",
        "unknown argument",
        "a long image path",
        "a long master path",
        "an empty master path",
        "no name",
    ],
)
def test_arguments_out_of_shape_are_invalid_arguments_and_nothing_runs(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path, arguments: dict[str, Any]
) -> None:
    """Every model forbids keys it does not know; a layout is a number or a name, never true."""
    stored(store, tmp_path, "master.pptx", mt.make_master)
    assert refused(call(client, arguments))["reason"] == "invalid_arguments"
    assert fake_executor.requests == []


def test_an_unknown_key_is_named_in_the_refusal(client: TestClient, store: Store, tmp_path: Path) -> None:
    """The model hears where the key is that no model takes."""
    stored(store, tmp_path, "master.pptx", mt.make_master)
    detail = refused(call(client, deck({"layout": 1}, {"layout": 1, "colour": "blue"})))["detail"]
    assert {
        "type": "extra_forbidden",
        "loc": ["slides", 1, "colour"],
        "msg": "Extra inputs are not permitted",
    } in detail


#: Where the second block of the first slide is; and what is no block: not text, or an object without one kind.
BLOCK: Final = ["slides", 0, "content", 1]
NOT_BLOCKS: Final[tuple[object, ...]] = (
    7,
    None,
    ["Invented"],
    {},
    {"table": [["Invented"]], "chart": {"type": "pie"}},
    {"caption": "Invented"},
)


@pytest.mark.parametrize(
    ("block", "wanted"),
    [
        (
            {"chart": {"type": "area", "categories": ["a"], "series": {"s": [1]}}},
            {
                "type": "literal_error",
                "loc": [*BLOCK, "chart", "chart", "type"],
                "msg": "Input should be 'bar', 'column', 'line' or 'pie'",
            },
        ),
        (
            {"table": [["Invented a", "Invented b"], ["Invented c", {"value": 1}]]},
            {
                "type": "invalid_cell",
                "loc": [*BLOCK, "table", "table", 1, 1],
                "msg": "a cell is a string, a number, true, false or null",
            },
        ),
        (
            {"image": f"{CHAT}/chart.png", "caption": "Invented"},
            {"type": "extra_forbidden", "loc": [*BLOCK, "image", "caption"], "msg": "Extra inputs are not permitted"},
        ),
        *[
            (
                bad,
                {
                    "type": "invalid_block",
                    "loc": BLOCK,
                    "msg": "a block is Markdown text, or an object with exactly one of table, chart or image",
                },
            )
            for bad in NOT_BLOCKS
        ],
    ],
    ids=["a chart of type area", "a bad table cell", "an unknown image key", *["not a block"] * len(NOT_BLOCKS)],
)
def test_a_bad_block_is_one_error_at_its_place(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path, block: object, wanted: dict[str, Any]
) -> None:
    """A block's kind is told by its one key, so a bad block is refused by its own model alone: one error, in place."""
    stored(store, tmp_path, "master.pptx", mt.make_master)
    body = refused(call(client, deck({"layout": 4, "content": ["Invented", block]})))
    assert body["reason"] == "invalid_arguments"
    assert body["detail"] == [wanted]
    assert "more_errors" not in body
    assert fake_executor.requests == []


def test_blocks_keep_their_kind(client: TestClient, store: Store, fake_executor: Local, tmp_path: Path) -> None:
    """A text block stays a string, a table keeps its cells' types, a chart its integer categories."""
    stored(store, tmp_path, "master.pptx", mt.make_master)
    chart = {"type": "line", "categories": [2025, 2026], "series": {"Invented tonnes": [1, None]}}
    content = ["12", "- x", {"table": [["Year", "Open"], [2026, True], [None, 1.5]]}, {"chart": chart}]
    answered(call(client, deck({"layout": 5, "content": content})))
    (req,) = fake_executor.requests
    sent = req.args["slides"][0]["content"]
    assert sent[:2] == ["12", "- x"]
    assert sent[2] == {"table": [["Year", "Open"], [2026, True], [None, 1.5]]}
    assert sent[3] == {
        "chart": {"type": "line", "categories": [2025, 2026], "series": {"Invented tonnes": [1.0, None]}}
    }
    assert all(isinstance(category, int) for category in sent[3]["chart"]["categories"])


# --- the job's answer -----------------------------------------------------------------------------------------------


def test_the_runner_refusal_is_recorded_by_its_code(
    client: TestClient, store: Store, tmp_path: Path, audit_lines: AuditLines
) -> None:
    """An unknown layout is the runner's refusal: the model hears it, the event its code alone, and no file is kept."""
    master = stored(store, tmp_path, "master.pptx", mt.make_master)
    reason, detail = refusal(call(client, deck({"layout": 99, "title": "Invented harbour review"})))
    assert reason == "refused"
    assert detail.startswith("invalid_value: slide 1: ")
    assert outputs(store) == []
    (event,) = events(audit_lines, "write_presentation")
    assert (event["action"], event["outcome"], event["target"]["ids"]) == ("modify", "error", [master.id])
    assert event["criteria"] == {"caller": "ghostwriter", "outputs": 0, "refused": "invalid_value"}


def test_a_job_that_makes_two_files_keeps_none(
    settings: Settings, store: Store, audit: AuditLog, tmp_path: Path, audit_lines: AuditLines
) -> None:
    """A deck is one file: a job that made two keeps neither, is an error event, and the tool fails with a 500."""
    master = stored(store, tmp_path, "master.pptx", mt.make_master)
    executor = Scripted(settings.data_dir)
    executor.payload = {"slides": 1, "warnings": []}
    executor.files = {"harbour-deck.pptx": b"PK", "other.pptx": b"PK"}
    queue = JobQueue(settings, executor, store, Metrics(settings.data_dir))
    with TestClient(create_app(settings, store=store, audit=audit, jobs=queue)) as scripted:
        reason, detail = refusal(call(scripted, deck({"layout": 1})))
        exc = raised(settings, store, audit, executor, deck({"layout": 1}))
    assert (reason, detail) == ("refused", "the deck could not be kept: the job made no file")
    assert (exc.status_code, exc.detail) == (500, detail)
    assert outputs(store) == []
    first, second = events(audit_lines, "write_presentation")
    for event in (first, second):
        assert (event["action"], event["outcome"], event["target"]["ids"]) == ("modify", "error", [master.id])
        assert event["criteria"]["outputs"] == 0


def test_an_answer_that_cannot_be_read_keeps_no_file(
    client: TestClient, store: Store, fake_executor: Local, tmp_path: Path, audit_lines: AuditLines
) -> None:
    """The model is told the tool failed and never learns of the file; the job is an error."""
    stored(store, tmp_path, "master.pptx", mt.make_master)
    fake_executor.files = {"harbour-deck.pptx": b"PK"}
    fake_executor.payload = {"slides": "all"}
    assert refusal(call(client, deck({"layout": 1}))) == (
        "refused",
        "the tool returned an answer that could not be read",
    )
    assert outputs(store) == []
    (event,) = events(audit_lines, "write_presentation")
    assert (event["outcome"], event["criteria"]) == ("error", {"caller": "ghostwriter", "outputs": 0})


def test_a_failed_audit_write_keeps_no_file(
    client: TestClient, store: Store, tmp_path: Path, broken_audit: None
) -> None:
    """As with an upload: a deck that could not be recorded does not stay."""
    stored(store, tmp_path, "master.pptx", mt.make_master)
    unavailable(call(client, deck({"layout": 1, "title": "Invented harbour review"})))
    assert outputs(store) == []


def test_warnings_are_cut_to_the_result_limit(
    settings: Settings, store: Store, audit: AuditLog, fake_executor: Local, tmp_path: Path
) -> None:
    """Warnings past the limit are left out from the end, and the last one says how many; the deck is kept."""
    stored(store, tmp_path, "master.pptx", mt.make_master)
    fake_executor.files = {"harbour-deck.pptx": b"PK"}
    fake_executor.payload = {"slides": 1, "warnings": [f"slide {n}: " + "w" * 200 for n in range(1, 21)]}
    with template_tests.small(settings, store, audit, fake_executor) as client:
        body = answered(call(client, deck({"layout": 1})))
    assert len(json.dumps({key: value for key, value in body.items() if key != "outputs"})) <= 1024
    assert 0 < len(body["warnings"]) < 21
    assert body["warnings"][-1].startswith("The answer is cut at the result limit (1 KB): ")
    assert [rec.name for rec in outputs(store)] == ["harbour-deck.pptx"]


# --- what the model is told -----------------------------------------------------------------------------------------


def tools_listed(client: TestClient) -> dict[str, dict[str, Any]]:
    """Ghostwriter's tools, by name, as ``tools/list`` serves them."""
    return {tool["name"]: tool for tool in result(rpc(client, "tools/list", headers=HG))["tools"]}


def test_ghostwriter_is_offered_write_presentation(client: TestClient) -> None:
    """Nine tools, in name order; the description says to agree the outline first and how a slide is filled."""
    tools = result(rpc(client, "tools/list", headers=HG))["tools"]
    assert [tool["name"] for tool in tools] == sorted(GHOSTWRITER_TOOLS)
    assert len(tools) == 10
    (tool,) = [tool for tool in tools if tool["name"] == "write_presentation"]
    description = tool["description"]
    for phrase in (
        "inspect_template",
        "show the user the outline",
        "call this tool only in a later message, after they agreed",
        "run_python",
        "read_document",
        "speaker notes",
        "30 slides",
        "card in the chat",
        "title and subtitle are plain text, not Markdown",
        '"" leaves a place empty',
        "Without path",
        "plain 16:9 master",
    ):
        assert phrase in description, phrase
    schema = tool["inputSchema"]
    assert schema["required"] == ["name", "slides"]
    assert schema["properties"]["path"]["type"] == "string"
    assert schema["additionalProperties"] is False
    for model, body in [("WritePresentationRequest", schema), *schema["$defs"].items()]:
        for field, spec in body["properties"].items():
            assert "type" in spec, (model, field)
            assert spec.get("description", "").strip(), (model, field)


def test_the_schema_stays_small(client: TestClient) -> None:
    """Ghostwriter sends every tool's schema on every round: the input schema and the description keep to a budget."""
    tool = tools_listed(client)["write_presentation"]
    assert len(json.dumps(tool["inputSchema"])) <= 5500
    assert len(tool["description"]) <= 1200


def test_inspect_templates_title_names_the_slide_masters(client: TestClient) -> None:
    """The title is what the model reads first."""
    assert tools_listed(client)["inspect_template"]["title"] == (
        "List the fields of a template or the layouts of a slide master"
    )
