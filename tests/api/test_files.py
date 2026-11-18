"""The file browser Open WebUI calls through its proxy, and the ``list_files`` tool on the MCP door.

Every read or change of content is one audit event naming the file by id; no answer and no log line carries a
file name, a path or a user id.
"""

import asyncio
import dataclasses
import errno
import io
import json
import logging
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from tests.api.conftest import CHAT, H_B, HG, HG_B, KEY_ONLY, ME, MIB, PG, PG_SETUP, AuditLines, H, put, tick
from tests.api.mcp_calls import acall, answered, rpc, sent_size
from workbench.api import store as store_module
from workbench.api.audit import AuditLog
from workbench.api.body_limit import BodyLimit
from workbench.api.main import create_app
from workbench.api.store import NotFound, StorageFull, Store
from workbench.config import Settings
from workbench.models import FileRecord
from workbench.paths import PathError, Workspaces


def upload(
    client: TestClient, name: str, data: bytes = b"x", *, directory: str = CHAT, headers: dict[str, str] = H
) -> Any:
    """Upload ``data`` as ``name`` the way Open WebUI's file browser does."""
    return client.post("/files/upload", params={"directory": directory}, headers=headers, files={"file": (name, data)})


def file_operations(audit_lines: AuditLines) -> list[str]:
    """The operations of the audit events about files, in order."""
    return [line["operation"] for line in audit_lines() if line["operation"].startswith("file.")]


def server_log(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The log records of the app and its server; the test client's own request lines are left out."""
    return [record for record in caplog.records if not record.name.startswith("httpx")]


def older_hint(left: int, total: int) -> str:
    """The start of the hint that says how many older files a cut list leaves out."""
    return f"{left} older {'file' if left == 1 else 'files'} not listed ({total} in all)"


@pytest.fixture
def limited_client(settings: Settings, store: Store, audit: AuditLog) -> Iterator[TestClient]:
    """A client on an app with small limits: 1 MiB per upload, 2 MiB per user, 1 KiB results."""
    small = dataclasses.replace(settings, upload_max_mb=1, workspace_max_mb=2, result_max_kb=1)
    with TestClient(create_app(small, store=store, audit=audit)) as test_client:
        yield test_client


# --- the happy path ------------------------------------------------------------------------------------------------


def test_upload_then_list_and_view(client: TestClient, audit_lines: AuditLines) -> None:
    """One file goes in and comes out of every route, and each disclosure is one audit event."""
    r = client.post("/files/upload", params={"directory": CHAT}, headers=H, files={"file": ("q.csv", b"a,b\n1,2\n")})
    assert r.status_code == 200
    assert r.json() == {"path": "/workspace/chat-1/q.csv", "size": 8}
    listing = client.get("/files/list", params={"directory": CHAT}, headers=H).json()
    assert listing["dir"] == CHAT
    assert listing["writable"] is True
    assert [e["name"] for e in listing["entries"]] == ["q.csv"]
    assert (listing["entries"][0]["type"], listing["entries"][0]["size"]) == ("file", 8)
    assert isinstance(listing["entries"][0]["modified"], float)
    assert client.get("/files/view", params={"path": f"{CHAT}/q.csv"}, headers=H).content == b"a,b\n1,2\n"
    assert [line["operation"] for line in audit_lines()][-2:] == ["file.upload", "file.view"]


def test_the_root_lists_the_chat_folders(client: TestClient) -> None:
    """``/workspace`` is a place to look at, with each chat as a folder."""
    upload(client, "a.txt")
    upload(client, "b.txt", directory="/workspace/chat-2")
    listing = client.get("/files/list", params={"directory": "/workspace"}, headers=H).json()
    assert listing["dir"] == "/workspace"
    assert [(e["name"], e["type"]) for e in listing["entries"]] == [("chat-1", "directory"), ("chat-2", "directory")]


def test_a_listing_path_is_given_back_in_its_plain_form(client: TestClient) -> None:
    """``dir`` is the normalised folder, however it was asked for."""
    upload(client, "a.txt")
    listing = client.get("/files/list", params={"directory": f"{CHAT}/./sub/.."}, headers=H)
    assert listing.json()["dir"] == CHAT
    assert [e["name"] for e in listing.json()["entries"]] == ["a.txt"]


def test_a_chat_folder_not_made_yet_lists_empty(client: TestClient) -> None:
    """Looking is not writing."""
    r = client.get("/files/list", params={"directory": CHAT}, headers=H)
    assert r.json() == {"dir": CHAT, "writable": True, "entries": []}


def test_a_file_is_not_a_folder_to_list(client: TestClient) -> None:
    """Listing a file answers not found."""
    upload(client, "a.txt")
    r = client.get("/files/list", params={"directory": f"{CHAT}/a.txt"}, headers=H)
    assert r.status_code == 404
    assert r.json() == {"detail": "not found"}


def test_a_second_upload_of_a_name_keeps_both(client: TestClient) -> None:
    """Inputs are immutable: the new file gets a number."""
    assert upload(client, "q.csv", b"one").json()["path"] == f"{CHAT}/q.csv"
    assert upload(client, "q.csv", b"two").json()["path"] == f"{CHAT}/q (1).csv"
    assert client.get("/files/view", params={"path": f"{CHAT}/q.csv"}, headers=H).content == b"one"
    assert client.get("/files/view", params={"path": f"{CHAT}/q (1).csv"}, headers=H).content == b"two"


def test_an_upload_into_a_folder_made_before(client: TestClient) -> None:
    """A folder is made with ``mkdir``, filled by an upload and listed by its own path."""
    made = client.post("/files/mkdir", json={"path": f"{CHAT}/reports"}, headers=H)
    assert made.json() == {"path": f"{CHAT}/reports"}
    assert upload(client, "q.csv", directory=f"{CHAT}/reports").json() == {"path": f"{CHAT}/reports/q.csv", "size": 1}
    folder = client.get("/files/list", params={"directory": f"{CHAT}/reports"}, headers=H).json()
    assert [e["name"] for e in folder["entries"]] == ["q.csv"]
    top = client.get("/files/list", params={"directory": CHAT}, headers=H).json()
    assert [(e["name"], e["type"]) for e in top["entries"]] == [("reports", "directory")]


def test_an_upload_into_a_folder_that_was_never_made_is_not_found(client: TestClient) -> None:
    """Only a chat folder is made on first use."""
    r = upload(client, "q.csv", directory=f"{CHAT}/reports")
    assert r.status_code == 404


def test_an_upload_to_the_root_is_refused(client: TestClient) -> None:
    """Files live inside a chat folder."""
    assert upload(client, "q.csv", directory="/workspace").status_code == 400


def test_an_upload_without_a_usable_name_is_refused(client: TestClient) -> None:
    """A name that is not one plain segment is a bad request, not a crash."""
    r = client.post(
        "/files/upload",
        params={"directory": CHAT},
        headers=H,
        files={"file": ("..", b"x")},
    )
    assert r.status_code == 400
    assert r.json() == {"detail": "invalid file name"}


def test_move_and_mkdir_change_the_layout_without_an_audit_event(client: TestClient, audit_lines: AuditLines) -> None:
    """Spec section 7: list, mkdir and move touch no content, so they are not recorded."""
    client.post("/files/mkdir", json={"path": f"{CHAT}/reports"}, headers=H)
    upload(client, "q.csv", directory=f"{CHAT}/reports")
    moved = client.post(
        "/files/move", json={"source": f"{CHAT}/reports/q.csv", "destination": f"{CHAT}/q.csv"}, headers=H
    )
    assert moved.status_code == 200
    assert moved.json() == {"path": f"{CHAT}/q.csv"}
    names = [e["name"] for e in client.get("/files/list", params={"directory": CHAT}, headers=H).json()["entries"]]
    assert names == ["q.csv", "reports"]
    assert file_operations(audit_lines) == ["file.upload"]


def test_move_onto_a_taken_name_is_a_conflict(client: TestClient) -> None:
    """Nothing is overwritten."""
    upload(client, "a.txt", b"a")
    upload(client, "b.txt", b"b")
    r = client.post("/files/move", json={"source": f"{CHAT}/a.txt", "destination": f"{CHAT}/b.txt"}, headers=H)
    assert r.status_code == 409
    assert r.json() == {"detail": "the destination exists"}


def test_move_of_a_missing_file_is_not_found(client: TestClient) -> None:
    """The same answer as for any absent file."""
    r = client.post("/files/move", json={"source": f"{CHAT}/a.txt", "destination": f"{CHAT}/b.txt"}, headers=H)
    assert r.status_code == 404


def test_mkdir_over_a_file_is_a_conflict(client: TestClient) -> None:
    """A folder cannot take a file's place."""
    upload(client, "a.txt")
    r = client.post("/files/mkdir", json={"path": f"{CHAT}/a.txt"}, headers=H)
    assert r.status_code == 409


@pytest.mark.parametrize(
    ("route", "field", "value"),
    [
        ("/files/mkdir", "folder", "harbour\\ud800"),
        ("/files/move", "source", "/workspace/chat-1/harbour\\udc00"),
        ("/files/cwd", "cwd", "\\ud800"),
    ],
    ids=["mkdir", "move", "cwd"],
)
def test_a_lone_surrogate_in_a_files_body_is_never_a_500(
    client: TestClient, route: str, field: str, value: str
) -> None:
    """JSON may carry the escape of half a pair, which UTF-8 cannot hold; the 422 names the field, never the body.

    The value comes back neither as it was sent nor as it was read, with U+FFFD in place of the half pair.
    """
    body = f'{{"{field}": "{value}"}}'.encode()
    r = client.post(route, content=body, headers={**H, "Content-Type": "application/json"})
    assert r.status_code == 422, r.text
    assert [set(error) for error in r.json()["detail"]] == [{"type", "loc", "msg"}]
    assert "�" not in r.text
    assert value not in r.text
    readable = value.split("\\u", 1)[0]
    assert readable == "" or readable not in r.text


def test_a_lone_surrogate_in_a_folders_name_is_read_as_a_replacement_character(client: TestClient) -> None:
    """The browser's routes read a body as the tools do: the folder is made, its name with U+FFFD."""
    body = b'{"path": "/workspace/chat-1/a\\ud800b"}'
    r = client.post("/files/mkdir", content=body, headers={**H, "Content-Type": "application/json"})
    assert (r.status_code, r.json()) == (200, {"path": f"{CHAT}/a�b"})


def test_every_route_of_the_app_reads_its_body_as_the_tools_do(app: FastAPI) -> None:
    """One route class for the whole app, so no route that takes JSON can answer a lone surrogate with a 500."""
    from workbench.api.routing import EncodableRoute

    routes = [context.original_route for context in iter_route_contexts(app.routes)]
    assert len(routes) > 10
    assert [route for route in routes if not isinstance(route, EncodableRoute)] == []


# --- view -----------------------------------------------------------------------------------------------------------


def test_view_serves_the_bytes_with_the_records_type(client: TestClient) -> None:
    """The type is the record's, ``preview`` changes nothing, and the browser may not sniff another one."""
    data = b"a,b\n1,2\n"
    upload(client, "q.csv", data)
    plain = client.get("/files/view", params={"path": f"{CHAT}/q.csv"}, headers=H)
    preview = client.get("/files/view", params={"path": f"{CHAT}/q.csv", "preview": "true"}, headers=H)
    for r in (plain, preview):
        assert r.status_code == 200
        assert r.content == data
        assert r.headers["content-type"] == "text/csv"
        assert r.headers["content-length"] == str(len(data))
        assert r.headers["content-disposition"] == "inline; filename*=UTF-8''q.csv"
        assert r.headers["x-content-type-options"] == "nosniff"


def test_view_knows_office_types_without_a_system_table(client: TestClient) -> None:
    """The private type table answers for the Office formats."""
    upload(client, "quarterly-figures.xlsx", b"PK")
    r = client.get("/files/view", params={"path": f"{CHAT}/quarterly-figures.xlsx"}, headers=H)
    assert r.headers["content-type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_view_streams_a_file_larger_than_a_chunk(client: TestClient) -> None:
    """The bytes come back whole across the read size."""
    data = bytes(range(256)) * 10240
    upload(client, "big.bin", data)
    r = client.get("/files/view", params={"path": f"{CHAT}/big.bin"}, headers=H)
    assert r.headers["content-length"] == str(len(data))
    assert r.content == data


def test_view_of_a_file_in_a_folder_names_only_its_base_name(client: TestClient) -> None:
    """The header carries the file's own name, never the folder."""
    client.post("/files/mkdir", json={"path": f"{CHAT}/reports"}, headers=H)
    upload(client, "q.csv", directory=f"{CHAT}/reports")
    r = client.get("/files/view", params={"path": f"{CHAT}/reports/q.csv"}, headers=H)
    assert r.headers["content-disposition"] == "inline; filename*=UTF-8''q.csv"


def test_view_of_a_folder_or_a_missing_file_is_not_found(client: TestClient) -> None:
    """Only files can be viewed."""
    client.post("/files/mkdir", json={"path": f"{CHAT}/reports"}, headers=H)
    for path in (f"{CHAT}/reports", f"{CHAT}/missing.txt", "/workspace/chat-2/a.txt"):
        r = client.get("/files/view", params={"path": path}, headers=H)
        assert r.status_code == 404, path
        assert r.json() == {"detail": "not found"}


def test_a_name_never_breaks_out_of_the_content_disposition_header(client: TestClient, store: Store) -> None:
    """Quotes, semicolons and line breaks in a name are percent-encoded."""
    for name, encoded in (
        ('say "hi"; x.txt', "say%20%22hi%22%3B%20x.txt"),
        ("line\nbreak.txt", "line%0Abreak.txt"),
        ("100% sure.txt", "100%25%20sure.txt"),
    ):
        rec = put(store, name)
        r = client.get("/files/view", params={"path": rec.virtual_path}, headers=H)
        assert r.headers["content-disposition"] == f"inline; filename*=UTF-8''{encoded}"


# --- active content ------------------------------------------------------------------------------------------------

CSP = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'"
HARDENING = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "cross-origin-resource-policy": "same-origin",
}
ACTIVE_FILES = [
    ("page.html", b"<!doctype html><!-- <script>alert(1)</script> --><script>alert(1)</script>"),
    ("PAGE.HTM", b"<html><script>alert(1)</script></html>"),
    ("doc.xhtml", b'<html xmlns="http://www.w3.org/1999/xhtml"><script>alert(1)</script></html>'),
    ("image.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'),
    ("data.xml", b"<?xml version='1.0'?><a/>"),
    ("app.js", b"alert(1)"),
]
ACTIVE_TYPES = [
    "text/html",
    "application/xhtml+xml",
    "image/svg+xml",
    "text/xml",
    "application/xml",
    "text/javascript",
    "application/javascript",
    "Text/HTML; charset=utf-8",
]


@pytest.mark.parametrize(("name", "data"), ACTIVE_FILES)
def test_active_content_is_served_as_a_text_attachment_under_a_sandbox(
    client: TestClient, name: str, data: bytes
) -> None:
    """A page or image that can run script never comes back inline with its own type."""
    upload(client, name, data)
    r = client.get("/files/view", params={"path": f"{CHAT}/{name}"}, headers=H)
    assert r.status_code == 200
    assert r.content == data
    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    assert r.headers["content-disposition"] == f"attachment; filename*=UTF-8''{name}"
    for header, value in HARDENING.items():
        assert r.headers[header] == value, header


@pytest.mark.parametrize("name", ["minutes.eml", "harbour-report.mht", "harbour-report.MHTML", "notes.nws"])
def test_a_message_or_web_archive_is_served_only_as_an_attachment(client: TestClient, name: str) -> None:
    """R1 M-8: a whole message keeps its type, but a browser saves it and never renders it inline."""
    data = b"From: user-a@example.invalid\r\nSubject: synthetic\r\n\r\nbody\r\n"
    upload(client, name, data)
    r = client.get("/files/view", params={"path": f"{CHAT}/{name}"}, headers=H)
    assert (r.status_code, r.content) == (200, data)
    assert r.headers["content-disposition"] == f"attachment; filename*=UTF-8''{name}"
    for header, value in HARDENING.items():
        assert r.headers[header] == value, header


def test_a_message_type_under_another_name_is_an_attachment_too(client: TestClient, store: Store) -> None:
    """The stored type decides as much as the name does."""
    rec = put(store, "letter.txt", b"x")
    with store._db() as db:
        db.execute("UPDATE files SET mime = 'message/rfc822' WHERE id = ?", (rec.id,))
    r = client.get("/files/view", params={"path": rec.virtual_path}, headers=H)
    assert r.headers["content-disposition"].startswith("attachment;")


@pytest.mark.parametrize("mime", ACTIVE_TYPES)
def test_an_active_type_is_never_inline_whatever_the_name(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, mime: str
) -> None:
    """The stored type decides too, so a file named ``notes.dat`` that is stored as HTML is still an attachment."""
    monkeypatch.setattr(store_module, "_mime", lambda _name: mime)
    upload(client, "notes.dat", b"<script>alert(1)</script>")
    r = client.get("/files/view", params={"path": f"{CHAT}/notes.dat"}, headers=H)
    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    assert r.headers["content-disposition"] == "attachment; filename*=UTF-8''notes.dat"


@pytest.mark.parametrize("name", ["a.html", "a.htm", "a.xhtml", "a.svg", "a.xml", "a.js", "A.JS", "x.tar.html"])
def test_an_active_name_is_never_inline_whatever_the_stored_type(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """The name decides too: each active suffix, stored as an unknown type, is still an attachment."""
    monkeypatch.setattr(store_module, "_mime", lambda _name: "application/octet-stream")
    upload(client, name, b"<svg/>")
    r = client.get("/files/view", params={"path": f"{CHAT}/{name}"}, headers=H)
    assert r.headers["content-type"] == "text/plain; charset=utf-8"
    assert r.headers["content-disposition"] == f"attachment; filename*=UTF-8''{name}"


@pytest.mark.parametrize(
    ("name", "mime"),
    [
        ("report.pdf", "application/pdf"),
        ("quarterly-figures.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("photo.png", "image/png"),
        ("q.csv", "text/csv"),
        ("notes.txt", "text/plain"),
    ],
)
def test_other_files_keep_their_type_inline_under_the_same_hardening(client: TestClient, name: str, mime: str) -> None:
    """Only active content is turned into text; the sandbox headers are on every file."""
    upload(client, name, b"data")
    r = client.get("/files/view", params={"path": f"{CHAT}/{name}"}, headers=H)
    assert r.headers["content-type"] == mime
    assert r.headers["content-disposition"] == f"inline; filename*=UTF-8''{name}"
    for header, value in HARDENING.items():
        assert r.headers[header] == value, header


@pytest.mark.parametrize(
    ("params", "headers", "status"),
    [
        ({"path": f"{CHAT}/missing.txt"}, H, 404),
        ({"path": "/workspace/chat-1/../../x"}, H, 400),
        ({"path": CHAT}, H, 400),
        ({"path": f"{CHAT}/q.csv"}, KEY_ONLY, 400),
        ({"path": f"{CHAT}/q.csv"}, {"X-User-Id": "user-a"}, 401),
        ({}, H, 422),
    ],
)
def test_the_hardening_headers_are_on_every_view_answer_errors_included(
    client: TestClient, params: dict[str, str], headers: dict[str, str], status: int
) -> None:
    """A refusal from ``/files/view`` carries the sandbox headers too, whichever handler wrote it."""
    upload(client, "q.csv")
    r = client.get("/files/view", params=params, headers=headers)
    assert r.status_code == status
    for header, value in HARDENING.items():
        assert r.headers[header] == value, header
    assert len(r.headers.get_list("content-security-policy")) == 1


def test_the_hardening_headers_are_on_a_view_that_cannot_be_recorded(
    client: TestClient, broken_audit: None, store: Store
) -> None:
    """The 500 for a failed audit write is a view answer as well."""
    rec = put(store, "q.csv")
    r = client.get("/files/view", params={"path": rec.virtual_path}, headers=H)
    assert r.status_code == 500
    for header, value in HARDENING.items():
        assert r.headers[header] == value, header


def test_the_hardening_headers_are_only_for_view(client: TestClient) -> None:
    """The other routes answer JSON for the caller, not files for a browser, and carry none of them."""
    upload(client, "q.csv")
    for r in (
        client.get("/files/list", params={"directory": CHAT}, headers=H),
        client.get("/files/read", params={"path": f"{CHAT}/q.csv"}, headers=H),
    ):
        assert "content-security-policy" not in r.headers


# --- file names in real life -------------------------------------------------------------------------------------


def test_unicode_names_round_trip(client: TestClient) -> None:
    """Umlauts and spaces survive upload, list and view exactly, and the header is RFC 5987."""
    name = "Übersicht März.xlsx"
    assert upload(client, name, b"data").json() == {"path": f"{CHAT}/{name}", "size": 4}
    names = [e["name"] for e in client.get("/files/list", params={"directory": CHAT}, headers=H).json()["entries"]]
    assert names == [name]
    view = client.get("/files/view", params={"path": f"{CHAT}/{name}"}, headers=H)
    assert view.content == b"data"
    assert view.headers["content-disposition"] == "inline; filename*=UTF-8''%C3%9Cbersicht%20M%C3%A4rz.xlsx"


def _case_sensitive(folder: Path) -> bool:
    """Tell whether ``folder`` is on a filesystem that tells names apart by case."""
    probe = folder / "case-probe"
    probe.write_bytes(b"")
    try:
        return not (folder / "CASE-PROBE").exists()
    finally:
        probe.unlink()


def test_names_differing_only_by_case_are_both_kept(client: TestClient, tmp_path: Path) -> None:
    """Both files are kept with their exact names where the filesystem allows it (Linux; not a default macOS volume)."""
    if not _case_sensitive(tmp_path):
        pytest.skip("this filesystem does not tell names apart by case")
    upper, lower = "Übersicht März.xlsx", "übersicht märz.xlsx"
    assert upload(client, upper, b"upper").json()["path"] == f"{CHAT}/{upper}"
    assert upload(client, lower, b"lower").json()["path"] == f"{CHAT}/{lower}"
    names = [e["name"] for e in client.get("/files/list", params={"directory": CHAT}, headers=H).json()["entries"]]
    assert sorted(names) == sorted([upper, lower])
    assert client.get("/files/view", params={"path": f"{CHAT}/{upper}"}, headers=H).content == b"upper"
    assert client.get("/files/view", params={"path": f"{CHAT}/{lower}"}, headers=H).content == b"lower"


# --- audit ---------------------------------------------------------------------------------------------------------


def test_no_audit_line_contains_a_file_name(
    client: TestClient, store: Store, audit_stream: io.StringIO, audit_lines: AuditLines
) -> None:
    """Upload, view, read and delete are all recorded, by file id and never by name."""
    upload(client, "secret-name.xlsx", b"PK")
    upload(client, "secret-notes.txt", b"hello")
    sheet, notes = store.get(ME, f"{CHAT}/secret-name.xlsx"), store.get(ME, f"{CHAT}/secret-notes.txt")
    client.get("/files/view", params={"path": sheet.virtual_path}, headers=H)
    client.get("/files/read", params={"path": notes.virtual_path}, headers=H)
    client.delete("/files/delete", params={"path": sheet.virtual_path}, headers=H)
    events = [line for line in audit_lines() if line["operation"].startswith("file.")]
    assert [(e["operation"], e["target"]["ids"]) for e in events] == [
        ("file.upload", [sheet.id]),
        ("file.upload", [notes.id]),
        ("file.view", [sheet.id]),
        ("file.read", [notes.id]),
        ("file.delete", [sheet.id]),
    ]
    assert all(e["criteria"] == {"caller": "open-webui"} for e in events)
    assert all(e["target"]["container"] == "chat:chat-1" for e in events)
    assert "secret-name" not in audit_stream.getvalue()
    assert "secret-notes" not in audit_stream.getvalue()


def test_the_audit_container_is_the_files_chat_folder_not_the_session_chat(
    client: TestClient, audit_lines: AuditLines
) -> None:
    """The browser sets ``X-Session-Id`` freely; the record names the folder the file is in, not the header."""
    assert H["X-Session-Id"] == "chat-1"
    uploaded = upload(client, "x.txt", b"hi", directory="/workspace/chat-2")
    path = uploaded.json()["path"]
    assert path == "/workspace/chat-2/x.txt"
    client.get("/files/view", params={"path": path}, headers=H)
    client.get("/files/read", params={"path": path}, headers=H)
    client.post("/files/mkdir", json={"path": "/workspace/chat-3/reports"}, headers=H)
    nested = upload(client, "y.txt", b"hi", directory="/workspace/chat-3/reports").json()["path"]
    client.get("/files/view", params={"path": nested}, headers=H)
    client.delete("/files/delete", params={"path": path}, headers=H)
    client.delete("/files/delete", params={"path": nested}, headers=H)
    events = [line for line in audit_lines() if line["operation"].startswith("file.")]
    assert [(e["operation"], e["target"]["container"]) for e in events] == [
        ("file.upload", "chat:chat-2"),
        ("file.view", "chat:chat-2"),
        ("file.read", "chat:chat-2"),
        ("file.upload", "chat:chat-3"),
        ("file.view", "chat:chat-3"),
        ("file.delete", "chat:chat-2"),
        ("file.delete", "chat:chat-3"),
    ]


def test_list_mkdir_and_move_are_not_recorded_and_absent_files_leave_no_event(
    client: TestClient, audit_lines: AuditLines
) -> None:
    """No file, no id, no event: a miss records nothing."""
    client.get("/files/list", params={"directory": CHAT}, headers=H)
    client.get("/files/view", params={"path": f"{CHAT}/none.txt"}, headers=H)
    client.get("/files/read", params={"path": f"{CHAT}/none.txt"}, headers=H)
    client.delete("/files/delete", params={"path": f"{CHAT}/none.txt"}, headers=H)
    assert file_operations(audit_lines) == []


def test_delete_removes_the_file_after_recording_it(
    client: TestClient, store: Store, audit_lines: AuditLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete event is already written when the store removes the file, and the file is gone after."""
    rec = put(store, "q.csv")
    seen: list[list[str]] = []
    original = store.remove

    def spy(record: FileRecord) -> None:
        seen.append(file_operations(audit_lines))
        original(record)

    monkeypatch.setattr(store, "remove", spy)
    r = client.delete("/files/delete", params={"path": rec.virtual_path}, headers=H)
    assert r.status_code == 200
    assert r.json() == {"path": rec.virtual_path}
    assert seen == [["file.delete"]]
    with pytest.raises(NotFound):
        store.get(ME, rec.virtual_path)
    assert client.get("/files/list", params={"directory": CHAT}, headers=H).json()["entries"] == []


def test_delete_is_recorded_before_the_file_goes(client: TestClient, broken_audit: None, store: Store) -> None:
    """A deletion that cannot be recorded does not happen."""
    rec = put(store, "q.csv", b"keep me")
    r = client.delete("/files/delete", params={"path": rec.virtual_path}, headers=H)
    assert r.status_code == 500
    assert r.json() == {"detail": "audit unavailable"}
    assert store.get(ME, rec.virtual_path) == rec
    assert client.get("/files/view", params={"path": rec.virtual_path}, headers=H).status_code == 500
    assert [e["name"] for e in client.get("/files/list", params={"directory": CHAT}, headers=H).json()["entries"]] == [
        "q.csv"
    ]


@pytest.mark.parametrize("route", ["view", "read"])
def test_a_read_that_cannot_be_recorded_returns_nothing(
    client: TestClient, broken_audit: None, store: Store, route: str
) -> None:
    """Fail closed: no event, no content."""
    rec = put(store, "notes.txt", b"top-secret-bytes")
    r = client.get(f"/files/{route}", params={"path": rec.virtual_path}, headers=H)
    assert r.status_code == 500
    assert r.json() == {"detail": "audit unavailable"}
    assert b"top-secret-bytes" not in r.content


def test_an_upload_that_cannot_be_recorded_does_not_happen(
    client: TestClient, broken_audit: None, store: Store, settings: Settings
) -> None:
    """The file is taken back out, so nothing unrecorded stays on the volume."""
    r = upload(client, "q.csv", b"data")
    assert r.status_code == 500
    assert r.json() == {"detail": "audit unavailable"}
    assert store.files_in_chat(ME) == []
    assert store.usage(ME) == 0
    assert [p for p in (settings.data_dir / "workspaces").rglob("*") if p.is_file()] == []


def test_a_failed_audit_write_logs_nothing_with_a_name(
    client: TestClient, broken_audit: None, store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """The refusal is logged as nothing at all: no traceback whose cause names a path."""
    rec = put(store, "secret-name.xlsx")
    caplog.set_level(logging.DEBUG)
    client.get("/files/view", params={"path": rec.virtual_path}, headers=H)
    client.delete("/files/delete", params={"path": rec.virtual_path}, headers=H)
    assert "secret-name" not in " ".join(record.getMessage() for record in server_log(caplog))
    assert [record for record in server_log(caplog) if record.exc_info] == []


def test_an_unrecorded_upload_answers_the_audit_failure_even_if_nothing_could_be_removed(
    client: TestClient,
    broken_audit: None,
    store: Store,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ``.part`` cannot be removed after the audit failure: that error is logged by its type, never the answer."""
    real = store_module._unlink
    tried: list[str] = []

    def refuse_the_part(path: Path) -> None:
        if path.name.endswith(".part"):
            tried.append(path.name)
            raise OSError(errno.EROFS, "synthetic: read-only file system", str(path))
        real(path)

    monkeypatch.setattr(store_module, "_unlink", refuse_the_part)
    with caplog.at_level(logging.DEBUG):
        r = upload(client, "salaries-of-user-b.csv", b"data")
    assert (r.status_code, r.json()) == (500, {"detail": "audit unavailable"})
    assert len(tried) == 1
    assert store.files_in_chat(ME) == []
    chat = settings.data_dir / "workspaces" / "open-webui" / "user-a" / "chat-1"
    assert [path.name for path in chat.iterdir()] == tried
    assert "OSError" in caplog.text
    assert "salaries" not in caplog.text
    assert tried[0] not in caplog.text
    assert [record for record in server_log(caplog) if record.exc_info] == []


def test_an_upload_is_recorded_by_its_id_before_it_is_a_file_of_the_chat(
    client: TestClient, store: Store, settings: Settings, audit_stream: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the ``collect`` is written, the bytes are only the ``<id>.part`` its id names; the kept file has that id."""
    seen: list[tuple[list[str], list[str]]] = []
    write = audit_stream.write
    chat = settings.data_dir / "workspaces" / "open-webui" / "user-a" / "chat-1"

    def look(text: str, /) -> int:
        if '"file.upload"' in text:
            seen.append(([rec.name for rec in store.files_in_chat(ME)], sorted(p.name for p in chat.iterdir())))
        return write(text)

    monkeypatch.setattr(audit_stream, "write", look)
    r = upload(client, "quarterly-figures.xlsx", b"PK")
    assert r.status_code == 200
    (rec,) = store.files_in_chat(ME)
    assert seen == [([], [f"{rec.id}.part"])]
    (event,) = [json.loads(line) for line in audit_stream.getvalue().splitlines() if '"file.upload"' in line]
    assert event["target"]["ids"] == [rec.id]


def test_an_upload_that_cannot_be_stored_after_its_record_is_recorded_again_as_an_error(
    client: TestClient, store: Store, settings: Settings, audit_lines: AuditLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row's commit fails after the ``ok``: the file is taken out, and an ``error`` event names the same id."""

    def full(_rec: FileRecord) -> None:
        raise StorageFull("storage is full")

    monkeypatch.setattr(store, "_insert", full)
    r = upload(client, "q.csv", b"data")
    assert (r.status_code, r.json()) == (507, {"detail": "storage is full"})
    first, again = [e for e in audit_lines() if e["operation"] == "file.upload"]
    assert (first["outcome"], again["outcome"]) == ("ok", "error")
    assert first["target"]["ids"] == again["target"]["ids"]
    assert [p for p in (settings.data_dir / "workspaces").rglob("*") if p.is_file()] == []


def test_an_error_record_that_fails_too_does_not_hide_the_stores_error(
    client: TestClient,
    store: Store,
    audit_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The store's failure decides the answer; the failed second record is logged by its type alone."""

    def full(_rec: FileRecord) -> None:
        raise StorageFull("storage is full")

    write = audit_stream.write

    def only_ok(text: str, /) -> int:
        if '"outcome":"error"' in text.replace(" ", ""):
            raise OSError("synthetic audit stream failure")
        return write(text)

    monkeypatch.setattr(store, "_insert", full)
    monkeypatch.setattr(audit_stream, "write", only_ok)
    with caplog.at_level(logging.WARNING):
        r = upload(client, "salaries-of-user-b.csv", b"data")
    assert (r.status_code, r.json()) == (507, {"detail": "storage is full"})
    assert "AuditWriteError" in caplog.text
    assert "salaries" not in caplog.text


def test_a_delete_that_fails_after_its_record_is_recorded_again_as_an_error(
    client: TestClient, store: Store, audit_lines: AuditLines, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``ok`` event is not the last word: a removal that fails adds an ``error`` event for the same file."""
    rec = put(store, "q.csv")

    def refuse(_record: FileRecord) -> None:
        raise OSError("synthetic: read-only file system")

    monkeypatch.setattr(store, "remove", refuse)
    r = client.delete("/files/delete", params={"path": rec.virtual_path}, headers=H)
    assert (r.status_code, r.json()) == (500, {"detail": "internal error"})
    deletes = [e for e in audit_lines() if e["operation"] == "file.delete"]
    assert [(e["outcome"], e["target"]["ids"]) for e in deletes] == [("ok", [rec.id]), ("error", [rec.id])]
    assert store.get(ME, rec.virtual_path) == rec


def test_a_failed_delete_whose_error_record_fails_too_answers_the_removals_error(
    client: TestClient,
    store: Store,
    audit_stream: io.StringIO,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The removal's failure decides the answer; the failed error record is logged by its type, with no name."""
    rec = put(store, "salaries-of-user-b.csv")

    def refuse(_record: FileRecord) -> None:
        raise PathError("a symlink is in the way")

    write = audit_stream.write

    def only_ok(text: str, /) -> int:
        if '"outcome":"error"' in text.replace(" ", ""):
            raise OSError("synthetic audit stream failure")
        return write(text)

    monkeypatch.setattr(store, "remove", refuse)
    monkeypatch.setattr(audit_stream, "write", only_ok)
    with caplog.at_level(logging.WARNING):
        r = client.delete("/files/delete", params={"path": rec.virtual_path}, headers=H)
    assert (r.status_code, r.json()) == (400, {"detail": "a symlink is in the way"})
    assert "AuditWriteError" in caplog.text
    assert "salaries" not in caplog.text


def test_the_store_has_no_delete_that_records_nothing() -> None:
    """Only ``remove`` deletes, and the routes call it once the deletion is recorded."""
    assert not hasattr(Store, "delete")


# --- isolation -----------------------------------------------------------------------------------------------------


def test_other_user_cannot_list_view_or_delete(client: TestClient, store: Store) -> None:
    """Another user sees an empty folder and not found, and the owner's file is untouched."""
    rec = put(store, "q.csv", b"mine")
    listing = client.get("/files/list", params={"directory": CHAT}, headers=H_B).json()
    assert listing["entries"] == []
    for method, url in (("GET", "/files/view"), ("GET", "/files/read"), ("DELETE", "/files/delete")):
        r = client.request(method, url, params={"path": rec.virtual_path}, headers=H_B)
        assert r.status_code == 404, url
        assert r.json() == {"detail": "not found"}
    moved = client.post(
        "/files/move", json={"source": rec.virtual_path, "destination": f"{CHAT}/stolen.csv"}, headers=H_B
    )
    assert moved.status_code == 404
    assert client.get("/files/view", params={"path": rec.virtual_path}, headers=H).content == b"mine"


def test_the_chat_header_does_not_select_another_users_files(client: TestClient, store: Store) -> None:
    """The chat is only a folder: another user naming the same chat id gets nothing."""
    put(store, "q.csv")
    r = client.get("/files/list", params={"directory": CHAT}, headers={**H_B, "X-Session-Id": "chat-1"})
    assert r.json()["entries"] == []


# --- reading text --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("notes.txt", "hello wörld".encode()),
        ("table.csv", b"a,b\n1,2\n"),
        ("README.md", b"# Title\n"),
        ("UPPER.MD", b"# Title\n"),
        ("script.py", b"print(1)\n"),
        ("empty.txt", b""),
    ],
)
def test_read_returns_text_files_and_records_it(
    client: TestClient, audit_lines: AuditLines, name: str, data: bytes
) -> None:
    """Text, Markdown and CSV come back as ``content``, one ``file.read`` event each."""
    upload(client, name, data)
    r = client.get("/files/read", params={"path": f"{CHAT}/{name}"}, headers=H)
    assert r.status_code == 200
    assert r.json() == {"content": data.decode()}
    assert file_operations(audit_lines) == ["file.upload", "file.read"]


def test_md_and_csv_are_text_whatever_the_type_table_says(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, audit_lines: AuditLines
) -> None:
    """The two extensions are named by the contract, so they do not depend on a type guess."""
    monkeypatch.setattr(store_module, "_mime", lambda _name: "application/octet-stream")
    upload(client, "table.csv", b"a,b\n")
    upload(client, "README.md", b"# T\n")
    assert client.get("/files/read", params={"path": f"{CHAT}/table.csv"}, headers=H).json() == {"content": "a,b\n"}
    assert client.get("/files/read", params={"path": f"{CHAT}/README.md"}, headers=H).json() == {"content": "# T\n"}
    assert file_operations(audit_lines) == ["file.upload", "file.upload", "file.read", "file.read"]


@pytest.mark.parametrize(
    "name",
    ["quarterly-figures.xlsx", "report.pdf", "mail.eml", "data.json", "photo.png", "blob", "archive.zip"],
)
def test_read_refuses_what_is_not_text(client: TestClient, audit_lines: AuditLines, name: str) -> None:
    """Anything else is 415 and nothing is disclosed, so nothing is recorded."""
    upload(client, name, b"PK\x03\x04")
    r = client.get("/files/read", params={"path": f"{CHAT}/{name}"}, headers=H)
    assert r.status_code == 415
    assert r.json() == {"detail": "not readable as text"}
    assert file_operations(audit_lines) == ["file.upload"]


def test_read_of_a_missing_file_is_not_found_and_unrecorded(client: TestClient, audit_lines: AuditLines) -> None:
    """Open WebUI probes ``<home>/AGENTS.md`` for every chat; it and any other absent file answer 404."""
    for path in ("/workspace/AGENTS.md", f"{CHAT}/AGENTS.md", f"{CHAT}/reports/none.txt"):
        r = client.get("/files/read", params={"path": path}, headers=H)
        assert r.status_code == 404, path
        assert r.json() == {"detail": "not found"}
    assert file_operations(audit_lines) == []


def test_read_of_a_folder_is_not_found(client: TestClient) -> None:
    """A folder is not a file."""
    client.post("/files/mkdir", json={"path": f"{CHAT}/reports"}, headers=H)
    assert client.get("/files/read", params={"path": f"{CHAT}/reports"}, headers=H).status_code == 404


def test_read_stops_at_the_result_limit(limited_client: TestClient, audit_stream: io.StringIO) -> None:
    """Up to ``result_max_kb`` is read whole; a larger file is refused and recorded as nothing."""
    exact, over = b"a" * 1024, b"a" * 1025
    upload(limited_client, "exact.txt", exact)
    upload(limited_client, "over.txt", over)
    ok = limited_client.get("/files/read", params={"path": f"{CHAT}/exact.txt"}, headers=H)
    assert ok.json() == {"content": exact.decode()}
    big = limited_client.get("/files/read", params={"path": f"{CHAT}/over.txt"}, headers=H)
    assert big.status_code == 413
    assert big.json() == {"detail": "file too large to read here"}
    assert audit_stream.getvalue().count('"file.read"') == 1


# --- limits --------------------------------------------------------------------------------------------------------


def test_upload_limits(limited_client: TestClient, store: Store) -> None:
    """Over the per-file limit is 413; over the user's quota is 507; the limits are in MiB and exact."""
    too_big = upload(limited_client, "big.bin", b"x" * (MIB + 1))
    assert too_big.status_code == 413
    assert too_big.json() == {"detail": "file too large"}
    assert upload(limited_client, "one.bin", b"x" * MIB).status_code == 200
    assert upload(limited_client, "two.bin", b"x" * MIB).status_code == 200
    over = upload(limited_client, "three.bin", b"x")
    assert over.status_code == 507
    assert over.json() == {"detail": "workspace quota exceeded"}
    assert [r.name for r in store.files_in_chat(ME)] == ["one.bin", "two.bin"]


def test_the_quota_is_per_user(limited_client: TestClient) -> None:
    """One user's full workspace does not stop another."""
    upload(limited_client, "one.bin", b"x" * MIB)
    upload(limited_client, "two.bin", b"x" * MIB)
    assert upload(limited_client, "three.bin", b"x", headers=H_B).status_code == 200


def test_a_full_volume_is_507(client: TestClient, monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    """The store's own refusal for a full disk reaches the browser as 507."""

    def full(*_args: object, **_kwargs: object) -> FileRecord:
        raise store_module.StorageFull("storage is full")

    monkeypatch.setattr(store, "add_upload", full)
    r = upload(client, "q.csv")
    assert r.status_code == 507
    assert r.json() == {"detail": "storage is full"}


# --- the upload limit while the body arrives ---------------------------------------------------------------------

BOUND = 2 * MIB  # the limited client's per-file limit (1 MiB) plus the allowance for multipart framing (1 MiB)
BOUNDARY = "wbtest"
MULTIPART = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}


class Drive:
    """What happened when a request went through ``BodyLimit``.

    Attributes:
        app_calls: How often the wrapped app was called.
        receives: How often the app (or the middleware) asked for a body chunk.
        sent: The messages sent to the client.
        error: The error that came out of the middleware, if any.
    """

    def __init__(self) -> None:
        """Start with nothing having happened."""
        self.app_calls = 0
        self.receives = 0
        self.sent: list[Message] = []
        self.error: BaseException | None = None

    @property
    def status(self) -> int | None:
        """The status the client was sent, if a response was started."""
        starts = [m["status"] for m in self.sent if m["type"] == "http.response.start"]
        return starts[0] if starts else None


def drive(
    *,
    bound: int = 1000,
    request_max: int = 10_000,
    path: str = "/files/upload",
    method: str = "POST",
    declared: int | None,
    chunks: list[bytes],
) -> Drive:
    """Send a request through ``BodyLimit`` in front of an app that reads its whole body and answers 200."""
    result = Drive()
    pending = list(chunks)

    async def inner(_scope: Scope, receive: Receive, send: Send) -> None:
        result.app_calls += 1
        more = True
        while more:
            more = (await receive()).get("more_body", False)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive() -> Message:
        result.receives += 1
        body = pending.pop(0) if pending else b""
        return {"type": "http.request", "body": body, "more_body": bool(pending)}

    async def send(message: Message) -> None:
        result.sent.append(message)

    headers = [] if declared is None else [(b"content-length", str(declared).encode())]
    scope: Scope = {"type": "http", "method": method, "path": path, "headers": headers}

    async def run() -> None:
        try:
            await BodyLimit(inner, upload_max=bound, request_max=request_max)(scope, receive, send)
        except HTTPException as exc:
            result.error = exc

    asyncio.run(run())
    return result


def test_a_declared_length_over_the_bound_is_refused_before_any_body_is_read() -> None:
    """The answer is 413 straight away: the body is never asked for and the app never runs."""
    r = drive(declared=1001, chunks=[b"x" * 1001])
    assert r.status == 413
    assert r.app_calls == 0
    assert r.receives == 0
    assert b'"file too large"' in r.sent[1]["body"]


def test_a_declared_length_at_the_bound_goes_through() -> None:
    """The bound itself is allowed."""
    r = drive(declared=1000, chunks=[b"x" * 1000])
    assert (r.status, r.app_calls, r.error) == (200, 1, None)


def test_a_body_without_a_declared_length_is_counted_as_it_arrives() -> None:
    """A chunked body has no length to trust: reading stops at the first chunk that passes the bound."""
    r = drive(declared=None, chunks=[b"x" * 400, b"x" * 400, b"x" * 201, b"x" * 400, b"x" * 400])
    assert isinstance(r.error, HTTPException)
    assert (r.error.status_code, r.error.detail) == (413, "file too large")
    assert r.receives == 3
    assert r.status is None


def test_a_chunked_body_up_to_the_bound_goes_through() -> None:
    """The count includes every chunk and the bound itself is allowed."""
    r = drive(declared=None, chunks=[b"x" * 400, b"x" * 400, b"x" * 200])
    assert (r.status, r.app_calls, r.error, r.receives) == (200, 1, None, 3)


def test_a_declared_length_that_lies_is_caught_by_the_count() -> None:
    """A small declared length does not excuse a larger body."""
    r = drive(declared=10, chunks=[b"x" * 600, b"x" * 600])
    assert isinstance(r.error, HTTPException)
    assert r.error.status_code == 413


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/files/mkdir"), ("GET", "/files/upload"), ("POST", "/files/upload/x"), ("POST", "/mcp")],
)
def test_every_other_request_has_the_request_bound_not_the_uploads(method: str, path: str) -> None:
    """Up to the request bound a body passes, whatever the upload's bound; past it, 413 ``request too large``."""
    r = drive(method=method, path=path, declared=5000, chunks=[b"x" * 5000])
    assert (r.status, r.app_calls, r.error) == (200, 1, None)
    r = drive(method=method, path=path, declared=10_001, chunks=[b"x" * 10_001])
    assert (r.status, r.app_calls, r.receives) == (413, 0, 0)
    assert b'"request too large"' in r.sent[1]["body"]
    r = drive(method=method, path=path, declared=None, chunks=[b"x" * 6000, b"x" * 6000])
    assert isinstance(r.error, HTTPException)
    assert (r.error.status_code, r.error.detail) == (413, "request too large")


def test_a_body_that_is_not_http_is_left_alone() -> None:
    """Lifespan and websocket scopes go straight to the app."""
    seen: list[str] = []

    async def inner(scope: Scope, _receive: Receive, _send: Send) -> None:
        seen.append(scope["type"])

    async def never_received() -> Message:
        raise AssertionError("not called")

    async def never_sent(_message: Message) -> None:
        raise AssertionError("not called")

    asyncio.run(BodyLimit(inner, upload_max=1)({"type": "lifespan"}, never_received, never_sent))
    assert seen == ["lifespan"]


def multipart_chunks(file_chunks: list[bytes]) -> Iterator[bytes]:
    """Yield a multipart upload of ``file_chunks`` one chunk at a time, so it is sent chunked."""
    yield (
        f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="file"; filename="secret-name-big.bin"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode()
    yield from file_chunks
    yield f"\r\n--{BOUNDARY}--\r\n".encode()


def test_an_oversized_declared_upload_is_refused_and_never_reaches_the_endpoint(
    limited_client: TestClient,
    store: Store,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Over the bound by ``Content-Length``: 413, no store call, no file, no audit event, nothing logged."""
    calls: list[str] = []
    monkeypatch.setattr(store, "add_upload", lambda *_a, **_k: calls.append("add_upload"))
    caplog.set_level(logging.DEBUG)
    r = upload(limited_client, "secret-name-big.bin", b"x" * (BOUND + 1))
    assert r.status_code == 413
    assert r.json() == {"detail": "file too large"}
    assert calls == []
    assert file_operations(audit_lines) == []
    assert store.files_in_chat(ME) == []
    logged = " ".join(record.getMessage() for record in server_log(caplog))
    assert "secret-name" not in logged
    assert "/files/upload" not in logged
    assert [record for record in server_log(caplog) if record.exc_info] == []


def test_an_oversized_streamed_upload_is_refused_while_it_arrives(
    limited_client: TestClient,
    store: Store,
    audit_lines: AuditLines,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A chunked body has no ``Content-Length``; the count stops it (where it stops reading: see the ASGI tests)."""
    calls: list[str] = []
    monkeypatch.setattr(store, "add_upload", lambda *_a, **_k: calls.append("add_upload"))
    caplog.set_level(logging.DEBUG)
    chunks = [b"x" * (256 * 1024)] * 24  # 6 MiB in all, three times the bound
    r = limited_client.post(
        "/files/upload",
        params={"directory": CHAT},
        headers={**H, **MULTIPART},
        content=multipart_chunks(chunks),
    )
    assert "content-length" not in r.request.headers
    assert r.request.headers["transfer-encoding"] == "chunked"
    assert r.status_code == 413
    assert r.json() == {"detail": "file too large"}
    assert calls == []
    assert file_operations(audit_lines) == []
    assert store.files_in_chat(ME) == []
    logged = " ".join(record.getMessage() for record in server_log(caplog))
    assert "secret-name" not in logged
    assert [record for record in server_log(caplog) if record.exc_info] == []


def test_uploads_under_the_limit_succeed_declared_and_streamed(limited_client: TestClient, store: Store) -> None:
    """The guard does not get in the way: a file at the per-file limit, and a chunked upload below it."""
    assert upload(limited_client, "exact.bin", b"x" * MIB).status_code == 200
    r = limited_client.post(
        "/files/upload",
        params={"directory": CHAT},
        headers={**H, **MULTIPART},
        content=multipart_chunks([b"y" * (128 * 1024)] * 4),
    )
    assert r.status_code == 200
    assert r.json() == {"path": f"{CHAT}/secret-name-big.bin", "size": 512 * 1024}
    assert [rec.size for rec in store.files_in_chat(ME)] == [MIB, 512 * 1024]


def test_a_file_over_the_per_file_limit_but_within_the_bound_is_still_refused_by_the_store(
    limited_client: TestClient, store: Store
) -> None:
    """The bound only allows for framing; the exact per-file limit stays the store's."""
    r = upload(limited_client, "big.bin", b"x" * (MIB + 1))
    assert r.status_code == 413
    assert r.json() == {"detail": "file too large"}
    assert store.files_in_chat(ME) == []


def test_other_routes_have_the_request_bound_not_the_uploads(limited_client: TestClient) -> None:
    """Within the request bound a large body reaches the route, which answers for itself; past it, 413."""
    r = limited_client.post("/files/mkdir", json={"path": "x" * (BOUND - 100)}, headers=H)
    assert r.status_code == 400
    r = limited_client.post("/files/mkdir", json={"path": "x" * (BOUND + 1)}, headers=H)
    assert (r.status_code, r.json()) == (413, {"detail": "request too large"})


# --- hostile input -------------------------------------------------------------------------------------------------

BAD_PATHS = [
    "/workspace/chat-1/../../x",
    "/workspace/../x",
    "/etc/passwd",
    "relative/path",
    "/workspace/chat-1/a\x00b",
    "/workspace2/chat-1/a.txt",
]

ROUTES: dict[str, Callable[[TestClient, str], Any]] = {
    "list": lambda c, bad: c.get("/files/list", params={"directory": bad}, headers=H),
    "upload": lambda c, bad: upload(c, "a.txt", directory=bad),
    "view": lambda c, bad: c.get("/files/view", params={"path": bad}, headers=H),
    "read": lambda c, bad: c.get("/files/read", params={"path": bad}, headers=H),
    "delete": lambda c, bad: c.delete("/files/delete", params={"path": bad}, headers=H),
    "mkdir": lambda c, bad: c.post("/files/mkdir", json={"path": bad}, headers=H),
    "move-source": lambda c, bad: c.post(
        "/files/move", json={"source": bad, "destination": f"{CHAT}/b.txt"}, headers=H
    ),
    "move-destination": lambda c, bad: c.post(
        "/files/move", json={"source": f"{CHAT}/a.txt", "destination": bad}, headers=H
    ),
}


@pytest.mark.parametrize("bad", BAD_PATHS)
@pytest.mark.parametrize("route", ROUTES)
def test_traversal_in_query_parameters_is_refused(client: TestClient, route: str, bad: str) -> None:
    """A path outside the user's root is a 400 on every route, and the answer does not repeat it."""
    upload(client, "a.txt")
    r = ROUTES[route](client, bad)
    assert r.status_code == 400, (route, bad, r.text)
    assert bad not in r.text
    assert "etc" not in r.text
    assert set(r.json()) == {"detail"}


def test_a_symlink_planted_in_a_folder_is_never_followed(client: TestClient, store: Store, settings: Settings) -> None:
    """A link in the way is refused, whatever it points at."""
    rec = put(store, "q.csv")
    outside = settings.data_dir / "outside.txt"
    outside.write_text("not yours")
    real = Workspaces(settings.data_dir).resolve(ME, rec.virtual_path)
    real.unlink()
    real.symlink_to(outside)
    for route in ("view", "read"):
        r = client.get(f"/files/{route}", params={"path": rec.virtual_path}, headers=H)
        assert r.status_code == 400, route
        assert "not yours" not in r.text


# --- answers and logs ----------------------------------------------------------------------------------------------


def test_no_answer_and_no_log_line_carries_a_name_a_path_or_a_user(
    limited_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Every refusal is short fixed text; the model sees the bodies and operators the logs."""
    caplog.set_level(logging.DEBUG)
    upload(limited_client, "secret-name-a.xlsx", b"a")
    upload(limited_client, "secret-name-b.xlsx", b"b")
    answers = [
        limited_client.get("/files/view", params={"path": f"{CHAT}/secret-name-none.xlsx"}, headers=H),
        limited_client.get("/files/view", params={"path": f"{CHAT}/../../secret-name-x.xlsx"}, headers=H),
        limited_client.post(
            "/files/move",
            json={"source": f"{CHAT}/secret-name-a.xlsx", "destination": f"{CHAT}/secret-name-b.xlsx"},
            headers=H,
        ),
        upload(limited_client, "secret-name-big.xlsx", b"x" * (MIB + 1)),
        upload(limited_client, "secret-name-big.xlsx", b"x" * MIB),
        upload(limited_client, "secret-name-big.xlsx", b"x" * MIB),
        limited_client.get("/files/read", params={"path": f"{CHAT}/secret-name-a.xlsx"}, headers=H),
    ]
    assert [a.status_code for a in answers] == [404, 400, 409, 413, 200, 507, 415]
    for answer in answers:
        assert set(answer.json()) <= {"detail", "path", "size"}, answer.text
    for answer in (a for a in answers if a.status_code >= 400):
        for forbidden in ("secret-name", "user-a", "/workspace/"):
            assert forbidden not in answer.text
    logged = " ".join(record.getMessage() for record in server_log(caplog))
    for forbidden in ("secret-name", "user-a"):
        assert forbidden not in logged
    assert [record for record in server_log(caplog) if record.exc_info] == []


# --- who may call --------------------------------------------------------------------------------------------------

BROWSER_ROUTES: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", "/files/list?directory=/workspace/chat-1", {}),
    ("POST", "/files/upload?directory=/workspace/chat-1", {"files": {"file": ("a.txt", b"x")}}),
    ("GET", "/files/view?path=/workspace/chat-1/a.txt", {}),
    ("GET", "/files/read?path=/workspace/chat-1/a.txt", {}),
    ("DELETE", "/files/delete?path=/workspace/chat-1/a.txt", {}),
    ("POST", "/files/mkdir", {"json": {"path": "/workspace/chat-1/x"}}),
    ("POST", "/files/move", {"json": {"source": "/workspace/chat-1/a.txt", "destination": "/workspace/chat-1/b.txt"}}),
]


@pytest.mark.parametrize(("method", "url", "kwargs"), BROWSER_ROUTES)
def test_every_route_needs_the_key_and_a_user(
    client: TestClient, method: str, url: str, kwargs: dict[str, Any]
) -> None:
    """The key is checked first; then the user header."""
    upload(client, "a.txt")
    no_key = client.request(method, url, headers={"X-User-Id": "user-a"}, **kwargs)
    assert no_key.status_code == 401
    no_user = client.request(method, url, headers=KEY_ONLY, **kwargs)
    assert no_user.status_code == 400
    assert no_user.json() == {"detail": "missing or invalid user"}
    assert client.get("/files/view", params={"path": f"{CHAT}/a.txt"}, headers=H).content == b"x"


# --- the list_files tool ----------------------------------------------------------------------------------------


def test_list_files_tool_lists_the_chats_files(client: TestClient, store: Store, audit_lines: AuditLines) -> None:
    """The model sees this chat's files with their paths, sizes, kinds and dates, and nothing of other chats."""
    upload(client, "q.csv", b"a,b\n", headers=HG)
    store.mkdir(PG, f"{CHAT}/reports")
    put(store, "notes.txt", b"hello", directory=f"{CHAT}/reports", p=PG_SETUP)
    put(
        store,
        "elsewhere.txt",
        b"other chat",
        directory="/workspace/chat-2",
        p=dataclasses.replace(PG_SETUP, chat_id="chat-2"),
    )
    no_arguments = answered(rpc(client, "tools/call", {"name": "list_files"}, headers=HG))
    empty_arguments = answered(acall(client, "list_files", {}))
    assert no_arguments == empty_arguments
    files = no_arguments["files"]
    assert [(f["path"], f["size"], f["kind"]) for f in files] == [
        (f"{CHAT}/reports/notes.txt", 5, "upload"),
        (f"{CHAT}/q.csv", 4, "upload"),
    ]
    for f in files:
        assert set(f) == {"path", "size", "modified", "kind"}
        assert datetime.fromisoformat(f["modified"]).tzinfo is not None
    assert set(no_arguments) == {"files", "truncated", "hint", "outputs"}
    assert (no_arguments["truncated"], no_arguments["hint"], no_arguments["outputs"]) == (False, None, [])
    assert file_operations(audit_lines) == ["file.upload"]


def test_list_files_is_capped_at_the_result_limit(
    limited_client: TestClient, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The newest files come first until the answer would pass ``result_max_kb``; the oldest are left out."""
    tick(store, monkeypatch)
    for i in range(40):
        put(store, f"file-{i:02d}.txt", p=PG)
    body = answered(acall(limited_client, "list_files"))
    shown = [f["path"] for f in body["files"]]
    assert sent_size(body) <= 1024
    assert body["truncated"] is True
    assert shown == [f"{CHAT}/file-{39 - i:02d}.txt" for i in range(len(shown))]
    assert shown[0] == f"{CHAT}/file-39.txt"
    assert f"{CHAT}/file-00.txt" not in shown
    assert 5 < len(shown) < 40
    assert older_hint(40 - len(shown), 40) in body["hint"]
    assert "newest first" in body["hint"]
    assert "1 KB" in body["hint"]


def test_list_files_lists_the_newest_first_and_breaks_ties_by_name(
    client: TestClient, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Newest first by creation time; files made at the same moment come in name order."""
    seconds = iter([1_000_001.0, 1_000_003.0, 1_000_002.0, 1_000_003.0, 1_000_003.0])
    monkeypatch.setattr(store, "_clock", lambda: next(seconds))
    for name in ("oldest.txt", "tie-b.txt", "middle.txt", "tie-c.txt", "tie-a.txt"):
        put(store, name, p=PG)
    files = answered(acall(client, "list_files"))["files"]
    assert [f["path"] for f in files] == [
        f"{CHAT}/tie-a.txt",
        f"{CHAT}/tie-b.txt",
        f"{CHAT}/tie-c.txt",
        f"{CHAT}/middle.txt",
        f"{CHAT}/oldest.txt",
    ]


def test_list_files_says_nothing_is_cut_when_all_fit(client: TestClient, store: Store) -> None:
    """A list that fits is whole, and has no hint."""
    put(store, "a.txt", p=PG)
    put(store, "b.txt", p=PG)
    body = answered(acall(client, "list_files"))
    assert body["truncated"] is False
    assert body["hint"] is None
    assert len(body["files"]) == 2


def test_list_files_cap_holds_at_every_length(
    limited_client: TestClient, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """From one file to far over the limit the answer stays within it, is cut only when it must be, and adds up."""
    tick(store, monkeypatch)
    cut_seen = False
    for count in range(1, 26):
        put(store, f"file-{count:02d}.txt", p=PG)
        body = answered(acall(limited_client, "list_files"))
        assert sent_size(body) <= 1024, count
        assert body["files"][0]["path"] == f"{CHAT}/file-{count:02d}.txt", count
        assert body["truncated"] is (len(body["files"]) < count), count
        cut_seen = cut_seen or body["truncated"]
        assert body["truncated"] == cut_seen, count
        if body["truncated"]:
            assert older_hint(count - len(body["files"]), count) in body["hint"]
    assert cut_seen


def test_list_files_cap_counts_bytes_not_characters(limited_client: TestClient, store: Store) -> None:
    """Names with umlauts take two bytes a character in the answer, and the limit is in bytes."""
    for i in range(20):
        put(store, f"{'\u00dc' * 60}-{i:02d}.txt", p=PG)
    body = answered(acall(limited_client, "list_files"))
    assert sent_size(body) <= 1024
    assert body["truncated"] is True


def test_list_files_cap_may_show_no_file_at_all(limited_client: TestClient, store: Store) -> None:
    """A single entry larger than the limit is left out, and the hint still counts it."""
    put(store, "\x01" * 190 + ".txt", p=PG)
    body = answered(acall(limited_client, "list_files"))
    assert body["files"] == []
    assert body["truncated"] is True
    assert older_hint(1, 1) in body["hint"]


def test_list_files_tool_is_empty_for_a_new_chat_and_other_users(client: TestClient, store: Store) -> None:
    """Nothing is made by asking, and another user's files, or another chat's, are not listed."""
    nothing: dict[str, Any] = {"files": [], "truncated": False, "hint": None, "outputs": []}
    assert answered(acall(client, "list_files")) == nothing
    put(store, "q.csv", p=PG)
    assert answered(acall(client, "list_files", headers=HG_B)) == nothing
    assert answered(acall(client, "list_files", headers={**HG, "X-Session-Id": "chat-2"})) == nothing
    assert len(answered(acall(client, "list_files"))["files"]) == 1


@pytest.mark.parametrize(
    ("depth", "past_the_parser"), [(300, False), (20_000, True)], ids=["past the depth limit", "past the parser"]
)
def test_a_files_body_nested_too_deeply_is_a_422(client: TestClient, depth: int, past_the_parser: bool) -> None:
    """The browser's JSON routes read a body as the tools do: past the depth limit, or the parser's, one short refusal.

    The premise of the second case is checked first: Python's JSON parser itself raises ``RecursionError`` on it.
    """
    body = '{"path": ' + "[" * depth + '"x"' + "]" * depth + "}"
    if past_the_parser:
        with pytest.raises(RecursionError):
            json.loads(body)
    r = client.post("/files/mkdir", content=body, headers={**H, "Content-Type": "application/json"})
    assert (r.status_code, r.json()) == (422, {"detail": "request nested too deeply"})
