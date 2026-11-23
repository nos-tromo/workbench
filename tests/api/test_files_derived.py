"""``POST /files/derived``: Ghostwriter streams Nextext's two artifacts of a clip in, as derived files of the clip.

The artifacts are invented: a jsonl body is one JSON object on a line, a zip body the four signature bytes and
zeros. Here the route's part: what it checks, what it answers, what it records and when, how it waits for the clip's
lock, and that nothing is left behind by any way out. What the store does with a staged artifact is ``test_store.py``'s.
"""

import asyncio
import contextlib
import dataclasses
import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import anyio
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect, Request
from starlette.types import Message

from tests.api.conftest import HD, HG, HG_B, PG, PG_SETUP, AuditLines, put
from tests.api.test_file_delete import delete, delete_events, output
from tests.api.test_files import drive
from tests.api.test_files_ghostwriter import P2, PURPOSE
from tests.api.test_store import derive_as
from tests.api.test_tools_documents import Scripted
from workbench.api import files as files_module
from workbench.api.audit import AuditLog
from workbench.api.body_limit import DERIVED_BODY_MAX
from workbench.api.jobs import JobQueue
from workbench.api.main import create_app
from workbench.api.metrics import Metrics
from workbench.api.store import ARTIFACT_EMPTY, ARTIFACT_NOT_ITS_ROLE, Store
from workbench.config import Settings
from workbench.models import DerivedSpec, FileRecord
from workbench.paths import Workspaces
from workbench.renders import JSONL_MAX_BYTES, ZIP_MAX_BYTES

MIB = 1024 * 1024
CLIP = b"synthetic clip bytes"
JSONL = b'{"text": "an invented sentence"}\n'
OTHER_JSONL = b'{"text": "another invented sentence"}\n'
ZIP = b"PK\x03\x04" + bytes(60)
HEADERS = {**HG, **PURPOSE}
NOT_FOUND = (404, {"detail": "not found"})
BUSY = (503, {"detail": "busy, retry later"})
UNSUPPORTED = "unsupported_format: the source is not an audio or video upload"
BODIES = {"nextext_jsonl": JSONL, "nextext_zip": ZIP}
TYPES = {"nextext_jsonl": "application/x-ndjson", "nextext_zip": "application/zip"}


@pytest.fixture
def executor(settings: Settings) -> Scripted:
    """An executor that is never asked: this route runs no job."""
    return Scripted(settings.data_dir)


@pytest.fixture
def queue(settings: Settings, store: Store, executor: Scripted) -> JobQueue:
    """The job queue, whose lock of a clip the route waits for."""
    return JobQueue(settings, executor, store, Metrics(settings.data_dir))


@pytest.fixture
def app(settings: Settings, store: Store, audit: AuditLog, queue: JobQueue) -> FastAPI:
    """The real app with a job queue."""
    return create_app(settings, store=store, audit=audit, jobs=queue)


def clip(store: Store, name: str = "harbour-tour.mp4") -> FileRecord:
    """Upload a clip of invented bytes to Ghostwriter's chat."""
    return put(store, name, CLIP)


def post(
    client: TestClient,
    path: str,
    role: str = "nextext_jsonl",
    body: bytes | None = None,
    headers: Mapping[str, Any] = HEADERS,
) -> Any:
    """Send an artifact as Ghostwriter does unless other headers are given: the raw body, its own type."""
    sent = BODIES[role] if body is None else body
    return client.post(
        "/files/derived",
        params={"source": path, "role": role},
        content=sent,
        headers={**headers, "Content-Type": TYPES[role]},
    )


def post_chunked(client: TestClient, path: str, role: str, chunks: Sequence[bytes]) -> Any:
    """Send an artifact as a body of no declared length, so that only the count of what arrives can stop it."""
    return client.post("/files/derived", params={"source": path, "role": role}, content=iter(chunks), headers=HEADERS)


def derived_dir(settings: Settings) -> Path:
    """Where the derived files of Ghostwriter's chat would be, made or not."""
    return Workspaces(settings.data_dir).chat_dir(PG_SETUP) / ".derived"


def on_disk(settings: Settings) -> list[str]:
    """The names of every file below the chat's derived folder, in order."""
    return sorted(each.name for each in derived_dir(settings).rglob("*") if each.is_file())


def parts_on_disk(settings: Settings) -> list[Path]:
    """The ``.part`` files below the chat's derived folder."""
    return sorted(derived_dir(settings).rglob("*.part"))


def derived_events(audit_lines: AuditLines) -> list[dict[str, Any]]:
    """The ``file.derived`` events, in order."""
    return [line for line in audit_lines() if line["operation"] == "file.derived"]


def nothing_left(settings: Settings, store: Store, source: FileRecord, audit_lines: AuditLines) -> None:
    """Check that no artifact, no ``.part``, no folder of them and no event was made."""
    assert store.derived_of(source) == []
    assert not derived_dir(settings).exists()
    assert derived_events(audit_lines) == []


def wait_for(condition: Callable[[], bool], seconds: float = 10.0) -> None:
    """Wait, from a test thread, until ``condition()`` holds."""
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


@contextlib.contextmanager
def holding(client: TestClient, queue: JobQueue, source: FileRecord) -> Iterator[Callable[[], None]]:
    """Hold the clip's lock on the app's own loop, as a prepare of it would; the block gets the function that lets go.

    The lock is let go when the block ends, whatever happens in it, so no waiting call outlives the test.
    """
    portal = client.portal
    assert portal is not None

    async def take() -> tuple[asyncio.Event, asyncio.Task[None]]:
        release, taken = asyncio.Event(), asyncio.Event()

        async def hold() -> None:
            async with queue.source_lock(source.id, 5):
                taken.set()
                await release.wait()

        task = asyncio.create_task(hold())
        await taken.wait()
        return release, task

    release, task = portal.call(take)

    async def let_go() -> None:
        release.set()
        await task

    def free() -> None:
        portal.call(let_go)

    try:
        yield free
    finally:
        free()


def part_is_complete(settings: Settings, size: int) -> bool:
    """Whether the clip's one ``.part`` holds all ``size`` bytes of its body."""
    parts = parts_on_disk(settings)
    return len(parts) == 1 and parts[0].stat().st_size == size


# --- what is kept ---------------------------------------------------------------------------------------------------


def test_an_artifact_is_kept_as_a_derived_file_of_its_clip(
    client: TestClient, store: Store, settings: Settings, audit_lines: AuditLines
) -> None:
    """The answer has four keys; the row is a derived file, hidden from the listing; one event names the clip and it."""
    source = clip(store)
    r = post(client, source.virtual_path)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"id", "role", "bytes", "replaced"}
    assert (body["role"], body["bytes"], body["replaced"]) == ("nextext_jsonl", len(JSONL), None)

    (row,) = store.derived_of(source)
    assert (row.id, row.role, row.mime, row.size) == (body["id"], "nextext_jsonl", TYPES[row.role], len(JSONL))
    assert (derived_dir(settings) / source.id / row.name).read_bytes() == JSONL
    listing = client.get("/files/list", params={"directory": "/workspace/chat-1"}, headers=HG).json()
    assert [entry["name"] for entry in listing["entries"]] == ["harbour-tour.mp4"]

    (event,) = derived_events(audit_lines)
    assert (event["action"], event["outcome"]) == ("modify", "ok")
    assert event["actor"] == {"user": "user-a", "via": "service"}
    assert event["target"] == {
        "kind": "file",
        "ids": [source.id, body["id"]],
        "count": 2,
        "container": "chat:chat-1",
    }
    assert event["criteria"] == {"caller": "ghostwriter", "role": "nextext_jsonl", "bytes": len(JSONL), "replaced": 0}
    assert event["purpose"] == {"reason": "ghostwriter chat"}
    assert event["request_id"] == "req-1"


def test_the_two_roles_are_kept_side_by_side(client: TestClient, store: Store) -> None:
    """A zip does not replace a jsonl: each role has its own row."""
    source = clip(store)
    first = post(client, source.virtual_path, "nextext_jsonl").json()
    second = post(client, source.virtual_path, "nextext_zip").json()
    assert second["replaced"] is None
    assert sorted((row.id, row.role) for row in store.derived_of(source)) == sorted(
        [(first["id"], "nextext_jsonl"), (second["id"], "nextext_zip")]
    )


@pytest.mark.parametrize("name", ["tour.mp4", "TOUR.MOV", "tour.m4v", "tour.webm", "tour.mkv", "tour.mp3", "tour.M4A"])
def test_any_audio_or_video_upload_is_a_clip(client: TestClient, store: Store, name: str) -> None:
    """The suffix decides, in any case."""
    source = clip(store, name)
    assert post(client, source.virtual_path).status_code == 200


# --- the source ---------------------------------------------------------------------------------------------------


def test_the_source_must_be_a_clip_upload(
    client: TestClient, store: Store, settings: Settings, audit_lines: AuditLines, tmp_path: Path
) -> None:
    """A PDF upload, and a tool's output named like a clip, answer 422 ``unsupported_format``: nothing is made."""
    document = put(store, "harbour-report.pdf", b"%PDF-1.4 invented")
    made = output(store, tmp_path, "harbour-tour.mp4")
    for path in (document.virtual_path, made.virtual_path):
        r = post(client, path)
        assert r.status_code == 422
        assert r.json() == {"detail": UNSUPPORTED}
    assert store.derived_of(document) == [] and store.derived_of(made) == []
    assert not derived_dir(settings).exists()
    assert derived_events(audit_lines) == []


@pytest.mark.parametrize(
    "spelling",
    [
        "/workspace/chat-2/harbour-tour.mp4",
        "/workspace/chat-1/../chat-2/harbour-tour.mp4",
        "/workspace/chat-1/missing.mp4",
        "/workspace/harbour-tour.mp4",
        "/workspace/chat-1/.derived/{id}/{id}.jsonl",
    ],
)
def test_another_chats_clip_or_a_derived_path_is_404(
    client: TestClient, store: Store, settings: Settings, audit_lines: AuditLines, spelling: str
) -> None:
    """Another chat's clip, a missing one, and a path into ``.derived`` are not found, by any spelling."""
    theirs = put(store, "harbour-tour.mp4", CLIP, directory="/workspace/chat-2", p=P2)
    own = clip(store, "quay-tour.mp4")
    r = post(client, spelling.format(id=own.id))
    assert (r.status_code, r.json()) == NOT_FOUND
    assert store.derived_of(theirs) == [] and store.derived_of(own) == []
    assert not derived_dir(settings).exists()
    assert derived_events(audit_lines) == []


def test_another_user_with_the_same_chat_id_finds_no_clip(
    client: TestClient, store: Store, settings: Settings, audit_lines: AuditLines
) -> None:
    """A clip is its user's: another user's request for the same chat and path finds nothing."""
    source = clip(store)
    r = post(client, source.virtual_path, headers={**HG_B, **PURPOSE})
    assert (r.status_code, r.json()) == NOT_FOUND
    nothing_left(settings, store, source, audit_lines)


def test_a_chat_folder_or_a_missing_session_is_400(
    client: TestClient, store: Store, settings: Settings, audit_lines: AuditLines
) -> None:
    """The chat folder is no file, and a request with no session has no chat: 400, nothing made."""
    source = clip(store)
    folder = post(client, "/workspace/chat-1")
    assert (folder.status_code, folder.json()) == (400, {"detail": "a file is inside a chat folder"})
    sessionless = post(client, source.virtual_path, headers={k: v for k, v in HEADERS.items() if k != "X-Session-Id"})
    assert (sessionless.status_code, sessionless.json()) == (400, {"detail": "invalid session"})
    nothing_left(settings, store, source, audit_lines)


def test_docint_cannot_post_artifacts(
    client: TestClient, store: Store, settings: Settings, audit_lines: AuditLines
) -> None:
    """The route is Ghostwriter's alone: docint's key, with the same user and chat, finds no such route."""
    source = clip(store)
    r = post(client, source.virtual_path, headers=HD)
    assert (r.status_code, r.json()) == NOT_FOUND
    nothing_left(settings, store, source, audit_lines)


@pytest.mark.parametrize("role", ["transcript", "frame", "pdf", "page", "image", "nextext", ""])
def test_only_the_two_artifact_roles_are_taken(
    client: TestClient, store: Store, settings: Settings, audit_lines: AuditLines, role: str
) -> None:
    """Any other role is a request that does not validate: 422 and nothing made, before the clip is looked at."""
    source = clip(store)
    r = client.post(
        "/files/derived", params={"source": source.virtual_path, "role": role}, content=JSONL, headers=HEADERS
    )
    assert r.status_code == 422
    assert "detail" in r.json() and isinstance(r.json()["detail"], list)
    nothing_left(settings, store, source, audit_lines)


# --- what the first bytes must be ----------------------------------------------------------------------------------


def a_line_of(size: int) -> bytes:
    """A JSON object on one line of exactly ``size`` bytes, without its line break."""
    frame = b'{"text": ""}'
    return b'{"text": "' + b"x" * (size - len(frame)) + b'"}'


@pytest.mark.parametrize(
    ("role", "body", "reason"),
    [
        ("nextext_zip", b"", ARTIFACT_EMPTY),
        ("nextext_jsonl", b"", ARTIFACT_EMPTY),
        ("nextext_zip", b"not a zip at all", ARTIFACT_NOT_ITS_ROLE),
        ("nextext_zip", b"PK\x03", ARTIFACT_NOT_ITS_ROLE),
        ("nextext_zip", JSONL, ARTIFACT_NOT_ITS_ROLE),
        ("nextext_jsonl", b"not json\n", ARTIFACT_NOT_ITS_ROLE),
        ("nextext_jsonl", b"[1, 2]\n", ARTIFACT_NOT_ITS_ROLE),
        ("nextext_jsonl", b"\n" + JSONL, ARTIFACT_NOT_ITS_ROLE),
        ("nextext_jsonl", ZIP, ARTIFACT_NOT_ITS_ROLE),
        ("nextext_jsonl", b"[" * 60_000 + b"\n", ARTIFACT_NOT_ITS_ROLE),
        ("nextext_jsonl", b"[" * 100_000 + b"\n", ARTIFACT_NOT_ITS_ROLE),
        ("nextext_jsonl", b"\xff\xfe\xfd\n", ARTIFACT_NOT_ITS_ROLE),
        ("nextext_jsonl", a_line_of(64 * 1024 + 1) + b"\n", ARTIFACT_NOT_ITS_ROLE),
        ("nextext_jsonl", a_line_of(64 * 1024 + 1), ARTIFACT_NOT_ITS_ROLE),
    ],
)
def test_an_artifact_must_look_like_its_role(
    client: TestClient,
    store: Store,
    settings: Settings,
    audit_lines: AuditLines,
    role: str,
    body: bytes,
    reason: str,
) -> None:
    """A zip must start with its signature and a jsonl with one JSON object; the answer is one of two fixed texts."""
    source = clip(store)
    r = post(client, source.virtual_path, role, body)
    assert (r.status_code, r.json()) == (422, {"detail": reason})
    nothing_left(settings, store, source, audit_lines)


@pytest.mark.parametrize(
    "body",
    [
        a_line_of(64 * 1024) + b"\n" + b"anything after the first line is the executor's to read\n",
        a_line_of(64 * 1024),
        JSONL.rstrip(b"\n"),
        JSONL.replace(b"\n", b"\r\n"),
    ],
)
def test_the_first_line_is_all_the_api_reads(client: TestClient, store: Store, body: bytes) -> None:
    """A first line of at most 64 KiB that is a JSON object passes, with or without its break; the rest is not read."""
    source = clip(store)
    r = post(client, source.virtual_path, "nextext_jsonl", body)
    assert r.status_code == 200
    (row,) = store.derived_of(source)
    assert row.size == len(body)


def test_a_check_that_finds_the_chat_gone_is_404_not_422(
    client: TestClient,
    store: Store,
    settings: Settings,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chat deleted while the first bytes are read (the ``.part`` is gone) is a gone clip, not a bad artifact."""
    source = clip(store)

    def vanished(_: Path) -> None:
        raise FileNotFoundError

    monkeypatch.setattr(files_module, "_artifact_check", lambda _role: vanished)
    r = post(client, source.virtual_path)
    assert (r.status_code, r.json()) == NOT_FOUND
    nothing_left(settings, store, source, audit_lines)


# --- the bounds ---------------------------------------------------------------------------------------------------


def test_a_jsonl_past_its_bound_is_413(
    client: TestClient, store: Store, settings: Settings, audit_lines: AuditLines
) -> None:
    """A jsonl of 8 MiB and a byte, sent chunked, is refused as it is read, a zip's bound being the larger."""
    source = clip(store)
    r = post_chunked(client, source.virtual_path, "nextext_jsonl", [b"x" * JSONL_MAX_BYTES, b"x"])
    assert (r.status_code, r.json()) == (413, {"detail": "file too large"})
    nothing_left(settings, store, source, audit_lines)


def test_a_jsonl_at_its_bound_is_kept(client: TestClient, store: Store) -> None:
    """The bound itself is allowed."""
    source = clip(store)
    body = JSONL + b"x" * (JSONL_MAX_BYTES - len(JSONL))
    assert post(client, source.virtual_path, "nextext_jsonl", body).status_code == 200
    (row,) = store.derived_of(source)
    assert row.size == JSONL_MAX_BYTES


def test_a_zip_at_its_bound_is_kept_and_one_past_it_is_413(
    client: TestClient, store: Store, settings: Settings, audit_lines: AuditLines
) -> None:
    """A zip of 32 MiB is kept; one that grows past it, with no length declared, is stopped as it arrives."""
    source = clip(store)
    body = ZIP + b"\0" * (ZIP_MAX_BYTES - len(ZIP))
    assert post(client, source.virtual_path, "nextext_zip", body).status_code == 200
    (row,) = store.derived_of(source)
    assert row.size == ZIP_MAX_BYTES

    again = clip(store, "quay-tour.mp4")
    r = post_chunked(client, again.virtual_path, "nextext_zip", [body, b"\0"])
    assert (r.status_code, r.json()) == (413, {"detail": "file too large"})
    assert store.derived_of(again) == []
    assert on_disk(settings) == [row.name]
    assert [event["target"]["ids"][0] for event in derived_events(audit_lines)] == [source.id]


def test_a_declared_length_past_the_largest_bound_is_refused_before_any_byte(
    client: TestClient, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``Content-Length`` of 32 MiB and a byte is 413 before the route runs: the store is never asked."""
    source = clip(store)

    def never(*_: object, **__: object) -> None:
        raise AssertionError("the route must not run")

    monkeypatch.setattr(store, "stage_derived", never)
    r = client.post(
        "/files/derived",
        params={"source": source.virtual_path, "role": "nextext_zip"},
        content=b"x",
        headers={**HEADERS, "Content-Length": str(ZIP_MAX_BYTES + 1)},
    )
    assert (r.status_code, r.json()) == (413, {"detail": "file too large"})


def test_the_derived_bound_is_the_zips_and_its_413_is_the_files() -> None:
    """``BodyLimit`` knows the route: its bound is the zip's, its text the upload's; no other path has it."""
    assert DERIVED_BODY_MAX == ZIP_MAX_BYTES == 32 * MIB
    refused = drive(path="/files/derived", declared=DERIVED_BODY_MAX + 1, chunks=[b"x"])
    assert (refused.status, refused.app_calls, refused.receives) == (413, 0, 0)
    assert b'"file too large"' in refused.sent[1]["body"]
    allowed = drive(path="/files/derived", declared=DERIVED_BODY_MAX, chunks=[b"x"])
    assert (allowed.status, allowed.app_calls) == (200, 1)
    counted = drive(path="/files/derived", declared=None, chunks=[b"x" * DERIVED_BODY_MAX, b"x"])
    assert isinstance(counted.error, HTTPException)
    assert (counted.error.status_code, counted.error.detail) == (413, "file too large")
    elsewhere = drive(path="/files/derived", method="DELETE", declared=2 * MIB + 1, chunks=[b"x"])
    assert elsewhere.status == 413 and b'"request too large"' in elsewhere.sent[1]["body"]


def test_the_quota_holds(settings: Settings, store: Store, audit: AuditLog, audit_lines: AuditLines) -> None:
    """The user's quota counts the artifacts: past it, 507 and nothing is left of the artifact that did not fit."""
    small = dataclasses.replace(settings, workspace_max_mb=1)
    jobs = JobQueue(small, Scripted(small.data_dir), store, Metrics(small.data_dir))
    source = clip(store)
    with TestClient(create_app(small, store=store, audit=audit, jobs=jobs)) as client:
        first = post(client, source.virtual_path, "nextext_jsonl", JSONL + b"x" * (600 * 1024))
        assert first.status_code == 200
        over = post(client, source.virtual_path, "nextext_zip", ZIP + bytes(600 * 1024))
        assert (over.status_code, over.json()) == (507, {"detail": "workspace quota exceeded"})
    assert [row.role for row in store.derived_of(source)] == ["nextext_jsonl"]
    assert len(on_disk(settings)) == 1
    assert len(derived_events(audit_lines)) == 1


# --- a second artifact of a role ---------------------------------------------------------------------------------


def test_a_second_artifact_replaces_the_first_and_says_so(
    client: TestClient, store: Store, settings: Settings, audit_lines: AuditLines
) -> None:
    """``replaced`` is the first one's id, the event names the clip, the new row and the replaced; its file is gone."""
    source = clip(store)
    first = post(client, source.virtual_path, "nextext_jsonl", JSONL).json()
    zipped = post(client, source.virtual_path, "nextext_zip").json()
    second = post(client, source.virtual_path, "nextext_jsonl", OTHER_JSONL).json()

    assert second["replaced"] == first["id"]
    assert second["id"] != first["id"] and second["bytes"] == len(OTHER_JSONL)
    assert sorted(row.id for row in store.derived_of(source)) == sorted([second["id"], zipped["id"]])
    assert len(on_disk(settings)) == 2
    event = derived_events(audit_lines)[-1]
    assert event["target"]["ids"] == [source.id, second["id"], first["id"]]
    assert event["target"]["count"] == 3
    assert event["criteria"] == {
        "caller": "ghostwriter",
        "role": "nextext_jsonl",
        "bytes": len(OTHER_JSONL),
        "replaced": 1,
    }


def test_a_second_artifact_also_takes_what_was_made_from_the_first(
    client: TestClient, store: Store, settings: Settings, audit_lines: AuditLines, tmp_path: Path
) -> None:
    """The transcript and the frames made from the old input go too, and are named; ``replaced`` is the artifact's."""
    source = clip(store)
    first = post(client, source.virtual_path, "nextext_jsonl", JSONL).json()
    transcript = derive_as(store, tmp_path, source, "transcript", "text/plain")
    frame = derive_as(store, tmp_path, source, "frame", "image/jpeg")
    second = post(client, source.virtual_path, "nextext_jsonl", OTHER_JSONL).json()

    assert second["replaced"] == first["id"]
    assert [row.id for row in store.derived_of(source)] == [second["id"]]
    event = derived_events(audit_lines)[-1]
    assert event["target"]["ids"][:2] == [source.id, second["id"]]
    assert sorted(event["target"]["ids"][2:]) == sorted([first["id"], transcript.id, frame.id])
    assert event["criteria"]["replaced"] == 3
    assert len(on_disk(settings)) == 1


def test_a_new_artifact_clears_a_remembered_refusal(client: TestClient, store: Store) -> None:
    """The clip's input is new, so a refusal remembered for it is forgotten; a refused artifact leaves it."""
    source = clip(store)
    store.remember_refusal(source, "unreadable_file: invented refusal")
    assert post(client, source.virtual_path, "nextext_zip", b"not a zip").status_code == 422
    assert store.refusal_of(source) == "unreadable_file: invented refusal"

    assert post(client, source.virtual_path).status_code == 200
    assert store.refusal_of(source) is None


# --- the clip's lock ----------------------------------------------------------------------------------------------


def test_a_replace_waits_for_the_clips_lock(
    client: TestClient, queue: JobQueue, store: Store, settings: Settings, audit_lines: AuditLines
) -> None:
    """The body is read whole while the lock is held by another call; the answer comes only after it is let go."""
    source = clip(store)
    with ThreadPoolExecutor(1) as pool, holding(client, queue, source) as release:
        waiting = pool.submit(post, client, source.virtual_path, "nextext_jsonl", JSONL)
        wait_for(lambda: part_is_complete(settings, len(JSONL)))
        time.sleep(0.2)
        assert not waiting.done()
        assert store.derived_of(source) == []
        assert derived_events(audit_lines) == []
        release()
        r = waiting.result(timeout=10)
    assert r.status_code == 200
    assert [row.id for row in store.derived_of(source)] == [r.json()["id"]]
    assert parts_on_disk(settings) == []
    assert len(derived_events(audit_lines)) == 1


def test_a_lock_held_past_its_wait_is_503_and_leaves_no_part(
    client: TestClient,
    queue: JobQueue,
    store: Store,
    settings: Settings,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Past ``SOURCE_LOCK_WAIT_S`` (lowered here) the answer is busy, and the staged ``.part`` is removed."""
    monkeypatch.setattr(files_module, "SOURCE_LOCK_WAIT_S", 0.1)
    source = clip(store)
    with holding(client, queue, source):
        r = post(client, source.virtual_path)
    assert (r.status_code, r.json()) == BUSY
    nothing_left(settings, store, source, audit_lines)


def test_a_replace_that_waits_on_a_prepare_replaces_after_it(
    client: TestClient, queue: JobQueue, store: Store, settings: Settings, audit_lines: AuditLines
) -> None:
    """Posted again while the clip's lock is held (a prepare), the first artifact stays until the lock is let go."""
    source = clip(store)
    first = post(client, source.virtual_path, "nextext_jsonl", JSONL).json()
    with ThreadPoolExecutor(1) as pool, holding(client, queue, source) as release:
        waiting = pool.submit(post, client, source.virtual_path, "nextext_jsonl", OTHER_JSONL)
        wait_for(lambda: part_is_complete(settings, len(OTHER_JSONL)))
        time.sleep(0.2)
        assert [row.id for row in store.derived_of(source)] == [first["id"]]
        release()
        r = waiting.result(timeout=10)
    assert r.json()["replaced"] == first["id"]
    assert [row.id for row in store.derived_of(source)] == [r.json()["id"]]
    assert len(on_disk(settings)) == 1


# --- a clip that goes while its artifact is on the way -----------------------------------------------------------


def test_a_clip_taken_back_while_its_artifact_waits_is_gone(
    client: TestClient, queue: JobQueue, store: Store, settings: Settings, audit_lines: AuditLines
) -> None:
    """Ghostwriter takes the clip back while its artifact waits for the lock: the artifact is 404 and nothing stays."""
    source = clip(store)
    with ThreadPoolExecutor(1) as pool, holding(client, queue, source) as release:
        waiting = pool.submit(post, client, source.virtual_path)
        wait_for(lambda: part_is_complete(settings, len(JSONL)))
        assert delete(client, source.virtual_path).json() == {"deleted": 1, "renders": 0}
        release()
        r = waiting.result(timeout=10)
    assert (r.status_code, r.json()) == NOT_FOUND
    assert store.files_in_chat(PG) == []
    assert on_disk(settings) == [] and parts_on_disk(settings) == []
    assert derived_events(audit_lines) == []


def test_a_taken_back_clip_takes_its_artifacts_with_it(
    client: TestClient, store: Store, settings: Settings, audit_lines: AuditLines
) -> None:
    """Both artifacts go with the clip, named in its event; the clip's path then finds no route for an artifact."""
    source = clip(store)
    jsonl = post(client, source.virtual_path, "nextext_jsonl").json()
    zipped = post(client, source.virtual_path, "nextext_zip").json()
    assert len(on_disk(settings)) == 2

    r = delete(client, source.virtual_path)
    assert (r.status_code, r.json()) == (200, {"deleted": 1, "renders": 2})
    assert store.derived_of(source) == []
    assert on_disk(settings) == []
    assert not (derived_dir(settings) / source.id).exists()
    (event,) = delete_events(audit_lines)
    assert sorted(event["target"]["ids"]) == sorted([source.id, jsonl["id"], zipped["id"]])
    assert event["criteria"]["renders"] == 2

    again = post(client, source.virtual_path)
    assert (again.status_code, again.json()) == NOT_FOUND
    assert len(derived_events(audit_lines)) == 2


# --- the purpose and the event --------------------------------------------------------------------------------


def test_derived_needs_a_purpose(
    settings: Settings, store: Store, audit: AuditLog, queue: JobQueue, audit_lines: AuditLines
) -> None:
    """With ``modify`` requiring one, an artifact without a purpose is 428, recorded denied, and not read."""
    required = dataclasses.replace(settings, require_purpose=frozenset({"modify"}))
    source = clip(store)
    with TestClient(create_app(required, store=store, audit=audit, jobs=queue)) as strict:
        r = post(strict, source.virtual_path, headers={**HG, "X-Request-ID": "req-1"})
        assert (r.status_code, r.json()) == (428, {"detail": "a stated purpose is required"})
        (denied,) = derived_events(audit_lines)
        assert (denied["action"], denied["outcome"], denied["request_id"]) == ("modify", "denied", "req-1")
        assert "target" not in denied
        assert denied["criteria"] == {
            "caller": "ghostwriter",
            "refused": "purpose_required",
            "container": "chat:chat-1",
        }
        assert store.derived_of(source) == []
        assert not derived_dir(settings).exists()

        assert post(strict, source.virtual_path).status_code == 200


def test_an_artifact_whose_event_cannot_be_written_is_not_kept(
    client: TestClient, store: Store, settings: Settings, broken_audit: None
) -> None:
    """Fail closed: with no event, the artifact is not visible, and no ``.part`` is left."""
    source = clip(store)
    r = post(client, source.virtual_path)
    assert (r.status_code, r.json()) == (500, {"detail": "audit unavailable"})
    assert store.derived_of(source) == []
    assert not derived_dir(settings).exists()


# --- the body, and the ways out ----------------------------------------------------------------------------------


def a_request(chunks: Sequence[bytes], *, disconnect: bool = False) -> tuple[Request, list[int]]:
    """A request whose body comes in ``chunks`` and then ends or the client goes; and how many messages were asked."""
    pending: list[Message] = [{"type": "http.request", "body": chunk, "more_body": True} for chunk in chunks]
    pending.append({"type": "http.disconnect"} if disconnect else {"type": "http.request", "body": b""})
    asked = [0]

    async def receive() -> Message:
        asked[0] += 1
        return pending.pop(0)

    return Request({"type": "http", "headers": []}, receive), asked


def test_a_client_that_goes_mid_body_is_a_quiet_400(
    app: FastAPI,
    client: TestClient,
    store: Store,
    settings: Settings,
    audit_lines: AuditLines,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Through the whole app, a client that goes while its body comes is answered as an upload's is, without a log.

    The answer is a fixed 400, no event is written, the ``.part`` is removed, and nothing is logged as an error: a
    client that goes is not a fault of the server.
    """
    source = clip(store)
    portal = client.portal
    assert portal is not None
    pending: list[Message] = [{"type": "http.request", "body": JSONL, "more_body": True}, {"type": "http.disconnect"}]
    sent: list[Message] = []

    async def receive() -> Message:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    query = urlencode({"source": source.virtual_path, "role": "nextext_jsonl"})
    headers = [(name.lower().encode(), value.encode()) for name, value in HEADERS.items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/files/derived",
        "root_path": "",
        "query_string": query.encode(),
        "headers": headers,
    }
    with caplog.at_level(logging.DEBUG):
        portal.call(app, scope, receive, send)
    started = next(message for message in sent if message["type"] == "http.response.start")
    assert started["status"] == 400
    body = b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body")
    assert json.loads(body) == {"detail": files_module.BODY_NOT_READ}
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
    nothing_left(settings, store, source, audit_lines)


def test_the_body_is_read_in_the_pieces_asked_for_and_one_chunk_at_a_time() -> None:
    """``read(n)`` gives at most ``n`` bytes, takes the next chunk when the last is used up, and ends with ``b''``."""
    request, asked = a_request([b"abcde", b"fg"])
    body = files_module._Body(request)

    async def read(size: int) -> bytes:
        return await anyio.to_thread.run_sync(body.read, size)

    async def go() -> list[tuple[bytes, int]]:
        seen = []
        for size in (2, 2, 2, 2, 2, 2):
            seen.append((await read(size), asked[0]))
        return seen

    assert asyncio.run(go()) == [(b"ab", 1), (b"cd", 1), (b"e", 1), (b"fg", 2), (b"", 3), (b"", 3)]


def test_a_client_that_goes_while_its_body_is_read_leaves_nothing(
    store: Store, settings: Settings, audit_lines: AuditLines
) -> None:
    """The stage stops with the client's disconnect, and its ``.part`` and folders are removed."""
    source = clip(store)
    request, _ = a_request([JSONL], disconnect=True)
    spec = DerivedSpec("nextext_jsonl", 0, TYPES["nextext_jsonl"], None, None, {})

    async def go() -> None:
        await anyio.to_thread.run_sync(
            partial(
                store.stage_derived,
                source,
                files_module._Body(request),
                spec,
                max_bytes=JSONL_MAX_BYTES,
                quota_bytes=10**9,
                check=lambda _: None,
            )
        )

    with pytest.raises(ClientDisconnect):
        asyncio.run(go())
    nothing_left(settings, store, source, audit_lines)


def test_a_call_that_is_cancelled_while_it_waits_leaves_nothing(
    store: Store, settings: Settings, audit: AuditLog, queue: JobQueue, audit_lines: AuditLines
) -> None:
    """A call that ends while it waits for the lock (its task cancelled) drops the artifact it staged."""
    source = clip(store)
    audit.start({})
    try:

        async def go() -> None:
            holding, release = asyncio.Event(), asyncio.Event()

            async def hold() -> None:
                async with queue.source_lock(source.id, 5):
                    holding.set()
                    await release.wait()

            holder = asyncio.create_task(hold())
            await holding.wait()
            request, _ = a_request([JSONL])
            call = asyncio.create_task(
                files_module.store_derived(
                    source=source.virtual_path,
                    role="nextext_jsonl",
                    request=request,
                    p=PG,
                    settings=settings,
                    store=store,
                    audit=audit.bound(None, None),
                    jobs=queue,
                )
            )
            async with asyncio.timeout(10):
                while not part_is_complete(settings, len(JSONL)):
                    await asyncio.sleep(0.01)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            release.set()
            await holder

        asyncio.run(go())
    finally:
        audit.close()
    nothing_left(settings, store, source, audit_lines)
