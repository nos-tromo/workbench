"""Ghostwriter's file routes: upload, view and list, each held to the request's chat, with purpose and request id.

The tools that take a path are held to the same chat by the store; here they are called as the MCP door will call them.
"""

import asyncio
import dataclasses
import io
import shutil
from collections.abc import Callable, Mapping
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from tests.api.conftest import CALL, DOCINT_KEY, FORMER, GHOSTWRITER_KEY, HG, PG, put, unrecorded
from tests.api.test_tools_documents import Scripted
from workbench.api.audit import AuditLog
from workbench.api.auth import require_file_caller
from workbench.api.errors import describe
from workbench.api.jobs import JobQueue
from workbench.api.main import create_app
from workbench.api.metrics import Metrics
from workbench.api.store import NotFound, Store
from workbench.api.tools import ReadDocumentRequest, read_document
from workbench.api.tools_mcp import CallJobs
from workbench.api.tools_templates import InspectTemplateRequest, inspect_template
from workbench.config import Settings
from workbench.layout import WORKSPACES
from workbench.models import FileRecord, Principal
from workbench.paths import Workspaces

CSV = b"author,posts\nuser-a,12\n"
P2 = Principal("ghostwriter", "user-a", "chat-2", confined=True)
PURPOSE = {"X-Audit-Purpose": "reason=ghostwriter+chat", "X-Request-ID": "req-1"}
CHAT = "/workspace/chat-1"
NOT_FOUND = (404, {"detail": "not found"})
MIB = 1024 * 1024

AuditLines = Callable[[], list[dict[str, Any]]]


def upload(
    client: TestClient,
    name: str = "quarterly-figures.csv",
    *,
    directory: str = CHAT,
    headers: Mapping[str, Any] = HG,
) -> Any:
    """Upload :data:`CSV` as ``name``, as Ghostwriter does unless other headers are given."""
    return client.post("/files/upload", params={"directory": directory}, files={"file": (name, CSV)}, headers=headers)


def view(client: TestClient, path: str, headers: Mapping[str, Any] = HG) -> Any:
    """View ``path``, as Ghostwriter does unless other headers are given."""
    return client.get("/files/view", params={"path": path}, headers=headers)


def keep(store: Store, p: Principal, name: str = "q.csv") -> FileRecord:
    """Store :data:`CSV` as ``name`` in ``p``'s own chat, without going through the api."""
    return store.add_upload(
        p, f"/workspace/{p.chat_id}", name, io.BytesIO(CSV), max_bytes=MIB, quota_bytes=10 * MIB, record=unrecorded
    )


def file_events(audit_lines: AuditLines) -> list[dict[str, Any]]:
    """The audit events about files, in order."""
    return [line for line in audit_lines() if line["operation"].startswith("file.")]


# --- upload, view and list -----------------------------------------------------------------------------------------


def test_the_ghostwriter_uploads_into_its_chat_and_gets_the_id(client: TestClient, store: Store) -> None:
    """Its upload lands in its chat as its own file, and the answer names the id beside the path and the size."""
    r = upload(client)
    rec = store.get(PG, "/workspace/chat-1/quarterly-figures.csv")
    assert (r.status_code, r.json()) == (200, {"path": rec.virtual_path, "size": len(CSV), "id": rec.id})
    assert rec.caller == "ghostwriter"


@pytest.mark.parametrize(
    "directory", ["/workspace", "/workspace/chat-2", "/workspace/chat-1/sub", "/workspace/default", "/etc"]
)
def test_an_upload_outside_its_chat_is_not_found(
    client: TestClient, store: Store, settings: Settings, audit_lines: AuditLines, directory: str
) -> None:
    """Any folder but the chat's own is not found, one inside it too: nothing is stored, made or recorded."""
    r = upload(client, directory=directory)
    assert (r.status_code, r.json()) == NOT_FOUND
    assert store.files_in_chat(PG) == [] and store.files_in_chat(P2) == []
    assert not (settings.data_dir / WORKSPACES / "ghostwriter").exists()
    assert file_events(audit_lines) == []
    assert upload(client).status_code == 200


def test_a_name_with_spaces_umlauts_and_a_hash_round_trips(client: TestClient) -> None:
    """A name users really give comes back exactly in the path, and the view finds the file by it."""
    name = "Übersicht März #1 (100%).csv"
    r = upload(client, name)
    assert (r.status_code, r.json()["path"]) == (200, f"{CHAT}/{name}")
    viewed = view(client, r.json()["path"])
    assert (viewed.status_code, viewed.content) == (200, CSV)


@pytest.mark.parametrize(
    "path", ["/workspace/chat-2/q.csv", "/workspace/chat-1/../chat-2/q.csv", "/workspace/q.csv", "/etc/passwd"]
)
def test_another_chats_file_is_not_found(client: TestClient, store: Store, audit_lines: AuditLines, path: str) -> None:
    """The user's file in another chat is not found, by any spelling of its path: nothing is disclosed or recorded."""
    keep(store, P2)
    own = keep(store, PG, "mine.csv")
    r = view(client, path)
    assert (r.status_code, r.json()) == NOT_FOUND
    assert file_events(audit_lines) == []
    assert view(client, own.virtual_path).content == CSV


@pytest.mark.parametrize("planted", ["file", "folder", "chat"])
def test_a_link_in_its_chat_is_not_found_and_never_followed(
    client: TestClient, store: Store, settings: Settings, audit_lines: AuditLines, planted: str
) -> None:
    """A link on a file, on a folder in the chat or as the chat folder itself is 404 for Ghostwriter.

    Nothing behind the link is read or written.
    """
    rec = keep(store, PG)
    outside = settings.data_dir / "outside"
    outside.mkdir()
    (outside / "q.csv").write_text("not yours")
    real = Workspaces(settings.data_dir).resolve(PG, rec.virtual_path)
    if planted == "file":
        real.unlink()
        real.symlink_to(outside / "q.csv")
        paths = [rec.virtual_path]
    elif planted == "folder":
        (real.parent / "sub").symlink_to(outside, target_is_directory=True)
        paths = [f"{CHAT}/sub", f"{CHAT}/sub/q.csv"]
    else:
        shutil.rmtree(real.parent)
        real.parent.symlink_to(outside, target_is_directory=True)
        paths = [CHAT, rec.virtual_path]
    answers: list[Any] = [upload(client, "new.csv")] if planted == "chat" else []
    for path in paths:
        answers += [view(client, path), client.get("/files/list", params={"directory": path}, headers=HG)]
    for r in answers:
        assert (r.status_code, r.json()) == NOT_FOUND
    assert sorted(entry.name for entry in outside.iterdir()) == ["q.csv"]
    assert file_events(audit_lines) == []


@pytest.mark.parametrize("session", [None, "default"])
def test_a_missing_or_default_session_is_400_on_each_file_route(
    client: TestClient, store: Store, settings: Settings, audit_lines: AuditLines, session: str | None
) -> None:
    """Ghostwriter's chat is never ``default``: without one of its own, upload, view and list answer 400.

    Nothing is stored, no folder is made and nothing is recorded.
    """
    headers = {name: value for name, value in HG.items() if name != "X-Session-Id"}
    if session is not None:
        headers["X-Session-Id"] = session
    answers: list[Any] = []
    for chat in (CHAT, "/workspace/default"):
        answers += [
            upload(client, directory=chat, headers=headers),
            view(client, f"{chat}/quarterly-figures.csv", headers),
            client.get("/files/list", params={"directory": chat}, headers=headers),
        ]
    for r in answers:
        assert (r.status_code, r.json()) == (400, {"detail": "invalid session"})
    assert not (settings.data_dir / WORKSPACES / "ghostwriter").exists()
    assert file_events(audit_lines) == []


def test_list_is_the_chat_alone(client: TestClient, store: Store) -> None:
    """The chat's folder lists its files; the root and another chat are not found."""
    keep(store, PG, "quarterly-figures.csv")
    keep(store, P2)
    r = client.get("/files/list", params={"directory": CHAT}, headers=HG)
    assert r.status_code == 200
    assert [entry["name"] for entry in r.json()["entries"]] == ["quarterly-figures.csv"]
    for directory in ("/workspace", "/workspace/chat-2"):
        r = client.get("/files/list", params={"directory": directory}, headers=HG)
        assert (r.status_code, r.json()) == NOT_FOUND, directory


def test_ghostwriters_files_are_its_own(client: TestClient, store: Store) -> None:
    """A former caller's file is not Ghostwriter's, for the same user id and chat."""
    theirs = put(store, "x.csv", CSV, p=FORMER)
    mine = keep(store, PG, "y.csv")
    r = view(client, theirs.virtual_path)
    assert (r.status_code, r.json()) == NOT_FOUND
    assert view(client, mine.virtual_path).content == CSV
    assert [
        entry["name"] for entry in client.get("/files/list", params={"directory": CHAT}, headers=HG).json()["entries"]
    ] == ["y.csv"]


# --- purpose and request id ----------------------------------------------------------------------------------------


def test_purpose_and_request_id_reach_the_file_events(client: TestClient, audit_lines: AuditLines) -> None:
    """The upload's and the view's events carry the stated purpose and the request id; without them, neither."""
    path = upload(client, headers={**HG, **PURPOSE}).json()["path"]
    assert view(client, path, {**HG, **PURPOSE}).status_code == 200
    assert view(client, path).status_code == 200

    uploaded, viewed, plain = file_events(audit_lines)
    assert [uploaded["operation"], viewed["operation"]] == ["file.upload", "file.view"]
    for event in (uploaded, viewed):
        assert event["purpose"] == {"reason": "ghostwriter chat"}
        assert event["request_id"] == "req-1"
        assert event["criteria"]["caller"] == "ghostwriter"
        assert event["target"]["container"] == "chat:chat-1"
    assert "purpose" not in plain and "request_id" not in plain


@pytest.mark.parametrize("purpose", ["reason=%C3", "reason=Prüfung".encode()])
def test_a_malformed_purpose_is_400(
    client: TestClient, store: Store, audit_lines: AuditLines, purpose: str | bytes
) -> None:
    """Not form-encoded UTF-8, or not ASCII: 400 "invalid purpose" on each route, and nothing is stored or recorded."""
    own = keep(store, PG)
    headers: dict[str, Any] = {**HG, "X-Audit-Purpose": purpose}
    refused = (
        upload(client, "new.csv", headers=headers),
        view(client, own.virtual_path, headers),
        client.get("/files/list", params={"directory": CHAT}, headers=headers),
    )
    for r in refused:
        assert (r.status_code, r.json()) == (400, {"detail": "invalid purpose"})
    assert store.files_in_chat(PG) == [own]
    assert file_events(audit_lines) == []


def test_a_required_purpose_is_428_and_recorded_denied(
    settings: Settings, store: Store, audit: AuditLog, audit_lines: AuditLines
) -> None:
    """With ``collect`` and ``disclose`` requiring one, Ghostwriter's upload and view without a purpose are refused.

    Each refusal is recorded as denied, by the chat, with no target; a stated purpose passes.
    """
    required = dataclasses.replace(settings, require_purpose=frozenset({"collect", "disclose"}))
    own = keep(store, PG)
    with TestClient(create_app(required, store=store, audit=audit)) as strict:
        r = upload(strict, "new.csv", headers={**HG, "X-Request-ID": "req-1"})
        assert (r.status_code, r.json()) == (428, {"detail": "a stated purpose is required"})
        denied = audit_lines()[-1]
        assert (denied["operation"], denied["outcome"], denied["request_id"]) == ("file.upload", "denied", "req-1")
        assert "target" not in denied
        assert denied["criteria"] == {
            "caller": "ghostwriter",
            "refused": "purpose_required",
            "container": "chat:chat-1",
        }
        assert store.files_in_chat(PG) == [own]

        r = view(strict, own.virtual_path)
        assert (r.status_code, r.json()) == (428, {"detail": "a stated purpose is required"})
        assert (audit_lines()[-1]["operation"], audit_lines()[-1]["outcome"]) == ("file.view", "denied")

        assert upload(strict, "new.csv", headers={**HG, **PURPOSE}).status_code == 200
        assert view(strict, own.virtual_path, {**HG, **PURPOSE}).content == CSV


# --- the door's own check ------------------------------------------------------------------------------------------


def _request(app: FastAPI, key: str, path: str = "/files/list", root_path: str = "") -> Request:
    """A request as routing hands it to a dependency."""
    scope = {"type": "http", "method": "GET", "path": path, "root_path": root_path, "app": app}
    return Request({**scope, "headers": [(b"authorization", f"Bearer {key}".encode())]})


def test_the_file_caller_check_keeps_ghostwriter_to_its_four_paths(app: FastAPI) -> None:
    """Should a request pass the gate, the routes' own check refuses Ghostwriter off its paths and under a root path."""
    assert asyncio.run(require_file_caller(_request(app, GHOSTWRITER_KEY))).name == "ghostwriter"
    for refused in (
        _request(app, GHOSTWRITER_KEY, root_path="/prefix"),
        _request(app, GHOSTWRITER_KEY, path="/files/read"),
        _request(app, DOCINT_KEY),
    ):
        with pytest.raises(HTTPException) as answer:
            asyncio.run(require_file_caller(refused))
        assert (answer.value.status_code, answer.value.detail) == (404, "not found")


def test_the_file_routes_refuse_ghostwriter_under_a_root_path(app: FastAPI, store: Store) -> None:
    """Under a root path, Ghostwriter finds no file route: routing would see another path than the gate judged."""
    own = keep(store, PG)
    with TestClient(app, root_path="/prefix") as prefixed:
        for prefix in ("", "/prefix"):
            answers = (
                prefixed.post(
                    prefix + "/files/upload", params={"directory": CHAT}, files={"file": ("new.csv", CSV)}, headers=HG
                ),
                prefixed.get(prefix + "/files/view", params={"path": own.virtual_path}, headers=HG),
                prefixed.get(prefix + "/files/list", params={"directory": CHAT}, headers=HG),
            )
            for r in answers:
                assert (r.status_code, r.json()) == NOT_FOUND, prefix
    assert store.files_in_chat(PG) == [own]


# --- the tools that take a path ------------------------------------------------------------------------------------


@pytest.fixture
def executor(settings: Settings) -> Scripted:
    """An executor that records every job it is sent."""
    return Scripted(settings.data_dir)


@pytest.fixture
def jobs(settings: Settings, store: Store, executor: Scripted) -> CallJobs:
    """A queue over that executor, as one call sees it."""
    return CallJobs(JobQueue(settings, executor, store, Metrics(settings.data_dir)), CALL)


@pytest.mark.parametrize("spelling", ["/workspace/chat-2/{}", "/workspace/chat-1/../chat-2/{}"])
def test_a_confined_read_of_another_chats_file_is_not_found_and_reads_nothing(
    settings: Settings,
    store: Store,
    audit: AuditLog,
    audit_lines: AuditLines,
    executor: Scripted,
    jobs: CallJobs,
    spelling: str,
) -> None:
    """``read_document`` and ``inspect_template`` stop at the store for another chat's file: no job, no event.

    So neither ever moves its principal into that chat.
    """
    document = spelling.format(keep(store, P2, "harbour-report.docx").name)
    template = spelling.format(keep(store, P2, "quarterly-template.docx").name)
    with pytest.raises(NotFound) as refused:
        asyncio.run(read_document(ReadDocumentRequest(path=document), PG, settings, store, audit, jobs))
    assert describe(refused.value) == (404, "not found")
    with pytest.raises(NotFound) as refused:
        asyncio.run(inspect_template(InspectTemplateRequest(path=template), PG, settings, store, audit, jobs))
    assert describe(refused.value) == (404, "not found")
    assert executor.requests == []
    assert audit_lines() == []
