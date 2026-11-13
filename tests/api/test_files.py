"""The file browser Open WebUI calls through its proxy, and the ``list_files`` and ``display_file`` tools.

Every read or change of content is one audit event naming the file by id; no answer and no log line carries a
file name, a path or a user id.
"""

import dataclasses
import io
import logging
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from tests.api.conftest import KEY
from workbench.api import store as store_module
from workbench.api.audit import AuditLog
from workbench.api.main import create_app
from workbench.api.openapi import TOOL_TAG
from workbench.api.store import NotFound, Store
from workbench.config import Settings
from workbench.models import FileRecord, Principal
from workbench.paths import Workspaces

H = {"Authorization": f"Bearer {KEY}", "X-User-Id": "user-a", "X-Session-Id": "chat-1"}
H_B = {**H, "X-User-Id": "user-b"}
KEY_ONLY = {"Authorization": f"Bearer {KEY}"}
CHAT = "/workspace/chat-1"
ME = Principal("open-webui", "user-a", "chat-1")
MIB = 1024 * 1024

AuditLines = Callable[[], list[dict[str, Any]]]


def upload(
    client: TestClient, name: str, data: bytes = b"x", *, directory: str = CHAT, headers: dict[str, str] = H
) -> Any:
    """Upload ``data`` as ``name`` the way Open WebUI's file browser does."""
    return client.post("/files/upload", params={"directory": directory}, headers=headers, files={"file": (name, data)})


def put(store: Store, name: str, data: bytes = b"x", *, directory: str = CHAT) -> FileRecord:
    """Store ``data`` as ``name`` for the synthetic user, without going through the api."""
    return store.add_upload(ME, directory, name, io.BytesIO(data), max_bytes=100 * MIB, quota_bytes=1000 * MIB)


def file_operations(audit_lines: AuditLines) -> list[str]:
    """The operations of the audit events about files, in order."""
    return [line["operation"] for line in audit_lines() if line["operation"].startswith("file.")]


def server_log(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The log records of the app and its server; the test client's own request lines are left out."""
    return [record for record in caplog.records if not record.name.startswith("httpx")]


@pytest.fixture
def limited_client(settings: Settings, store: Store, audit: AuditLog) -> Iterator[TestClient]:
    """A client on an app with small limits: 1 MiB per upload, 2 MiB per user, 1 KiB results."""
    small = dataclasses.replace(settings, upload_max_mb=1, workspace_max_mb=2, result_max_kb=1)
    with TestClient(create_app(small, store=store, audit=audit)) as test_client:
        yield test_client


# --- the happy path ------------------------------------------------------------------------------------------------


def test_upload_then_list_view_display(client: TestClient, audit_lines: AuditLines) -> None:
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
    display = client.get("/files/display", params={"path": f"{CHAT}/q.csv"}, headers=H).json()
    assert display == {"path": f"{CHAT}/q.csv", "exists": True, "name": "q.csv", "mime_type": "text/csv"}
    assert [line["operation"] for line in audit_lines()][-3:] == ["file.upload", "file.view", "file.display"]


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


# --- file names in real life -------------------------------------------------------------------------------------


def test_unicode_names_round_trip(client: TestClient) -> None:
    """Umlauts and spaces survive upload, list, view and display exactly, and the header is RFC 5987."""
    name = "Übersicht März.xlsx"
    assert upload(client, name, b"data").json() == {"path": f"{CHAT}/{name}", "size": 4}
    names = [e["name"] for e in client.get("/files/list", params={"directory": CHAT}, headers=H).json()["entries"]]
    assert names == [name]
    view = client.get("/files/view", params={"path": f"{CHAT}/{name}"}, headers=H)
    assert view.content == b"data"
    assert view.headers["content-disposition"] == "inline; filename*=UTF-8''%C3%9Cbersicht%20M%C3%A4rz.xlsx"
    display = client.get("/files/display", params={"path": f"{CHAT}/{name}"}, headers=H).json()
    assert (display["path"], display["name"], display["exists"]) == (f"{CHAT}/{name}", name, True)


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
    """Upload, view, read, display and delete are all recorded, by file id and never by name."""
    upload(client, "secret-name.xlsx", b"PK")
    upload(client, "secret-notes.txt", b"hello")
    sheet, notes = store.get(ME, f"{CHAT}/secret-name.xlsx"), store.get(ME, f"{CHAT}/secret-notes.txt")
    client.get("/files/view", params={"path": sheet.virtual_path}, headers=H)
    client.get("/files/read", params={"path": notes.virtual_path}, headers=H)
    client.get("/files/display", params={"path": sheet.virtual_path}, headers=H)
    client.delete("/files/delete", params={"path": sheet.virtual_path}, headers=H)
    events = [line for line in audit_lines() if line["operation"].startswith("file.")]
    assert [(e["operation"], e["target"]["ids"]) for e in events] == [
        ("file.upload", [sheet.id]),
        ("file.upload", [notes.id]),
        ("file.view", [sheet.id]),
        ("file.read", [notes.id]),
        ("file.display", [sheet.id]),
        ("file.delete", [sheet.id]),
    ]
    assert all(e["criteria"] == {"caller": "open-webui"} for e in events)
    assert all(e["target"]["container"] == "chat:chat-1" for e in events)
    assert "secret-name" not in audit_stream.getvalue()
    assert "secret-notes" not in audit_stream.getvalue()


def test_list_mkdir_and_move_are_not_recorded_and_absent_files_leave_no_event(
    client: TestClient, audit_lines: AuditLines
) -> None:
    """No file, no id, no event: a miss records nothing."""
    client.get("/files/list", params={"directory": CHAT}, headers=H)
    client.get("/files/view", params={"path": f"{CHAT}/none.txt"}, headers=H)
    client.get("/files/read", params={"path": f"{CHAT}/none.txt"}, headers=H)
    client.get("/files/display", params={"path": f"{CHAT}/none.txt"}, headers=H)
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


@pytest.mark.parametrize("route", ["view", "read", "display"])
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
    display = client.get("/files/display", params={"path": rec.virtual_path}, headers=H_B).json()
    assert display["exists"] is False
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
    "display": lambda c, bad: c.get("/files/display", params={"path": bad}, headers=H),
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
    for route in ("view", "read", "display"):
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
    ("GET", "/files/display?path=/workspace/chat-1/a.txt", {}),
    ("DELETE", "/files/delete?path=/workspace/chat-1/a.txt", {}),
    ("POST", "/files/mkdir", {"json": {"path": "/workspace/chat-1/x"}}),
    ("POST", "/files/move", {"json": {"source": "/workspace/chat-1/a.txt", "destination": "/workspace/chat-1/b.txt"}}),
    ("POST", "/tools/list_files", {}),
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


# --- the display_file tool ---------------------------------------------------------------------------------------


def test_display_of_a_missing_file_says_so(client: TestClient, audit_lines: AuditLines) -> None:
    """Not an error for the model: ``exists`` is false, and no event is written for a file that is not there."""
    r = client.get("/files/display", params={"path": f"{CHAT}/none.xlsx"}, headers=H)
    assert r.status_code == 200
    assert r.json() == {"path": f"{CHAT}/none.xlsx", "exists": False, "name": None, "mime_type": None}
    assert file_operations(audit_lines) == []


def test_display_of_a_file_in_a_folder_gives_its_base_name(client: TestClient) -> None:
    """``name`` is the file's own name; ``path`` is the whole path."""
    client.post("/files/mkdir", json={"path": f"{CHAT}/reports"}, headers=H)
    upload(client, "q.xlsx", b"PK", directory=f"{CHAT}/reports")
    r = client.get("/files/display", params={"path": f"{CHAT}/reports/q.xlsx"}, headers=H).json()
    assert r == {
        "path": f"{CHAT}/reports/q.xlsx",
        "exists": True,
        "name": "q.xlsx",
        "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }


# --- the tools in the schema -------------------------------------------------------------------------------------


def test_list_files_tool_is_in_the_schema(app: FastAPI, client: TestClient) -> None:
    """Exactly the two tools, each with an operationId set on its route, and every parameter typed."""
    doc = client.get("/openapi.json", headers=KEY_ONLY).json()
    assert {op["operationId"] for item in doc["paths"].values() for op in item.values()} == {
        "list_files",
        "display_file",
    }
    tools = [r for r in iter_route_contexts(app.routes) if TOOL_TAG in (r.tags or [])]
    assert {r.operation_id for r in tools} == {"list_files", "display_file"}
    assert doc["paths"]["/tools/list_files"]["post"]["operationId"] == "list_files"
    display = doc["paths"]["/files/display"]["get"]
    assert display["operationId"] == "display_file"
    assert [(p["name"], p["in"], p["required"], p["schema"]["type"]) for p in display["parameters"]] == [
        ("path", "query", True, "string")
    ]
    for item in doc["paths"].values():
        for op in item.values():
            assert op["description"].strip()
            assert "Args:" not in op["description"]
            assert "Returns:" not in op["description"]


def test_list_files_tool_lists_the_chats_files(client: TestClient, audit_lines: AuditLines) -> None:
    """The model sees this chat's files with their paths, sizes, kinds and dates, and nothing of other chats."""
    upload(client, "q.csv", b"a,b\n")
    client.post("/files/mkdir", json={"path": f"{CHAT}/reports"}, headers=H)
    upload(client, "notes.txt", b"hello", directory=f"{CHAT}/reports")
    upload(client, "elsewhere.txt", b"other chat", directory="/workspace/chat-2")
    no_body = client.post("/tools/list_files", headers=H)
    empty_body = client.post("/tools/list_files", json={}, headers=H)
    assert no_body.status_code == empty_body.status_code == 200
    assert no_body.json() == empty_body.json()
    files = no_body.json()["files"]
    assert [(f["path"], f["size"], f["kind"]) for f in files] == [
        (f"{CHAT}/q.csv", 4, "upload"),
        (f"{CHAT}/reports/notes.txt", 5, "upload"),
    ]
    for f in files:
        assert set(f) == {"path", "size", "modified", "kind"}
        assert datetime.fromisoformat(f["modified"]).tzinfo is not None
    assert set(no_body.json()) == {"files"}
    assert file_operations(audit_lines) == ["file.upload"] * 3


def test_list_files_tool_is_empty_for_a_new_chat_and_other_users(client: TestClient, store: Store) -> None:
    """Nothing is made by asking, and another user's files are not listed."""
    assert client.post("/tools/list_files", headers=H).json() == {"files": []}
    put(store, "q.csv")
    assert client.post("/tools/list_files", headers=H_B).json() == {"files": []}
    assert client.post("/tools/list_files", headers={**H, "X-Session-Id": "chat-2"}).json() == {"files": []}
    assert len(client.post("/tools/list_files", headers=H).json()["files"]) == 1
