"""Virtual ``/workspace`` paths map only into the caller's own user root, never out of it."""

import os
import stat
from pathlib import Path

import pytest

from workbench.models import Principal
from workbench.paths import (
    DEFAULT_CHAT,
    VIRTUAL_ROOT,
    PathError,
    Workspaces,
    check_filename,
    check_id,
)

USER_A = Principal("open-webui", "user-a", "chat-1")
USER_B = Principal("open-webui", "user-b", "chat-1")


def test_virtual_path_maps_into_the_callers_own_root(tmp_path: Path) -> None:
    """A virtual file path lands under ``workspaces/<caller>/<user>/<chat>/``."""
    ws = Workspaces(tmp_path)
    assert ws.resolve(USER_A, "/workspace/chat-1/q.xlsx") == tmp_path / "workspaces/open-webui/user-a/chat-1/q.xlsx"


def test_constants_are_the_federation_namespace() -> None:
    """The virtual root and the default chat are the documented values."""
    assert VIRTUAL_ROOT == "/workspace"
    assert DEFAULT_CHAT == "default"


def test_nested_folders_are_allowed(tmp_path: Path) -> None:
    """Names after the chat id may form folders; each segment is checked."""
    ws = Workspaces(tmp_path)
    real = ws.resolve(USER_A, "/workspace/chat-1/reports/2026/q.xlsx")
    assert real == tmp_path / "workspaces/open-webui/user-a/chat-1/reports/2026/q.xlsx"


def test_unsaved_chat_ids_are_accepted(tmp_path: Path) -> None:
    """Open WebUI names an unsaved chat ``local:<id>``; the colon is allowed."""
    ws = Workspaces(tmp_path)
    p = Principal("open-webui", "user-a", "local:abc123")
    assert ws.resolve(p, "/workspace/local:abc123/q.xlsx").parts[-2] == "local:abc123"
    assert ws.chat_dir(p).name == "local:abc123"


def test_the_virtual_root_and_a_chat_folder_resolve_to_directories(tmp_path: Path) -> None:
    """``/workspace`` is the user root and ``/workspace/<chat>`` a folder in it."""
    ws = Workspaces(tmp_path)
    assert ws.resolve(USER_A, "/workspace") == ws.user_root(USER_A)
    assert ws.resolve(USER_A, "/workspace/") == ws.user_root(USER_A)
    assert ws.resolve(USER_A, "/workspace/chat-1") == ws.user_root(USER_A) / "chat-1"


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "/workspace/../x",
        "/workspace/chat-1/../../user-b/x",
        "workspace/chat-1/x",
        "/workspace/chat-1/a\x00b",
        "/workspace//chat-1/../..",
        "",
        ".",
        "/",
        "//workspace/chat-1/x",
        "/workspacex/chat-1/x",
        "/workspace/../workspace2/x",
        "/workspace/chat-1/../../../etc/passwd",
        "/workspace/-bad/x",
        "/workspace/chat-1/" + "x" * 201,
        "/workspace/" + "x" * 129 + "/f",
    ],
)
def test_paths_outside_the_root_are_refused(tmp_path: Path, bad: str) -> None:
    """Anything that leaves ``/workspace`` or breaks a segment rule raises ``PathError``."""
    ws = Workspaces(tmp_path)
    with pytest.raises(PathError):
        ws.resolve(USER_A, bad)


def test_every_result_stays_inside_the_user_root(tmp_path: Path) -> None:
    """For hostile inputs the answer is either a refusal or a path under the user root."""
    ws = Workspaces(tmp_path)
    root = ws.user_root(USER_A)
    hostile = [
        "/workspace/chat-1/./q.xlsx",
        "/workspace/chat-1//q.xlsx",
        "/workspace/chat-1/sub/../q.xlsx",
        "/workspace/chat-1/../chat-2/q.xlsx",
        "/workspace/chat-1/..",
        "/workspace/chat-1/../..",
        "/workspace/chat-1/../../..",
        "/workspace/chat-1/%2e%2e/x",
        "/workspace/chat-1/..\\..\\x",
        "/workspace/chat-1/\n",
        "/workspace/chat-1/a\x00",
        "/workspace/chat-1/.../x",
    ]
    for virtual in hostile:
        try:
            real = ws.resolve(USER_A, virtual)
        except PathError:
            continue
        assert real == root or root in real.parents, virtual
        assert ".." not in real.parts, virtual


def test_symlinked_chat_dir_is_refused(tmp_path: Path) -> None:
    """A chat folder that is a symlink never resolves, even to a place inside the root."""
    ws = Workspaces(tmp_path)
    root = ws.user_root(USER_A)
    (root / "real").mkdir(parents=True)
    (root / "chat-1").symlink_to(root / "real")
    with pytest.raises(PathError):
        ws.resolve(USER_A, "/workspace/chat-1/q.xlsx")
    with pytest.raises(PathError):
        ws.resolve(USER_A, "/workspace/chat-1")


def test_symlinked_file_is_refused(tmp_path: Path) -> None:
    """A file that is a symlink never resolves, even when its target is inside the root."""
    ws = Workspaces(tmp_path)
    chat = ws.chat_dir(USER_A)
    (chat / "target.txt").write_text("synthetic")
    (chat / "link.txt").symlink_to(chat / "target.txt")
    assert ws.resolve(USER_A, "/workspace/chat-1/target.txt") == chat / "target.txt"
    with pytest.raises(PathError):
        ws.resolve(USER_A, "/workspace/chat-1/link.txt")


def test_dangling_symlink_is_refused(tmp_path: Path) -> None:
    """A symlink whose target is missing is still a symlink and is refused."""
    ws = Workspaces(tmp_path)
    chat = ws.chat_dir(USER_A)
    (chat / "dangling").symlink_to(tmp_path / "does-not-exist")
    with pytest.raises(PathError):
        ws.resolve(USER_A, "/workspace/chat-1/dangling")


def test_symlink_to_another_users_file_is_refused(tmp_path: Path) -> None:
    """A link planted in one user's folder cannot reach the other user's files."""
    ws = Workspaces(tmp_path)
    secret = ws.chat_dir(USER_B) / "secret.txt"
    secret.write_text("synthetic")
    chat_a = ws.chat_dir(USER_A)
    (chat_a / "peek.txt").symlink_to(secret)
    (chat_a / "peek-dir").symlink_to(ws.chat_dir(USER_B))
    with pytest.raises(PathError):
        ws.resolve(USER_A, "/workspace/chat-1/peek.txt")
    with pytest.raises(PathError):
        ws.resolve(USER_A, "/workspace/chat-1/peek-dir/secret.txt")


def test_symlinked_nested_folder_is_refused(tmp_path: Path) -> None:
    """A symlink in the middle of a nested path is refused, not followed."""
    ws = Workspaces(tmp_path)
    chat = ws.chat_dir(USER_A)
    (tmp_path / "outside").mkdir()
    (chat / "sub").symlink_to(tmp_path / "outside")
    with pytest.raises(PathError):
        ws.resolve(USER_A, "/workspace/chat-1/sub/deeper/q.xlsx")


def test_symlinked_user_root_is_refused(tmp_path: Path) -> None:
    """A user folder that is a link to someone else's folder is refused."""
    ws = Workspaces(tmp_path)
    ws.chat_dir(USER_B)
    ws.user_root(USER_A).parent.mkdir(parents=True, exist_ok=True)
    ws.user_root(USER_A).symlink_to(ws.user_root(USER_B))
    with pytest.raises(PathError):
        ws.resolve(USER_A, "/workspace/chat-1/q.xlsx")
    with pytest.raises(PathError):
        ws.chat_dir(USER_A)


def test_a_file_in_the_middle_of_a_path_is_not_a_crash(tmp_path: Path) -> None:
    """A regular file used as a folder resolves lexically; the later file operation fails."""
    ws = Workspaces(tmp_path)
    chat = ws.chat_dir(USER_A)
    (chat / "plain.txt").write_text("synthetic")
    assert ws.resolve(USER_A, "/workspace/chat-1/plain.txt/x") == chat / "plain.txt" / "x"


def test_two_users_never_share_a_path(tmp_path: Path) -> None:
    """The same virtual path is a different real path for each user."""
    ws = Workspaces(tmp_path)
    a = ws.resolve(USER_A, "/workspace/chat-1/q.xlsx")
    b = ws.resolve(USER_B, "/workspace/chat-1/q.xlsx")
    assert a != b
    assert ws.user_root(USER_A) in a.parents
    assert ws.user_root(USER_B) in b.parents
    assert ws.user_root(USER_A) not in b.parents


def test_two_callers_never_share_a_path(tmp_path: Path) -> None:
    """The caller is part of the root, so equal user ids from two callers stay apart."""
    ws = Workspaces(tmp_path)
    other = Principal("other-caller", "user-a", "chat-1")
    assert ws.user_root(USER_A) != ws.user_root(other)


def test_the_chat_id_in_the_principal_never_selects_another_users_data(tmp_path: Path) -> None:
    """The chat id is only a folder: a hostile one is refused, a free one stays in the user's root."""
    ws = Workspaces(tmp_path)
    for chat in ("../user-b", "user-b/chat-1", "..", ""):
        with pytest.raises(PathError):
            ws.chat_dir(Principal("open-webui", "user-a", chat))
    assert ws.user_root(USER_A) in ws.chat_dir(Principal("open-webui", "user-a", "user-b")).parents


@pytest.mark.parametrize("field", ["caller", "user_id"])
@pytest.mark.parametrize("bad", ["", "..", "../user-b", "a/b", "-x", "x" * 129])
def test_a_hostile_caller_or_user_id_never_builds_a_root(tmp_path: Path, field: str, bad: str) -> None:
    """The caller and the user id become folder names, so they pass ``check_id`` too."""
    ws = Workspaces(tmp_path)
    p = Principal(bad, "user-a", "chat-1") if field == "caller" else Principal("open-webui", bad, "chat-1")
    with pytest.raises(PathError):
        ws.user_root(p)
    with pytest.raises(PathError):
        ws.resolve(p, "/workspace/chat-1/q.xlsx")
    with pytest.raises(PathError):
        ws.chat_dir(p)


def test_chat_dir_is_created_private_with_private_parents(tmp_path: Path) -> None:
    """The chat folder and every folder made on the way are mode 0700."""
    ws = Workspaces(tmp_path)
    chat = ws.chat_dir(USER_A)
    assert chat == tmp_path / "workspaces/open-webui/user-a/chat-1"
    for path in (chat, chat.parent, chat.parent.parent, chat.parent.parent.parent):
        assert stat.S_IMODE(path.stat().st_mode) == 0o700, path


def test_chat_dir_is_private_whatever_the_umask(tmp_path: Path) -> None:
    """A permissive umask does not loosen the folders chat_dir creates."""
    ws = Workspaces(tmp_path)
    old = os.umask(0)
    try:
        chat = ws.chat_dir(USER_A)
    finally:
        os.umask(old)
    assert stat.S_IMODE(chat.stat().st_mode) == 0o700
    assert stat.S_IMODE(chat.parent.stat().st_mode) == 0o700


def test_chat_dir_is_idempotent_and_keeps_existing_content(tmp_path: Path) -> None:
    """Calling it again returns the same folder and touches nothing in it."""
    ws = Workspaces(tmp_path)
    chat = ws.chat_dir(USER_A)
    (chat / "keep.txt").write_text("synthetic")
    assert ws.chat_dir(USER_A) == chat
    assert (chat / "keep.txt").read_text() == "synthetic"


def test_chat_dir_refuses_a_symlinked_chat_folder(tmp_path: Path) -> None:
    """A chat folder replaced by a link is refused instead of followed or recreated."""
    ws = Workspaces(tmp_path)
    root = ws.user_root(USER_A)
    root.mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (root / "chat-1").symlink_to(tmp_path / "outside")
    with pytest.raises(PathError):
        ws.chat_dir(USER_A)


def test_chat_dir_refuses_a_file_in_its_place(tmp_path: Path) -> None:
    """A regular file where the chat folder should be is an error, not a directory."""
    ws = Workspaces(tmp_path)
    root = ws.user_root(USER_A)
    root.mkdir(parents=True)
    (root / "chat-1").write_text("synthetic")
    with pytest.raises(PathError):
        ws.chat_dir(USER_A)


@pytest.mark.parametrize(
    "bad", ["", "..", "a/b", "../x", "x" * 129, "-x", ".x", "a..b", "a b", "a\nb", "a\n", "é", "a\x00"]
)
def test_bad_ids_are_refused(bad: str) -> None:
    """``check_id`` raises ``PathError`` for anything outside the id alphabet."""
    with pytest.raises(PathError):
        check_id(bad, what="chat id")


@pytest.mark.parametrize(
    "good",
    ["chat-1", "user-a", "0", "a", "x" * 128, "f47ac10b-58cc-4372-a567-0e02b2c3d479", "local:abc123", "a.b_c-d:e"],
)
def test_good_ids_are_returned_unchanged(good: str) -> None:
    """Open WebUI's UUIDs and ``local:<id>`` names pass untouched."""
    assert check_id(good, what="chat id") == good


def test_the_id_error_names_what_was_refused() -> None:
    """The message says which field was wrong, never the value."""
    with pytest.raises(PathError, match="chat id"):
        check_id("../secret-value", what="chat id")
    with pytest.raises(PathError) as info:
        check_id("../secret-value", what="chat id")
    assert "secret-value" not in str(info.value)


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "/a", "a/", "a\x00b", "x" * 201])
def test_bad_filenames_are_refused(bad: str) -> None:
    """``check_filename`` raises ``PathError`` for anything but a plain name."""
    with pytest.raises(PathError):
        check_filename(bad)


@pytest.mark.parametrize(
    "good", ["q.xlsx", "quarterly-figures.xlsx", ".hidden", "a..b", "x" * 200, "with space.txt", "-dash"]
)
def test_good_filenames_are_returned_unchanged(good: str) -> None:
    """A plain name of one to 200 characters passes untouched."""
    assert check_filename(good) == good


def test_filenames_are_limited_to_255_bytes_in_utf_8() -> None:
    """A name within 200 characters but over 255 bytes is refused; the byte limit is Linux NAME_MAX."""
    with pytest.raises(PathError):
        check_filename("é" * 200)
    assert check_filename("é" * 127) == "é" * 127


def test_a_filename_that_is_not_valid_unicode_is_refused() -> None:
    """A lone surrogate cannot be encoded, so it is a ``PathError`` and not a ``UnicodeEncodeError``."""
    with pytest.raises(PathError):
        check_filename("a\ud800b")


def test_path_error_is_a_value_error() -> None:
    """Callers may catch ``ValueError``."""
    assert issubclass(PathError, ValueError)


def test_round_trip_to_virtual(tmp_path: Path) -> None:
    """``to_virtual(resolve(v)) == v`` for a file, a nested file, a chat folder and the root."""
    ws = Workspaces(tmp_path)
    for virtual in (
        "/workspace/chat-1/q.xlsx",
        "/workspace/chat-1/reports/2026/q.xlsx",
        "/workspace/chat-1",
        "/workspace/local:abc123/q.xlsx",
        "/workspace",
    ):
        assert ws.to_virtual(USER_A, ws.resolve(USER_A, virtual)) == virtual


def test_to_virtual_refuses_what_is_not_in_the_users_root(tmp_path: Path) -> None:
    """Another user's path, a sibling, an outside path and a ``..`` walk are all refused."""
    ws = Workspaces(tmp_path)
    root = ws.user_root(USER_A)
    for real in (
        ws.resolve(USER_B, "/workspace/chat-1/q.xlsx"),
        root.parent / "user-a2" / "chat-1" / "q.xlsx",
        tmp_path / "elsewhere.txt",
        root / "chat-1" / ".." / ".." / "user-b" / "q.xlsx",
        root / "chat-1" / "..",
        Path("relative/chat-1/q.xlsx"),
    ):
        with pytest.raises(PathError):
            ws.to_virtual(USER_A, real)
