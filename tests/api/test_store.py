"""The store keeps files on disk and their metadata in SQLite, and never crosses the user's own root."""

import dataclasses
import errno
import io
import os
import shutil
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import override

import pytest

from tests.api.conftest import FORMER, PG, PG_SETUP, unrecorded
from workbench import renders
from workbench.api import store as store_module
from workbench.api.store import (
    Conflict,
    Gone,
    NotFound,
    QuotaExceeded,
    RendersFull,
    StagedArtifact,
    StorageFull,
    Store,
    StoreError,
    TooLarge,
    UnreadableArtifact,
    new_file_id,
)
from workbench.models import DerivedRecord, DerivedRole, DerivedSpec, DirEntry, FileRecord, Principal
from workbench.paths import PathError, Workspaces, check_filename

MAX = 100
QUOTA = 1000


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: float = 1_000_000.0) -> None:
        """Start at ``now`` seconds."""
        self.now = now

    def __call__(self) -> float:
        """Return the current test time."""
        return self.now


@pytest.fixture
def clock() -> Clock:
    """A clock the test controls."""
    return Clock()


@pytest.fixture
def workspaces(tmp_path: Path) -> Workspaces:
    """Workspaces under a throwaway data volume."""
    return Workspaces(tmp_path / "data")


@pytest.fixture
def store(tmp_path: Path, workspaces: Workspaces, clock: Clock) -> Store:
    """A store on a throwaway data volume with the test clock."""
    return Store(tmp_path / "data" / "workbench.db", workspaces, clock)


@pytest.fixture
def alice() -> Principal:
    """The first synthetic user."""
    return Principal("ghostwriter", "user-a", "chat-1")


@pytest.fixture
def bob() -> Principal:
    """A second synthetic user of the same caller."""
    return Principal("ghostwriter", "user-b", "chat-1")


def put(
    store: Store, p: Principal, name: str = "q.xlsx", data: bytes = b"x" * 10, directory: str = "/workspace/chat-1"
) -> FileRecord:
    """Upload ``data`` as ``name`` with roomy limits."""
    return store.add_upload(p, directory, name, io.BytesIO(data), max_bytes=MAX, quota_bytes=QUOTA, record=unrecorded)


def stage(tmp_path: Path, job: str, name: str, data: bytes = b"out") -> Path:
    """Write a file the way the executor stages an output."""
    folder = tmp_path / "data" / "staging" / job
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def disk_names(folder: Path) -> list[str]:
    """Names in a real folder, sorted; temporaries included."""
    return sorted(entry.name for entry in folder.iterdir())


# --- uploads ---------------------------------------------------------------


def test_upload_streams_to_disk_and_records_metadata(
    store: Store, alice: Principal, workspaces: Workspaces, clock: Clock
) -> None:
    """The bytes land under the user's root and the record says what was stored."""
    rec = store.add_upload(
        alice, "/workspace/chat-1", "q.xlsx", io.BytesIO(b"x" * 10), max_bytes=MAX, quota_bytes=QUOTA, record=unrecorded
    )
    assert (rec.kind, rec.size, rec.virtual_path) == ("upload", 10, "/workspace/chat-1/q.xlsx")
    assert (rec.caller, rec.user_id, rec.chat_id, rec.name) == ("ghostwriter", "user-a", "chat-1", "q.xlsx")
    assert rec.job_id is None
    assert rec.created_at == clock.now
    assert len(rec.id) == 32
    assert rec.mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    real = workspaces.resolve(alice, rec.virtual_path)
    assert real.read_bytes() == b"x" * 10
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    assert stat.S_IMODE(real.parent.stat().st_mode) == 0o700
    assert store.get(alice, rec.virtual_path) == rec


def test_unknown_types_are_octet_stream(store: Store, alice: Principal) -> None:
    """A name the platform cannot place gets the generic type."""
    assert put(store, alice, "noextension").mime == "application/octet-stream"
    assert put(store, alice, "data.zzunknown").mime == "application/octet-stream"
    assert put(store, alice, "q.csv").mime == "text/csv"
    assert put(store, alice, "notes.md").mime == "text/markdown"
    assert put(store, alice, "mail.eml").mime == "message/rfc822"
    assert put(store, alice, "orders.json").mime == "application/json"
    assert put(store, alice, "orders.jsonl").mime == "application/x-ndjson"
    assert put(store, alice, "events.ndjson").mime == "application/x-ndjson"
    assert put(store, alice, "Q.DOCX").mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_office_templates_have_their_types() -> None:
    """A template is served under its own type, not as an opaque download."""
    assert store_module._mime("deck.potx") == "application/vnd.openxmlformats-officedocument.presentationml.template"
    assert store_module._mime("form.dotx") == "application/vnd.openxmlformats-officedocument.wordprocessingml.template"


def test_the_upload_may_name_a_chat_other_than_the_principals(
    store: Store, alice: Principal, workspaces: Workspaces
) -> None:
    """The chat comes from the path: it is only a folder inside the user's root."""
    rec = put(store, alice, directory="/workspace/chat-2")
    assert (rec.chat_id, rec.virtual_path) == ("chat-2", "/workspace/chat-2/q.xlsx")
    assert workspaces.resolve(alice, rec.virtual_path).read_bytes() == b"x" * 10


def test_unicode_names_round_trip(store: Store, alice: Principal) -> None:
    """A name with non-ASCII characters is kept exactly and listed exactly."""
    rec = put(store, alice, "Übersicht März.xlsx")
    assert rec.name == "Übersicht März.xlsx"
    assert [e.name for e in store.list_dir(alice, "/workspace/chat-1")] == ["Übersicht März.xlsx"]


def test_name_clash_gets_a_suffix(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """A second ``q.xlsx`` becomes ``q (1).xlsx``, a third ``q (2).xlsx``; nothing is overwritten."""
    first = put(store, alice, data=b"one")
    second = put(store, alice, data=b"two")
    third = put(store, alice, data=b"three")
    assert [r.name for r in (first, second, third)] == ["q.xlsx", "q (1).xlsx", "q (2).xlsx"]
    assert len({first.id, second.id, third.id}) == 3
    for rec, data in ((first, b"one"), (second, b"two"), (third, b"three")):
        assert workspaces.resolve(alice, rec.virtual_path).read_bytes() == data


def test_a_clash_with_a_planted_symlink_never_writes_through_it(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """A symlink sitting on the target name counts as taken and its target stays untouched."""
    victim = tmp_path / "victim.txt"
    victim.write_text("synthetic")
    chat = workspaces.chat_dir(alice)
    (chat / "q.xlsx").symlink_to(victim)
    rec = put(store, alice, data=b"new")
    assert rec.name == "q (1).xlsx"
    assert victim.read_text() == "synthetic"
    assert (chat / "q.xlsx").is_symlink()


def test_a_clash_suffix_never_makes_the_name_invalid(store: Store, alice: Principal) -> None:
    """Names at the character and byte limits still get a valid suffixed name."""
    for name in ("a" * 196 + ".csv", "é" * 124 + "a.csv"):
        check_filename(name)
        first = put(store, alice, name)
        second = put(store, alice, name)
        assert first.name == name
        assert second.name != name
        check_filename(second.name)
        assert second.name.endswith(" (1).csv")


def test_concurrent_uploads_of_one_name_all_survive(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """Threads uploading the same name each get their own file; none replaces another."""
    count = 8
    barrier = threading.Barrier(count)
    records: dict[int, FileRecord] = {}
    errors: list[BaseException] = []

    def work(index: int) -> None:
        try:
            barrier.wait()
            records[index] = put(store, alice, data=bytes([index]) * 20)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len({r.name for r in records.values()}) == count
    for index, rec in records.items():
        assert workspaces.resolve(alice, rec.virtual_path).read_bytes() == bytes([index]) * 20
    assert len(store.files_in_chat(alice)) == count


def test_upload_over_the_limit_leaves_nothing(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """Too many bytes raise ``TooLarge``; no file, no temporary and no row remain."""
    with pytest.raises(TooLarge):
        store.add_upload(
            alice,
            "/workspace/chat-1",
            "q.xlsx",
            io.BytesIO(b"x" * (MAX + 1)),
            max_bytes=MAX,
            quota_bytes=QUOTA,
            record=unrecorded,
        )
    assert disk_names(workspaces.chat_dir(alice)) == []
    assert store.files_in_chat(alice) == []
    assert store.usage(alice) == 0


def test_upload_of_exactly_the_limit_is_accepted(store: Store, alice: Principal) -> None:
    """``max_bytes`` is inclusive."""
    rec = store.add_upload(
        alice, "/workspace/chat-1", "q.bin", io.BytesIO(b"x" * MAX), max_bytes=MAX, quota_bytes=QUOTA, record=unrecorded
    )
    assert rec.size == MAX


def test_an_empty_upload_is_a_file(store: Store, alice: Principal) -> None:
    """Zero bytes is a valid file."""
    assert put(store, alice, "empty.txt", b"").size == 0


def test_quota_counts_all_chats_of_the_user(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """Files in ``chat-1`` use up the room for ``chat-2``; the refused upload leaves nothing."""
    store.add_upload(
        alice, "/workspace/chat-1", "a.bin", io.BytesIO(b"x" * 60), max_bytes=MAX, quota_bytes=100, record=unrecorded
    )
    with pytest.raises(QuotaExceeded):
        store.add_upload(
            alice,
            "/workspace/chat-2",
            "b.bin",
            io.BytesIO(b"x" * 50),
            max_bytes=MAX,
            quota_bytes=100,
            record=unrecorded,
        )
    assert disk_names(workspaces.resolve(alice, "/workspace/chat-2")) == []
    assert store.files_in_chat(Principal("ghostwriter", "user-a", "chat-2")) == []
    assert store.usage(alice) == 60
    store.add_upload(
        alice, "/workspace/chat-2", "b.bin", io.BytesIO(b"x" * 40), max_bytes=MAX, quota_bytes=100, record=unrecorded
    )
    assert store.usage(alice) == 100


def test_concurrent_uploads_cannot_both_fit_the_last_room(store: Store, alice: Principal) -> None:
    """Both uploads pass the early check before either is stored; the quota is checked again when stored."""
    gate = threading.Barrier(2)

    class Gated(io.BytesIO):
        def __init__(self) -> None:
            super().__init__(b"x" * 60)
            self.waited = False

        @override
        def read(self, size: int | None = -1) -> bytes:
            if not self.waited:
                self.waited = True
                gate.wait(timeout=10)
            return super().read(size)

    outcomes: list[str] = []

    def work(name: str) -> None:
        try:
            store.add_upload(
                alice, "/workspace/chat-1", name, Gated(), max_bytes=MAX, quota_bytes=100, record=unrecorded
            )
            outcomes.append("stored")
        except QuotaExceeded:
            outcomes.append("quota")

    threads = [threading.Thread(target=work, args=(name,)) for name in ("a.bin", "b.bin")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(outcomes) == ["quota", "stored"]
    assert store.usage(alice) == 60
    assert len(store.files_in_chat(alice)) == 1


def test_quota_is_per_user(store: Store, alice: Principal, bob: Principal) -> None:
    """One user's files never use up another user's quota."""
    store.add_upload(
        alice, "/workspace/chat-1", "a.bin", io.BytesIO(b"x" * 90), max_bytes=MAX, quota_bytes=100, record=unrecorded
    )
    store.add_upload(
        bob, "/workspace/chat-1", "a.bin", io.BytesIO(b"x" * 90), max_bytes=MAX, quota_bytes=100, record=unrecorded
    )
    assert (store.usage(alice), store.usage(bob)) == (90, 90)


def test_quota_counts_outputs_too(store: Store, alice: Principal, tmp_path: Path) -> None:
    """Outputs a tool made use the same room as uploads."""
    job = store.add_job(alice, "run_python")
    store.register_output(
        alice, stage(tmp_path, job.id, "r.csv", b"y" * 70), "r.csv", job.id, file_id=new_file_id(), quota_bytes=10**12
    )
    with pytest.raises(QuotaExceeded):
        store.add_upload(
            alice,
            "/workspace/chat-1",
            "a.bin",
            io.BytesIO(b"x" * 40),
            max_bytes=MAX,
            quota_bytes=100,
            record=unrecorded,
        )


def test_disk_full_maps_to_storage_full(
    store: Store, alice: Principal, workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOSPC while writing raises ``StorageFull`` and removes the partial file."""
    real_write = store_module._write_all
    calls: list[int] = []

    def failing(fd: int, data: bytes) -> None:
        calls.append(len(data))
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        real_write(fd, data)

    monkeypatch.setattr(store_module, "_write_all", failing)
    big = io.BytesIO(b"x" * (3 * 1024 * 1024))
    with pytest.raises(StorageFull):
        store.add_upload(
            alice, "/workspace/chat-1", "q.bin", big, max_bytes=10**9, quota_bytes=10**9, record=unrecorded
        )
    assert len(calls) == 2
    assert disk_names(workspaces.chat_dir(alice)) == []
    assert store.files_in_chat(alice) == []


def test_other_write_errors_are_not_hidden(
    store: Store, alice: Principal, workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An OSError that is not about space is raised as it is, and cleaned up after."""

    def failing(fd: int, data: bytes) -> None:
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(store_module, "_write_all", failing)
    with pytest.raises(OSError, match="I/O error") as info:
        put(store, alice)
    assert not isinstance(info.value, StoreError)
    assert disk_names(workspaces.chat_dir(alice)) == []


def test_a_failing_source_leaves_nothing(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """A client that drops mid-upload leaves no temporary behind."""

    class Broken(io.BytesIO):
        def __init__(self) -> None:
            super().__init__()
            self.sent = False

        @override
        def read(self, size: int | None = -1) -> bytes:
            if not self.sent:
                self.sent = True
                return b"partial"
            raise ConnectionResetError

    with pytest.raises(ConnectionResetError):
        store.add_upload(
            alice, "/workspace/chat-1", "q.bin", Broken(), max_bytes=MAX, quota_bytes=QUOTA, record=unrecorded
        )
    assert disk_names(workspaces.chat_dir(alice)) == []
    assert store.files_in_chat(alice) == []


def test_upload_into_a_folder_made_before(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """A folder inside a chat takes files; the record's name carries the folder."""
    store.mkdir(alice, "/workspace/chat-1/reports")
    rec = put(store, alice, "q.csv", directory="/workspace/chat-1/reports")
    assert (rec.chat_id, rec.name, rec.virtual_path) == ("chat-1", "reports/q.csv", "/workspace/chat-1/reports/q.csv")
    assert workspaces.resolve(alice, rec.virtual_path).read_bytes() == b"x" * 10
    assert store.get(alice, "/workspace/chat-1/reports/q.csv") == rec
    assert put(store, alice, "q.csv", directory="/workspace/chat-1/reports").name == "reports/q (1).csv"
    assert put(store, alice, "q.csv").name == "q.csv"


def test_upload_into_a_missing_folder_is_not_found(store: Store, alice: Principal) -> None:
    """Folders below a chat are made with ``mkdir``; an upload does not invent them."""
    with pytest.raises(NotFound):
        put(store, alice, directory="/workspace/chat-1/nowhere")
    assert store.files_in_chat(alice) == []


def test_upload_into_a_file_is_not_found(store: Store, alice: Principal) -> None:
    """A path that names a file is not a folder to upload into."""
    put(store, alice, "plain.txt")
    with pytest.raises(NotFound):
        put(store, alice, "x.txt", directory="/workspace/chat-1/plain.txt")


def test_part_names_are_reserved(store: Store, alice: Principal, tmp_path: Path) -> None:
    """``.part`` marks a half-written file, so no file or folder may end with it."""
    with pytest.raises(PathError):
        put(store, alice, "x.part")
    with pytest.raises(PathError):
        store.mkdir(alice, "/workspace/chat-1/x.part")
    job = store.add_job(alice, "run_python")
    with pytest.raises(PathError):
        store.register_output(
            alice, stage(tmp_path, job.id, "x.part"), "x.part", job.id, file_id=new_file_id(), quota_bytes=10**12
        )


# --- isolation between users ------------------------------------------------


def test_other_users_files_are_not_found(store: Store, alice: Principal, bob: Principal) -> None:
    """The same virtual path means nothing to another user of the same caller."""
    rec = put(store, alice)
    with pytest.raises(NotFound):
        store.get(bob, rec.virtual_path)
    with pytest.raises(NotFound):
        deleted(store, bob, rec.virtual_path)
    assert store.list_dir(bob, "/workspace/chat-1") == []
    assert store.list_dir(bob, "/workspace") == []
    assert store.files_in_chat(bob) == []
    assert store.usage(bob) == 0
    assert store.get(alice, rec.virtual_path) == rec


def test_the_same_user_id_under_another_caller_is_another_user(store: Store, alice: Principal) -> None:
    """The caller is part of the identity: equal user ids from two callers stay apart."""
    other = Principal("other-caller", "user-a", "chat-1")
    rec = put(store, alice)
    with pytest.raises(NotFound):
        store.get(other, rec.virtual_path)
    assert store.list_dir(other, "/workspace/chat-1") == []
    assert store.usage(other) == 0


def test_two_users_may_hold_the_same_name(store: Store, alice: Principal, bob: Principal) -> None:
    """A clash is only within one user's own chat folder."""
    assert put(store, alice).name == "q.xlsx"
    assert put(store, bob).name == "q.xlsx"


# --- a confined principal ---------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/workspace", "/workspace/chat-2", "/workspace/chat-2/q.csv", "/workspace/chat-1/../chat-2/q.csv", "/etc"],
)
def test_a_confined_principal_finds_nothing_outside_its_chat(store: Store, path: str) -> None:
    """The user's other chat, the root and a path outside the workspace are all not found for a confined principal."""
    other = put(store, dataclasses.replace(PG, chat_id="chat-2"), "q.csv", directory="/workspace/chat-2")
    assert store.get(dataclasses.replace(PG, confined=False), other.virtual_path) == other
    with pytest.raises(NotFound):
        store.list_dir(PG, path)
    with pytest.raises(NotFound):
        store.get(PG, path)
    with pytest.raises(NotFound):
        store.open_file(PG, path)


@pytest.mark.parametrize(
    "path",
    [
        "/workspace",
        "/workspace/chat-2",
        "/workspace/chat-2/q.csv",
        "/workspace/chat-1/../chat-2/q.csv",
        "/workspace/chat-1x/q.csv",
        "workspace/chat-1/q.csv",
        "/etc/passwd",
    ],
)
def test_a_path_outside_the_confined_chat_is_refused_before_anything_is_looked_up(
    store: Store, workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """The chat is read from the path's text first, as a whole segment: another chat's folder is never looked at."""
    put(store, dataclasses.replace(PG, chat_id="chat-2"), "q.csv", directory="/workspace/chat-2")
    resolved: list[str] = []
    real_resolve = workspaces.resolve

    def resolve(p: Principal, virtual: str) -> Path:
        resolved.append(virtual)
        return real_resolve(p, virtual)

    monkeypatch.setattr(workspaces, "resolve", resolve)
    with pytest.raises(NotFound):
        store.get(PG, path)
    with pytest.raises(NotFound):
        store.open_file(PG, path)
    with pytest.raises(NotFound):
        store.list_dir(PG, path)
    with pytest.raises(NotFound):
        put(store, PG, "new.csv", directory=path)
    assert resolved == []
    assert store.list_dir(PG, "/workspace/chat-1") == []
    assert resolved == ["/workspace/chat-1"]


def test_a_confined_upload_goes_into_the_chat_folder_itself(store: Store) -> None:
    """A confined principal uploads into its chat folder, never into a folder below it, even one its user made."""
    store.mkdir(dataclasses.replace(PG, confined=False), "/workspace/chat-1/sub")
    with pytest.raises(NotFound):
        put(store, PG, "q.csv", directory="/workspace/chat-1/sub")
    assert put(store, PG, "q.csv", directory="/workspace/chat-1").virtual_path == "/workspace/chat-1/q.csv"
    assert [rec.name for rec in store.files_in_chat(PG)] == ["q.csv"]


@pytest.mark.parametrize(
    "bad",
    [
        "/workspace/chat-1/../../user-b/chat-1/q.xlsx",
        "/workspace/../etc/passwd",
        "/etc/passwd",
        "workspace/chat-1/q.xlsx",
        "/workspace/chat-1/a\x00b",
        "/workspace/chat-1/" + "x" * 201,
    ],
)
def test_hostile_paths_are_refused_everywhere(store: Store, alice: Principal, bad: str) -> None:
    """Every operation that takes a path raises ``PathError`` for one outside the user's root."""
    ok = "/workspace/chat-1/q.xlsx"
    put(store, alice)
    with pytest.raises(PathError):
        store.get(alice, bad)
    with pytest.raises(PathError):
        deleted(store, alice, bad)
    with pytest.raises(PathError):
        store.mkdir(alice, bad)
    with pytest.raises(PathError):
        store.list_dir(alice, bad)
    with pytest.raises(PathError):
        put(store, alice, directory=bad)
    assert store.get(alice, ok).size == 10


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "../x", "x" * 201, "é" * 200, "a\x00b"])
def test_hostile_file_names_are_refused(store: Store, alice: Principal, tmp_path: Path, name: str) -> None:
    """An upload or an output named like a path is refused before anything is written."""
    with pytest.raises(PathError):
        put(store, alice, name)
    job = store.add_job(alice, "run_python")
    staged = stage(tmp_path, job.id, "ok.txt")
    with pytest.raises(PathError):
        store.register_output(alice, staged, name, job.id, file_id=new_file_id(), quota_bytes=10**12)
    assert staged.exists()
    assert store.files_in_chat(alice) == []


def test_symlinked_chat_folder_is_refused(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """A chat folder replaced by a link is never followed."""
    outside = tmp_path / "outside"
    outside.mkdir()
    root = workspaces.user_root(alice)
    root.mkdir(parents=True)
    (root / "chat-1").symlink_to(outside)
    with pytest.raises(PathError):
        put(store, alice)
    with pytest.raises(PathError):
        store.list_dir(alice, "/workspace/chat-1")
    assert list(outside.iterdir()) == []


# --- files live in chat folders (R9) -----------------------------------------


@pytest.mark.parametrize("target", ["/workspace", "/workspace/chat-1", "/workspace/q.xlsx"])
def test_files_never_live_outside_a_chat_folder(store: Store, alice: Principal, target: str) -> None:
    """The root and a chat folder are not files: ``get`` and ``delete`` refuse them."""
    put(store, alice)
    with pytest.raises(PathError):
        store.get(alice, target)
    with pytest.raises(PathError):
        deleted(store, alice, target)
    assert store.get(alice, "/workspace/chat-1/q.xlsx").size == 10


def test_an_upload_into_the_root_is_refused(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """``/workspace`` is not a chat folder, so a file cannot land at ``/workspace/q.xlsx``."""
    with pytest.raises(PathError):
        put(store, alice, directory="/workspace")
    with pytest.raises(PathError):
        put(store, alice, directory="/workspace/")
    assert not workspaces.user_root(alice).exists() or disk_names(workspaces.user_root(alice)) == []


def test_the_root_lists_the_chats_and_a_chat_lists_its_content(store: Store, alice: Principal) -> None:
    """Listing ``/workspace`` shows the chat folders; listing a chat shows its files and folders."""
    put(store, alice, "a.csv", b"12345")
    put(store, alice, "b.csv", directory="/workspace/chat-2")
    store.mkdir(alice, "/workspace/chat-1/reports")
    chats = store.list_dir(alice, "/workspace")
    assert [(e.name, e.type) for e in chats] == [("chat-1", "directory"), ("chat-2", "directory")]
    entries = store.list_dir(alice, "/workspace/chat-1")
    assert [(e.name, e.type, e.size) for e in entries] == [("a.csv", "file", 5), ("reports", "directory", 0)]


# --- listing ----------------------------------------------------------------


def test_listing_skips_temporaries_and_links(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """Half-written ``.part`` files and symlinks never show; entries are sorted by name."""
    chat = workspaces.chat_dir(alice)
    put(store, alice, "b.txt")
    put(store, alice, "a.txt")
    (chat / "abc.part").write_bytes(b"half")
    (chat / "dir.part").mkdir()
    (chat / "link.txt").symlink_to(chat / "a.txt")
    entries = store.list_dir(alice, "/workspace/chat-1")
    assert [e.name for e in entries] == ["a.txt", "b.txt"]
    assert all(isinstance(e, DirEntry) for e in entries)


def test_listing_reports_size_and_modified_from_disk(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """``modified`` is the file's mtime in seconds."""
    rec = put(store, alice, data=b"abc")
    real = workspaces.resolve(alice, rec.virtual_path)
    os.utime(real, (1_500_000, 1_500_000))
    (entry,) = store.list_dir(alice, "/workspace/chat-1")
    assert (entry.name, entry.type, entry.size, entry.modified) == ("q.xlsx", "file", 3, 1_500_000)


def test_a_chat_folder_not_made_yet_lists_empty_but_a_missing_deeper_folder_does_not(
    store: Store, alice: Principal
) -> None:
    """Root and chat folders exist from first use; a folder below one must have been made."""
    assert store.list_dir(alice, "/workspace") == []
    assert store.list_dir(alice, "/workspace/new-chat") == []
    with pytest.raises(NotFound):
        store.list_dir(alice, "/workspace/new-chat/nowhere")
    put(store, alice, "plain.txt")
    with pytest.raises(NotFound):
        store.list_dir(alice, "/workspace/chat-1/plain.txt")
    with pytest.raises(NotFound):
        store.list_dir(alice, "/workspace/chat-1/plain.txt/inside")


# --- mkdir ------------------------------------------------------------------


def test_mkdir_makes_private_folders_with_parents(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """Every folder made on the way is mode 0700; the answer is the canonical virtual path."""
    assert store.mkdir(alice, "/workspace/chat-1/./reports//2026") == "/workspace/chat-1/reports/2026"
    leaf = workspaces.resolve(alice, "/workspace/chat-1/reports/2026")
    for folder in (leaf, leaf.parent, leaf.parent.parent, workspaces.user_root(alice)):
        assert stat.S_ISDIR(folder.stat().st_mode)
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700, folder


def test_mkdir_is_idempotent_and_keeps_content(store: Store, alice: Principal) -> None:
    """Making a folder that exists changes nothing."""
    store.mkdir(alice, "/workspace/chat-1/reports")
    rec = put(store, alice, "q.csv", directory="/workspace/chat-1/reports")
    assert store.mkdir(alice, "/workspace/chat-1/reports") == "/workspace/chat-1/reports"
    assert store.mkdir(alice, "/workspace/chat-1") == "/workspace/chat-1"
    assert store.get(alice, rec.virtual_path) == rec


def test_mkdir_over_a_file_is_a_conflict(store: Store, alice: Principal) -> None:
    """A file where the folder should be, or in the middle of the path, is ``Conflict``."""
    put(store, alice, "plain.txt")
    with pytest.raises(Conflict):
        store.mkdir(alice, "/workspace/chat-1/plain.txt")
    with pytest.raises(Conflict):
        store.mkdir(alice, "/workspace/chat-1/plain.txt/sub")


def test_mkdir_of_a_new_chat_folder(store: Store, alice: Principal) -> None:
    """A chat folder is a folder: it can be made on its own."""
    assert store.mkdir(alice, "/workspace/chat-9") == "/workspace/chat-9"
    assert [e.name for e in store.list_dir(alice, "/workspace")] == ["chat-9"]


# --- get, delete -------------------------------------------------------------


def test_get_of_a_folder_or_a_missing_file_is_not_found(store: Store, alice: Principal) -> None:
    """Only files with a record can be fetched."""
    store.mkdir(alice, "/workspace/chat-1/reports")
    for path in ("/workspace/chat-1/reports", "/workspace/chat-1/missing.txt", "/workspace/chat-2/q.xlsx"):
        with pytest.raises(NotFound):
            store.get(alice, path)


def test_a_record_whose_file_is_gone_is_not_found_but_can_be_deleted(
    store: Store, alice: Principal, workspaces: Workspaces
) -> None:
    """``get`` checks the disk; ``remove``, by the record, still clears the orphan row."""
    rec = put(store, alice)
    workspaces.resolve(alice, rec.virtual_path).unlink()
    with pytest.raises(NotFound):
        store.get(alice, rec.virtual_path)
    store.remove(rec)
    assert store.files_in_chat(alice) == []


def deleted(store: Store, p: Principal, path: str) -> FileRecord:
    """Delete a file by its path, with no event under test: ``get``, then ``remove`` by the record."""
    rec = store.get(p, path)
    store.remove(rec)
    return rec


def test_get_then_remove_deletes_file_and_row(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """``get`` then ``remove``: the file and the row are gone; the usage drops."""
    rec = put(store, alice)
    real = workspaces.resolve(alice, rec.virtual_path)
    assert real.exists()
    assert deleted(store, alice, rec.virtual_path) == rec
    assert not real.exists()
    assert store.files_in_chat(alice) == []
    assert store.usage(alice) == 0
    with pytest.raises(NotFound):
        store.get(alice, rec.virtual_path)
    with pytest.raises(NotFound):
        deleted(store, alice, rec.virtual_path)


def test_delete_of_a_folder_is_not_found(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """Folders have no record; delete answers ``NotFound`` and leaves them."""
    store.mkdir(alice, "/workspace/chat-1/reports")
    with pytest.raises(NotFound):
        deleted(store, alice, "/workspace/chat-1/reports")
    assert workspaces.resolve(alice, "/workspace/chat-1/reports").is_dir()


def test_a_deleted_name_can_be_used_again(store: Store, alice: Principal) -> None:
    """The name is free once the file is gone, with no suffix."""
    rec = put(store, alice)
    deleted(store, alice, rec.virtual_path)
    assert put(store, alice).name == "q.xlsx"


# --- open for reading --------------------------------------------------------


def test_open_file_gives_the_record_and_the_bytes(store: Store, alice: Principal) -> None:
    """The file comes back open in binary mode, for the caller to read and close."""
    rec = put(store, alice, data=b"content")
    found, src = store.open_file(alice, rec.virtual_path)
    with src:
        assert found == rec
        assert src.read() == b"content"


def test_open_file_is_not_found_for_what_is_not_the_users_file(
    store: Store, alice: Principal, bob: Principal, workspaces: Workspaces
) -> None:
    """A folder, a missing file, another user's file and a record whose file is gone are all not found."""
    rec = put(store, alice)
    store.mkdir(alice, "/workspace/chat-1/reports")
    for path in ("/workspace/chat-1/reports", "/workspace/chat-1/missing.txt", "/workspace/chat-2/q.xlsx"):
        with pytest.raises(NotFound):
            store.open_file(alice, path)
    with pytest.raises(NotFound):
        store.open_file(bob, rec.virtual_path)
    workspaces.resolve(alice, rec.virtual_path).unlink()
    with pytest.raises(NotFound):
        store.open_file(alice, rec.virtual_path)


@pytest.mark.parametrize("target", ["/workspace", "/workspace/chat-1", "/workspace/q.xlsx", "/etc/passwd"])
def test_open_file_refuses_what_is_not_a_file_path(store: Store, alice: Principal, target: str) -> None:
    """The root and a chat folder are not files, and nothing outside the root is opened."""
    put(store, alice)
    with pytest.raises(PathError):
        store.open_file(alice, target)


def test_open_file_never_follows_a_link(store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path) -> None:
    """A file replaced by a link is refused, whatever it points at."""
    rec = put(store, alice)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"not yours")
    real = workspaces.resolve(alice, rec.virtual_path)
    real.unlink()
    real.symlink_to(outside)
    with pytest.raises(PathError):
        store.open_file(alice, rec.virtual_path)


# --- outputs -----------------------------------------------------------------


def test_register_output_moves_the_staged_file_in(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path, clock: Clock
) -> None:
    """The staged file becomes an output of the principal's chat, tied to its job."""
    job = store.add_job(alice, "run_python")
    staged = stage(tmp_path, job.id, "r.csv", b"a,b\n1,2\n")
    rec = store.register_output(alice, staged, "r.csv", job.id, file_id=new_file_id(), quota_bytes=10**12)
    assert (rec.kind, rec.job_id, rec.size, rec.virtual_path) == ("output", job.id, 8, "/workspace/chat-1/r.csv")
    assert (rec.mime, rec.created_at) == ("text/csv", clock.now)
    real = workspaces.resolve(alice, rec.virtual_path)
    assert real.read_bytes() == b"a,b\n1,2\n"
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    assert not staged.exists()
    assert store.files_in_chat(alice) == [rec]


def test_register_output_clash_gets_a_suffix(store: Store, alice: Principal, tmp_path: Path) -> None:
    """Outputs never replace an upload or an earlier output of the same name."""
    put(store, alice, "r.csv", b"upload")
    job = store.add_job(alice, "run_python")
    first = store.register_output(
        alice, stage(tmp_path, job.id, "r.csv", b"one"), "r.csv", job.id, file_id=new_file_id(), quota_bytes=10**12
    )
    second = store.register_output(
        alice, stage(tmp_path, job.id, "s.csv", b"two"), "r.csv", job.id, file_id=new_file_id(), quota_bytes=10**12
    )
    assert (first.name, second.name) == ("r (1).csv", "r (2).csv")


def test_register_output_refuses_links_and_other_non_files(store: Store, alice: Principal, tmp_path: Path) -> None:
    """A symlink or a folder is never moved into a workspace; the staged path stays as it was."""
    job = store.add_job(alice, "run_python")
    target = tmp_path / "target.txt"
    target.write_text("synthetic")
    link = tmp_path / "data" / "staging" / job.id / "link.txt"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    with pytest.raises(PathError):
        store.register_output(alice, link, "link.txt", job.id, file_id=new_file_id(), quota_bytes=10**12)
    folder = link.parent / "folder"
    folder.mkdir()
    with pytest.raises(PathError):
        store.register_output(alice, folder, "folder", job.id, file_id=new_file_id(), quota_bytes=10**12)
    assert link.is_symlink()
    assert target.read_text() == "synthetic"
    assert store.files_in_chat(alice) == []


def test_register_output_of_a_missing_staged_file_is_not_found(store: Store, alice: Principal, tmp_path: Path) -> None:
    """A staged path that is not there is ``NotFound``."""
    job = store.add_job(alice, "run_python")
    with pytest.raises(NotFound):
        store.register_output(alice, tmp_path / "gone.csv", "r.csv", job.id, file_id=new_file_id(), quota_bytes=10**12)


def test_register_output_refuses_a_job_whose_row_is_gone(store: Store, workspaces: Workspaces, tmp_path: Path) -> None:
    """An output of a job whose chat was deleted while it ran is not found; the chat folder is not made again."""
    put(store, PG, "q.csv")
    job = store.add_job(PG, "run_python")
    store.delete_chat(PG, unrecorded)
    staged = stage(tmp_path, job.id, "r.csv")
    with pytest.raises(NotFound):
        store.register_output(PG, staged, "r.csv", job.id, file_id=new_file_id(), quota_bytes=10**12)
    assert not (workspaces.user_root(PG) / "chat-1").exists()
    assert staged.exists()
    assert store.files_in_chat(PG) == []


def test_register_output_lands_in_the_principals_chat_only(
    store: Store, alice: Principal, bob: Principal, tmp_path: Path
) -> None:
    """The output belongs to the principal who ran the job, and to nobody else."""
    job = store.add_job(alice, "run_python")
    rec = store.register_output(
        alice, stage(tmp_path, job.id, "r.csv"), "r.csv", job.id, file_id=new_file_id(), quota_bytes=10**12
    )
    with pytest.raises(NotFound):
        store.get(bob, rec.virtual_path)
    assert store.files_in_chat(bob) == []


# --- files_in_chat, usage, due, remove ---------------------------------------


def test_files_in_chat_lists_only_that_chat_in_creation_order(
    store: Store, alice: Principal, bob: Principal, clock: Clock, tmp_path: Path
) -> None:
    """Uploads and outputs of the principal's chat, oldest first; other chats and users stay out."""
    first = put(store, alice, "b.txt")
    clock.now += 10
    job = store.add_job(alice, "run_python")
    second = store.register_output(
        alice, stage(tmp_path, job.id, "a.txt"), "a.txt", job.id, file_id=new_file_id(), quota_bytes=10**12
    )
    clock.now += 10
    store.mkdir(alice, "/workspace/chat-1/folder")
    third = put(store, alice, "c.txt", directory="/workspace/chat-1/folder")
    put(store, alice, "other.txt", directory="/workspace/chat-2")
    put(store, bob, "bob.txt")
    assert store.files_in_chat(alice) == [first, second, third]


def test_usage_is_the_sum_over_all_chats(store: Store, alice: Principal) -> None:
    """``usage`` counts every chat of the user and nothing else."""
    assert store.usage(alice) == 0
    put(store, alice, "a.bin", b"x" * 7)
    put(store, alice, "b.bin", b"x" * 5, directory="/workspace/chat-2")
    assert store.usage(alice) == 12


def test_count_files_counts_every_users_files(store: Store, alice: Principal, bob: Principal) -> None:
    """``count_files`` is the metrics' total: all users, all chats."""
    assert store.count_files() == 0
    put(store, alice, "a.bin")
    put(store, alice, "b.bin", directory="/workspace/chat-2")
    put(store, bob, "c.bin")
    assert store.count_files() == 3


def test_due_returns_files_older_than_the_cutoff(
    store: Store, alice: Principal, bob: Principal, clock: Clock, tmp_path: Path
) -> None:
    """Strictly older than the cutoff, oldest first, across users, up to ``limit``."""
    clock.now = 1000.0
    old = put(store, alice, "old.txt")
    clock.now = 2000.0
    mid = put(store, bob, "mid.txt")
    clock.now = 3000.0
    job = store.add_job(alice, "run_python")
    new = store.register_output(
        alice, stage(tmp_path, job.id, "new.txt"), "new.txt", job.id, file_id=new_file_id(), quota_bytes=10**12
    )
    assert store.due(1000.0) == []
    assert store.due(1500.0) == [old]
    assert store.due(2500.0) == [old, mid]
    assert store.due(9999.0) == [old, mid, new]
    assert store.due(9999.0, limit=2) == [old, mid]


def test_due_pages_on_past_the_last_file_of_a_page(
    store: Store, alice: Principal, bob: Principal, clock: Clock
) -> None:
    """``after`` is the last ``(created_at, id)`` seen: a sweep goes on past files it could not delete, ties by id."""
    clock.now = 1000.0
    tied = sorted(
        [put(store, alice, "a.txt"), put(store, bob, "b.txt"), put(store, alice, "c.txt")], key=lambda r: r.id
    )
    clock.now = 2000.0
    later = put(store, bob, "d.txt")
    first = store.due(9999.0, limit=2)
    assert first == tied[:2]
    rest = store.due(9999.0, limit=2, after=(first[-1].created_at, first[-1].id))
    assert rest == [tied[2], later]
    assert store.due(9999.0, after=(later.created_at, later.id)) == []
    assert store.due(1500.0, after=(tied[0].created_at, tied[0].id)) == tied[1:]


def test_checkpoint_empties_the_write_ahead_log(store: Store, alice: Principal, tmp_path: Path) -> None:
    """``secure_delete`` zeroes the deleted rows in the database; their older pages stay in ``-wal`` until then."""
    database = tmp_path / "data" / "workbench.db"
    reader = sqlite3.connect(database)  # an open connection keeps the log from going with the store's last one
    try:
        reader.execute("SELECT COUNT(*) FROM files").fetchone()
        rec = put(store, alice, "quarterly-figures.xlsx")
        store.remove(rec)
        wal = database.with_name("workbench.db-wal")
        assert wal.stat().st_size > 0
        assert store.checkpoint() is True
        assert wal.stat().st_size == 0
    finally:
        reader.close()


def test_summarize_due_counts_by_kind_and_opens_the_database_read_only(
    store: Store, alice: Principal, clock: Clock, tmp_path: Path
) -> None:
    """What the dry run reads: counts and ages per kind, through a connection that cannot write."""
    clock.now = 1000.0
    a = put(store, alice, "a.txt")
    clock.now = 2000.0
    b = put(store, alice, "b.txt")
    job = store.add_job(alice, "run_python")
    out = store.register_output(
        alice, stage(tmp_path, job.id, "chart.png"), "chart.png", job.id, file_id=new_file_id(), quota_bytes=10**12
    )
    clock.now = 3000.0
    put(store, alice, "c.txt")
    database = tmp_path / "data" / "workbench.db"
    before = database.read_bytes()
    summary = store_module.summarize_due(database, 2500.0)
    assert summary["upload"] == store_module.DueSummary(due=2, oldest=a.created_at, newest=b.created_at, kept=1)
    assert summary["output"] == store_module.DueSummary(due=1, oldest=out.created_at, newest=out.created_at, kept=0)
    assert store_module.summarize_due(database, 0.0)["output"] == store_module.DueSummary(0, None, None, 1)
    assert database.read_bytes() == before
    with pytest.raises(sqlite3.OperationalError):
        store_module.summarize_due(tmp_path / "data" / "missing.db", 2500.0)
    assert not (tmp_path / "data" / "missing.db").exists()


def test_remove_deletes_file_and_row(store: Store, alice: Principal, workspaces: Workspaces, clock: Clock) -> None:
    """What ``due`` returns can be removed: the file and its row go; usage drops."""
    rec = put(store, alice)
    clock.now += 100
    (due,) = store.due(clock.now)
    real = workspaces.resolve(alice, due.virtual_path)
    store.remove(due)
    assert not real.exists()
    assert store.due(clock.now) == []
    assert store.usage(alice) == 0
    assert rec == due


def test_remove_is_idempotent_and_clears_an_orphan_row(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """A file that is already gone does not stop its row from being removed."""
    rec = put(store, alice)
    workspaces.resolve(alice, rec.virtual_path).unlink()
    store.remove(rec)
    assert store.files_in_chat(alice) == []
    store.remove(rec)


def test_remove_after_a_delete_and_a_new_upload_of_the_same_name_changes_nothing(
    store: Store, alice: Principal, workspaces: Workspaces, clock: Clock
) -> None:
    """If the record's file is already gone, a new file of the same name is left alone."""
    old = put(store, alice, "q.xlsx", b"old")
    clock.now += 100
    (stale,) = store.due(clock.now)
    deleted(store, alice, old.virtual_path)
    fresh = put(store, alice, "q.xlsx", b"new")
    store.remove(stale)
    chat = workspaces.chat_dir(alice)
    assert (chat / "q.xlsx").read_bytes() == b"new"
    assert disk_names(chat) == ["q.xlsx"]
    assert store.get(alice, fresh.virtual_path) == fresh
    assert store.usage(alice) == 3


def test_remove_refuses_a_path_that_is_now_a_link(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """If an intermediate folder became a link, nothing behind it is deleted."""
    store.mkdir(alice, "/workspace/chat-1/folder")
    rec = put(store, alice, "q.txt", directory="/workspace/chat-1/folder")
    chat = workspaces.chat_dir(alice)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "q.txt").write_text("synthetic")
    (chat / "folder" / "q.txt").unlink()
    (chat / "folder").rmdir()
    (chat / "folder").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(PathError):
        store.remove(rec)
    assert (tmp_path / "elsewhere" / "q.txt").read_text() == "synthetic"


# --- jobs ----------------------------------------------------------------------


def test_add_job_records_the_run_and_finish_job_its_status(store: Store, alice: Principal, clock: Clock) -> None:
    """A new job is ``running``; finishing it stores the final status."""
    job = store.add_job(alice, "run_python")
    assert (job.caller, job.user_id, job.chat_id, job.tool) == ("ghostwriter", "user-a", "chat-1", "run_python")
    assert (job.status, job.created_at, len(job.id)) == ("running", clock.now, 32)
    assert store.get_job(alice, job.id) == job
    store.finish_job(job.id, "ok")
    assert store.get_job(alice, job.id).status == "ok"


def test_get_job_is_scoped_to_caller_and_user(store: Store, alice: Principal, bob: Principal) -> None:
    """Another user's, or another caller's, lookup of a job is ``NotFound``."""
    job = store.add_job(alice, "run_python")
    with pytest.raises(NotFound):
        store.get_job(bob, job.id)
    with pytest.raises(NotFound):
        store.get_job(Principal("other-caller", "user-a", "chat-1"), job.id)
    with pytest.raises(NotFound):
        store.get_job(alice, "0" * 32)
    assert store.get_job(Principal("ghostwriter", "user-a", "chat-9"), job.id) == job


def test_due_jobs_and_forget_jobs(store: Store, alice: Principal, bob: Principal, clock: Clock) -> None:
    """Jobs older than the cutoff are listed by id and can be forgotten."""
    clock.now = 1000.0
    old = store.add_job(alice, "run_python")
    clock.now = 2000.0
    mid = store.add_job(bob, "read_document")
    clock.now = 3000.0
    new = store.add_job(alice, "run_python")
    assert store.due_jobs(1000.0) == []
    assert store.due_jobs(2500.0) == [old.id, mid.id]
    store.forget_jobs([old.id, mid.id])
    assert store.due_jobs(9999.0) == [new.id]
    with pytest.raises(NotFound):
        store.get_job(alice, old.id)
    store.forget_jobs([])
    store.forget_jobs([old.id])


def test_find_job_looks_a_job_up_by_its_id_alone(store: Store, alice: Principal) -> None:
    """Retention names the owner of a job's leftover staging folder; it has the job's id and nothing else."""
    job = store.add_job(alice, "run_python")
    assert store.find_job(job.id) == job
    assert store.find_job("0" * 32) is None


# --- deleting a chat -----------------------------------------------------------


def test_delete_chat_records_first_then_rows_jobs_and_folder(store: Store, workspaces: Workspaces) -> None:
    """The record sees every file of the chat, a folder's too, still there; then the rows, jobs and folder go.

    Another chat of the user keeps its file, its job and its folder.
    """
    p2 = dataclasses.replace(PG, chat_id="chat-2")
    store.mkdir(dataclasses.replace(PG, confined=False), "/workspace/chat-1/sub")
    files = [
        put(store, PG, "q.csv"),
        put(store, PG, "r.csv"),
        put(store, dataclasses.replace(PG, confined=False), "s.csv", directory="/workspace/chat-1/sub"),
    ]
    other = put(store, p2, "q.csv", directory="/workspace/chat-2")
    job, theirs = store.add_job(PG, "run_python"), store.add_job(p2, "run_python")
    folder = workspaces.user_root(PG) / "chat-1"
    seen: list[tuple[list[str], str, list[str], bool, bool]] = []

    def record(ids: Sequence[str], outcome: str) -> None:
        names = [rec.name for rec in store.files_in_chat(PG)]
        seen.append((sorted(ids), outcome, names, store.find_job(job.id) is not None, folder.is_dir()))

    assert store.delete_chat(PG, record) == 3
    assert seen == [(sorted(rec.id for rec in files), "ok", ["q.csv", "r.csv", "sub/s.csv"], True, True)]
    assert store.files_in_chat(PG) == []
    assert store.find_job(job.id) is None
    assert not folder.exists()
    assert store.files_in_chat(p2) == [other]
    assert store.find_job(theirs.id) == theirs
    assert disk_names(workspaces.user_root(PG) / "chat-2") == ["q.csv"]


def test_a_record_that_raises_deletes_nothing(store: Store, workspaces: Workspaces) -> None:
    """A chat delete whose record cannot be written leaves the files, their rows, the job row and the folder."""
    files = [put(store, PG, "q.csv")]
    job = store.add_job(PG, "run_python")

    def refuse(*_: object) -> None:
        raise RuntimeError("synthetic audit failure")

    with pytest.raises(RuntimeError):
        store.delete_chat(PG, refuse)
    assert store.files_in_chat(PG) == files
    assert store.find_job(job.id) == job
    assert disk_names(workspaces.user_root(PG) / "chat-1") == ["q.csv"]


def test_a_deleted_chat_takes_no_new_job_and_no_upload(store: Store, workspaces: Workspaces) -> None:
    """Work that reaches the store after its chat went is refused: no job row, nothing recorded, written or made."""
    put(store, PG, "q.csv")
    store.delete_chat(PG, unrecorded)
    recorded: list[tuple[object, ...]] = []
    with pytest.raises(NotFound):
        store.add_job(PG, "run_python")
    with pytest.raises(NotFound):
        store.add_upload(
            PG,
            "/workspace/chat-1",
            "r.csv",
            io.BytesIO(b"x"),
            max_bytes=MAX,
            quota_bytes=QUOTA,
            record=lambda *args: recorded.append(args),
        )
    assert recorded == []
    assert store.due_jobs(float("inf")) == []
    assert not (workspaces.user_root(PG) / "chat-1").exists()


def test_a_chat_delete_never_slips_between_a_jobs_check_and_its_row(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delete that starts right after a new job's check waits for its row, and deletes it with the chat."""
    check = store._refuse_deleted
    deleting: list[threading.Thread] = []

    def then_delete(*args: str) -> None:
        check(*args)
        thread = threading.Thread(target=store.delete_chat, args=(PG, unrecorded))
        thread.start()
        deleting.append(thread)
        thread.join(0.2)  # it waits for the lock the check holds; without that, it would be done by now

    monkeypatch.setattr(store, "_refuse_deleted", then_delete)
    job = store.add_job(PG, "run_python")
    deleting[0].join(5)
    assert not deleting[0].is_alive()
    assert store.find_job(job.id) is None


def test_the_hold_is_the_deleted_chats_alone(store: Store) -> None:
    """Another chat of the user, and a former caller's chat of the same ids, still take jobs and uploads."""
    store.delete_chat(PG, unrecorded)
    for p in (FORMER, dataclasses.replace(PG, chat_id="chat-2")):
        assert store.add_job(p, "run_python").chat_id == p.chat_id
        assert put(store, p, "q.csv", directory=f"/workspace/{p.chat_id}").chat_id == p.chat_id


def test_a_hold_past_its_deadline_is_pruned(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hold refuses until its deadline and no longer after; the next chat delete prunes it."""
    now = [1000.0]
    monkeypatch.setattr(store, "_monotonic", lambda: now[0])
    store.delete_chat(PG, unrecorded)
    now[0] += store_module.DELETED_CHAT_HOLD_S - 1
    with pytest.raises(NotFound):
        store.add_job(PG, "run_python")
    now[0] += 1
    assert store.add_job(PG, "run_python").chat_id == "chat-1"
    store.delete_chat(dataclasses.replace(PG, chat_id="chat-2"), unrecorded)
    assert list(store._deleted) == [("ghostwriter", "user-a", "chat-2")]


def test_a_chat_delete_that_fails_after_its_record_is_recorded_again(
    store: Store, workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The removal's error is raised and the same ids are recorded as ``error``; a failure of that is only logged.

    The rows are gone; the files left on disk have none, so retention's orphan sweep records and removes them.
    """
    rec = put(store, PG, "q.csv")
    seen: list[tuple[list[str], str]] = []

    def record(ids: Sequence[str], outcome: str) -> None:
        seen.append((list(ids), outcome))
        if outcome == "error":
            raise RuntimeError("synthetic audit failure")

    def refuse(*_: object, **__: object) -> None:
        raise OSError("synthetic removal failure")

    monkeypatch.setattr(shutil, "rmtree", refuse)
    with pytest.raises(OSError, match="synthetic removal failure"):
        store.delete_chat(PG, record)
    assert seen == [([rec.id], "ok"), ([rec.id], "error")]
    assert [entry.getMessage() for entry in caplog.records] == [
        "could not record a chat delete that failed (RuntimeError)"
    ]
    assert store.files_in_chat(PG) == []
    assert disk_names(workspaces.user_root(PG) / "chat-1") == ["q.csv"]


# --- persistence and storage ------------------------------------------------------


def test_records_survive_a_restart(tmp_path: Path, store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """A second store on the same database sees what the first one wrote."""
    rec = put(store, alice)
    job = store.add_job(alice, "run_python")
    again = Store(tmp_path / "data" / "workbench.db", workspaces)
    assert again.get(alice, rec.virtual_path) == rec
    assert again.get_job(alice, job.id) == job


def test_the_database_is_private_and_in_wal_mode(tmp_path: Path, store: Store, alice: Principal) -> None:
    """The database and its write-ahead files are mode 0600; the journal mode is WAL."""
    db = tmp_path / "data" / "workbench.db"
    hold = sqlite3.connect(db)
    try:
        hold.execute("SELECT COUNT(*) FROM files").fetchone()
        put(store, alice)
        assert hold.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        names = sorted(path.name for path in db.parent.glob("workbench.db*"))
        assert "workbench.db" in names
        for path in db.parent.glob("workbench.db*"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    finally:
        hold.close()


def test_an_existing_database_with_open_permissions_is_tightened(tmp_path: Path, workspaces: Workspaces) -> None:
    """Opening the store makes the database 0600, whatever it was or the umask is."""
    db = tmp_path / "fresh" / "workbench.db"
    db.parent.mkdir()
    db.touch(mode=0o644)
    db.chmod(0o644)
    Store(db, workspaces)
    assert stat.S_IMODE(db.stat().st_mode) == 0o600
    old = os.umask(0)
    try:
        Store(tmp_path / "second" / "workbench.db", workspaces)
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "second" / "workbench.db").stat().st_mode) == 0o600


def test_deleted_rows_are_overwritten_in_the_database_file(tmp_path: Path, store: Store, alice: Principal) -> None:
    """``secure_delete``: a deleted file name does not stay readable in free pages."""
    marker = "synthetic-marker-name-zq9.txt"
    rec = put(store, alice, marker)
    db = tmp_path / "data" / "workbench.db"
    check = sqlite3.connect(db)
    try:
        check.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        assert marker.encode() in db.read_bytes()
        deleted(store, alice, rec.virtual_path)
        check.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    finally:
        check.close()
    assert marker.encode() not in db.read_bytes()


def test_an_output_past_the_users_quota_is_refused_and_stays_staged(
    store: Store, alice: Principal, tmp_path: Path
) -> None:
    """Outputs count against the quota, as uploads do; the refused file is left where it was staged."""
    store.add_upload(
        alice, "/workspace/chat-1", "a.bin", io.BytesIO(b"x" * 90), max_bytes=MAX, quota_bytes=100, record=unrecorded
    )
    job = store.add_job(alice, "run_python")
    staged = stage(tmp_path, job.id, "r.csv", b"y" * 11)
    with pytest.raises(QuotaExceeded):
        store.register_output(alice, staged, "r.csv", job.id, file_id=new_file_id(), quota_bytes=100)
    assert staged.exists()
    assert [rec.name for rec in store.files_in_chat(alice)] == ["a.bin"]
    kept = store.register_output(
        alice, stage(tmp_path, job.id, "s.csv", b"y" * 10), "s.csv", job.id, file_id="a" * 32, quota_bytes=100
    )
    assert (kept.id, store.usage(alice)) == ("a" * 32, 100)


def test_an_output_takes_the_id_its_event_named(store: Store, alice: Principal, tmp_path: Path) -> None:
    """The id is the caller's, chosen before anything was recorded; anything but 32 hex digits is refused."""
    job = store.add_job(alice, "run_python")
    with pytest.raises(ValueError, match="32 lowercase hex"):
        store.register_output(
            alice, stage(tmp_path, job.id, "r.csv"), "r.csv", job.id, file_id="../x", quota_bytes=10**6
        )
    assert store.files_in_chat(alice) == []


def test_an_upload_is_recorded_under_the_lock_before_its_rename(
    store: Store, alice: Principal, workspaces: Workspaces
) -> None:
    """The record sees the ``<id>.part`` and no file of the chat; the stored file has that id and the record's chat."""
    seen: list[tuple[str, str, str, list[str], list[str]]] = []

    def record(chat_id: str, file_id: str, outcome: str) -> None:
        names = sorted(path.name for path in workspaces.chat_dir(alice).iterdir())
        seen.append((chat_id, file_id, outcome, names, [rec.name for rec in store.files_in_chat(alice)]))

    rec = store.add_upload(
        alice, "/workspace/chat-1", "q.xlsx", io.BytesIO(b"PK"), max_bytes=MAX, quota_bytes=QUOTA, record=record
    )
    assert seen == [("chat-1", rec.id, "ok", [f"{rec.id}.part"], [])]


def test_an_upload_whose_record_fails_leaves_nothing(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """Nothing is stored without its record: the ``.part`` goes, no row is made, and the record's error goes up."""

    def refuse(*_: object) -> None:
        raise RuntimeError("synthetic audit failure")

    with pytest.raises(RuntimeError):
        store.add_upload(
            alice, "/workspace/chat-1", "q.xlsx", io.BytesIO(b"PK"), max_bytes=MAX, quota_bytes=QUOTA, record=refuse
        )
    assert disk_names(workspaces.chat_dir(alice)) == []
    assert store.files_in_chat(alice) == []


# --- derived files -------------------------------------------------------------------

RENDER = b"synthetic render bytes"


def spec(n: int = 1, meta: Mapping[str, object] | None = None, *, role: DerivedRole = "page") -> DerivedSpec:
    """The spec of a render of the page ``n``."""
    return DerivedSpec(role, n, "image/jpeg", 10, 20, {} if meta is None else meta)


def staged(tmp_path: Path, data: bytes = RENDER, name: str = "render.jpg") -> Path:
    """Stage a render the way the executor stages an output, each in a staging folder of its own."""
    return stage(tmp_path, new_file_id(), name, data)


def derive(
    store: Store,
    tmp_path: Path,
    source: FileRecord,
    n: int = 1,
    *,
    data: bytes = RENDER,
    meta: Mapping[str, object] | None = None,
    quota_bytes: int = 10**9,
    cap_bytes: int = 10**9,
) -> DerivedRecord:
    """Register one render of ``source`` with roomy limits."""
    return store.register_derived(
        source,
        staged(tmp_path, data),
        spec(n, meta),
        file_id=new_file_id(),
        quota_bytes=quota_bytes,
        cap_bytes=cap_bytes,
    )


def every_derived(store: Store) -> list[DerivedRecord]:
    """Every render the store holds, whoever's, as retention's stray pass would list them at the end of time."""
    return store.due_derived(float("inf"), 10**6)


def derived_folder(workspaces: Workspaces, p: Principal) -> Path:
    """The chat's reserved folder, ``.derived``."""
    return workspaces.chat_dir(p) / ".derived"


def test_derived_files_live_under_their_source_and_carry_its_time(
    store: Store, alice: Principal, workspaces: Workspaces, clock: Clock, tmp_path: Path
) -> None:
    """The file is ``.derived/<source id>/<file id>.jpg`` below the chat folder, private, and as old as its source."""
    source = put(store, alice)
    clock.now += 500
    file_id = new_file_id()
    rec = store.register_derived(
        source,
        staged(tmp_path),
        DerivedSpec("page", 3, "image/jpeg", 10, 20, {"strip": 1, "strips": 2}),
        file_id=file_id,
        quota_bytes=QUOTA,
    )
    real = workspaces.chat_dir(alice) / ".derived" / source.id / f"{file_id}.jpg"
    assert real.read_bytes() == RENDER
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    assert stat.S_IMODE(real.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(real.parent.parent.stat().st_mode) == 0o700
    assert rec == DerivedRecord(
        id=file_id,
        source_id=source.id,
        caller="ghostwriter",
        user_id="user-a",
        chat_id="chat-1",
        role="page",
        ordinal=3,
        name=f"{file_id}.jpg",
        size=len(RENDER),
        mime="image/jpeg",
        width=10,
        height=20,
        meta={"strip": 1, "strips": 2},
        created_at=source.created_at,
    )
    assert rec.created_at != clock.now
    assert rec.relative_path == f".derived/{source.id}/{file_id}.jpg"
    assert (rec.strip, rec.strips) == (1, 2)
    assert store.derived_of(source) == [rec]


def test_a_derived_png_is_a_png_and_another_type_is_refused(store: Store, alice: Principal, tmp_path: Path) -> None:
    """The extension follows the type; a type that is no render is a path error and the staged file stays."""
    source = put(store, alice)
    png = store.register_derived(
        source,
        staged(tmp_path, name="render.png"),
        DerivedSpec("image", 1, "image/png", 5, 5, {}),
        file_id=new_file_id(),
        quota_bytes=QUOTA,
    )
    assert (png.name.endswith(".png"), png.mime) == (True, "image/png")
    odd = staged(tmp_path, name="render.gif")
    with pytest.raises(PathError):
        store.register_derived(
            source, odd, DerivedSpec("page", 2, "image/gif", 5, 5, {}), file_id=new_file_id(), quota_bytes=QUOTA
        )
    assert odd.exists()
    assert store.derived_of(source) == [png]


#: The first bytes of a PDF, then invented ones: what an Office file is converted to.
CONVERTED = b"%PDF-1.7 synthetic converted pages"


def converted_spec(meta: Mapping[str, object] | None = None) -> DerivedSpec:
    """The spec of the PDF an Office file was converted to: role ``pdf``, no size in pixels."""
    return DerivedSpec("pdf", 0, "application/pdf", None, None, {"pages": 3} if meta is None else meta)


def test_a_converted_pdf_is_a_derived_file(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """An Office file's PDF is ``.derived/<source id>/<id>.pdf``, private and counted in the quota; a second conflicts.

    The second's staged file stays, and the source keeps the one PDF.
    """
    source = put(store, alice, "harbour-deck.pptx")
    used = store.usage(alice)
    file_id = new_file_id()
    rec = store.register_derived(
        source, staged(tmp_path, CONVERTED, "converted.pdf"), converted_spec(), file_id=file_id, quota_bytes=QUOTA
    )
    real = workspaces.chat_dir(alice) / ".derived" / source.id / f"{file_id}.pdf"
    assert real.read_bytes() == CONVERTED
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    assert rec.relative_path == f".derived/{source.id}/{file_id}.pdf"
    assert (rec.role, rec.ordinal, rec.mime, rec.width, rec.height, rec.size, dict(rec.meta)) == (
        "pdf",
        0,
        "application/pdf",
        None,
        None,
        len(CONVERTED),
        {"pages": 3},
    )
    assert store.usage(alice) == used + len(CONVERTED)
    second = staged(tmp_path, CONVERTED, "converted.pdf")
    with pytest.raises(Conflict):
        store.register_derived(source, second, converted_spec(), file_id=new_file_id(), quota_bytes=QUOTA)
    assert second.exists()
    assert store.derived_of(source) == [rec]


@pytest.mark.parametrize(
    ("role", "mime", "width", "height"),
    [
        ("pdf", "image/jpeg", None, None),
        ("page", "application/pdf", None, None),
        ("image", "application/pdf", None, None),
        ("pdf", "application/pdf", 10, 20),
        ("pdf", "application/pdf", None, 20),
    ],
    ids=["pdf-as-jpeg", "page-as-pdf", "image-as-pdf", "pdf-with-a-size", "pdf-with-a-height"],
)
def test_a_pdf_role_needs_its_type(
    role: DerivedRole,
    mime: str,
    width: int | None,
    height: int | None,
    store: Store,
    alice: Principal,
    tmp_path: Path,
) -> None:
    """Role ``pdf`` is ``application/pdf`` with no size in pixels, and that type is no other role's: else a path error.

    The staged file stays, and no row is made.
    """
    source = put(store, alice, "harbour-report.docx")
    odd = staged(tmp_path, CONVERTED, "converted.pdf")
    with pytest.raises(PathError):
        store.register_derived(
            source, odd, DerivedSpec(role, 0, mime, width, height, {}), file_id=new_file_id(), quota_bytes=QUOTA
        )
    assert odd.exists()
    assert store.derived_of(source) == []


def test_a_derived_file_needs_its_source(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """A source that was removed takes no render, and the staged file stays."""
    source = put(store, alice)
    store.remove(source)
    render = staged(tmp_path)
    with pytest.raises(NotFound):
        store.register_derived(source, render, spec(), file_id=new_file_id(), quota_bytes=QUOTA)
    assert render.exists()
    assert not derived_folder(workspaces, alice).exists()
    assert every_derived(store) == []


def test_a_derived_file_needs_its_sources_own_owner_and_chat(store: Store, alice: Principal, tmp_path: Path) -> None:
    """A record that names another owner or chat than its row has is not the source."""
    source = put(store, alice)
    for forged in (
        dataclasses.replace(source, user_id="user-b"),
        dataclasses.replace(source, caller="former-caller"),
        dataclasses.replace(source, chat_id="chat-2"),
    ):
        render = staged(tmp_path)
        with pytest.raises(NotFound):
            store.register_derived(forged, render, spec(), file_id=new_file_id(), quota_bytes=QUOTA)
        assert render.exists()
        assert store.derived_of(forged) == []
    assert every_derived(store) == []


def test_a_deleted_chat_takes_no_derived_file(store: Store, tmp_path: Path) -> None:
    """After the chat's delete the source is gone, and so is any place for a render."""
    source = put(store, PG)
    store.delete_chat(PG, lambda ids, outcome: None)
    render = staged(tmp_path)
    with pytest.raises(NotFound):
        store.register_derived(source, render, spec(), file_id=new_file_id(), quota_bytes=QUOTA)
    assert render.exists()


def test_a_held_chat_takes_no_derived_file(store: Store, alice: Principal, tmp_path: Path) -> None:
    """A chat held as deleted is refused even where a source row is still there: the hold is checked under the lock."""
    source = put(store, alice)
    store._deleted[(alice.caller, alice.user_id, alice.chat_id)] = store._monotonic() + 100
    render = staged(tmp_path)
    with pytest.raises(NotFound):
        store.register_derived(source, render, spec(), file_id=new_file_id(), quota_bytes=QUOTA)
    assert render.exists()


def test_a_source_holds_at_most_its_cap_of_renders(store: Store, alice: Principal, tmp_path: Path) -> None:
    """Renders past the cap are refused with their own error and stay staged; one that fits exactly is kept."""
    source = put(store, alice)
    first = derive(store, tmp_path, source, 1, data=b"x" * 8, cap_bytes=10)
    over = staged(tmp_path, b"y" * 4)
    with pytest.raises(RendersFull, match="the renders of this file are at their limit"):
        store.register_derived(source, over, spec(2), file_id=new_file_id(), quota_bytes=QUOTA, cap_bytes=10)
    assert over.exists()
    assert issubclass(RendersFull, store_module.StoreError)
    last = derive(store, tmp_path, source, 3, data=b"z" * 2, cap_bytes=10)
    assert store.derived_of(source) == [first, last]
    other = put(store, alice, "other.pdf")
    assert derive(store, tmp_path, other, 1, data=b"w" * 10, cap_bytes=10).size == 10  # the cap is each source's own


def test_renders_count_against_the_quota(store: Store, alice: Principal, tmp_path: Path) -> None:
    """The user's usage grows by a render's size; a render past the quota is refused and stays staged."""
    source = put(store, alice)
    assert store.usage(alice) == 10
    rec = derive(store, tmp_path, source, 1, data=b"x" * 7, quota_bytes=20)
    assert store.usage(alice) == 17
    over = staged(tmp_path, b"y" * 4)
    with pytest.raises(QuotaExceeded):
        store.register_derived(source, over, spec(2), file_id=new_file_id(), quota_bytes=20)
    assert over.exists()
    assert store.usage(alice) == 17
    derive(store, tmp_path, source, 2, data=b"z" * 3, quota_bytes=20)
    assert store.usage(alice) == 20
    store.remove_derived(rec)
    assert store.usage(alice) == 13


def test_renders_leave_the_quota_of_other_users_alone(
    store: Store, alice: Principal, bob: Principal, tmp_path: Path
) -> None:
    """Usage is the user's own, renders included."""
    derive(store, tmp_path, put(store, alice), data=b"x" * 7)
    assert (store.usage(alice), store.usage(bob)) == (17, 0)


def test_an_upload_counts_the_renders_in_its_room(store: Store, alice: Principal, tmp_path: Path) -> None:
    """An upload that would fit without the renders does not fit with them."""
    derive(store, tmp_path, put(store, alice), data=b"x" * 50)
    with pytest.raises(QuotaExceeded):
        store.add_upload(
            alice,
            "/workspace/chat-1",
            "b.bin",
            io.BytesIO(b"y" * 41),
            max_bytes=MAX,
            quota_bytes=100,
            record=unrecorded,
        )
    store.add_upload(
        alice, "/workspace/chat-1", "b.bin", io.BytesIO(b"y" * 40), max_bytes=MAX, quota_bytes=100, record=unrecorded
    )


def test_the_same_render_is_refused_twice(store: Store, alice: Principal, tmp_path: Path) -> None:
    """One role, ordinal, strip and strips make one render; a strip not made yet, or another page, is kept."""
    source = put(store, alice)
    first = derive(store, tmp_path, source, 1, meta={"strip": 1, "strips": 2})
    duplicate = staged(tmp_path)
    with pytest.raises(Conflict):
        store.register_derived(
            source, duplicate, spec(1, {"strip": 1, "strips": 2}), file_id=new_file_id(), quota_bytes=QUOTA
        )
    assert duplicate.exists()
    second = derive(store, tmp_path, source, 1, meta={"strip": 2, "strips": 2})
    page = derive(store, tmp_path, source, 2, meta={"strip": 1, "strips": 2})
    assert store.derived_of(source) == [first, second, page]


def test_a_missing_strip_is_the_first_of_one(store: Store, alice: Principal, tmp_path: Path) -> None:
    """``meta`` without ``strip`` and ``strips`` is the whole page: the same as ``strip: 1, strips: 1``."""
    source = put(store, alice)
    whole = derive(store, tmp_path, source, 1)
    assert (whole.strip, whole.strips) == (1, 1)
    with pytest.raises(Conflict):
        derive(store, tmp_path, source, 1, meta={"strip": 1, "strips": 1})
    assert derive(store, tmp_path, source, 1, meta={"strip": 1, "strips": 3}).strips == 3


def test_the_same_role_and_ordinal_of_another_source_is_no_conflict(
    store: Store, alice: Principal, tmp_path: Path
) -> None:
    """A source's renders are its own."""
    one, two = put(store, alice, "a.pdf"), put(store, alice, "b.pdf")
    derive(store, tmp_path, one, 1)
    derive(store, tmp_path, two, 1)
    assert (len(store.derived_of(one)), len(store.derived_of(two))) == (1, 1)


def test_a_render_id_is_used_once(store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path) -> None:
    """A second render under an id a row has is refused, and the first one's file is not touched."""
    source = put(store, alice)
    first = derive(store, tmp_path, source, 1)
    clash = staged(tmp_path, b"other")
    with pytest.raises(Conflict):
        store.register_derived(source, clash, spec(2), file_id=first.id, quota_bytes=QUOTA)
    assert clash.exists()
    real = workspaces.chat_dir(alice) / first.relative_path
    assert real.read_bytes() == RENDER
    assert store.derived_of(source) == [first]


def test_a_staged_render_is_checked_as_an_output_is(store: Store, alice: Principal, tmp_path: Path) -> None:
    """A link or a folder is no render, a missing file is not found, and an id is 32 lowercase hex digits."""
    source = put(store, alice)
    target = tmp_path / "elsewhere.jpg"
    target.write_bytes(b"x")
    link = tmp_path / "data" / "staging" / "j" / "link.jpg"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    folder = tmp_path / "data" / "staging" / "j" / "folder.jpg"
    folder.mkdir()
    for bad in (link, folder):
        with pytest.raises(PathError):
            store.register_derived(source, bad, spec(), file_id=new_file_id(), quota_bytes=QUOTA)
    with pytest.raises(NotFound):
        store.register_derived(source, tmp_path / "missing.jpg", spec(), file_id=new_file_id(), quota_bytes=QUOTA)
    with pytest.raises(ValueError, match="32 lowercase hex"):
        store.register_derived(source, staged(tmp_path), spec(), file_id="../x", quota_bytes=QUOTA)
    assert target.read_bytes() == b"x"
    assert every_derived(store) == []


def test_a_render_that_cannot_be_inserted_leaves_no_file(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is renamed first and the row follows; a row that fails takes the file back out."""
    source = put(store, alice)

    def refuse(*_: object) -> None:
        raise sqlite3.OperationalError("synthetic failure")

    monkeypatch.setattr(store, "_insert_derived", refuse)
    with pytest.raises(sqlite3.OperationalError):
        derive(store, tmp_path, source)
    assert not any(path.is_file() for path in derived_folder(workspaces, alice).rglob("*"))


def test_the_reserved_name_is_refused(store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path) -> None:
    """No upload or output takes the name of the folder the renders are in, in any case."""
    for name in (".derived", ".Derived", ".DERIVED"):
        with pytest.raises(PathError, match="invalid file name"):
            put(store, alice, name)
        job = store.add_job(alice, "run_python")
        with pytest.raises(PathError, match="invalid file name"):
            store.register_output(
                alice, stage(tmp_path, job.id, "o.bin"), name, job.id, file_id=new_file_id(), quota_bytes=10**6
            )
    assert store.files_in_chat(alice) == []


@pytest.mark.parametrize("confined", [True, False])
@pytest.mark.parametrize(
    "below",
    ["", "/{source}", "/{source}/{name}", "/../.derived", "/./sub/../.derived/{source}", "/{source}/../{source}"],
)
def test_the_reserved_folder_is_not_found_for_everyone(
    store: Store, workspaces: Workspaces, tmp_path: Path, confined: bool, below: str
) -> None:
    """``get``, ``open_file``, ``list_dir``, ``mkdir`` and an upload's folder never reach ``.derived``."""
    p = dataclasses.replace(PG_SETUP, confined=confined)
    source = put(store, PG_SETUP)
    rec = derive(store, tmp_path, source)
    path = "/workspace/chat-1/.derived" + below.format(source=source.id, name=rec.name)
    assert (derived_folder(workspaces, p) / source.id / rec.name).is_file()  # it is there; it is not reachable
    with pytest.raises(NotFound):
        store.get(p, path)
    with pytest.raises(NotFound):
        store.open_file(p, path)
    with pytest.raises(NotFound):
        store.list_dir(p, path)
    with pytest.raises(NotFound):
        store.mkdir(p, path)
    with pytest.raises(NotFound):
        store.add_upload(p, path, "q.txt", io.BytesIO(b"x"), max_bytes=MAX, quota_bytes=QUOTA, record=unrecorded)
    assert disk_names(derived_folder(workspaces, p) / source.id) == [rec.name]


def test_the_reserved_name_is_refused_below_the_chat_too(
    store: Store, alice: Principal, workspaces: Workspaces
) -> None:
    """A folder named ``.derived`` anywhere below a chat is not made and not found; the other names are."""
    store.mkdir(alice, "/workspace/chat-1/sub")
    for path in ("/workspace/chat-1/sub/.derived", "/workspace/chat-1/sub/.Derived/x"):
        with pytest.raises(NotFound):
            store.mkdir(alice, path)
    assert disk_names(workspaces.chat_dir(alice)) == ["sub"]
    assert store.mkdir(alice, "/workspace/chat-1/sub/derived") == "/workspace/chat-1/sub/derived"


def test_derived_files_are_hidden(store: Store, alice: Principal, tmp_path: Path) -> None:
    """The chat's listing, its files and the root's listing know nothing of the renders."""
    source = put(store, alice)
    derive(store, tmp_path, source)
    assert [entry.name for entry in store.list_dir(alice, "/workspace/chat-1")] == [source.name]
    assert [entry.name for entry in store.list_dir(alice, "/workspace")] == ["chat-1"]
    assert store.files_in_chat(alice) == [source]
    assert store.count_files() == 1


def test_the_reserved_folder_is_left_out_wherever_it_is_listed(
    store: Store, alice: Principal, workspaces: Workspaces
) -> None:
    """A ``.derived`` that exists on disk below a folder, however it came, is not listed either."""
    store.mkdir(alice, "/workspace/chat-1/sub")
    (workspaces.chat_dir(alice) / "sub" / ".derived").mkdir()
    assert store.list_dir(alice, "/workspace/chat-1/sub") == []


def test_derived_for_gives_every_row_of_the_sources_and_no_other(
    store: Store, alice: Principal, bob: Principal, tmp_path: Path
) -> None:
    """The rows of several sources at once, each by its own owner and chat."""
    one, two, theirs = put(store, alice, "a.pdf"), put(store, alice, "b.pdf"), put(store, bob, "a.pdf")
    rows = [derive(store, tmp_path, one, 1), derive(store, tmp_path, one, 2), derive(store, tmp_path, two, 1)]
    derive(store, tmp_path, theirs, 1)
    assert store.derived_for([]) == []
    assert sorted(store.derived_for([one, two]), key=lambda r: r.id) == sorted(rows, key=lambda r: r.id)
    assert store.derived_for([one]) == store.derived_of(one) == rows[:2]
    assert store.derived_for([dataclasses.replace(theirs, user_id="user-a")]) == []
    assert [r.source_id for r in store.derived_for([theirs])] == [theirs.id]


def test_derived_of_is_ordered_by_role_ordinal_and_strips(store: Store, alice: Principal, tmp_path: Path) -> None:
    """Whatever the order they were made in."""
    source = put(store, alice)
    keys: list[tuple[DerivedRole, int, int, int]] = [
        ("page", 2, 1, 1),
        ("page", 1, 2, 2),
        ("page", 1, 2, 1),
        ("image", 5, 1, 1),
        ("page", 1, 1, 1),
    ]
    made = {
        key: store.register_derived(
            source,
            staged(tmp_path),
            spec(key[1], {"strip": key[3], "strips": key[2]}, role=key[0]),
            file_id=new_file_id(),
            quota_bytes=QUOTA,
        )
        for key in keys
    }
    ordered: list[tuple[DerivedRole, int, int, int]] = [
        ("image", 5, 1, 1),
        ("page", 1, 1, 1),
        ("page", 1, 2, 1),
        ("page", 1, 2, 2),
        ("page", 2, 1, 1),
    ]
    assert store.derived_of(source) == [made[key] for key in ordered]


def test_open_derived_opens_what_open_file_would(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """The bytes of a render; a link, a missing file or a render whose source is gone is not found."""
    source = put(store, alice)
    rec = derive(store, tmp_path, source)
    with store.open_derived(rec) as src:
        assert src.read() == RENDER
    real = workspaces.chat_dir(alice) / rec.relative_path
    with pytest.raises(NotFound):
        store.open_derived(dataclasses.replace(rec, chat_id="chat-2"))
    with pytest.raises(NotFound):
        store.open_derived(dataclasses.replace(rec, user_id="user-b"))
    real.unlink()
    with pytest.raises(NotFound):
        store.open_derived(rec)
    target = tmp_path / "elsewhere.jpg"
    target.write_bytes(b"secret")
    real.symlink_to(target)
    with pytest.raises((NotFound, PathError)):
        store.open_derived(rec)


def test_open_derived_needs_the_source_to_be_there(store: Store, alice: Principal, tmp_path: Path) -> None:
    """A render whose source went, and whose row stayed, is a stray: nobody is served it."""
    source = put(store, alice)
    rec = derive(store, tmp_path, source)
    store.remove(source)
    with pytest.raises(NotFound):
        store.open_derived(rec)
    assert store.due_derived(0) == [rec]


def test_a_chat_delete_names_and_takes_its_derived_files(store: Store, workspaces: Workspaces, tmp_path: Path) -> None:
    """The record gets the files' ids and then the renders'; the renders' rows and folder go with the chat's."""
    p2 = dataclasses.replace(PG, chat_id="chat-2")
    source, other = put(store, PG), put(store, p2, directory="/workspace/chat-2")
    rec = derive(store, tmp_path, source)
    theirs = derive(store, tmp_path, other)
    seen: list[tuple[list[str], str, list[str]]] = []

    def record(ids: Sequence[str], outcome: str) -> None:
        seen.append((list(ids), outcome, [r.id for r in every_derived(store)]))

    assert store.delete_chat(PG, record) == 1
    assert seen == [([source.id, rec.id], "ok", sorted([rec.id, theirs.id]))]
    assert every_derived(store) == [theirs]
    assert not (workspaces.user_root(PG) / "chat-1").exists()
    assert store.derived_of(other) == [theirs]
    assert (workspaces.user_root(PG) / "chat-2" / theirs.relative_path).is_file()


def test_a_chat_delete_takes_the_strays_of_the_chat_too(store: Store, tmp_path: Path) -> None:
    """A render whose source is already gone is still the chat's, and goes with it, named in the record."""
    source = put(store, PG)
    rec = derive(store, tmp_path, source)
    store.remove(source)
    seen: list[list[str]] = []
    assert store.delete_chat(PG, lambda ids, outcome: seen.append(list(ids))) == 0
    assert seen == [[rec.id]]
    assert every_derived(store) == []


def test_a_chat_delete_that_fails_names_the_renders_again(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same ids, the renders' included, are recorded again as an error."""
    source = put(store, PG)
    rec = derive(store, tmp_path, source)
    seen: list[tuple[list[str], str]] = []

    def refuse(*_: object, **__: object) -> None:
        raise OSError("synthetic removal failure")

    monkeypatch.setattr(shutil, "rmtree", refuse)
    with pytest.raises(OSError, match="synthetic removal failure"):
        store.delete_chat(PG, lambda ids, outcome: seen.append((list(ids), outcome)))
    assert seen == [([source.id, rec.id], "ok"), ([source.id, rec.id], "error")]


def test_remove_takes_the_named_derived_files_and_the_source_together(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """The renders' files, the source's file and every named row go; the empty folders after them."""
    source = put(store, alice)
    renders = [derive(store, tmp_path, source, n) for n in (1, 2)]
    store.remove(source, renders)
    assert every_derived(store) == []
    assert store.files_in_chat(alice) == []
    assert disk_names(workspaces.chat_dir(alice)) == []
    assert store.usage(alice) == 0


def test_remove_takes_only_the_named_derived_files(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """A render not named stays, row and file; retention's stray pass finds it, its source being gone."""
    source = put(store, alice)
    named, unnamed = derive(store, tmp_path, source, 1), derive(store, tmp_path, source, 2)
    store.remove(source, [named])
    folder = derived_folder(workspaces, alice) / source.id
    assert disk_names(folder) == [unnamed.name]
    assert every_derived(store) == [unnamed]
    assert store.due_derived(0) == [unnamed]
    store.remove_derived(unnamed)
    assert every_derived(store) == []
    assert not derived_folder(workspaces, alice).exists()


def test_remove_never_takes_a_render_of_another_file_or_user(
    store: Store, alice: Principal, bob: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """A render named with the wrong source stays; so does one the record's owner does not own."""
    one, two, theirs = put(store, alice, "a.pdf"), put(store, alice, "b.pdf"), put(store, bob, "a.pdf")
    mine = derive(store, tmp_path, one)
    other = derive(store, tmp_path, two)
    stranger = derive(store, tmp_path, theirs)
    store.remove(one, [mine, other, stranger])
    assert sorted(r.id for r in every_derived(store)) == sorted([other.id, stranger.id])
    assert (workspaces.chat_dir(alice) / other.relative_path).is_file()
    assert (workspaces.chat_dir(bob) / stranger.relative_path).is_file()


def test_remove_of_a_source_already_gone_still_takes_the_renders_it_names(
    store: Store, alice: Principal, tmp_path: Path
) -> None:
    """A second delete of one file, naming renders the first left, takes them."""
    source = put(store, alice)
    rec = derive(store, tmp_path, source)
    store.remove(source)
    store.remove(source, [rec])
    assert every_derived(store) == []


def test_remove_derived_is_one_render_and_idempotent(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """One render's file and row go, the others and the source stay; a second call is fine."""
    source = put(store, alice)
    first, second = derive(store, tmp_path, source, 1), derive(store, tmp_path, source, 2)
    store.remove_derived(first)
    store.remove_derived(first)
    assert store.derived_of(source) == [second]
    assert store.files_in_chat(alice) == [source]
    assert not (workspaces.chat_dir(alice) / first.relative_path).exists()
    assert (workspaces.chat_dir(alice) / second.relative_path).is_file()


def test_remove_refuses_a_path_that_is_now_a_link_and_deletes_nothing(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """A folder of renders swapped for a link is never followed; the source and its rows stay."""
    source = put(store, alice)
    rec = derive(store, tmp_path, source)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / rec.name).write_bytes(b"not a render")
    folder = derived_folder(workspaces, alice) / source.id
    (folder / rec.name).unlink()
    folder.rmdir()
    folder.symlink_to(elsewhere)
    with pytest.raises(PathError):
        store.remove(source, [rec])
    assert (elsewhere / rec.name).read_bytes() == b"not a render"
    assert store.files_in_chat(alice) == [source]
    assert every_derived(store) == [rec]
    assert (workspaces.chat_dir(alice) / source.name).is_file()


def test_due_derived_lists_by_age_or_by_a_source_that_is_gone(
    store: Store, alice: Principal, clock: Clock, tmp_path: Path
) -> None:
    """In ``(created_at, id)`` order, paged by ``after`` and ``limit``."""
    clock.now = 1000.0
    old = put(store, alice, "old.pdf")
    clock.now = 3000.0
    new = put(store, alice, "new.pdf")
    gone = put(store, alice, "gone.pdf")
    renders = {name: [derive(store, tmp_path, src, n) for n in (1, 2)] for name, src in (("old", old), ("new", new))}
    stray = derive(store, tmp_path, gone)
    store.remove(gone)
    assert store.due_derived(2000.0) == [
        *sorted(renders["old"], key=lambda r: r.id),
        stray,
    ]  # a stray is due at any age
    due = store.due_derived(3001.0)
    assert due == sorted([*renders["old"], *renders["new"], stray], key=lambda r: (r.created_at, r.id))
    assert store.due_derived(500.0) == [stray]
    page = store.due_derived(3001.0, 2)
    assert page == due[:2]
    assert store.due_derived(3001.0, 2, after=(page[-1].created_at, page[-1].id)) == due[2:4]
    assert store.due_derived(3001.0, 10, after=(due[-1].created_at, due[-1].id)) == []


def test_the_orphan_sweep_keeps_derived_files_and_names_orphans_by_id(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """A render with its row stays; one without is removed and recorded by the id its name carries."""
    source = put(store, alice)
    kept = derive(store, tmp_path, source)
    chat = workspaces.chat_dir(alice)
    lost_id = new_file_id()
    lost = chat / ".derived" / source.id / f"{lost_id}.jpg"
    lost.write_bytes(b"x")
    loose = chat / f"{new_file_id()}.jpg"  # no row, and not in the reserved folder: no id to name
    loose.write_bytes(b"x")
    seen: list[tuple[str, list[str], int, str]] = []

    def record(chat_id: str, ids: Sequence[str], count: int, outcome: str) -> None:
        seen.append((chat_id, list(ids), count, outcome))

    outcome = store.sweep_user(
        "ghostwriter",
        "user-a",
        untouched_before=time.time() + 3600,
        device=os.stat(tmp_path / "data").st_dev,
        record=record,
    )
    assert outcome == (2, 0)
    assert seen == [("chat-1", [lost_id], 2, "ok")]
    assert not lost.exists()
    assert (chat / kept.relative_path).is_file()
    assert (chat / source.name).is_file()
    assert store.derived_of(source) == [kept]


def test_an_orphan_pdf_is_swept_by_its_id(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """A converted PDF with its row stays; one a kill left without its row goes, recorded by the id its name carries."""
    source = put(store, alice, "harbour-deck.pptx")
    kept = store.register_derived(
        source, staged(tmp_path, CONVERTED, "converted.pdf"), converted_spec(), file_id=new_file_id(), quota_bytes=QUOTA
    )
    chat = workspaces.chat_dir(alice)
    lost_id = new_file_id()
    lost = chat / ".derived" / source.id / f"{lost_id}.pdf"
    lost.write_bytes(CONVERTED)
    seen: list[tuple[str, list[str], int, str]] = []
    outcome = store.sweep_user(
        "ghostwriter",
        "user-a",
        untouched_before=time.time() + 3600,
        device=os.stat(tmp_path / "data").st_dev,
        record=lambda chat_id, ids, count, outcome: seen.append((chat_id, list(ids), count, outcome)),
    )
    assert outcome == (1, 0)
    assert seen == [("chat-1", [lost_id], 1, "ok")]
    assert not lost.exists()
    assert (chat / kept.relative_path).is_file()
    assert store.derived_of(source) == [kept]


def test_the_sweep_names_a_render_by_its_id_only_in_its_own_folder(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """A file under ``.derived`` that is not ``<source id>/<id>.<ext>`` carries no id."""
    source = put(store, alice)
    derive(store, tmp_path, source)
    chat = workspaces.chat_dir(alice)
    (chat / ".derived" / "not-a-source").mkdir()
    (chat / ".derived" / "not-a-source" / f"{new_file_id()}.jpg").write_bytes(b"x")
    (chat / ".derived" / f"{new_file_id()}.jpg").write_bytes(b"x")
    (chat / ".derived" / source.id / "notes.jpg").write_bytes(b"x")
    part_id = new_file_id()
    (chat / ".derived" / source.id / f"{part_id}.part").write_bytes(b"x")
    seen: list[tuple[list[str], int]] = []
    removed, failed = store.sweep_user(
        "ghostwriter",
        "user-a",
        untouched_before=time.time() + 3600,
        device=os.stat(tmp_path / "data").st_dev,
        record=lambda chat_id, ids, count, outcome: seen.append((list(ids), count)),
    )
    assert (removed, failed) == (4, 0)
    assert seen == [([part_id], 4)]


def test_the_sweep_removes_the_empty_folders_of_renders_that_are_gone(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """``.derived/<source id>`` and ``.derived`` are folders like any other once they are empty and old."""
    source = put(store, alice)
    rec = derive(store, tmp_path, source)
    (workspaces.chat_dir(alice) / rec.relative_path).unlink()
    store.sweep_user(
        "ghostwriter",
        "user-a",
        untouched_before=time.time() + 3600,
        device=os.stat(tmp_path / "data").st_dev,
        record=unrecorded,
    )
    assert not derived_folder(workspaces, alice).exists()


def test_summarize_due_counts_derived_files(store: Store, alice: Principal, clock: Clock, tmp_path: Path) -> None:
    """The dry run's counts: renders as old as their sources are due with them."""
    clock.now = 1000.0
    old = put(store, alice, "old.pdf")
    clock.now = 3000.0
    new = put(store, alice, "new.pdf")
    derive(store, tmp_path, old)
    derive(store, tmp_path, new)
    database = tmp_path / "data" / "workbench.db"
    summary = store_module.summarize_due(database, 2000.0)
    assert summary["derived"] == store_module.DueSummary(due=1, oldest=1000.0, newest=1000.0, kept=1)
    assert summary["upload"].due == 1
    assert store_module.summarize_due(database, 0.0)["derived"] == store_module.DueSummary(0, None, None, 2)


def test_summarize_due_counts_a_stray_as_due(store: Store, alice: Principal, clock: Clock, tmp_path: Path) -> None:
    """A render whose source is gone is due whatever its age, as ``due_derived`` lists it."""
    source = put(store, alice)
    derive(store, tmp_path, source)
    store.remove(source)
    summary = store_module.summarize_due(tmp_path / "data" / "workbench.db", 0.0)
    assert summary["derived"].due == 1
    assert "upload" not in summary


def test_an_existing_database_gains_the_derived_table(store: Store, alice: Principal, tmp_path: Path) -> None:
    """A database made before renders has no such table: opening it adds one, and the dry run reads it as it is."""
    database = tmp_path / "data" / "workbench.db"
    put(store, alice)
    conn = sqlite3.connect(database)
    try:
        conn.execute("DROP TABLE derived")
        conn.commit()
    finally:
        conn.close()
    summary = store_module.summarize_due(database, 2_000_000.0)
    assert summary["upload"].due == 1
    assert "derived" not in summary
    reopened = Store(database, Workspaces(tmp_path / "data"))
    source = reopened.files_in_chat(alice)[0]
    assert derive(reopened, tmp_path, source).source_id == source.id


# --- the raw artifacts of a clip, streamed in ------------------------------------------------------------------------

JSONL = b'{"text": "a synthetic sentence"}\n'
OTHER_JSONL = b'{"text": "another synthetic sentence"}\n'
ZIP = b"PK\x03\x04 a synthetic archive"
TRANSCRIPT = b"[00:00] Speaker 1: a synthetic sentence\n"
JSONL_TYPE = "application/x-ndjson"
ZIP_TYPE = "application/zip"
TEXT_TYPE = "text/plain"
CLIP = b"synthetic clip bytes"  # 20 bytes


def media_source(store: Store, p: Principal) -> FileRecord:
    """Upload ``harbour-tour.mp4`` with invented bytes."""
    return put(store, p, "harbour-tour.mp4", CLIP)


def artifact(role: DerivedRole = "nextext_jsonl") -> DerivedSpec:
    """The spec of a clip's raw artifact: role ``nextext_jsonl`` or ``nextext_zip``, with no size in pixels."""
    return DerivedSpec(role, 0, ZIP_TYPE if role == "nextext_zip" else JSONL_TYPE, None, None, {})


def accept(_: Path) -> None:
    """A check that takes every artifact."""


class Recorder:
    """What ``place_derived`` records, call by call."""

    def __init__(self) -> None:
        """Start with no call."""
        self.calls: list[tuple[str, int, list[str], str]] = []

    def __call__(self, file_id: str, size: int, replaced: Sequence[str], outcome: str) -> None:
        """Note one record."""
        self.calls.append((file_id, size, list(replaced), outcome))


def stage_artifact(
    store: Store,
    source: FileRecord,
    data: bytes = JSONL,
    role: DerivedRole = "nextext_jsonl",
    *,
    max_bytes: int = 10**6,
    quota_bytes: int = 10**6,
    check: Callable[[Path], None] = accept,
) -> StagedArtifact:
    """Stream ``data`` in as an artifact of ``source``, with roomy limits."""
    return store.stage_derived(
        source, io.BytesIO(data), artifact(role), max_bytes=max_bytes, quota_bytes=quota_bytes, check=check
    )


def keep_artifact(
    store: Store,
    source: FileRecord,
    data: bytes = JSONL,
    role: DerivedRole = "nextext_jsonl",
    *,
    record: Recorder | None = None,
    quota_bytes: int = 10**6,
    cap_bytes: int = renders.DERIVED_MAX_BYTES,
) -> tuple[DerivedRecord, list[DerivedRecord]]:
    """Stage an artifact and place it."""
    ready = stage_artifact(store, source, data, role, quota_bytes=quota_bytes)
    return store.place_derived(
        ready, quota_bytes=quota_bytes, record=Recorder() if record is None else record, cap_bytes=cap_bytes
    )


def derive_as(store: Store, tmp_path: Path, source: FileRecord, role: DerivedRole, mime: str) -> DerivedRecord:
    """Register one file of ``role`` and ``mime`` through the executor's door, ``register_derived``."""
    return store.register_derived(
        source,
        staged(tmp_path, TRANSCRIPT, "made.bin"),
        DerivedSpec(role, 0, mime, 1280 if role == "frame" else None, 720 if role == "frame" else None, {}),
        file_id=new_file_id(),
        quota_bytes=QUOTA,
    )


def clip_folder(workspaces: Workspaces, p: Principal, source: FileRecord) -> Path:
    """The folder of a clip's derived files."""
    return workspaces.chat_dir(p) / ".derived" / source.id


def test_an_artifact_is_streamed_in_beside_its_clip(
    store: Store, alice: Principal, workspaces: Workspaces, clock: Clock
) -> None:
    """Staged, it is ``<id>.part`` in the clip's folder; placed, ``<id>.jsonl``, recorded before it is visible."""
    source = media_source(store, alice)
    clock.now += 500
    folder = clip_folder(workspaces, alice, source)
    ready = stage_artifact(store, source)
    assert (ready.source, ready.spec, ready.size) == (source, artifact(), len(JSONL))
    assert ready.part == folder / f"{ready.id}.part"
    assert disk_names(folder) == [f"{ready.id}.part"]
    assert stat.S_IMODE(ready.part.stat().st_mode) == 0o600
    assert stat.S_IMODE(folder.stat().st_mode) == stat.S_IMODE(folder.parent.stat().st_mode) == 0o700
    assert store.derived_of(source) == []
    seen = Recorder()
    visible: list[tuple[list[DerivedRecord], list[str]]] = []

    def record(file_id: str, size: int, replaced: Sequence[str], outcome: str) -> None:
        visible.append((store.derived_of(source), disk_names(folder)))
        seen(file_id, size, replaced, outcome)

    rec, replaced = store.place_derived(ready, quota_bytes=QUOTA, record=record)
    assert seen.calls == [(ready.id, len(JSONL), [], "ok")]
    assert visible == [([], [f"{ready.id}.part"])]  # nothing was visible when the event was written
    real = folder / f"{ready.id}.jsonl"
    assert real.read_bytes() == JSONL
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    assert disk_names(folder) == [real.name]
    assert replaced == []
    assert rec == DerivedRecord(
        id=ready.id,
        source_id=source.id,
        caller="ghostwriter",
        user_id="user-a",
        chat_id="chat-1",
        role="nextext_jsonl",
        ordinal=0,
        name=f"{ready.id}.jsonl",
        size=len(JSONL),
        mime=JSONL_TYPE,
        width=None,
        height=None,
        meta={},
        created_at=source.created_at,
    )
    assert rec.created_at != clock.now
    assert store.derived_of(source) == [rec]


def test_an_artifact_counts_in_the_quota(store: Store, alice: Principal) -> None:
    """Its bytes are the user's like a render's."""
    source = media_source(store, alice)
    used = store.usage(alice)
    keep_artifact(store, source, ZIP, "nextext_zip")
    assert store.usage(alice) == used + len(ZIP)


def test_an_artifact_past_its_bound_leaves_nothing(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """One byte over ``max_bytes`` stops the transfer: no ``.part``, no empty folder, no row."""
    source = media_source(store, alice)
    with pytest.raises(TooLarge):
        stage_artifact(store, source, b"x" * 11, max_bytes=10)
    assert not (workspaces.chat_dir(alice) / ".derived").exists()
    assert store.derived_of(source) == []
    assert stage_artifact(store, source, b"x" * 10, max_bytes=10).size == 10  # exactly the bound is taken


def test_an_artifact_past_the_quota_leaves_nothing(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """A transfer that would take the user over the quota stops, and leaves nothing."""
    source = media_source(store, alice)  # 20 bytes
    with pytest.raises(QuotaExceeded):
        stage_artifact(store, source, b"x" * 21, quota_bytes=40)
    assert not (workspaces.chat_dir(alice) / ".derived").exists()


def test_a_failed_check_leaves_nothing(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """The check is handed the whole ``.part``; what it raises is raised, and nothing is left."""
    source = media_source(store, alice)
    seen: list[Path] = []

    def refuse(part: Path) -> None:
        seen.append(part)
        assert part.read_bytes() == b"not json"
        raise UnreadableArtifact("the first line is not a JSON object")

    with pytest.raises(UnreadableArtifact) as raised:
        stage_artifact(store, source, b"not json", check=refuse)
    assert str(raised.value) == "unreadable_file: the first line is not a JSON object"
    assert len(seen) == 1
    assert seen[0].parent == clip_folder(workspaces, alice, source)
    assert not (workspaces.chat_dir(alice) / ".derived").exists()
    assert store.derived_of(source) == []


def test_an_unreadable_artifact_answers_with_its_code() -> None:
    """The message starts ``unreadable_file: ``, whether the caller wrote the code or not."""
    assert str(UnreadableArtifact("empty")) == "unreadable_file: empty"
    assert str(UnreadableArtifact("unreadable_file: empty")) == "unreadable_file: empty"
    assert isinstance(UnreadableArtifact("empty"), StoreError)


def test_a_dropped_stage_leaves_nothing(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """``drop_staged`` takes the ``.part`` and its empty folders; a second call, or one for a placed file, is fine."""
    source = media_source(store, alice)
    ready = stage_artifact(store, source)
    assert ready.part.is_file()
    store.drop_staged(ready)
    assert not ready.part.exists()
    assert not (workspaces.chat_dir(alice) / ".derived").exists()
    store.drop_staged(ready)
    placed = stage_artifact(store, source)
    rec, _ = store.place_derived(placed, quota_bytes=QUOTA, record=Recorder())
    store.drop_staged(placed)
    assert store.derived_of(source) == [rec]
    assert (clip_folder(workspaces, alice, source) / rec.name).is_file()


def test_a_staged_artifact_is_placed_once(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """Its ``.part`` is gone once placed: a second placing is refused and records nothing."""
    source = media_source(store, alice)
    ready = stage_artifact(store, source)
    rec, _ = store.place_derived(ready, quota_bytes=QUOTA, record=Recorder())
    seen = Recorder()
    with pytest.raises(NotFound):
        store.place_derived(ready, quota_bytes=QUOTA, record=seen)
    assert seen.calls == []
    assert store.derived_of(source) == [rec]
    assert disk_names(clip_folder(workspaces, alice, source)) == [rec.name]


def test_a_second_artifact_replaces_the_first_and_what_was_made_from_it(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """The row of its role, the transcript and the frames go with their files; the other artifact stays."""
    source = media_source(store, alice)
    first, _ = keep_artifact(store, source)
    zipped, _ = keep_artifact(store, source, ZIP, "nextext_zip")
    transcript = derive_as(store, tmp_path, source, "transcript", TEXT_TYPE)
    frame = derive_as(store, tmp_path, source, "frame", "image/jpeg")
    folder = clip_folder(workspaces, alice, source)
    assert len(disk_names(folder)) == 4
    seen = Recorder()
    second, replaced = keep_artifact(store, source, OTHER_JSONL, record=seen)
    assert replaced == [frame, first, transcript]  # by role
    assert seen.calls == [(second.id, len(OTHER_JSONL), [frame.id, first.id, transcript.id], "ok")]
    assert store.derived_of(source) == [second, zipped]
    assert disk_names(folder) == sorted([second.name, zipped.name])
    assert (folder / zipped.name).read_bytes() == ZIP
    assert store.usage(alice) == len(CLIP) + len(OTHER_JSONL) + len(ZIP)


def test_replacing_counts_the_replaced_bytes(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """The cap is met by the new bytes less the old ones; another role's bytes count, and a refusal leaves nothing."""
    source = media_source(store, alice)
    keep_artifact(store, source, b"x" * 12, cap_bytes=20)
    again, _ = keep_artifact(store, source, b"y" * 12, cap_bytes=20)
    assert store.derived_of(source) == [again]
    with pytest.raises(RendersFull):
        keep_artifact(store, source, b"z" * 12, "nextext_zip", cap_bytes=20)
    assert store.derived_of(source) == [again]
    assert disk_names(clip_folder(workspaces, alice, source)) == [again.name]


def test_replacing_needs_room_for_the_new_artifact_alone(store: Store, alice: Principal) -> None:
    """The room is the quota less what is held, plus what is replaced, when it is staged and again when it is placed."""
    source = media_source(store, alice)
    first, _ = keep_artifact(store, source, b"x" * 12, quota_bytes=40)  # 20 + 12 held, 8 free
    second, replaced = keep_artifact(store, source, b"y" * 12, quota_bytes=40)
    assert replaced == [first]
    assert store.usage(alice) == 32
    assert store.derived_of(source) == [second]


def test_an_artifact_that_no_longer_fits_is_not_kept(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """Another upload may take the room while the body arrives: the placing checks the quota again."""
    source = media_source(store, alice)  # 20 bytes
    ready = stage_artifact(store, source, b"x" * 12, quota_bytes=40)
    put(store, alice, "harbour-notes.txt", b"y" * 20)
    seen = Recorder()
    with pytest.raises(QuotaExceeded):
        store.place_derived(ready, quota_bytes=40, record=seen)
    assert seen.calls == []
    assert store.derived_of(source) == []
    assert not (workspaces.chat_dir(alice) / ".derived").exists()


@pytest.mark.parametrize("how", ["taken back", "chat deleted"])
def test_an_artifact_needs_its_source_and_its_chat(
    store: Store, alice: Principal, workspaces: Workspaces, how: str
) -> None:
    """A clip taken back (Ghostwriter's take-back) or a chat deleted between the two calls leaves nothing."""
    source = media_source(store, alice)
    ready = stage_artifact(store, source)
    if how == "taken back":
        assert store.remove_upload(source, lambda ids, outcome: None) == 0
        assert ready.part.is_file()  # the part is no render: only the placing finds that it is for nothing
    else:
        store.delete_chat(alice, lambda ids, outcome: None)
    seen = Recorder()
    with pytest.raises(Gone):
        store.place_derived(ready, quota_bytes=QUOTA, record=seen)
    assert seen.calls == []
    root = workspaces.user_root(alice)
    assert not (root / "chat-1" / ".derived").exists()
    assert not list(root.rglob("*.part"))
    assert store.derived_of(source) == []


def test_a_chat_deleted_while_its_artifact_is_checked_is_gone(
    store: Store, alice: Principal, workspaces: Workspaces
) -> None:
    """The check finds the ``.part`` gone with its chat: the answer is the chat's, not an error of the disk."""
    source = media_source(store, alice)

    def check(part: Path) -> None:
        store.delete_chat(alice, lambda ids, outcome: None)
        part.read_bytes()

    with pytest.raises(Gone):
        stage_artifact(store, source, check=check)
    assert not list(workspaces.user_root(alice).rglob("*.part"))


def test_a_staged_artifact_is_of_its_sources_own_owner_and_chat(store: Store, alice: Principal) -> None:
    """A record of another owner finds no source; a chat held as deleted takes no artifact, staged or placed."""
    source = media_source(store, alice)
    with pytest.raises(Gone):
        stage_artifact(store, dataclasses.replace(source, user_id="user-b"))
    ready = stage_artifact(store, source)
    store._deleted[(alice.caller, alice.user_id, alice.chat_id)] = store._monotonic() + 100
    with pytest.raises(Gone):
        stage_artifact(store, source)
    seen = Recorder()
    with pytest.raises(Gone):
        store.place_derived(ready, quota_bytes=QUOTA, record=seen)
    assert seen.calls == []
    assert not ready.part.exists()
    assert store.derived_of(source) == []


def test_an_unrecorded_artifact_is_not_kept(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """A record that cannot be written leaves no row, no file and no ``.part``, and its error goes up."""
    source = media_source(store, alice)
    ready = stage_artifact(store, source)

    def refuse(*_: object) -> None:
        raise RuntimeError("synthetic audit failure")

    with pytest.raises(RuntimeError, match="synthetic audit failure"):
        store.place_derived(ready, quota_bytes=QUOTA, record=refuse)
    assert store.derived_of(source) == []
    assert not (workspaces.chat_dir(alice) / ".derived").exists()


def test_an_artifact_that_fails_after_its_record_is_recorded_again(
    store: Store, alice: Principal, workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row that fails takes the file back out and records ``error``; what it was to replace stays, file and row."""
    source = media_source(store, alice)
    first, _ = keep_artifact(store, source)
    ready = stage_artifact(store, source, OTHER_JSONL)

    def refuse(*_: object) -> None:
        raise sqlite3.OperationalError("synthetic failure")

    monkeypatch.setattr(store, "_insert_derived", refuse)
    seen = Recorder()
    with pytest.raises(sqlite3.OperationalError):
        store.place_derived(ready, quota_bytes=QUOTA, record=seen)
    assert seen.calls == [
        (ready.id, len(OTHER_JSONL), [first.id], "ok"),
        (ready.id, len(OTHER_JSONL), [first.id], "error"),
    ]
    assert store.derived_of(source) == [first]
    folder = clip_folder(workspaces, alice, source)
    assert disk_names(folder) == [first.name]
    assert (folder / first.name).read_bytes() == JSONL


def test_taking_a_clip_back_takes_its_artifacts_with_it(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """Both artifacts, the transcript and the frames go with the clip: rows, files and folders, all named."""
    source = media_source(store, alice)
    jsonl, _ = keep_artifact(store, source)
    zipped, _ = keep_artifact(store, source, ZIP, "nextext_zip")
    transcript = derive_as(store, tmp_path, source, "transcript", TEXT_TYPE)
    frame = derive_as(store, tmp_path, source, "frame", "image/jpeg")
    events: list[tuple[list[str], str]] = []
    gone = store.remove_upload(source, lambda ids, outcome: events.append((list(ids), outcome)))
    assert gone == 4
    assert [outcome for _, outcome in events] == ["ok"]
    assert events[0][0][0] == source.id
    assert sorted(events[0][0][1:]) == sorted(each.id for each in (jsonl, zipped, transcript, frame))
    assert every_derived(store) == []
    assert not (workspaces.chat_dir(alice) / ".derived").exists()
    assert disk_names(workspaces.chat_dir(alice)) == []
    assert store.usage(alice) == 0


@pytest.mark.parametrize(
    ("role", "mime"),
    [
        ("transcript", "text/plain"),
        ("frame", "image/jpeg"),
        ("page", "image/jpeg"),
        ("image", "image/png"),
        ("pdf", "application/pdf"),
        ("nextext_jsonl", ZIP_TYPE),
        ("nextext_zip", JSONL_TYPE),
        ("nextext_zip", "text/plain"),
    ],
)
def test_only_the_two_artifacts_are_streamed_in(
    store: Store, alice: Principal, workspaces: Workspaces, role: DerivedRole, mime: str
) -> None:
    """Another role, or an artifact with another type, is no stage: nothing is made."""
    source = media_source(store, alice)
    with pytest.raises(PathError):
        store.stage_derived(
            source,
            io.BytesIO(JSONL),
            DerivedSpec(role, 0, mime, None, None, {}),
            max_bytes=10**6,
            quota_bytes=10**6,
            check=accept,
        )
    assert not (workspaces.chat_dir(alice) / ".derived").exists()


def test_the_orphan_sweep_names_parts_and_artifacts_by_id(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """A ``.part`` or an artifact or transcript with no row, in a clip's folder, is named by its id and removed."""
    source = media_source(store, alice)
    kept, _ = keep_artifact(store, source)
    folder = clip_folder(workspaces, alice, source)
    lost = {suffix: new_file_id() for suffix in ("part", "zip", "jsonl", "txt")}
    for suffix, file_id in lost.items():
        (folder / f"{file_id}.{suffix}").write_bytes(b"x")
    seen: list[tuple[str, list[str], int, str]] = []
    outcome = store.sweep_user(
        "ghostwriter",
        "user-a",
        untouched_before=time.time() + 3600,
        device=os.stat(tmp_path / "data").st_dev,
        record=lambda chat_id, ids, count, outcome: seen.append((chat_id, sorted(ids), count, outcome)),
    )
    assert outcome == (4, 0)
    assert seen == [("chat-1", sorted(lost.values()), 4, "ok")]
    assert disk_names(folder) == [kept.name]
    assert store.derived_of(source) == [kept]


def test_a_transcript_is_a_derived_text_file(
    store: Store, alice: Principal, workspaces: Workspaces, tmp_path: Path
) -> None:
    """The executor's transcript is ``.derived/<source id>/<id>.txt``, private, with no size in pixels."""
    source = media_source(store, alice)
    file_id = new_file_id()
    rec = store.register_derived(
        source,
        staged(tmp_path, TRANSCRIPT, "transcript.txt"),
        DerivedSpec("transcript", 0, TEXT_TYPE, None, None, {}),
        file_id=file_id,
        quota_bytes=QUOTA,
    )
    real = clip_folder(workspaces, alice, source) / f"{file_id}.txt"
    assert real.read_bytes() == TRANSCRIPT
    assert stat.S_IMODE(real.stat().st_mode) == 0o600
    assert (rec.role, rec.name, rec.mime, rec.width, rec.height, rec.size) == (
        "transcript",
        f"{file_id}.txt",
        TEXT_TYPE,
        None,
        None,
        len(TRANSCRIPT),
    )
    assert store.derived_of(source) == [rec]
    assert store.open_derived(rec).read() == TRANSCRIPT


@pytest.mark.parametrize(
    ("role", "mime", "width"),
    [
        ("transcript", "image/jpeg", None),
        ("nextext_zip", JSONL_TYPE, None),
        ("nextext_jsonl", ZIP_TYPE, None),
        ("frame", "image/png", 5),
        ("page", "text/plain", None),
        ("image", ZIP_TYPE, None),
        ("pdf", "text/plain", None),
        ("transcript", "text/plain", 5),
        ("nextext_zip", ZIP_TYPE, 5),
        ("page", "application/octet-stream", None),
    ],
)
def test_media_roles_need_their_types(
    store: Store, alice: Principal, tmp_path: Path, role: DerivedRole, mime: str, width: int | None
) -> None:
    """A role takes its own types alone, and only a picture has a size in pixels; the staged file stays."""
    source = media_source(store, alice)
    odd = staged(tmp_path, name="odd.bin")
    with pytest.raises(PathError):
        store.register_derived(
            source, odd, DerivedSpec(role, 0, mime, width, width, {}), file_id=new_file_id(), quota_bytes=QUOTA
        )
    assert odd.exists()
    assert store.derived_of(source) == []


def test_image_types_are_known() -> None:
    """The picture types a render is made from have their own, whatever the host's ``mime.types`` says."""
    assert store_module._mime("a.webp") == "image/webp"
    assert store_module._mime("a.bmp") == "image/bmp"
    assert store_module._mime("a.tif") == "image/tiff"
    assert store_module._mime("a.TIFF") == "image/tiff"
    assert store_module._mime("a.heic") == "image/heic"
    assert store_module._mime("a.HEIF") == "image/heif"


def test_media_types_are_known() -> None:
    """The types of a clip are the shared table's, whatever the host's ``mime.types`` says."""
    for suffix, mime in renders.MEDIA_TYPES.items():
        assert store_module._mime(f"clip{suffix}") == mime
        assert store_module._mime(f"CLIP{suffix.upper()}") == mime


# --- refusals remembered -----------------------------------------------------

#: A refusal as the runner words it: its code, then a fixed text that names no file.
REFUSED = "unreadable_file: the file holds a picture larger than 120 megapixels"


def test_a_refusal_is_remembered_for_its_source_alone(store: Store, alice: Principal, bob: Principal) -> None:
    """The refusal of a source is answered for that source, never for another file, a copy of it or another user's."""
    source = put(store, alice, "harbour-deck.pptx")
    other = put(store, alice, "harbour-notes.docx")
    theirs = put(store, bob, "harbour-deck.pptx")
    assert store.refusal_of(source) is None
    store.remember_refusal(source, REFUSED)

    assert store.refusal_of(source) == REFUSED
    assert (store.refusal_of(other), store.refusal_of(theirs)) == (None, None)
    again = put(store, alice, "harbour-deck.pptx")  # the same name again: a new file, with a new id
    assert again.id != source.id and store.refusal_of(again) is None


def test_a_refusal_is_not_inherited_by_a_record_that_is_not_its_files(store: Store, alice: Principal) -> None:
    """A record of another size under the source's id is another file's: it inherits nothing, and the refusal goes."""
    source = put(store, alice, "harbour-deck.pptx")
    store.remember_refusal(source, REFUSED)

    assert store.refusal_of(dataclasses.replace(source, size=source.size + 1)) is None
    assert store.refusal_of(source) is None


def test_refusals_are_bounded_the_least_recently_used_first(
    store: Store, alice: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past ``REFUSALS_MAX`` (a few thousand) the refusal asked for least recently goes; one asked for again stays."""
    assert 1000 <= store_module.REFUSALS_MAX <= 10_000
    monkeypatch.setattr(store_module, "REFUSALS_MAX", 2)
    first, second, third = (put(store, alice, f"deck-{n}.pptx") for n in (1, 2, 3))
    store.remember_refusal(first, REFUSED)
    store.remember_refusal(second, REFUSED)
    assert store.refusal_of(first) == REFUSED  # asked for: now the most recent
    store.remember_refusal(third, REFUSED)

    assert [store.refusal_of(rec) for rec in (first, second, third)] == [REFUSED, None, REFUSED]


def test_a_refusal_goes_with_its_source(store: Store, alice: Principal, tmp_path: Path) -> None:
    """Removed (retention) or deleted with its chat, a source's refusal is forgotten; so is every one on a restart."""
    removed = put(store, alice, "harbour-deck.pptx")
    deleted = put(store, alice, "harbour-notes.docx")
    kept = put(store, alice, "quay-deck.pptx")
    for rec in (removed, deleted, kept):
        store.remember_refusal(rec, REFUSED)

    store.remove(removed)
    assert (store.refusal_of(removed), store.refusal_of(kept)) == (None, REFUSED)
    assert store.delete_chat(alice, unrecorded) == 2
    assert (store.refusal_of(deleted), store.refusal_of(kept)) == (None, None)
    assert store._refusals == {}  # nothing is kept of a file that went
    survivor = put(
        store, dataclasses.replace(alice, chat_id="chat-2"), "harbour-deck.pptx", directory="/workspace/chat-2"
    )
    store.remember_refusal(survivor, REFUSED)
    restarted = Store(tmp_path / "data" / "workbench.db", Workspaces(tmp_path / "data"))
    assert restarted.refusal_of(survivor) is None


def test_a_refusal_that_may_pass_is_remembered_at_its_second_in_a_row(store: Store, alice: Principal) -> None:
    """Ruling R22: a refusal no file decides alone is remembered when it comes twice in a row for the same source.

    A success between the two resets the count; so does a record of another size under the same id.
    """
    source = put(store, alice, "harbour-deck.pptx")
    assert store.remember_refusal(source, REFUSED, at_once=False) is False
    assert store.refusal_of(source) is None
    assert store.remember_refusal(source, REFUSED, at_once=False) is True
    assert store.refusal_of(source) == REFUSED

    other = put(store, alice, "harbour-notes.docx")
    assert store.remember_refusal(other, REFUSED, at_once=False) is False
    store.forget_refusal(other)  # a success between them
    assert store.remember_refusal(other, REFUSED, at_once=False) is False
    assert store.refusal_of(other) is None
    resized = dataclasses.replace(other, size=other.size + 1)
    assert store.remember_refusal(resized, REFUSED, at_once=False) is False
    assert store.remember_refusal(source, REFUSED) is True  # at once, and the first is still remembered


def test_no_refusal_is_remembered_of_a_deleted_chat(store: Store, alice: Principal) -> None:
    """Review M2: a job of a chat deleted while it ran cannot leave a refusal of one of its files in memory."""
    source = put(store, alice, "harbour-deck.pptx")
    assert store.delete_chat(alice, unrecorded) == 1
    assert store.remember_refusal(source, REFUSED) is False
    assert store.remember_refusal(source, REFUSED, at_once=False) is False
    assert store.refusal_of(source) is None
    assert store._refusals == {}
