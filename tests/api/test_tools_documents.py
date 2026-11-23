"""The ``read_document`` and ``search_files`` tools on the MCP door, as Ghostwriter calls them.

The executor is scripted. Its own work is tested in ``tests/executor``; here the executor answers what a test tells
it to, and the api's part is checked: which files go to the job, what is recorded, what the model is
told, what is capped, and that no error carries more than a short fixed text.
"""

import asyncio
import dataclasses
import io
import itertools
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, override

import httpx2
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image
from pptx import Presentation

from tests.api.conftest import CHAT, FORMER, HG, HG_B, HG_TEXT, PG, PG_SETUP, AuditLines, put, tick, unrecorded
from tests.api.mcp_calls import Headers, acall, answered, refusal, refused, result, rpc, sent_size, unavailable
from tests.api.test_files_renders import artifact, deck
from tests.api.test_jobs import FakeClient
from tests.api.test_renders import capped
from tests.api.test_tools_templates import Local
from tests.api.test_tools_views import CLIP_BYTES, file_of, images, pdf, photo, raw, seen, whole_renders
from tests.callers import TEXT_ONLY_PAGE_HINT
from tests.conftest import FakeOffice
from tests.fixtures import make_fixtures as mk
from tests.fixtures import make_images as mi
from tests.fixtures import make_media
from workbench.api import tools_views
from workbench.api.audit import AuditLog
from workbench.api.jobs import Busy, CallLimits, JobQueue
from workbench.api.main import create_app
from workbench.api.metrics import Metrics
from workbench.api.renders import NOT_KEPT
from workbench.api.store import QuotaExceeded, RendersFull, StorageFull, Store
from workbench.api.tools import ReadDocumentRequest
from workbench.api.tools_mcp import CallJobs
from workbench.config import Settings
from workbench.models import FileRecord, Principal
from workbench.protocol import JobRequest, JobResult
from workbench.renders import NOTHING_FOUND

Payload = dict[str, Any] | Callable[[JobRequest], dict[str, Any]]


class Scripted(FakeClient):
    """An executor that answers every job with the result a test chose.

    Attributes:
        payload: The runner's answer, or a function of the request that makes it.
    """

    def __init__(self, data_dir: Path) -> None:
        """Answer ``{}`` until told otherwise."""
        super().__init__(data_dir)
        self.payload: Payload = {}

    @override
    async def run(self, req: JobRequest, timeout_s: float) -> JobResult:
        """Run the fake, then put the chosen answer in its result."""
        base = await super().run(req, timeout_s)
        chosen = self.payload(req) if callable(self.payload) else self.payload
        return base.model_copy(update={"result": chosen})


@pytest.fixture
def executor(settings: Settings) -> Scripted:
    """The scripted executor."""
    return Scripted(settings.data_dir)


@pytest.fixture
def app(settings: Settings, store: Store, audit: AuditLog, executor: Scripted) -> FastAPI:
    """The real app with a job queue over the scripted executor."""
    return create_app(
        settings, store=store, audit=audit, jobs=JobQueue(settings, executor, store, Metrics(settings.data_dir))
    )


def reading(path: str, *parts: tuple[str, str], **extra: Any) -> dict[str, Any]:
    """A read runner's answer for ``path`` with ``parts``."""
    return {
        "path": path,
        "kind": "docx",
        "parts": [{"label": label, "text": text} for label, text in parts],
        "total_parts": len(parts),
        "pages_without_text": [],
        "truncated": False,
        **extra,
    }


def read(
    client: TestClient, path: str, part: str | None = None, headers: Headers = HG, strips: int | None = None
) -> httpx2.Response:
    """Call ``read_document`` as Ghostwriter does, on the MCP door."""
    arguments: dict[str, Any] = {"path": path}
    if part is not None:
        arguments["part"] = part
    if strips is not None:
        arguments["strips"] = strips
    return acall(client, "read_document", arguments, headers=headers)


def find(client: TestClient, query: str, headers: Headers = HG, **more: Any) -> httpx2.Response:
    """Call ``search_files`` as Ghostwriter does, on the MCP door."""
    return acall(client, "search_files", {"query": query, **more}, headers=headers)


# --- read_document: what goes to the job -----------------------------------------------------------------------


def test_read_document_sends_only_the_requested_file(client: TestClient, store: Store, executor: Scripted) -> None:
    """One input file, the path relative to its chat folder, and the answer as the runner gave it.

    The runner gets 3/4 of the job's wall less the five seconds the previews keep: (120 - 5) * 3 // 4.
    """
    put(store, "review.docx", b"x", p=PG)
    put(store, "other.docx", b"y", p=PG)
    executor.payload = reading("ignored", ("paragraph 1", "Hello"))
    answer = answered(read(client, f"{CHAT}/review.docx", "paragraph 1"))
    (req,) = executor.requests
    assert req.tool == "read_document"
    assert req.input_files == ["review.docx"]
    assert req.args == {"path": "review.docx", "part": "paragraph 1", "budget_s": 86, "images": True}
    assert (req.caller, req.user_id, req.chat_id) == ("ghostwriter", "user-a", "chat-1")
    assert req.max_chars == 16 * 1024
    assert answer == {
        "path": f"{CHAT}/review.docx",
        "kind": "docx",
        "parts": [{"label": "paragraph 1", "text": "Hello"}],
        "total_parts": 1,
        "pages_without_text": [],
        "shown": [],
        "truncated": False,
        "next_part": None,
        "hint": None,
        "outputs": [],
    }


def test_the_path_in_the_answer_is_the_stores_not_the_jobs(
    client: TestClient, store: Store, executor: Scripted
) -> None:
    """A job that names another path does not get the model to see it."""
    put(store, "review.docx", b"x", p=PG)
    executor.payload = reading("/data/jobs/0123/workspace/chat-1/review.docx", ("paragraph 1", "Hello"))
    assert answered(read(client, f"{CHAT}/review.docx"))["path"] == f"{CHAT}/review.docx"


def test_no_part_is_sent_as_none(client: TestClient, store: Store, executor: Scripted) -> None:
    """Without ``part``, or with ``null``, the runner is told there is none."""
    put(store, "a.txt", p=PG)
    executor.payload = reading("x", ("lines 1-1", "a"))
    answered(read(client, f"{CHAT}/a.txt"))
    answered(acall(client, "read_document", {"path": f"{CHAT}/a.txt", "part": None}))
    assert [r.args for r in executor.requests] == [{"path": "a.txt", "part": None, "budget_s": 86, "images": True}] * 2


def test_a_json_file_is_read_like_any_document(client: TestClient, store: Store, executor: Scripted) -> None:
    """The file goes to the job as it is; its kind and the indented lines come back as the runner gave them."""
    put(store, "orders.json", b'{"order":"A-0001","qty":2}', p=PG)
    executor.payload = reading("ignored", ("lines 1-4", '{\n  "order": "A-0001",\n  "qty": 2\n}'), kind="json")
    answer = answered(read(client, f"{CHAT}/orders.json"))
    (req,) = executor.requests
    assert req.input_files == ["orders.json"]
    assert req.args == {"path": "orders.json", "part": None, "budget_s": 86, "images": True}
    assert answer["kind"] == "json"
    assert answer["parts"] == [{"label": "lines 1-4", "text": '{\n  "order": "A-0001",\n  "qty": 2\n}'}]


def test_a_file_in_a_folder_runs_in_its_chat_folder_and_another_chats_is_not_found(
    client: TestClient, store: Store, executor: Scripted, audit_lines: AuditLines
) -> None:
    """The job's chat and input are the file's, a folder below the chat included; another chat's file is not found.

    Ghostwriter is held to its chat: the user's file in another chat runs no job and is recorded nowhere.
    """
    store.mkdir(PG, f"{CHAT}/reports")
    put(store, "q1.txt", directory=f"{CHAT}/reports", p=PG_SETUP)
    put(store, "x.txt", directory="/workspace/chat-2", p=Principal("ghostwriter", "user-a", "chat-2"))
    executor.payload = reading("x", ("lines 1-1", "a"))
    answered(read(client, f"{CHAT}/reports/q1.txt"))
    assert refusal(read(client, "/workspace/chat-2/x.txt")) == ("refused", "not found")
    (first,) = executor.requests
    assert (first.chat_id, first.input_files, first.args["path"]) == ("chat-1", ["reports/q1.txt"], "reports/q1.txt")
    containers = [e["target"]["container"] for e in audit_lines() if e["operation"] == "tool.read_document"]
    assert containers == ["chat:chat-1"]


def test_a_read_records_query_with_file_ids(
    client: TestClient, store: Store, executor: Scripted, audit_lines: AuditLines
) -> None:
    """One ``query`` event with the file's id; the file's name, the part and the text are in no line."""
    rec = put(store, "salary-review.docx", b"x", p=PG)
    executor.payload = reading("x", ("paragraph 1", "synthetic body text"))
    answered(read(client, f"{CHAT}/salary-review.docx", "paragraph 4"))
    (event,) = [e for e in audit_lines() if e["operation"] == "tool.read_document"]
    assert event["action"] == "query"
    assert event["outcome"] == "ok"
    assert event["actor"] == {"user": "user-a", "via": "service"}
    assert event["target"]["ids"] == [rec.id]
    assert event["target"]["count"] == 1
    assert event["target"]["container"] == "chat:chat-1"
    assert event["criteria"] == {"caller": "ghostwriter", "paged": True, "images": 0}
    raw = json.dumps(audit_lines())
    for secret in ("salary-review", "paragraph 4", "synthetic body text", "docx"):
        assert secret not in raw


def test_a_read_refuses_other_users_files(
    client: TestClient, store: Store, executor: Scripted, audit_lines: AuditLines
) -> None:
    """Another user's file, a missing one and a path outside the chat are all not found, and no job runs."""
    put(store, "private.docx", b"x", p=PG)
    for path, headers in (
        (f"{CHAT}/private.docx", HG_B),
        (f"{CHAT}/missing.docx", HG),
        ("/etc/passwd", HG),
        (f"{CHAT}/../../user-b/chat-1/x.docx", HG),
    ):
        assert refusal(read(client, path, headers=headers)) == ("refused", "not found"), path
    assert executor.requests == []
    assert [e for e in audit_lines() if e["operation"].startswith("tool.")] == []


def test_a_folder_is_not_a_document(client: TestClient, store: Store, executor: Scripted) -> None:
    """The workspace and a chat folder are paths, not files."""
    put(store, "a.txt", p=PG)
    assert refused(read(client, "/workspace"))["reason"] == "refused"
    assert refused(read(client, CHAT))["reason"] == "refused"
    assert executor.requests == []


# --- read_document: what the model is told ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "text"),
    [
        ("unsupported_format", ".msg cannot be read here; save it as .eml or PDF"),
        ("unreadable_file", "the PDF is password-protected (encrypted)"),
        ("no_such_part", "this document has 2 pages"),
        ("read_timeout", "reading the file took longer than 90 s; it is too large or too complex"),
        ("file_not_found", "is not in this conversation's workspace"),
        ("storage_full", "storage is full"),
        ("hidden_memory", "memory outside any job's count"),
    ],
)
def test_the_runners_failures_are_answered_with_their_code_and_message(
    client: TestClient, store: Store, executor: Scripted, code: str, text: str
) -> None:
    """The model sees ``code: message``: the code says whose fault it is."""
    put(store, "a.pdf", p=PG)
    executor.status = "error"
    executor.payload = {"error": f"{code}: {text}"}
    assert refusal(read(client, f"{CHAT}/a.pdf")) == ("refused", f"{code}: {text}")


def test_msg_is_answered_with_the_hint(client: TestClient, store: Store, executor: Scripted) -> None:
    """Through the real runner's wording: the model is told to ask for .eml or PDF."""
    put(store, "mail.msg", p=PG)
    executor.status = "error"
    executor.payload = {"error": "unsupported_format: .msg cannot be read here; save it as .eml or PDF"}
    assert ".eml or PDF" in refusal(read(client, f"{CHAT}/mail.msg"))[1]


def test_a_failure_the_api_does_not_know_is_not_echoed(
    client: TestClient, store: Store, executor: Scripted, caplog: pytest.LogCaptureFixture
) -> None:
    """An unexpected exception text may hold a path of the job; the model gets a fixed text, and nothing is logged."""
    put(store, "a.docx", p=PG)
    executor.status = "error"
    executor.payload = {"error": "KeyError: /data/jobs/abc/workspace/chat-1/a.docx"}
    caplog.set_level("DEBUG")
    r = read(client, f"{CHAT}/a.docx")
    assert refusal(r) == ("refused", "the tool failed")
    assert "/data/jobs" not in r.text
    assert "/data/jobs" not in caplog.text


@pytest.mark.parametrize(
    ("status", "message"),
    [("timeout", "the job ran past its wall time of 120 s"), ("killed", "the job was stopped on a resource limit")],
)
def test_a_job_that_ran_out_of_time_or_memory_is_refused_saying_how(
    client: TestClient, store: Store, executor: Scripted, status: str, message: str
) -> None:
    """The file could not be processed within the limits; the executor's own short message says how."""
    put(store, "huge.xlsx", p=PG)
    executor.status = status
    executor.message = message
    assert refusal(read(client, f"{CHAT}/huge.xlsx")) == ("refused", f"the file could not be processed: {message}")


def test_a_busy_executor_is_executor_busy(client: TestClient, store: Store, executor: Scripted) -> None:
    """The same short text as every other refusal of the queue, for both tools."""
    put(store, "a.docx", p=PG)
    executor.status = "busy"
    assert refusal(read(client, f"{CHAT}/a.docx")) == ("executor_busy", "busy, retry later")
    assert refusal(find(client, "x")) == ("executor_busy", "busy, retry later")


@pytest.mark.parametrize("answer", [{}, {"parts": "no"}, {"path": "x", "kind": "docx", "parts": [{"label": 1}]}, []])
def test_an_unreadable_job_answer_is_a_fixed_error(
    client: TestClient, store: Store, executor: Scripted, answer: Any
) -> None:
    """The api does not forward what it cannot validate."""
    put(store, "a.docx", p=PG)
    executor.payload = answer
    assert refusal(read(client, f"{CHAT}/a.docx")) == ("refused", "the tool returned an answer that could not be read")


def test_pages_without_text_and_the_runners_hint_pass_through(
    client: TestClient, store: Store, executor: Scripted
) -> None:
    """The runner's hint is the model's; the slides are listed, and never in ``parts``.

    Read without images: with them, a deck's slide without text is converted and drawn, which no scripted job does
    (``test_a_picture_only_slide_comes_back_as_an_image``).
    """
    put(store, "slides.pptx", p=PG)
    executor.payload = reading(
        "x", ("slide 1", "text"), kind="pptx", pages_without_text=[2], hint="slide 2 hold no text (pictures only?)."
    )
    body = answered(read(client, f"{CHAT}/slides.pptx", headers=HG_TEXT))
    assert body["pages_without_text"] == [2]
    assert [p["label"] for p in body["parts"]] == ["slide 1"]
    assert body["shown"] == []
    assert body["hint"] == "slide 2 hold no text (pictures only?)."


def test_a_cut_answer_says_how_to_go_on(client: TestClient, store: Store, executor: Scripted) -> None:
    """``truncated`` with ``next_part`` adds the sentence that tells the model what to call next."""
    put(store, "long.pdf", p=PG)
    executor.payload = reading(
        "x", ("page 1", "a"), kind="pdf", truncated=True, next_part="page 2", hint="Tell the user."
    )
    body = answered(read(client, f"{CHAT}/long.pdf"))
    assert body["truncated"] is True
    assert body["next_part"] == "page 2"
    assert body["hint"].startswith("Tell the user. ")
    assert 'part="page 2"' in body["hint"]
    assert "16 KB" in body["hint"]


# --- read_document: the cap ------------------------------------------------------------------------------------


@pytest.fixture
def small(settings: Settings, store: Store, audit: AuditLog, executor: Scripted) -> TestClient:
    """A client on an app whose answers may be 1 KB."""
    limited = dataclasses.replace(settings, result_max_kb=1)
    queue = JobQueue(limited, executor, store, Metrics(settings.data_dir))
    return TestClient(create_app(limited, store=store, audit=audit, jobs=queue))


def test_a_long_answer_is_cut_at_the_limit_with_whole_parts(
    small: TestClient, store: Store, executor: Scripted
) -> None:
    """Parts are dropped from the end, the first dropped one is ``next_part``, and the answer fits."""
    put(store, "long.docx", p=PG)
    parts = [(f"paragraph {n}", f"Paragraph {n}: " + "text " * 40) for n in range(1, 9)]
    executor.payload = reading("x", *parts)
    with small:
        body = answered(read(small, f"{CHAT}/long.docx"))
    assert sent_size(body) <= 1024
    assert body["truncated"] is True
    kept = len(body["parts"])
    assert 1 <= kept < 8
    assert [p["label"] for p in body["parts"]] == [f"paragraph {n}" for n in range(1, kept + 1)]
    assert body["next_part"] == f"paragraph {kept + 1}"
    assert f'part="paragraph {kept + 1}"' in body["hint"]
    assert body["total_parts"] == 8


def test_the_first_part_alone_over_the_limit_is_cut_and_marked(
    small: TestClient, store: Store, executor: Scripted
) -> None:
    """Paging must move on, so a part that cannot fit is cut rather than refused; the next part is next."""
    put(store, "long.docx", p=PG)
    executor.payload = reading("x", ("paragraph 1", "word " * 1000), ("paragraph 2", "short"))
    with small:
        body = answered(read(small, f"{CHAT}/long.docx"))
    assert sent_size(body) <= 1024
    assert [p["label"] for p in body["parts"]] == ["paragraph 1"]
    assert body["parts"][0]["text"].endswith(" [cut]")
    assert body["next_part"] == "paragraph 2"
    assert "paragraph 1 is longer than the result limit" in body["hint"]


def test_a_cut_first_part_leaves_room_for_the_label_of_the_next_part(
    small: TestClient, store: Store, executor: Scripted
) -> None:
    """The next part's label may be longer than the first's; the answer still fits."""
    put(store, "long.docx", p=PG)
    longer = "a much longer label of the next part " * 3
    executor.payload = reading("x", ("p", "word " * 1000), (longer, "short"))
    with small:
        body = answered(read(small, f"{CHAT}/long.docx"))
    assert sent_size(body) <= 1024
    assert body["next_part"] == longer


def test_a_hint_too_long_for_the_limit_is_shortened_as_the_last_resort(
    small: TestClient, store: Store, executor: Scripted
) -> None:
    """Nothing the job says can make the answer pass the limit."""
    put(store, "a.docx", p=PG)
    executor.payload = reading("x", ("paragraph 1", "short"), ("paragraph 2", "x" * 3000), hint="long hint " * 200)
    with small:
        body = answered(read(small, f"{CHAT}/a.docx"))
    assert sent_size(body) <= 1024
    assert body["truncated"] is True


@pytest.mark.parametrize("text", ["plain " * 300, "Üäö " * 300, "☃ " * 300, '"\\\n' * 300, "\x01" * 300])
def test_the_cap_counts_bytes_of_the_json_not_characters(
    small: TestClient, store: Store, executor: Scripted, text: str
) -> None:
    """Umlauts take two bytes, a snowman three, a control character six: the limit is on what is sent."""
    put(store, "a.docx", p=PG)
    executor.payload = reading("x", ("paragraph 1", text), ("paragraph 2", text))
    with small:
        body = answered(read(small, f"{CHAT}/a.docx"))
    assert sent_size(body) <= 1024
    assert body["truncated"] is True


def test_the_cap_holds_at_every_size(small: TestClient, store: Store, executor: Scripted) -> None:
    """From one short part to far over the limit: within 1 KB, cut only when it must be."""
    put(store, "a.docx", p=PG)
    with small:
        cut_seen = False
        for count in range(1, 15):
            executor.payload = reading("x", *[(f"paragraph {n}", "x" * 100) for n in range(1, count + 1)])
            body = answered(read(small, f"{CHAT}/a.docx"))
            assert sent_size(body) <= 1024, count
            assert body["truncated"] is (len(body["parts"]) < count), count
            cut_seen = cut_seen or body["truncated"]
        assert cut_seen


# --- search_files ----------------------------------------------------------------------------------------------


def hits(*items: tuple[str, str, str]) -> dict[str, Any]:
    """A search runner's answer."""
    return {
        "hits": [{"path": p, "location": loc, "snippet": snip} for p, loc, snip in items],
        "truncated": False,
        "files_searched": len(items),
        "unsupported_files": 0,
    }


def test_search_files_sends_every_file_of_the_chat_newest_first(
    client: TestClient, store: Store, executor: Scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All of this chat's files, in folders too, and nothing of another chat, user or caller."""
    tick(store, monkeypatch)
    put(store, "old.txt", p=PG)
    store.mkdir(PG, f"{CHAT}/reports")
    put(store, "q1.txt", directory=f"{CHAT}/reports", p=PG_SETUP)
    put(store, "new.docx", p=PG)
    put(store, "elsewhere.txt", directory="/workspace/chat-2", p=Principal("ghostwriter", "user-a", "chat-2"))
    store.add_upload(
        Principal("ghostwriter", "user-b", "chat-1"),
        CHAT,
        "theirs.txt",
        io.BytesIO(b"x"),
        max_bytes=10,
        quota_bytes=100,
        record=unrecorded,
    )
    put(store, "former-callers.txt", p=FORMER)
    executor.payload = hits((f"{CHAT}/new.docx", "paragraph 1", "match here"))
    answer = answered(find(client, "match", regex=True, max_hits=5))
    (req,) = executor.requests
    assert req.tool == "search_files"
    assert req.input_files == ["new.docx", "reports/q1.txt", "old.txt"]
    assert (req.args["query"], req.args["regex"], req.args["max_hits"]) == ("match", True, 5)
    assert req.args["budget_s"] == 86
    assert (req.caller, req.chat_id, req.user_id) == ("ghostwriter", "chat-1", "user-a")
    assert answer == {
        "hits": [{"path": f"{CHAT}/new.docx", "location": "paragraph 1", "snippet": "match here"}],
        "truncated": False,
        "files_searched": 1,
        "unsupported_files": 0,
        "skipped": None,
        "hint": None,
        "outputs": [],
    }


def test_search_defaults_and_the_hit_cap(client: TestClient, store: Store, executor: Scripted) -> None:
    """No regex, 50 hits; whatever is asked beyond 200 or below 1 is brought into range."""
    put(store, "a.txt", p=PG)
    executor.payload = hits()
    answered(find(client, "x"))
    answered(find(client, "x", max_hits=5000))
    answered(find(client, "x", max_hits=0))
    assert [(r.args["regex"], r.args["max_hits"]) for r in executor.requests] == [(False, 50), (False, 200), (False, 1)]


@pytest.mark.parametrize(
    "arguments",
    [{}, {"query": ""}, {"query": "x" * 1001}, {"query": "x", "regex": "maybe"}, {"query": "x", "max_hits": "many"}],
)
def test_search_arguments_that_are_not_valid_are_invalid_arguments(
    client: TestClient, store: Store, executor: Scripted, arguments: dict[str, Any]
) -> None:
    """The model's mistakes are told at once, and nothing runs."""
    put(store, "a.txt", p=PG)
    assert refused(acall(client, "search_files", arguments))["reason"] == "invalid_arguments"
    assert executor.requests == []


def test_search_records_every_file_id_and_never_the_query(
    client: TestClient, store: Store, executor: Scripted, audit_lines: AuditLines
) -> None:
    """One ``query`` event with the ids of the files looked in; the query, which may be a name, is in no line."""
    a = put(store, "a.txt", p=PG)
    b = put(store, "b.docx", p=PG)
    executor.payload = hits((f"{CHAT}/a.txt", "line 1", "Mustermann was here"))
    answered(find(client, "Mustermann", regex=False))
    (event,) = [e for e in audit_lines() if e["operation"] == "tool.search_files"]
    assert event["action"] == "query"
    assert sorted(event["target"]["ids"]) == sorted([a.id, b.id])
    assert event["target"]["count"] == 2
    assert event["target"]["container"] == "chat:chat-1"
    assert event["criteria"] == {"caller": "ghostwriter", "regex": False, "max_hits": 50}
    assert "Mustermann" not in json.dumps(audit_lines())


def test_search_in_an_empty_chat_runs_no_job(client: TestClient, executor: Scripted, audit_lines: AuditLines) -> None:
    """Nothing to look in is an answer, not a job; and nothing is recorded."""
    body = answered(find(client, "x"))
    assert body["hits"] == []
    assert body["files_searched"] == 0
    assert "no files" in body["hint"]
    assert executor.requests == []
    assert [e for e in audit_lines() if e["operation"] == "tool.search_files"] == []


def test_search_never_shows_a_path_the_job_was_not_given(client: TestClient, store: Store, executor: Scripted) -> None:
    """A hit on a path outside the files sent is dropped."""
    put(store, "a.txt", p=PG)
    executor.payload = hits(
        (f"{CHAT}/a.txt", "line 1", "real"),
        ("/data/jobs/abc/workspace/chat-1/a.txt", "line 1", "forged"),
        (f"{CHAT}/not-sent.txt", "line 1", "forged"),
    )
    assert [h["snippet"] for h in answered(find(client, "x"))["hits"]] == ["real"]


def test_search_over_1000_files_looks_at_the_newest_1000_and_says_so(
    client: TestClient, store: Store, executor: Scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job protocol takes 1000 inputs; the oldest files are left out, and the answer says how many."""
    clock = itertools.count(1_000_000)
    monkeypatch.setattr(store, "_clock", lambda: float(next(clock)))
    for n in range(1005):
        put(store, f"f{n:04d}.txt", p=PG)
    executor.payload = hits()
    body = answered(find(client, "x"))
    (req,) = executor.requests
    assert len(req.input_files) == 1000
    assert req.input_files[0] == "f1004.txt"
    assert req.input_files[-1] == "f0005.txt"
    assert body["truncated"] is True
    assert "1000 newest of 1005 files" in body["hint"]


def test_a_search_over_450_files_names_every_one_in_three_events(
    client: TestClient, store: Store, executor: Scripted, audit_lines: AuditLines
) -> None:
    """An event holds 200 ids: the search's event names the first 200 and counts 450, two continuations the rest."""
    stored = {put(store, f"f{n:03d}.txt", p=PG).id for n in range(450)}
    executor.payload = hits()
    answered(find(client, "x"))
    primary, *more = [e for e in audit_lines() if e["operation"].startswith("tool.search_files")]
    assert (primary["operation"], primary["target"]["count"], len(primary["target"]["ids"])) == (
        "tool.search_files",
        450,
        200,
    )
    assert [(e["operation"], e["parent_event_id"], e["target"]["count"]) for e in more] == [
        ("tool.search_files.ids", primary["event_id"], 200),
        ("tool.search_files.ids", primary["event_id"], 50),
    ]
    named = [i for e in [primary, *more] for i in e["target"]["ids"]]
    assert sorted(named) == sorted(stored)
    assert {e["target"]["container"] for e in [primary, *more]} == {"chat:chat-1"}
    assert "ids_truncated" not in json.dumps(audit_lines())


def test_search_answer_is_cut_at_the_limit_and_says_so(small: TestClient, store: Store, executor: Scripted) -> None:
    """Hits from the end are dropped until it fits, with a sentence about it."""
    put(store, "a.txt", p=PG)
    executor.payload = hits(*[(f"{CHAT}/a.txt", f"line {n}", "snippet " * 20) for n in range(1, 31)])
    with small:
        body = answered(find(small, "snippet"))
    assert sent_size(body) <= 1024
    assert body["truncated"] is True
    assert 1 <= len(body["hits"]) < 30
    assert [h["location"] for h in body["hits"]] == [f"line {n}" for n in range(1, len(body["hits"]) + 1)]
    assert f"{30 - len(body['hits'])} further hits" in body["hint"]


def test_the_runners_search_hints_and_skipped_files_pass_through(
    client: TestClient, store: Store, executor: Scripted
) -> None:
    """What could not be searched is told to the model as the runner found it."""
    put(store, "scan.pdf", p=PG)
    executor.payload = {
        **hits(),
        "truncated": True,
        "skipped": [{"path": f"{CHAT}/scan.pdf", "reason": "no text on pages 2, 3"}],
        "hint": "The search stopped after 60 s; 4 files were not searched.",
    }
    body = answered(find(client, "x"))
    assert body["skipped"] == [{"path": f"{CHAT}/scan.pdf", "reason": "no text on pages 2, 3"}]
    assert body["hint"] == "The search stopped after 60 s; 4 files were not searched."
    assert body["truncated"] is True


def test_a_regex_that_spent_its_budget_answers_with_its_hits_and_the_note(
    client: TestClient, store: Store, executor: Scripted
) -> None:
    """The runner's partial answer is a success: the hits found before the budget ran out reach the model."""
    put(store, "a.txt", p=PG)
    note = "regex budget exhausted; use a simpler expression, or a plain text query."
    executor.payload = {**hits((f"{CHAT}/a.txt", "line 1", "aaaa")), "truncated": True, "hint": note}
    body = answered(find(client, "(a+)+$", regex=True))
    assert [(h["location"], h["snippet"]) for h in body["hits"]] == [("line 1", "aaaa")]
    assert body["truncated"] is True
    assert body["hint"] == note


def test_search_returns_no_more_hits_than_asked_for(client: TestClient, store: Store, executor: Scripted) -> None:
    """A job that gives more is held to ``max_hits``."""
    put(store, "a.txt", p=PG)
    executor.payload = hits(*[(f"{CHAT}/a.txt", f"line {n}", "x") for n in range(1, 11)])
    assert len(answered(find(client, "x", max_hits=3))["hits"]) == 3


@pytest.mark.parametrize(("wall", "budget"), [(120, 86), (40, 26), (5, 1)])
def test_the_time_budget_of_a_search_is_three_quarters_of_the_jobs(
    settings: Settings, store: Store, audit: AuditLog, executor: Scripted, wall: int, budget: int
) -> None:
    """The job is given time to answer before the executor stops it, less the previews' 5 s; never under a second."""
    put(store, "a.txt", p=PG)
    executor.payload = hits()
    changed = dataclasses.replace(settings, job_wall_s=wall)
    queue = JobQueue(changed, executor, store, Metrics(settings.data_dir))
    with TestClient(create_app(changed, store=store, audit=audit, jobs=queue)) as client:
        answered(find(client, "x"))
    assert executor.requests[-1].args["budget_s"] == budget


@pytest.mark.parametrize("code", ["bad_regex", "regex_timeout"])
def test_a_bad_or_slow_regex_is_refused_with_the_reason(
    client: TestClient, store: Store, executor: Scripted, code: str
) -> None:
    """The model can fix its expression."""
    put(store, "a.txt", p=PG)
    executor.status = "error"
    executor.payload = {"error": f"{code}: the reason"}
    assert refusal(find(client, "(", regex=True)) == ("refused", f"{code}: the reason")


# --- the tools in the list -------------------------------------------------------------------------------------


def listed(client: TestClient) -> dict[str, dict[str, Any]]:
    """Ghostwriter's tools as ``tools/list`` names them, by name."""
    return {tool["name"]: tool for tool in result(rpc(client, "tools/list", headers=HG))["tools"]}


def test_the_tools_are_listed_with_typed_parameters(client: TestClient) -> None:
    """Both are listed with a description for the model; every field of their arguments has an explicit type."""
    tools = listed(client)
    for name in ("read_document", "search_files"):
        tool = tools[name]
        assert tool["description"].strip()
        assert "Args:" not in tool["description"]
        schema = tool["inputSchema"]
        assert schema["properties"]
        for field, spec in schema["properties"].items():
            assert "type" in spec, (name, field)
            assert spec["description"].strip(), (name, field)
    read_schema = tools["read_document"]["inputSchema"]
    assert read_schema["required"] == ["path"]
    assert read_schema["properties"]["part"]["type"] == "string"
    assert tools["search_files"]["inputSchema"]["required"] == ["query"]


def test_the_model_is_told_how_json_is_read_and_how_a_data_file_becomes_a_table(client: TestClient) -> None:
    """``read_document`` names JSON among what it reads; ``run_python`` keeps a data file's rows out of write_table."""
    tools = listed(client)
    reading_text = tools["read_document"]["description"]
    python_text = tools["run_python"]["description"]
    assert "HTML, JSON (shown indented), JSONL, NDJSON, TXT and MD" in reading_text
    assert "load it in code (json, pandas.json_normalize)" in python_text
    assert "do not copy its rows into write_table" in python_text


# --- refusals are recorded (R72) ---------------------------------------------------------------------------------

#: A refusal that quotes the file, as the sheet reader's does: the names of a workbook's sheets.
SHEET_REFUSAL = "no_such_part: this workbook has no such sheet; its sheets are: 'salaries-of-user-b', 'Summary'"


def tool_events(audit_lines: AuditLines, tool: str) -> list[dict[str, Any]]:
    """The audit events of one tool."""
    return [e for e in audit_lines() if e["operation"] == f"tool.{tool}"]


def test_a_read_refusal_is_recorded_as_an_error_by_its_code_alone(
    client: TestClient, store: Store, executor: Scripted, audit_lines: AuditLines
) -> None:
    """The model gets the refusal, sheet names and all; the trail gets one ``error`` event with the code, no text."""
    rec = put(store, "quarterly-figures.xlsx", p=PG)
    executor.status = "error"
    executor.payload = {"error": SHEET_REFUSAL}
    assert refusal(read(client, f"{CHAT}/quarterly-figures.xlsx", "Nope!A1:B2")) == ("refused", SHEET_REFUSAL)
    (event,) = tool_events(audit_lines, "read_document")
    assert (event["action"], event["outcome"], event["target"]["ids"]) == ("query", "error", [rec.id])
    assert event["criteria"] == {"caller": "ghostwriter", "paged": True, "refused": "no_such_part"}
    assert "salaries" not in json.dumps(audit_lines())


def test_a_search_refusal_is_recorded_as_an_error_by_its_code_alone(
    client: TestClient, store: Store, executor: Scripted, audit_lines: AuditLines
) -> None:
    """A search's refusal is recorded like a read's, by the ids of every file given to it."""
    recs = [put(store, "quarterly-figures.xlsx", p=PG), put(store, "harbour-report.pdf", p=PG)]
    executor.status = "error"
    executor.payload = {"error": "bad_regex: missing ), unterminated subpattern at position 0 in 'salaries-of-(user-b'"}
    assert refused(find(client, "salaries-of-(user-b", regex=True))["reason"] == "refused"
    (event,) = tool_events(audit_lines, "search_files")
    assert (event["outcome"], sorted(event["target"]["ids"])) == ("error", sorted(rec.id for rec in recs))
    assert event["criteria"] == {"caller": "ghostwriter", "regex": True, "max_hits": 50, "refused": "bad_regex"}
    assert "salaries" not in json.dumps(audit_lines())


@pytest.mark.parametrize("tool", ["read_document", "search_files"])
def test_a_refusal_whose_record_cannot_be_written_never_reaches_the_model(
    client: TestClient, store: Store, executor: Scripted, broken_audit: None, tool: str
) -> None:
    """The audit failure is the answer, never the refusal's text."""
    put(store, "quarterly-figures.xlsx", p=PG)
    executor.status = "error"
    executor.payload = {"error": SHEET_REFUSAL}
    r = read(client, f"{CHAT}/quarterly-figures.xlsx", "Nope!A1:B2") if tool == "read_document" else find(client, "x")
    unavailable(r)
    assert "salaries" not in r.text


@pytest.mark.parametrize(
    ("status", "payload"),
    [("timeout", {}), ("killed", {}), ("error", {"error": "KeyError: /data/jobs/abc"}), ("ok", {"parts": "no"})],
)
def test_every_other_answer_that_reached_the_api_is_recorded_as_an_error(
    client: TestClient, store: Store, executor: Scripted, audit_lines: AuditLines, status: str, payload: Any
) -> None:
    """Out of time or memory, a failed tool, an answer that cannot be read: an ``error`` event, with no code."""
    rec = put(store, "a.docx", p=PG)
    executor.status = status
    executor.payload = payload
    assert refused(read(client, f"{CHAT}/a.docx"))["reason"] == "refused"
    (event,) = tool_events(audit_lines, "read_document")
    assert (event["outcome"], event["target"]["ids"]) == ("error", [rec.id])
    assert event["criteria"] == {"caller": "ghostwriter", "paged": False}


# --- read_document: pages without text, and pictures, as images ---------------------------------------------------

SHOWN_KEYS = {"label", "n", "width", "height", "mime"}
#: What the model is told when the pictures of a PDF's pages without text cannot be made now, but may be later.
UNSHOWN = "The pages without text could not be shown now; view_file shows them."
#: What it is told when they cannot be made at all, by the cause: view_file is not promised.
AT_LIMIT = "The pages without text cannot be shown: the renders of this file are at their limit."
OVER_QUOTA = "The pages without text cannot be shown: the workspace quota is exceeded."
FULL = "The pages without text cannot be shown: storage is full."
UNDRAWABLE = "The pages without text cannot be shown: the file cannot be drawn."
#: What a read of a deck is told when its slides cannot be matched to their pictures.
MISMATCH = "The slides without text cannot be matched to their pictures here; view_file shows the slides as drawn."
#: The end of the first read of a Word or OpenDocument text file, on a call that takes images.
PAGES_VIEWABLE = "view_file shows its pages as images, as they would print."
SCANNED = ["Harbour figures 2026", None, "Quay list"]


class Picky(Local):
    """The in-process executor, but the jobs of a tool a test names get another status, or a scripted answer.

    Attributes:
        statuses: The status a job of a tool is answered with instead of running, by the tool's name.
        scripted: The runner's answer a job of a tool gets instead of the real runner's, by the tool's name.
    """

    def __init__(self, data_dir: Path, jobs: Path) -> None:
        """Run every tool for real until told otherwise."""
        super().__init__(data_dir, jobs)
        self.statuses: dict[str, str] = {}
        self.scripted: dict[str, dict[str, Any]] = {}

    @override
    async def run(self, req: JobRequest, timeout_s: float) -> JobResult:
        """Answer as the test chose for the job's tool, else as :class:`Local` does."""
        if req.tool in self.scripted:
            base = await FakeClient.run(self, req, timeout_s)
            return base.model_copy(update={"result": self.scripted[req.tool]})
        if req.tool not in self.statuses:
            return await super().run(req, timeout_s)
        self.status = self.statuses[req.tool]
        try:
            return await super().run(req, timeout_s)
        finally:
            self.status = "ok"

    def tools(self) -> list[str]:
        """The tools of the jobs it was given, in order."""
        return [req.tool for req in self.requests]


@pytest.fixture
def local(settings: Settings, tmp_path: Path) -> Picky:
    """The in-process executor."""
    return Picky(settings.data_dir, tmp_path / "jobs")


@pytest.fixture
def built(tmp_path: Path) -> Path:
    """Where files are built before they are uploaded."""
    folder = tmp_path / "built"
    folder.mkdir()
    return folder


def served(settings: Settings, store: Store, audit: AuditLog, local: Picky) -> Iterator[TestClient]:
    """A client on an app whose jobs run the real runners in this process, with ``settings``."""
    queue = JobQueue(settings, local, store, Metrics(settings.data_dir))
    with TestClient(create_app(settings, store=store, audit=audit, jobs=queue)) as test_client:
        yield test_client


@pytest.fixture
def seeing(settings: Settings, store: Store, audit: AuditLog, local: Picky) -> Iterator[TestClient]:
    """A client on that app."""
    yield from served(settings, store, audit, local)


@pytest.fixture
def seeing_small(settings: Settings, store: Store, audit: AuditLog, local: Picky) -> Iterator[TestClient]:
    """A client on that app whose answers may be 1 KB."""
    yield from served(dataclasses.replace(settings, result_max_kb=1), store, audit, local)


def read_events(audit_lines: AuditLines) -> list[dict[str, Any]]:
    """The ``read_document`` events written so far."""
    return tool_events(audit_lines, "read_document")


def test_a_scanned_page_comes_back_as_an_image_in_the_same_answer(
    seeing: TestClient, local: Picky, store: Store, built: Path, settings: Settings
) -> None:
    """One call: the text of the pages that have it, and the page that has none as an image, with no round between."""
    source = pdf(store, built, "harbour-report.pdf", SCANNED)
    answer = seen(read(seeing, source.virtual_path))

    structured = answer["structuredContent"]
    assert (structured["kind"], structured["total_parts"], structured["pages_without_text"]) == ("pdf", 3, [2])
    assert [part["label"] for part in structured["parts"]] == ["page 1", "page 3"]
    assert (structured["truncated"], structured["next_part"]) == (False, None)
    (item,) = structured["shown"]
    assert set(item) == SHOWN_KEYS
    assert (item["label"], item["n"], item["mime"]) == ("page 2", 2, "image/jpeg")
    (block,) = images(answer)
    assert (block["mimeType"], block["annotations"]) == ("image/jpeg", {"audience": ["assistant"]})
    assert raw(block) == file_of(settings, whole_renders(store, source)[2]).read_bytes()
    assert Image.open(io.BytesIO(raw(block))).size == (item["width"], item["height"])
    assert "have no text layer" in structured["hint"]
    assert structured["hint"].endswith(" Page 2 comes with this answer as an image.")
    assert json.loads(answer["content"][0]["text"]) == structured
    assert local.tools() == ["read_document", "prepare_file"]


def test_the_window_ends_before_a_page_whose_image_does_not_fit(seeing: TestClient, store: Store, built: Path) -> None:
    """Four images at most: the fifth page is where the answer stops, and ``next_part`` names it."""
    source = pdf(store, built, "harbour-scans.pdf", [None] * 6)
    answer = seen(read(seeing, source.virtual_path))

    structured = answer["structuredContent"]
    assert [item["n"] for item in structured["shown"]] == [1, 2, 3, 4]
    assert len(images(answer)) == 4
    assert structured["parts"] == []
    assert (structured["truncated"], structured["next_part"]) == (True, "page 5")
    assert structured["hint"].endswith(
        " The answer is cut before page 5 to keep its images within the limit."
        ' Call read_document again with part="page 5" to continue.'
        " Pages 1, 2, 3, 4 come with this answer as images."
    )
    assert "result limit" not in structured["hint"]
    # The pages the call reads on to are not listed: the model must not take them for pages it was not shown.
    assert structured["pages_without_text"] == [1, 2, 3, 4]


def test_the_text_after_the_cut_goes_with_the_next_part(seeing: TestClient, store: Store, built: Path) -> None:
    """The window ends where the images stop: the text of the pages after it is read again from ``next_part``."""
    source = pdf(store, built, "harbour-mixed.pdf", [None, "A", None, None, None, "B", None, "C"])
    structured = seen(read(seeing, source.virtual_path))["structuredContent"]

    assert [item["n"] for item in structured["shown"]] == [1, 3, 4, 5]
    assert [part["label"] for part in structured["parts"]] == ["page 2", "page 6"]
    assert (structured["truncated"], structured["next_part"]) == (True, "page 7")
    assert 'part="page 7"' in structured["hint"]
    rest = seen(read(seeing, source.virtual_path, structured["next_part"]))["structuredContent"]
    assert [item["n"] for item in rest["shown"]] == [7]
    assert [part["label"] for part in rest["parts"]] == ["page 8"]
    assert rest["truncated"] is False


@pytest.mark.parametrize("strips", [1, 2, 3])
def test_paging_always_moves_on(seeing: TestClient, store: Store, built: Path, strips: int) -> None:
    """Whatever the strips, each call shows at least one page and its ``next_part`` leads to the others."""
    source = pdf(store, built, "harbour-scans.pdf", [None, None, None])
    seen_pages: list[int] = []
    part: str | None = None
    for _ in range(3):
        structured = seen(read(seeing, source.virtual_path, part, strips=strips))["structuredContent"]
        assert structured["shown"], "a call that shows no page would not move on"
        seen_pages += sorted({item["n"] for item in structured["shown"]})
        if not structured["truncated"]:
            break
        part = structured["next_part"]
    assert seen_pages == [1, 2, 3]


def test_three_strips_show_one_page_a_call(seeing: TestClient, store: Store, built: Path) -> None:
    """Three images a page leave room for one page in four images: the calls follow ``next_part`` page by page."""
    source = pdf(store, built, "harbour-scans.pdf", [None, None, None])
    firsts = [
        seen(read(seeing, source.virtual_path, part, strips=3))["structuredContent"]
        for part in (None, "page 2", "page 3")
    ]
    assert [[item["label"] for item in call["shown"]] for call in firsts] == [
        [f"page {n}, strip {i} of 3" for i in (1, 2, 3)] for n in (1, 2, 3)
    ]
    assert [(call["truncated"], call["next_part"]) for call in firsts] == [
        (True, "page 2"),
        (True, "page 3"),
        (False, None),
    ]


def test_the_blocks_follow_shown_in_order(seeing: TestClient, store: Store, built: Path, settings: Settings) -> None:
    """Block ``i`` is the render ``shown[i]`` names: page by page, each page's strips in order."""
    source = pdf(store, built, "harbour-mixed.pdf", [None, "A", None])
    answer = seen(read(seeing, source.virtual_path, strips=2))

    structured = answer["structuredContent"]
    assert [item["label"] for item in structured["shown"]] == [
        "page 1, strip 1 of 2",
        "page 1, strip 2 of 2",
        "page 3, strip 1 of 2",
        "page 3, strip 2 of 2",
    ]
    cut = {(rec.ordinal, rec.strip): rec for rec in store.derived_of(source) if rec.strips == 2}
    for item, block in zip(structured["shown"], images(answer), strict=True):
        rec = cut[(item["n"], int(item["label"].split("strip ")[1][0]))]
        assert raw(block) == file_of(settings, rec).read_bytes()
        assert (rec.width, rec.height, rec.mime) == (item["width"], item["height"], item["mime"])
        assert block["mimeType"] == item["mime"]


def test_an_image_file_is_read_as_its_image(
    seeing: TestClient, local: Picky, store: Store, built: Path, settings: Settings
) -> None:
    """A picture has no text to read: no read job, and the answer says what it is, with the image."""
    source = photo(store, built)
    answer = seen(read(seeing, source.virtual_path))

    structured = answer["structuredContent"]
    assert (structured["kind"], structured["total_parts"], structured["pages_without_text"]) == ("image", 1, [1])
    assert (structured["parts"], structured["truncated"], structured["next_part"]) == ([], False, None)
    assert structured["hint"] == "This is a picture: it comes with this answer for you to see."
    (item,) = structured["shown"]
    assert (item["label"], item["n"], item["mime"]) == ("image", 1, "image/jpeg")
    (block,) = images(answer)
    assert raw(block) == file_of(settings, whole_renders(store, source)[1]).read_bytes()
    assert local.tools() == ["prepare_file"]


def test_a_picture_can_be_cut_into_strips(seeing: TestClient, store: Store, built: Path) -> None:
    """Strips are images of the one picture, in order, all with the same page number."""
    source = photo(store, built)
    answer = seen(read(seeing, source.virtual_path, strips=2))

    shown = answer["structuredContent"]["shown"]
    assert [(item["label"], item["n"]) for item in shown] == [("image, strip 1 of 2", 1), ("image, strip 2 of 2", 1)]
    assert len(images(answer)) == 2


def test_a_picture_with_several_frames_says_only_the_first_is_shown(
    seeing: TestClient, store: Store, built: Path
) -> None:
    """Two sentences, each whole, joined by a space."""
    source = put(store, "harbour-cranes.gif", mi.make_gif(built / "harbour-cranes.gif", frames=2).read_bytes(), p=PG)
    hint = seen(read(seeing, source.virtual_path))["structuredContent"]["hint"]
    assert hint == "This is a picture: it comes with this answer for you to see. Only the first of 2 frames is shown."


def test_a_text_pdf_reads_as_before(
    seeing: TestClient, local: Picky, store: Store, built: Path, audit_lines: AuditLines
) -> None:
    """No page lacks text: no image block, no render job, and the event names the file alone with no image."""
    source = pdf(store, built, "harbour-report.pdf", ["Harbour figures 2026", "Quay list"])
    answer = seen(read(seeing, source.virtual_path))

    assert len(answer["content"]) == 1
    structured = answer["structuredContent"]
    assert (structured["shown"], structured["pages_without_text"], structured["hint"]) == ([], [], None)
    assert [part["label"] for part in structured["parts"]] == ["page 1", "page 2"]
    assert local.tools() == ["read_document"]
    (event,) = read_events(audit_lines)
    assert event["target"]["ids"] == [source.id]
    assert event["criteria"] == {"caller": "ghostwriter", "paged": False, "images": 0}


def test_an_encrypted_pdf_reads_as_before(
    seeing: TestClient, local: Picky, store: Store, built: Path, audit_lines: AuditLines
) -> None:
    """Review Focus 3: the same refusal and the same ``error`` event as ever; no prepare, no render row."""
    source = pdf(store, built, "sealed-report.pdf", ["Harbour figures 2026"], password="synthetic-password")
    assert refusal(read(seeing, source.virtual_path)) == (
        "refused",
        "unreadable_file: the PDF is password-protected (encrypted)",
    )
    assert local.tools() == ["read_document"]
    assert store.derived_of(source) == []
    (event,) = read_events(audit_lines)
    assert (event["outcome"], event["target"]["ids"]) == ("error", [source.id])
    assert event["criteria"] == {"caller": "ghostwriter", "paged": False, "refused": "unreadable_file"}


def test_without_images_an_office_read_is_as_in_v07(
    seeing: TestClient, local: Picky, store: Store, built: Path, audit_lines: AuditLines, fake_office: FakeOffice
) -> None:
    """R15: a call without ``X-Accept-Images`` reads a deck and a Word file as v0.7.0 did, word for word.

    No image, no note of one, no conversion and no render job; each read is recorded with no image.
    """
    blank: Any = Presentation()
    blank.slides.add_slide(blank.slide_layouts[6])
    blank.save(str(built / "harbour-deck.pptx"))
    source = put(store, "harbour-deck.pptx", (built / "harbour-deck.pptx").read_bytes(), p=PG)
    notes = put(store, "harbour-notes.docx", mk.make_docx(built / "harbour-notes.docx").read_bytes(), p=PG)
    answer = seen(read(seeing, source.virtual_path, headers=HG_TEXT))

    assert len(answer["content"]) == 1
    assert answer["structuredContent"] == {
        "path": source.virtual_path,
        "kind": "pptx",
        "parts": [],
        "total_parts": 1,
        "pages_without_text": [1],
        "shown": [],
        "truncated": False,
        "next_part": None,
        "hint": "slide 1 hold no text (pictures only?); read_document does no OCR.",
        "outputs": [],
    }
    document = answered(read(seeing, notes.virtual_path, headers=HG_TEXT))
    assert "view_file" not in json.dumps(document)
    assert local.tools() == ["read_document", "read_document"]
    assert [req.args["images"] for req in local.requests] == [False, False]
    assert fake_office.calls == 0
    assert store.derived_of(source) == store.derived_of(notes) == []
    first, second = read_events(audit_lines)
    assert (first["target"]["ids"], second["target"]["ids"]) == ([source.id], [notes.id])
    assert first["criteria"] == second["criteria"] == {"caller": "ghostwriter", "paged": False, "images": 0}


def test_a_picture_only_slide_comes_back_as_an_image(
    seeing: TestClient,
    local: Picky,
    store: Store,
    built: Path,
    settings: Settings,
    audit_lines: AuditLines,
    fake_office: FakeOffice,
) -> None:
    """One call: the text of the slides that have it, and the slide that has none as an image, as for a PDF's page."""
    source = deck(store, built, 3, picture_only=[2])
    answer = seen(read(seeing, source.virtual_path))

    structured = answer["structuredContent"]
    assert (structured["kind"], structured["total_parts"], structured["pages_without_text"]) == ("pptx", 3, [2])
    assert [part["label"] for part in structured["parts"]] == ["slide 1", "slide 3"]
    assert (structured["truncated"], structured["next_part"]) == (False, None)
    (item,) = structured["shown"]
    assert set(item) == SHOWN_KEYS
    assert (item["label"], item["n"], item["mime"]) == ("slide 2", 2, "image/jpeg")
    (block,) = images(answer)
    render = whole_renders(store, source)[2]
    assert raw(block) == file_of(settings, render).read_bytes()
    assert structured["hint"] == (
        "slide 2 holds no text (pictures only?); do not treat them as blank. "
        "Slide 2 comes with this answer as an image."
    )
    assert local.tools() == ["read_document", "convert_office", "prepare_file"]
    assert fake_office.calls == 1
    (event,) = read_events(audit_lines)
    assert event["target"]["ids"] == [source.id, render.id]
    assert event["criteria"] == {"caller": "ghostwriter", "paged": False, "images": 1}


def test_a_deck_window_ends_before_a_slide_that_does_not_fit(
    seeing: TestClient, store: Store, built: Path, fake_office: FakeOffice
) -> None:
    """Four images at most: the fifth slide is where the answer stops, and ``next_part`` names it as a slide."""
    source = deck(store, built, 6, picture_only=list(range(1, 7)), chart_on=None)
    answer = seen(read(seeing, source.virtual_path))

    structured = answer["structuredContent"]
    assert [item["label"] for item in structured["shown"]] == ["slide 1", "slide 2", "slide 3", "slide 4"]
    assert len(images(answer)) == 4
    assert structured["parts"] == []
    assert (structured["truncated"], structured["next_part"]) == (True, "slide 5")
    assert structured["hint"].endswith(
        " The answer is cut before slide 5 to keep its images within the limit."
        ' Call read_document again with part="slide 5" to continue.'
        " Slides 1, 2, 3, 4 come with this answer as images."
    )
    assert structured["pages_without_text"] == [1, 2, 3, 4]
    rest = seen(read(seeing, source.virtual_path, structured["next_part"]))["structuredContent"]
    assert [item["label"] for item in rest["shown"]] == ["slide 5", "slide 6"]
    assert rest["truncated"] is False
    assert fake_office.calls == 1


def test_a_deck_whose_pictures_do_not_match_attaches_nothing(
    seeing: TestClient,
    local: Picky,
    store: Store,
    built: Path,
    audit_lines: AuditLines,
    fake_office: FakeOffice,
) -> None:
    """Review Focus 2: a PDF with another count of pages than the deck has slides shows no picture, and says why."""
    fake_office.drop = {1}
    source = deck(store, built, 3, picture_only=[2])
    answer = seen(read(seeing, source.virtual_path))

    structured = answer["structuredContent"]
    assert (len(answer["content"]), structured["shown"]) == (1, [])
    assert [part["label"] for part in structured["parts"]] == ["slide 1", "slide 3"]
    assert structured["pages_without_text"] == [2]
    assert structured["hint"] == f"slide 2 holds no text (pictures only?); do not treat them as blank. {MISMATCH}"
    assert local.tools() == ["read_document", "convert_office", "prepare_file"]  # no render job for the slide
    (event,) = read_events(audit_lines)
    assert event["target"]["ids"] == [source.id]
    assert event["criteria"] == {"caller": "ghostwriter", "paged": False, "images": 0}


def test_a_word_file_reads_as_text_and_points_to_view_file(
    seeing: TestClient,
    local: Picky,
    store: Store,
    built: Path,
    audit_lines: AuditLines,
    fake_office: FakeOffice,
) -> None:
    """A document is read as text alone, with no conversion; its first read says view_file shows its pages."""
    source = put(store, "harbour-notes.docx", mk.make_docx(built / "harbour-notes.docx").read_bytes(), p=PG)
    answer = seen(read(seeing, source.virtual_path))

    structured = answer["structuredContent"]
    assert (len(answer["content"]), structured["shown"]) == (1, [])
    assert structured["parts"]
    assert structured["hint"] == PAGES_VIEWABLE
    paged = answered(read(seeing, source.virtual_path, "paragraph 2"))
    assert PAGES_VIEWABLE not in (paged["hint"] or "")
    assert local.tools() == ["read_document", "read_document"]
    assert fake_office.calls == 0
    assert store.derived_of(source) == []
    first, second = read_events(audit_lines)
    assert first["criteria"] == {"caller": "ghostwriter", "paged": False, "images": 0}
    assert second["criteria"] == {"caller": "ghostwriter", "paged": True, "images": 0}


def test_a_busy_conversion_still_answers_the_text(
    seeing: TestClient, local: Picky, store: Store, built: Path, audit_lines: AuditLines, fake_office: FakeOffice
) -> None:
    """The text was read: a conversion the executor has no room for does not take it back."""
    source = deck(store, built, 3, picture_only=[2])
    local.statuses["convert_office"] = "busy"
    answer = seen(read(seeing, source.virtual_path))

    structured = answer["structuredContent"]
    assert [part["label"] for part in structured["parts"]] == ["slide 1", "slide 3"]
    assert (structured["shown"], structured["pages_without_text"]) == ([], [2])
    assert len(answer["content"]) == 1
    assert structured["hint"].endswith(" The slides without text could not be shown now; view_file shows them.")
    assert (structured["truncated"], structured["next_part"]) == (False, None)
    (event,) = read_events(audit_lines)
    assert (event["outcome"], event["target"]["ids"]) == ("ok", [source.id])
    assert event["criteria"] == {"caller": "ghostwriter", "paged": False, "images": 0}

    del local.statuses["convert_office"]  # the next call has room, and shows the slide
    assert [item["label"] for item in seen(read(seeing, source.virtual_path))["structuredContent"]["shown"]] == [
        "slide 2"
    ]


def test_a_deck_that_cannot_be_converted_is_read_and_not_converted_again(
    seeing: TestClient, local: Picky, store: Store, built: Path, fake_office: FakeOffice
) -> None:
    """Final review: a deck refused by its conversion is still read as text; the next read does not convert it again.

    Each read says its slides without text cannot be drawn; the refusal of the first is remembered for the second.
    """
    fake_office.ceiling = 0
    source = deck(store, built, 3, picture_only=[2])
    answers = [answered(read(seeing, source.virtual_path)) for _ in range(2)]

    for structured in answers:
        assert [part["label"] for part in structured["parts"]] == ["slide 1", "slide 3"]
        assert structured["shown"] == []
        assert structured["hint"].endswith(" The slides without text cannot be shown: the file cannot be drawn.")
    assert local.tools() == ["read_document", "convert_office", "read_document"]


@pytest.mark.parametrize("strips", [None, 2])
def test_a_scanned_pdf_read_without_images_is_read_as_in_0_6(
    seeing: TestClient, local: Picky, store: Store, built: Path, audit_lines: AuditLines, strips: int | None
) -> None:
    """Final review I1: a call without ``X-Accept-Images`` gets no image, no note of one and 0.6's hint; no render job.

    ``strips`` is ignored.
    """
    source = pdf(store, built, "harbour-report.pdf", SCANNED)
    answer = seen(read(seeing, source.virtual_path, headers=HG_TEXT, strips=strips))

    assert len(answer["content"]) == 1
    structured = answer["structuredContent"]
    assert (structured["pages_without_text"], structured["shown"]) == ([2], [])
    assert [part["label"] for part in structured["parts"]] == ["page 1", "page 3"]
    assert structured["hint"] == TEXT_ONLY_PAGE_HINT
    assert "comes with this answer" not in answer["content"][0]["text"]
    assert local.tools() == ["read_document"]
    assert local.requests[0].args["images"] is False
    assert store.derived_of(source) == []
    (event,) = read_events(audit_lines)
    assert event["criteria"] == {"caller": "ghostwriter", "paged": False, "images": 0}


def test_a_picture_read_without_images_is_refused_as_in_0_6(
    seeing: TestClient, local: Picky, store: Store, built: Path
) -> None:
    """A picture is read by the reader, which refuses it as it did in 0.6: no prepare, no render row."""
    source = photo(store, built)
    reason, text = refusal(read(seeing, source.virtual_path, headers=HG_TEXT))

    assert reason == "refused"
    assert text.startswith("unsupported_format: .jpg cannot be read here")
    assert local.tools() == ["read_document"]
    assert store.derived_of(source) == []


def test_the_images_are_recorded_with_the_read(
    seeing: TestClient, store: Store, built: Path, audit_lines: AuditLines
) -> None:
    """One ``query`` event names the file, then each render shown; no name or text of the file is in any line."""
    source = pdf(store, built, "harbour-report.pdf", SCANNED)
    seen(read(seeing, source.virtual_path, "pages 1-3"))

    (event,) = read_events(audit_lines)
    render = whole_renders(store, source)[2]
    assert (event["action"], event["operation"], event["outcome"]) == ("query", "tool.read_document", "ok")
    assert event["target"]["ids"] == [source.id, render.id]
    assert event["target"]["container"] == "chat:chat-1"
    assert event["criteria"] == {"caller": "ghostwriter", "paged": True, "images": 1}
    raw_lines = json.dumps(audit_lines())
    for secret in ("harbour-report", "Harbour figures", "Quay list"):
        assert secret not in raw_lines


def test_a_picture_is_recorded_with_its_image(
    seeing: TestClient, store: Store, built: Path, audit_lines: AuditLines
) -> None:
    """The file, then its render, and the call's strips of a picture are counted as images."""
    source = photo(store, built)
    seen(read(seeing, source.virtual_path, strips=2))

    (event,) = read_events(audit_lines)
    strips = [rec for rec in store.derived_of(source) if rec.strips == 2]
    assert event["target"]["ids"] == [source.id, *(rec.id for rec in sorted(strips, key=lambda rec: rec.strip))]
    assert event["criteria"] == {"caller": "ghostwriter", "paged": False, "images": 2}


def test_no_image_leaves_when_the_read_cannot_be_recorded(
    seeing: TestClient,
    store: Store,
    built: Path,
    audit_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed: with the chain broken the call fails, and nothing of the pages goes out."""
    source = pdf(store, built, "harbour-report.pdf", SCANNED)
    seen(read(seeing, source.virtual_path))

    def refuse(_text: str, /) -> int:
        raise OSError("synthetic audit stream failure")

    monkeypatch.setattr(audit_stream, "write", refuse)
    r = read(seeing, source.virtual_path)
    unavailable(r)
    assert "image" not in r.text


# --- read_document: when the images cannot be made ----------------------------------------------------------------


def test_a_busy_render_still_answers_the_text(
    seeing: TestClient, local: Picky, store: Store, built: Path, audit_lines: AuditLines
) -> None:
    """The text was read: a render job the executor has no room for does not take it back."""
    source = pdf(store, built, "harbour-report.pdf", SCANNED)
    local.statuses["prepare_file"] = "busy"
    answer = seen(read(seeing, source.virtual_path))

    structured = answer["structuredContent"]
    assert [part["label"] for part in structured["parts"]] == ["page 1", "page 3"]
    assert (structured["shown"], structured["pages_without_text"]) == ([], [2])
    assert len(answer["content"]) == 1
    assert structured["hint"].endswith(f" {UNSHOWN}")
    assert (structured["truncated"], structured["next_part"]) == (False, None)
    (event,) = read_events(audit_lines)
    assert (event["outcome"], event["target"]["ids"]) == ("ok", [source.id])
    assert event["criteria"] == {"caller": "ghostwriter", "paged": False, "images": 0}

    del local.statuses["prepare_file"]  # the next call has room, and shows the page
    assert [item["n"] for item in seen(read(seeing, source.virtual_path))["structuredContent"]["shown"]] == [2]


def test_a_head_the_store_cannot_keep_still_answers_the_text(
    seeing: TestClient, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source's cap of renders is spent before the first one: the prepare is refused, the text still goes."""
    source = pdf(store, built, "harbour-report.pdf", SCANNED)
    capped(store, monkeypatch, 1)
    answer = seen(read(seeing, source.virtual_path))

    structured = answer["structuredContent"]
    assert [part["label"] for part in structured["parts"]] == ["page 1", "page 3"]
    assert structured["shown"] == []
    assert structured["hint"].endswith(f" {AT_LIMIT}")


def test_a_quota_that_refuses_the_strips_still_answers_the_text(
    seeing: TestClient, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user's quota, on the strips a call asks for after the prepare: the text goes, with no image."""
    source = pdf(store, built, "harbour-report.pdf", SCANNED)
    seen(read(seeing, source.virtual_path))

    def refuse(*_: Any, **__: Any) -> None:
        raise QuotaExceeded("workspace quota exceeded")

    monkeypatch.setattr(store, "register_derived", refuse)
    answer = seen(read(seeing, source.virtual_path, strips=2))

    structured = answer["structuredContent"]
    assert [part["label"] for part in structured["parts"]] == ["page 1", "page 3"]
    assert (structured["shown"], len(answer["content"])) == ([], 1)
    assert structured["hint"].endswith(f" {OVER_QUOTA}")


@pytest.mark.parametrize(
    ("raised", "told"),
    [
        (Busy("executor_busy"), UNSHOWN),
        (Busy("wait_timeout"), UNSHOWN),
        (HTTPException(status_code=500, detail=NOT_KEPT), UNSHOWN),
        (
            HTTPException(status_code=422, detail="the file could not be processed: the job ran past its wall time"),
            UNSHOWN,
        ),
        (HTTPException(status_code=503, detail="hidden_memory: memory outside any job's count"), UNSHOWN),
        (RendersFull("the renders of this file are at their limit"), AT_LIMIT),
        (QuotaExceeded("workspace quota exceeded"), OVER_QUOTA),
        (StorageFull("storage is full"), FULL),
        (HTTPException(status_code=507, detail="storage_full: storage is full"), FULL),
        (HTTPException(status_code=422, detail="unreadable_file: the PDF cannot be opened"), UNDRAWABLE),
        # Final review M3: a runner that ran out of time may not next time, so view_file is promised.
        (HTTPException(status_code=422, detail="read_timeout: reading the file took longer than 90 s"), UNSHOWN),
        (HTTPException(status_code=404, detail="file_not_found: the file is not in the workspace"), UNDRAWABLE),
    ],
    ids=[
        "busy",
        "wait",
        "not-kept",
        "job-timeout",
        "hidden-memory",
        "renders-full",
        "quota",
        "storage",
        "runner-storage",
        "unreadable",
        "read-timeout",
        "not-found",
    ],
)
def test_the_pictures_of_a_pdf_never_fail_its_read(
    seeing: TestClient, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch, raised: Exception, told: str
) -> None:
    """Every way the renders can be refused or lost answers the text, with ``shown`` empty and a whole sentence.

    A cause that may pass sends the model to ``view_file``; one that will not names the cause and promises nothing.
    """

    async def refuse(*_: Any, **__: Any) -> None:
        raise raised

    source = pdf(store, built, "harbour-report.pdf", SCANNED)
    monkeypatch.setattr(tools_views, "prepared", refuse)
    structured = answered(read(seeing, source.virtual_path))
    assert [part["label"] for part in structured["parts"]] == ["page 1", "page 3"]
    assert structured["shown"] == []
    assert structured["hint"].endswith(f" {told}")
    assert structured["hint"].count("The pages without text") == 1


def test_a_render_job_that_fails_in_an_unexpected_way_still_fails_the_read(
    seeing: TestClient, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch, audit_lines: AuditLines
) -> None:
    """Only the refusals and the losses above are the pictures' own: any other failure is the call's.

    The read job's answer reached the api, so the trail has it: an ``error`` event naming the file, with no image.
    """

    async def fail(*_: Any, **__: Any) -> None:
        raise HTTPException(status_code=500, detail="the tool failed")

    source = pdf(store, built, "harbour-report.pdf", SCANNED)
    monkeypatch.setattr(tools_views, "prepared", fail)
    assert refusal(read(seeing, source.virtual_path)) == ("refused", "the tool failed")
    (event,) = read_events(audit_lines)
    assert (event["action"], event["outcome"], event["target"]["ids"]) == ("query", "error", [source.id])
    assert event["criteria"] == {"caller": "ghostwriter", "paged": False, "images": 0}


def test_a_purpose_the_renders_need_and_the_read_did_not_is_recorded_too(
    settings: Settings, store: Store, audit: AuditLog, local: Picky, built: Path, audit_lines: AuditLines
) -> None:
    """With ``modify`` required and ``query`` not, the read runs and its renders are refused: both are in the trail."""
    required = dataclasses.replace(settings, require_purpose=frozenset({"modify"}))
    source = pdf(store, built, "harbour-report.pdf", SCANNED)
    queue = JobQueue(required, local, store, Metrics(settings.data_dir))
    with TestClient(create_app(required, store=store, audit=audit, jobs=queue)) as client:
        assert refusal(read(client, source.virtual_path))[0] == "purpose_required"
        trail = [e for e in audit_lines() if e["operation"] in {"file.prepare", "tool.read_document"}]
        assert [(e["operation"], e["outcome"]) for e in trail] == [
            ("file.prepare", "denied"),
            ("tool.read_document", "error"),
        ]
        assert trail[1]["target"]["ids"] == [source.id]


def test_a_cancelled_call_records_its_read_and_stays_cancelled(
    seeing: TestClient,
    settings: Settings,
    store: Store,
    audit: AuditLog,
    local: Picky,
    built: Path,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A call cancelled while it waits for its renders writes the ``error`` event and is still cancelled."""

    async def cancelled(*_: Any, **__: Any) -> None:
        raise asyncio.CancelledError

    source = pdf(store, built, "harbour-report.pdf", SCANNED)
    monkeypatch.setattr(tools_views, "prepared", cancelled)
    jobs = CallJobs(JobQueue(settings, local, store, Metrics(settings.data_dir)), CallLimits(120, 60))
    body = ReadDocumentRequest(path=source.virtual_path)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(tools_views.read_document(body, PG, settings, store, audit, jobs, images=True))
    (event,) = read_events(audit_lines)
    assert (event["outcome"], event["target"]["ids"]) == ("error", [source.id])
    assert event["criteria"] == {"caller": "ghostwriter", "paged": False, "images": 0}


def test_a_trail_that_cannot_take_the_error_does_not_hide_the_failure_it_records(
    seeing: TestClient,
    store: Store,
    built: Path,
    audit_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed write of that event is the call's failure (audit unavailable), as every unwritable event is."""

    async def fail(*_: Any, **__: Any) -> None:
        raise HTTPException(status_code=500, detail="the tool failed")

    source = pdf(store, built, "harbour-report.pdf", SCANNED)
    monkeypatch.setattr(tools_views, "prepared", fail)

    def refuse(_text: str, /) -> int:
        raise OSError("synthetic audit stream failure")

    monkeypatch.setattr(audit_stream, "write", refuse)
    unavailable(read(seeing, source.virtual_path))


def test_a_picture_has_no_text_to_fall_back_on(seeing: TestClient, local: Picky, store: Store, built: Path) -> None:
    """A picture whose render cannot be made fails the call, as ``view_file`` does."""
    source = photo(store, built)
    local.statuses["prepare_file"] = "busy"
    assert refusal(read(seeing, source.virtual_path)) == ("executor_busy", "busy, retry later")


def test_a_picture_that_cannot_be_read_back_is_answered_as_not_shown(
    seeing: TestClient, store: Store, built: Path, settings: Settings, audit_lines: AuditLines
) -> None:
    """A render that is not what its row says is never sent: the answer says so, with no image, and is recorded so."""
    source = photo(store, built)
    seen(read(seeing, source.virtual_path))
    file_of(settings, whole_renders(store, source)[1]).write_bytes(b"not a picture, whatever its row says")

    answer = seen(read(seeing, source.virtual_path))
    structured = answer["structuredContent"]
    assert (structured["shown"], len(answer["content"])) == ([], 1)
    assert structured["hint"] == "This picture could not be shown."
    *_, event = read_events(audit_lines)
    assert (event["target"]["ids"], event["criteria"]["images"]) == ([source.id], 0)


def test_a_file_that_is_not_a_picture_named_as_one_is_refused(seeing: TestClient, store: Store) -> None:
    """Review Focus 2: a text file named ``.png`` is ``unreadable_file``, with nothing stored."""
    source = put(store, "harbour-notes.png", b"these are notes, not a picture", p=PG)
    reason, text = refusal(read(seeing, source.virtual_path))
    assert (reason, text.partition(":")[0]) == ("refused", "unreadable_file")
    assert store.derived_of(source) == []


def test_a_page_that_cannot_be_read_back_is_named_and_the_others_still_go(
    seeing: TestClient, store: Store, built: Path, settings: Settings, audit_lines: AuditLines
) -> None:
    """A render that is not what its row says is left out of the answer and named; the rest is shown."""
    source = pdf(store, built, "harbour-scans.pdf", [None, None, "Quay list"])
    seen(read(seeing, source.virtual_path))
    renders = whole_renders(store, source)
    file_of(settings, renders[1]).write_bytes(b"not a picture, whatever its row says")

    answer = seen(read(seeing, source.virtual_path))
    structured = answer["structuredContent"]
    assert [item["n"] for item in structured["shown"]] == [2]
    assert len(images(answer)) == 1
    assert "Page 2 comes with this answer as an image. Page 1 could not be shown." in structured["hint"]
    _, second = read_events(audit_lines)
    assert second["target"]["ids"] == [source.id, renders[2].id]
    assert "not a picture" not in json.dumps(audit_lines())


def test_when_every_page_of_the_window_fails_the_rest_is_still_offered(
    seeing: TestClient, store: Store, built: Path, settings: Settings
) -> None:
    """No page of the first four can be read back: the call says so and names where to go on, not nothing."""
    source = pdf(store, built, "harbour-scans.pdf", [None] * 6)
    seen(read(seeing, source.virtual_path))
    for n, rec in whole_renders(store, source).items():
        if n <= 4:
            file_of(settings, rec).write_bytes(b"not a picture")

    answer = seen(read(seeing, source.virtual_path))
    structured = answer["structuredContent"]
    assert (structured["shown"], len(answer["content"])) == ([], 1)
    assert (structured["truncated"], structured["next_part"]) == (True, "page 5")
    assert "Pages 1, 2, 3, 4 could not be shown." in structured["hint"]
    assert [item["n"] for item in seen(read(seeing, source.virtual_path, "page 5"))["structuredContent"]["shown"]] == [
        5,
        6,
    ]


# --- read_document: which pages the images are of -----------------------------------------------------------------


def test_a_page_past_where_the_text_stops_has_no_image_yet(
    seeing: TestClient, local: Picky, store: Store, built: Path
) -> None:
    """The text stops before page 6: the scan there is shown by the call that reads on, not by this one."""
    source = pdf(store, built, "harbour-mixed.pdf", ["A", None, "B", "C", "D", None, "E", "F"])
    local.scripted["read_document"] = reading(
        "x",
        ("page 1", "a"),
        ("page 3", "b"),
        kind="pdf",
        total_parts=8,
        pages_without_text=[2, 6],
        truncated=True,
        next_part="page 4",
    )
    answer = seen(read(seeing, source.virtual_path))

    structured = answer["structuredContent"]
    assert [item["n"] for item in structured["shown"]] == [2]
    assert len(images(answer)) == 1
    assert (structured["truncated"], structured["next_part"]) == (True, "page 4")
    assert structured["hint"].count('part="page 4"') == 1
    assert "result limit" in structured["hint"]  # the text's own cut keeps its own sentence
    assert structured["pages_without_text"] == [2]


def test_the_images_the_results_own_cut_leaves_out_are_left_out_too(
    seeing_small: TestClient, local: Picky, store: Store, built: Path
) -> None:
    """The answer is cut to the result limit after the images are chosen: pages past that cut wait for the next call."""
    source = pdf(store, built, "harbour-mixed.pdf", ["A", None, "B", "C", None, "D"])
    text = "harbour " * 50
    local.scripted["read_document"] = reading(
        "x",
        ("page 1", text),
        ("page 3", text),
        ("page 4", text),
        ("page 6", text),
        kind="pdf",
        total_parts=6,
        pages_without_text=[2, 5],
    )
    answer = seen(read(seeing_small, source.virtual_path))

    structured = answer["structuredContent"]
    assert (structured["truncated"], structured["next_part"]) == (True, "page 3")
    assert [item["n"] for item in structured["shown"]] == [2]
    assert len(images(answer)) == 1
    assert sent_size(structured) <= 1024
    assert structured["hint"].endswith(" Page 2 comes with this answer as an image.")
    assert "Pages 2, 5" not in structured["hint"]
    assert structured["pages_without_text"] == [2]


def scripted_text(local: Picky, pad: int, scans: list[int], *pages: int) -> None:
    """Have the read give long parts for ``pages``, ``pad`` characters longer, and ``scans`` as pages without text."""
    local.scripted["read_document"] = reading(
        "x",
        *((f"page {n}", "w" * (250 + pad)) for n in pages),
        kind="pdf",
        total_parts=max([*pages, *scans]),
        pages_without_text=scans,
    )


def test_the_note_of_an_unshown_page_is_whole_however_full_the_text_is(
    seeing_small: TestClient, local: Picky, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Room is kept for the hint before the text is cut: no slack left over by a part may shorten it."""

    async def busy(*_: Any, **__: Any) -> None:
        raise Busy("executor_busy")

    source = pdf(store, built, "harbour-mixed.pdf", ["A", None, "B", "C", "D"])
    monkeypatch.setattr(tools_views, "prepared", busy)
    for pad in range(80):  # the bytes a part leaves over change with its length, so every slack is met
        scripted_text(local, pad, [2], 1, 3, 4, 5)
        structured = answered(read(seeing_small, source.virtual_path))
        assert structured["hint"].endswith(f" {UNSHOWN}"), pad
        assert (structured["truncated"], structured["shown"]) == (True, []), pad
        assert sent_size(structured) <= 1024, pad


def test_the_note_of_the_images_is_whole_however_full_the_text_is(
    seeing_small: TestClient, local: Picky, store: Store, built: Path
) -> None:
    """The same for the sentence that names the pages that come as images, and for a page that could not be shown."""
    source = pdf(store, built, "harbour-mixed.pdf", ["A", None, "B", "C", "D", "E"])
    for pad in range(120):
        scripted_text(local, pad, [2], 1, 3, 4, 5, 6)
        structured = answered(read(seeing_small, source.virtual_path))
        assert structured["hint"].endswith(" Page 2 comes with this answer as an image."), pad
        assert [item["n"] for item in structured["shown"]] == [2], pad
        assert sent_size(structured) <= 1024, pad


def test_a_cut_of_the_text_before_the_images_end_keeps_its_own_reason(
    seeing_small: TestClient, local: Picky, store: Store, built: Path
) -> None:
    """The images would end the answer at page 7, but the text is cut at the result limit at page 3: that is told."""
    source = pdf(store, built, "harbour-mixed.pdf", ["A", None, "B", None, None, None, None, None, "C"])
    scripted_text(local, 100, [2, 4, 5, 6, 7, 8], 1, 3, 9)
    answer = seen(read(seeing_small, source.virtual_path))

    structured = answer["structuredContent"]
    assert (structured["truncated"], structured["next_part"]) == (True, "page 3")
    assert [item["n"] for item in structured["shown"]] == [2]
    assert structured["pages_without_text"] == [2]
    assert "The answer is cut at the result limit (1 KB). Call read_document again" in structured["hint"]
    assert "images within the limit" not in structured["hint"]


def test_paging_moves_on_at_the_lowest_result_limit_however_long_the_notes(
    seeing_small: TestClient, local: Picky, store: Store, built: Path
) -> None:
    """Task 10 minor a: at 1 KB, the room kept for the notes of four images never leaves the answer without text.

    Text on pages 1 and 3, scans on 2, 4, 5 and 6, the read cut at page 7, the runner's own hint and paths of many
    lengths: the first part is kept, cut if it must be, so that ``next_part`` is never the page the call began at; the
    notes may then be shortened. Without the guard, names of about 100 to 150 characters stalled on page 1. (Past
    that, the path, the hint and four ``shown`` entries alone fill 1 KB, whatever room the notes are given.)
    """
    for length in range(20, 152, 3):
        name = f"harbour-{'q' * length}.pdf"
        source = pdf(store, built, name, ["A", None, "B", None, None, None, "C", "D"])
        local.scripted["read_document"] = reading(
            "x",
            ("page 1", "w" * 400),
            ("page 3", "w" * 400),
            kind="pdf",
            total_parts=8,
            pages_without_text=[2, 4, 5, 6],
            truncated=True,
            next_part="page 7",
            hint=(
                "pages 2, 4, 5, 6 of those looked at have no text layer (scans or pictures); do not treat them as "
                "blank. page 3 is longer than the result limit and is cut; its rest cannot be read by paging."
            ),
        )
        structured = answered(read(seeing_small, source.virtual_path))
        assert structured["parts"], length
        assert structured["next_part"] != "page 1", length
        assert sent_size(structured) <= 1024, length


# --- read_document: what the model is told ------------------------------------------------------------------------


def test_the_model_is_told_where_the_images_of_a_read_are(client: TestClient) -> None:
    """The description names the pictures it reads, the pages and slides that come as images, and view_file; no OCR."""
    tools = listed(client)
    text = tools["read_document"]["description"]
    assert "pictures (PNG, JPEG, WebP, GIF, BMP, TIFF, HEIC), which come back as the image" in text
    assert (
        "pages_without_text lists pages or slides with no text layer (scans, pictures); never treat them as blank. "
        "Such PDF pages and PowerPoint slides come back as images in the same answer (shown names them): read them "
        "from the images. To look at any page or slide, use view_file."
    ) in text
    assert "OCR" not in text
    assert "pillow, pypdfium2" in tools["run_python"]["description"]


def test_strips_are_one_to_three_and_hidden_from_the_model_by_its_text(client: TestClient) -> None:
    """The schema lets Ghostwriter set ``strips``; its description tells the model to leave it out."""
    schema = listed(client)["read_document"]["inputSchema"]
    strips = schema["properties"]["strips"]
    assert (strips["type"], strips["description"]) == ("integer", "Leave it out.")
    assert strips["anyOf"] == [{"type": "integer", "minimum": 1, "maximum": 3}, {"type": "null"}]
    assert schema["required"] == ["path"]


@pytest.mark.parametrize("strips", [0, 4])
def test_strips_outside_one_to_three_are_invalid_arguments(
    seeing: TestClient, store: Store, built: Path, strips: int
) -> None:
    """The same bounds as ``view_file``'s."""
    source = pdf(store, built, "harbour-report.pdf", SCANNED)
    assert refusal(read(seeing, source.virtual_path, strips=strips))[0] == "invalid_arguments"


# --- read_document of a clip: its transcript ---------------------------------------------------------------------

#: What a clip's read says of its frames when it has three and the call takes images.
SEE_THREE = "Its 3 frames can be looked at with view_file ('frame 3', 'at 01:24')."
NO_SPEECH = "No speech was found in this clip."
#: The three sentences of the invented tour, as the transcript's lines: a clip over a minute, a speaker for two of them.
TOUR_TEXT = "[00:00] Speaker 1: Welcome aboard.\n[01:24] Speaker 2: The quay opens at nine.\n[01:30] Thanks."
TOUR = [
    make_media.jsonl_record(0, 3, "Welcome aboard.", speaker="Speaker 1", index=0),
    make_media.jsonl_record(84, 88, "The quay opens at nine.", speaker="Speaker 2", index=1),
    make_media.jsonl_record(90, 91, "Thanks.", index=2),
]


def tour(
    client: TestClient,
    store: Store,
    built: Path,
    *,
    frames: int = 3,
    records: list[dict[str, Any]] | None = None,
    name: str = "harbour-tour.mp4",
) -> FileRecord:
    """A clip of Ghostwriter's chat with Nextext's artifacts of an invented tour, posted as Ghostwriter does.

    ``records`` are the JSON Lines (the three sentences of ``TOUR`` unless it says otherwise; none: no transcript
    artifact); ``frames`` the keyframes zip's frames (none: no zip).
    """
    source = put(store, name, CLIP_BYTES, p=PG)
    sentences = TOUR if records is None else records
    if sentences:
        lines = make_media.make_jsonl(built / "tour.jsonl", sentences).read_bytes()
        assert artifact(client, source.virtual_path, "nextext_jsonl", lines).status_code == 200
    if frames:
        zipped = make_media.make_keyframes_zip(built / "tour.zip", frames, times=[30.0 * n for n in range(frames)])
        assert artifact(client, source.virtual_path, "nextext_zip", zipped.read_bytes()).status_code == 200
    return source


def transcript_row(store: Store, source: FileRecord) -> Any:
    """The transcript row of a clip that was prepared."""
    (row,) = [rec for rec in store.derived_of(source) if rec.role == "transcript"]
    return row


def test_a_clip_reads_as_its_transcript(
    seeing: TestClient, local: Picky, store: Store, built: Path, audit_lines: AuditLines
) -> None:
    """One call: the stamped lines as text parts, no image block; the clip itself reaches no job."""
    source = tour(seeing, store, built)
    answer = seen(read(seeing, source.virtual_path))

    assert len(answer["content"]) == 1
    structured = answer["structuredContent"]
    assert (structured["kind"], structured["path"]) == ("transcript", source.virtual_path)
    assert structured["parts"] == [{"label": "lines 1-3", "text": TOUR_TEXT}]
    assert (structured["total_parts"], structured["pages_without_text"], structured["shown"]) == (3, [], [])
    assert (structured["truncated"], structured["next_part"], structured["hint"]) == (False, None, SEE_THREE)
    assert ".derived" not in json.dumps(answer)
    row = transcript_row(store, source)
    (job,) = [req for req in local.requests if req.tool == "read_document"]
    assert job.input_files == [row.relative_path]
    assert (job.args["path"], job.args["images"]) == (row.relative_path, False)
    assert all(source.name not in req.input_files for req in local.requests)  # the clip is no input of any job
    (event,) = read_events(audit_lines)
    assert (event["action"], event["outcome"]) == ("query", "ok")
    assert event["target"]["ids"] == [source.id, row.id]
    assert event["criteria"] == {"caller": "ghostwriter", "paged": False, "images": 0}


def test_a_clip_reads_the_same_without_the_header(
    seeing: TestClient, local: Picky, store: Store, built: Path, audit_lines: AuditLines
) -> None:
    """Review focus 4: a blind caller reads the transcript, as text, and is told of no frame nor of ``view_file``."""
    source = tour(seeing, store, built)
    blind = seen(read(seeing, source.virtual_path, headers=HG_TEXT))
    sighted = seen(read(seeing, source.virtual_path))

    assert len(blind["content"]) == 1
    structured = blind["structuredContent"]
    assert structured["parts"] == sighted["structuredContent"]["parts"] == [{"label": "lines 1-3", "text": TOUR_TEXT}]
    assert (structured["kind"], structured["shown"], structured["hint"]) == ("transcript", [], None)
    assert "view_file" not in json.dumps(blind)
    assert sighted["structuredContent"]["hint"] == SEE_THREE
    names = [tool["name"] for tool in result(rpc(seeing, "tools/list", headers=HG_TEXT))["tools"]]
    assert "view_file" not in names
    assert [req.args["images"] for req in local.requests if req.tool == "read_document"] == [False, False]
    first, second = read_events(audit_lines)
    assert first["target"]["ids"] == second["target"]["ids"]
    assert first["criteria"] == second["criteria"] == {"caller": "ghostwriter", "paged": False, "images": 0}


def test_a_clip_without_speech_says_so(
    seeing: TestClient, local: Picky, store: Store, built: Path, audit_lines: AuditLines
) -> None:
    """No transcript, no read job: the hint says there was no speech, and names ``view_file`` only when it can look."""
    silent = tour(seeing, store, built, records=[])
    sighted = seen(read(seeing, silent.virtual_path))
    blind = seen(read(seeing, silent.virtual_path, headers=HG_TEXT))

    for answer in (sighted, blind):
        assert len(answer["content"]) == 1
        structured = answer["structuredContent"]
        assert (structured["kind"], structured["path"]) == ("transcript", silent.virtual_path)
        assert (structured["parts"], structured["total_parts"], structured["pages_without_text"]) == ([], 0, [])
        assert (structured["shown"], structured["truncated"], structured["next_part"]) == ([], False, None)
    assert sighted["structuredContent"]["hint"] == f"{NO_SPEECH} {SEE_THREE}"
    assert blind["structuredContent"]["hint"] == NO_SPEECH
    assert local.tools() == ["prepare_file"]  # the prepare of the frames; nothing was read in a job
    assert [event["target"]["ids"] for event in read_events(audit_lines)] == [[silent.id], [silent.id]]
    assert [event["criteria"] for event in read_events(audit_lines)] == [
        {"caller": "ghostwriter", "paged": False, "images": 0}
    ] * 2


def test_a_clip_with_nothing_found_says_so_with_no_job(
    seeing: TestClient, local: Picky, store: Store, audit_lines: AuditLines
) -> None:
    """A clip with no artifact has no speech and no pictures: the manifest's own hint, and not a job ran."""
    bare = put(store, "quay-walk.mp4", CLIP_BYTES, p=PG)
    structured = seen(read(seeing, bare.virtual_path))["structuredContent"]

    assert (structured["kind"], structured["parts"], structured["total_parts"]) == ("transcript", [], 0)
    assert structured["hint"] == NOTHING_FOUND
    assert local.requests == []
    (event,) = read_events(audit_lines)
    assert event["target"]["ids"] == [bare.id]


def test_a_clip_of_one_frame_is_told_in_the_singular(seeing: TestClient, store: Store, built: Path) -> None:
    """One keyframe is "1 frame", never "1 frames", and is asked for by its number."""
    source = tour(seeing, store, built, frames=1)
    structured = seen(read(seeing, source.virtual_path))["structuredContent"]

    assert structured["hint"] == "Its 1 frame can be looked at with view_file ('frame 1')."


def test_audio_with_speech_reads_without_frames(seeing: TestClient, local: Picky, store: Store, built: Path) -> None:
    """A recording has no zip: its transcript is read, and the hint names no frame."""
    memo = tour(seeing, store, built, frames=0, name="voice-memo.m4a")
    structured = seen(read(seeing, memo.virtual_path))["structuredContent"]

    assert structured["parts"] == [{"label": "lines 1-3", "text": TOUR_TEXT}]
    assert structured["hint"] is None


@pytest.fixture
def seeing_4kb(settings: Settings, store: Store, audit: AuditLog, local: Picky) -> Iterator[TestClient]:
    """A client on that app whose answers may be 4 KB: the reader keeps 2 KB of them for its own words."""
    yield from served(dataclasses.replace(settings, result_max_kb=4), store, audit, local)


def test_a_long_transcript_pages_by_lines(
    seeing_4kb: TestClient, store: Store, built: Path, audit_lines: AuditLines
) -> None:
    """Following ``next_part`` reads on, line by line, until the last line: none twice, none left out."""
    paging = seeing_4kb
    sentences = [
        make_media.jsonl_record(2 * n, 2 * n + 1, f"Sentence number {n} of the invented tour.", index=n)
        for n in range(120)
    ]
    source = tour(paging, store, built, frames=0, records=sentences)
    lines: list[str] = []
    part: str | None = None
    calls = 0
    while True:
        structured = seen(read(paging, source.virtual_path, part))["structuredContent"]
        calls += 1
        assert structured["kind"] == "transcript"
        for piece in structured["parts"]:
            lines += piece["text"].split("\n")
        part = structured["next_part"]
        if not structured["truncated"]:
            break
        assert part is not None
        assert calls < 20
    assert calls > 1
    assert len(lines) == 120
    assert lines[0] == "[00:00] Sentence number 0 of the invented tour."
    assert lines[-1] == "[03:58] Sentence number 119 of the invented tour."
    assert [line[:7] for line in lines] == [f"[{(2 * n) // 60:02d}:{(2 * n) % 60:02d}]" for n in range(120)]
    assert [event["criteria"]["paged"] for event in read_events(audit_lines)] == [False] + [True] * (calls - 1)


def test_a_part_the_transcript_has_not_is_refused_and_recorded_with_the_clip_and_its_transcript(
    seeing: TestClient, store: Store, built: Path, audit_lines: AuditLines
) -> None:
    """The reader's refusal goes on as for any file; the event is an ``error`` of the code alone, naming both files."""
    source = tour(seeing, store, built)
    reason, text = refusal(read(seeing, source.virtual_path, "lines 40-50"))

    assert reason == "refused"
    assert text.startswith("no_such_part: ")
    assert ".derived" not in text
    (event,) = read_events(audit_lines)
    assert (event["action"], event["outcome"]) == ("query", "error")
    assert event["target"]["ids"] == [source.id, transcript_row(store, source).id]
    assert event["criteria"] == {"caller": "ghostwriter", "paged": True, "refused": "no_such_part"}


def test_a_transcript_the_job_cannot_find_does_not_name_its_path(
    seeing: TestClient, local: Picky, store: Store, built: Path, audit_lines: AuditLines
) -> None:
    """The runner's words name the file as the store keeps it; the model is told the transcript is gone, no path."""
    source = tour(seeing, store, built)
    seen(read(seeing, source.virtual_path))  # the first read prepares the clip
    row = transcript_row(store, source)
    local.status = "error"
    local.scripted["read_document"] = {"error": f"file_not_found: {CHAT}/{row.relative_path} is not in this workspace"}
    reason, text = refusal(read(seeing, source.virtual_path))

    assert (reason, text) == ("refused", "file_not_found: the transcript of this clip is not there now")
    _, event = read_events(audit_lines)
    assert (event["outcome"], event["target"]["ids"]) == ("error", [source.id, row.id])
    assert event["criteria"] == {"caller": "ghostwriter", "paged": False, "refused": "file_not_found"}
    assert row.relative_path not in json.dumps(audit_lines())


def test_a_transcript_that_goes_while_the_clip_is_read_is_made_again(
    seeing: TestClient, store: Store, built: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artifact posted again removes the transcript between the manifest and the read: one more prepare makes it."""
    source = tour(seeing, store, built)
    real = tools_views.prepared
    calls: list[int] = []

    async def racing(*args: Any, **kwargs: Any) -> Any:
        manifest = await real(*args, **kwargs)
        calls.append(1)
        if len(calls) == 1:  # the first prepare ends, and the artifact is posted again before the read looks
            for rec in store.derived_of(source):
                if rec.role in {"transcript", "frame"}:
                    store.remove_derived(rec)
        return manifest

    monkeypatch.setattr(tools_views, "prepared", racing)
    structured = seen(read(seeing, source.virtual_path))["structuredContent"]

    assert len(calls) == 2
    assert structured["parts"] == [{"label": "lines 1-3", "text": TOUR_TEXT}]
    assert structured["hint"] == SEE_THREE


# --- a clip is left out of every job of search_files ------------------------------------------------------------

#: What a search says when it left clips out, for one clip and for two.
NOT_SEARCHED = "Video and audio are not searched (1 file): read a clip's transcript with read_document."
TWO_NOT_SEARCHED = "Video and audio are not searched (2 files): read a clip's transcript with read_document."


def test_search_leaves_a_clip_out_of_its_job_and_counts_it_as_not_searched(
    client: TestClient, store: Store, executor: Scripted, audit_lines: AuditLines
) -> None:
    """The job is given the CSV and not the clip; the clip is counted in ``unsupported_files`` and the hint says so."""
    sheet = put(store, "quay-list.csv", p=PG)
    put(store, "harbour-tour.mp4", CLIP_BYTES, p=PG)
    executor.payload = {**hits((f"{CHAT}/quay-list.csv", "line 1", "match here")), "unsupported_files": 1}
    body = answered(find(client, "match"))

    (req,) = executor.requests
    assert req.input_files == ["quay-list.csv"]
    assert (body["files_searched"], body["unsupported_files"], body["truncated"]) == (1, 2, False)
    assert body["hint"] == NOT_SEARCHED
    (event,) = tool_events(audit_lines, "search_files")
    assert event["target"]["ids"] == [sheet.id]  # the job's files, which the clip was not


def test_search_counts_every_kind_of_clip_and_the_runners_hint_comes_first(
    client: TestClient, store: Store, executor: Scripted
) -> None:
    """A clip in a folder, in capitals or audio is one too; the runner's own hint and the file counts stay true."""
    put(store, "quay-list.csv", p=PG)
    put(store, "voice-memo.M4A", CLIP_BYTES, p=PG)
    store.mkdir(PG, f"{CHAT}/reports")
    put(store, "quay-walk.webm", CLIP_BYTES, directory=f"{CHAT}/reports", p=PG_SETUP)
    executor.payload = {**hits(), "hint": "There are more hits than the limit; narrow the query."}
    body = answered(find(client, "x"))

    assert [req.input_files for req in executor.requests] == [["quay-list.csv"]]
    assert body["unsupported_files"] == 2
    assert body["hint"] == f"There are more hits than the limit; narrow the query. {TWO_NOT_SEARCHED}"


def test_search_of_a_chat_of_clips_alone_runs_no_job(
    client: TestClient, store: Store, executor: Scripted, audit_lines: AuditLines
) -> None:
    """Nothing is left to look in: an answer, not a job, and no event, as for a chat with no files."""
    put(store, "harbour-tour.mp4", CLIP_BYTES, p=PG)
    body = answered(find(client, "x"))

    assert (body["hits"], body["files_searched"], body["unsupported_files"], body["truncated"]) == ([], 0, 1, False)
    assert body["hint"] == NOT_SEARCHED
    assert executor.requests == []
    assert tool_events(audit_lines, "search_files") == []


def test_search_only_counts_the_newest_files_that_can_be_searched(
    client: TestClient, store: Store, executor: Scripted, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 1000-file bound is over the files a job is given: a clip takes none of it, nor is it among the oldest."""
    monkeypatch.setattr("workbench.api.tools.MAX_INPUT_FILES", 2)
    tick(store, monkeypatch)
    put(store, "a-old.txt", p=PG)
    put(store, "b.txt", p=PG)
    put(store, "harbour-tour.mp4", CLIP_BYTES, p=PG)
    put(store, "c-new.txt", p=PG)
    executor.payload = hits()
    body = answered(find(client, "x"))

    assert executor.requests[0].input_files == ["c-new.txt", "b.txt"]
    assert body["hint"] == f"{NOT_SEARCHED} Searched the 2 newest of 3 files; the 1 oldest were not searched."


def test_a_search_over_a_clip_and_a_text_file_runs_the_real_runner_over_the_text_alone(
    seeing: TestClient, local: Picky, store: Store
) -> None:
    """With the real runner: the text is searched, the clip is not given to it, and its count is the model's to read."""
    put(store, "quay-notes.txt", b"The quay opens at nine.\n", p=PG)
    put(store, "harbour-tour.mp4", CLIP_BYTES, p=PG)
    body = answered(find(seeing, "quay"))

    (req,) = local.requests
    assert req.input_files == ["quay-notes.txt"]
    assert [hit["path"] for hit in body["hits"]] == [f"{CHAT}/quay-notes.txt"]
    assert (body["files_searched"], body["unsupported_files"]) == (1, 1)
    assert body["hint"] == NOT_SEARCHED
