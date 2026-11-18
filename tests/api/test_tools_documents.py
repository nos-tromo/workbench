"""The ``read_document`` and ``search_files`` tools on the MCP door, as Ghostwriter calls them.

The executor is scripted. Its own work is tested in ``tests/executor``; here the executor answers what a test tells
it to, and the api's part is checked: which files go to the job, what is recorded, what the model is
told, what is capped, and that no error carries more than a short fixed text.
"""

import dataclasses
import io
import itertools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, override

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api.conftest import CHAT, FORMER, HG, HG_B, PG, PG_SETUP, AuditLines, put, tick, unrecorded
from tests.api.mcp_calls import Headers, acall, answered, refusal, refused, result, rpc, sent_size, unavailable
from tests.api.test_jobs import FakeClient
from workbench.api.audit import AuditLog
from workbench.api.jobs import JobQueue
from workbench.api.main import create_app
from workbench.api.metrics import Metrics
from workbench.api.store import Store
from workbench.config import Settings
from workbench.models import Principal
from workbench.protocol import JobRequest, JobResult

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


def read(client: TestClient, path: str, part: str | None = None, headers: Headers = HG) -> httpx2.Response:
    """Call ``read_document`` as Ghostwriter does, on the MCP door."""
    arguments: dict[str, Any] = {"path": path}
    if part is not None:
        arguments["part"] = part
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
    assert req.args == {"path": "review.docx", "part": "paragraph 1", "budget_s": 86}
    assert (req.caller, req.user_id, req.chat_id) == ("ghostwriter", "user-a", "chat-1")
    assert req.max_chars == 16 * 1024
    assert answer == {
        "path": f"{CHAT}/review.docx",
        "kind": "docx",
        "parts": [{"label": "paragraph 1", "text": "Hello"}],
        "total_parts": 1,
        "pages_without_text": [],
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
    assert [r.args for r in executor.requests] == [{"path": "a.txt", "part": None, "budget_s": 86}] * 2


def test_a_json_file_is_read_like_any_document(client: TestClient, store: Store, executor: Scripted) -> None:
    """The file goes to the job as it is; its kind and the indented lines come back as the runner gave them."""
    put(store, "orders.json", b'{"order":"A-0001","qty":2}', p=PG)
    executor.payload = reading("ignored", ("lines 1-4", '{\n  "order": "A-0001",\n  "qty": 2\n}'), kind="json")
    answer = answered(read(client, f"{CHAT}/orders.json"))
    (req,) = executor.requests
    assert req.input_files == ["orders.json"]
    assert req.args == {"path": "orders.json", "part": None, "budget_s": 86}
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
    assert event["criteria"] == {"caller": "ghostwriter", "paged": True}
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


def test_pages_without_text_and_the_ocr_hint_pass_through(client: TestClient, store: Store, executor: Scripted) -> None:
    """The runner's hint is the model's; the pages are listed, and never in ``parts``."""
    put(store, "scan.pdf", p=PG)
    executor.payload = reading(
        "x",
        ("page 1", "text"),
        kind="pdf",
        pages_without_text=[2],
        hint="page 2 of those looked at have no text layer.",
    )
    body = answered(read(client, f"{CHAT}/scan.pdf"))
    assert body["pages_without_text"] == [2]
    assert [p["label"] for p in body["parts"]] == ["page 1"]
    assert body["hint"] == "page 2 of those looked at have no text layer."


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
