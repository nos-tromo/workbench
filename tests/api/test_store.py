"""The store keeps files on disk and their metadata in SQLite, and never crosses the user's own root."""

import dataclasses
import errno
import io
import os
import sqlite3
import stat
import threading
from pathlib import Path
from typing import override

import pytest

from tests.api.conftest import PG, unrecorded
from workbench.api import store as store_module
from workbench.api.store import (
    Conflict,
    NotFound,
    QuotaExceeded,
    StorageFull,
    Store,
    StoreError,
    TooLarge,
    new_file_id,
)
from workbench.models import DirEntry, FileRecord, Principal
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
    return Principal("open-webui", "user-a", "chat-1")


@pytest.fixture
def bob() -> Principal:
    """A second synthetic user of the same caller."""
    return Principal("open-webui", "user-b", "chat-1")


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
    assert (rec.caller, rec.user_id, rec.chat_id, rec.name) == ("open-webui", "user-a", "chat-1", "q.xlsx")
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
    assert put(store, alice, "Q.DOCX").mime == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


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
    assert store.files_in_chat(Principal("open-webui", "user-a", "chat-2")) == []
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
    keep = put(store, alice, "keep.txt")
    with pytest.raises(PathError):
        store.move(alice, keep.virtual_path, "/workspace/chat-1/x.part")
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
    with pytest.raises(NotFound):
        store.move(bob, rec.virtual_path, "/workspace/chat-1/stolen.xlsx")
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
        store.move(alice, bad, ok + ".moved")
    with pytest.raises(PathError):
        store.move(alice, ok, bad)
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
    """The root and a chat folder are not files: ``get``, ``delete`` and ``move`` refuse them."""
    put(store, alice)
    with pytest.raises(PathError):
        store.get(alice, target)
    with pytest.raises(PathError):
        deleted(store, alice, target)
    with pytest.raises(PathError):
        store.move(alice, target, "/workspace/chat-1/other.xlsx")
    with pytest.raises(PathError):
        store.move(alice, "/workspace/chat-1/q.xlsx", target)
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
    """Delete a file as ``/files/delete`` does, with no event under test: ``get``, then ``remove`` by the record."""
    rec = store.get(p, path)
    store.remove(rec)
    return rec


def test_get_then_remove_deletes_file_and_row(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """The route's delete, ``get`` then ``remove``: the file and the row are gone; the usage drops."""
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


# --- move ---------------------------------------------------------------------


def test_move_updates_the_path(store: Store, alice: Principal, workspaces: Workspaces, clock: Clock) -> None:
    """A rename keeps the id, the age and the content, and changes the path."""
    rec = put(store, alice, data=b"content")
    clock.now += 5000
    moved = store.move(alice, rec.virtual_path, "/workspace/chat-1/renamed.csv")
    assert moved.virtual_path == "/workspace/chat-1/renamed.csv"
    assert (moved.id, moved.size, moved.created_at, moved.kind) == (rec.id, rec.size, rec.created_at, rec.kind)
    assert moved.mime == "text/csv"
    assert workspaces.resolve(alice, moved.virtual_path).read_bytes() == b"content"
    assert not workspaces.resolve(alice, rec.virtual_path).exists()
    assert store.get(alice, moved.virtual_path) == moved
    assert store.files_in_chat(alice) == [moved]
    with pytest.raises(NotFound):
        store.get(alice, rec.virtual_path)


def test_move_into_a_folder_and_into_another_chat(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """A file moves to a folder, or to another chat folder, inside the user's own root."""
    rec = put(store, alice, data=b"content")
    store.mkdir(alice, "/workspace/chat-1/reports")
    in_folder = store.move(alice, rec.virtual_path, "/workspace/chat-1/reports/q.xlsx")
    assert (in_folder.chat_id, in_folder.name) == ("chat-1", "reports/q.xlsx")
    elsewhere = store.move(alice, in_folder.virtual_path, "/workspace/chat-2/q.xlsx")
    assert (elsewhere.chat_id, elsewhere.name) == ("chat-2", "q.xlsx")
    assert workspaces.resolve(alice, elsewhere.virtual_path).read_bytes() == b"content"
    assert store.files_in_chat(alice) == []
    assert store.files_in_chat(Principal("open-webui", "user-a", "chat-2")) == [elsewhere]
    assert store.usage(alice) == 7


def test_move_to_a_taken_name_is_a_conflict(store: Store, alice: Principal, workspaces: Workspaces) -> None:
    """Nothing is overwritten: a file, a folder or the source itself at the target is ``Conflict``."""
    a = put(store, alice, "a.txt", b"aaa")
    b = put(store, alice, "b.txt", b"bbb")
    store.mkdir(alice, "/workspace/chat-1/folder")
    for target in (b.virtual_path, "/workspace/chat-1/folder", a.virtual_path):
        with pytest.raises(Conflict):
            store.move(alice, a.virtual_path, target)
    assert workspaces.resolve(alice, a.virtual_path).read_bytes() == b"aaa"
    assert workspaces.resolve(alice, b.virtual_path).read_bytes() == b"bbb"
    assert store.get(alice, a.virtual_path) == a


def test_move_to_a_missing_folder_or_of_a_missing_file_is_not_found(store: Store, alice: Principal) -> None:
    """The destination folder below a chat must exist; the source must be a file with a record."""
    rec = put(store, alice)
    with pytest.raises(NotFound):
        store.move(alice, rec.virtual_path, "/workspace/chat-1/nowhere/q.xlsx")
    with pytest.raises(NotFound):
        store.move(alice, "/workspace/chat-1/missing.txt", "/workspace/chat-1/x.txt")
    store.mkdir(alice, "/workspace/chat-1/reports")
    with pytest.raises(NotFound):
        store.move(alice, "/workspace/chat-1/reports", "/workspace/chat-1/folder2")
    assert store.get(alice, rec.virtual_path) == rec


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


def test_remove_after_a_move_and_a_new_upload_of_the_old_name_removes_the_moved_file(
    store: Store, alice: Principal, workspaces: Workspaces, clock: Clock
) -> None:
    """A stale record names its file by id: the file that now sits at the old path is not touched."""
    old = put(store, alice, "q.xlsx", b"old")
    clock.now += 100
    (stale,) = store.due(clock.now)
    moved = store.move(alice, old.virtual_path, "/workspace/chat-1/r.xlsx")
    fresh = put(store, alice, "q.xlsx", b"new")
    assert fresh.name == "q.xlsx"
    store.remove(stale)
    chat = workspaces.chat_dir(alice)
    assert disk_names(chat) == ["q.xlsx"]
    assert (chat / "q.xlsx").read_bytes() == b"new"
    assert store.files_in_chat(alice) == [fresh]
    assert store.get(alice, fresh.virtual_path) == fresh
    assert store.usage(alice) == 3
    with pytest.raises(NotFound):
        store.get(alice, moved.virtual_path)


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
    assert (job.caller, job.user_id, job.chat_id, job.tool) == ("open-webui", "user-a", "chat-1", "run_python")
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
    assert store.get_job(Principal("open-webui", "user-a", "chat-9"), job.id) == job


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
