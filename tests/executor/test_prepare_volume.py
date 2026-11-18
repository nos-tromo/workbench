"""The volume layout: made once by root before the api and the executor start, refusing symlinks."""

import os
import re
import stat
import sys
from pathlib import Path

import pytest

from workbench.executor import prepare_volume as pv
from workbench.executor.prepare_volume import Owners, VolumeError, prepare_volume

LINUX = sys.platform == "linux"
ROOT_ON_LINUX = LINUX and os.geteuid() == 0
ME = (os.getuid(), os.getgid())
# Off root, every part belongs to the test runner; the modes and refusals are the same.
MINE = Owners(root=ME, api=ME)


def _mode(path: Path) -> int:
    """Permission bits of ``path``, sticky bit included, without following a symlink."""
    return stat.S_IMODE(os.lstat(path).st_mode)


def _ids(path: Path) -> tuple[int, int]:
    """Owner and group of ``path``, without following a symlink."""
    st = os.lstat(path)
    return st.st_uid, st.st_gid


def test_the_layout_is_created_with_its_modes(tmp_path: Path) -> None:
    """An empty volume gets three folders and an empty database file, each with its own mode."""
    prepare_volume(tmp_path, MINE)

    assert _mode(tmp_path) == 0o1771
    assert _mode(tmp_path / "workspaces") == 0o700
    assert _mode(tmp_path / "staging") == 0o700
    assert _mode(tmp_path / "jobs") == 0o711
    assert _mode(tmp_path / "workbench.db") == 0o600
    assert stat.S_ISREG(os.lstat(tmp_path / "workbench.db").st_mode)
    assert (tmp_path / "workbench.db").read_bytes() == b""
    assert sorted(p.name for p in tmp_path.iterdir()) == ["jobs", "staging", "workbench.db", "workspaces"]


def test_running_it_again_keeps_the_content_and_repairs_the_modes(tmp_path: Path) -> None:
    """It is safe to run at every start: files stay, loosened modes are tightened again."""
    prepare_volume(tmp_path, MINE)
    (tmp_path / "workspaces" / "ghostwriter").mkdir()
    (tmp_path / "workbench.db").write_bytes(b"SQLite format 3\x00")
    (tmp_path / "workspaces").chmod(0o755)
    (tmp_path / "jobs").chmod(0o777)
    (tmp_path / "workbench.db").chmod(0o644)
    tmp_path.chmod(0o755)

    prepare_volume(tmp_path, MINE)

    assert (tmp_path / "workspaces" / "ghostwriter").is_dir()
    assert (tmp_path / "workbench.db").read_bytes() == b"SQLite format 3\x00"
    assert _mode(tmp_path / "workspaces") == 0o700
    assert _mode(tmp_path / "jobs") == 0o711
    assert _mode(tmp_path / "workbench.db") == 0o600
    assert _mode(tmp_path) == 0o1771


@pytest.mark.parametrize("name", ["workspaces", "staging", "jobs", "workbench.db"])
def test_a_symlink_in_the_layout_is_refused_and_not_followed(name: str, tmp_path: Path) -> None:
    """A symlink where a part of the layout belongs could hand root's chown and chmod to anything."""
    data = tmp_path / "data"
    data.mkdir()
    target = tmp_path / "elsewhere"
    target.mkdir() if name != "workbench.db" else target.write_bytes(b"")
    target.chmod(0o750)
    (data / name).symlink_to(target)

    with pytest.raises(VolumeError, match=re.escape(f"{name} is a symlink")):
        prepare_volume(data, MINE)

    assert _mode(target) == 0o750
    assert (data / name).is_symlink()


def test_a_data_dir_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    """The volume root itself is never reached through a symlink either."""
    real = tmp_path / "real"
    real.mkdir()
    real.chmod(0o750)
    (tmp_path / "data").symlink_to(real)

    with pytest.raises(VolumeError, match="data folder is a symlink"):
        prepare_volume(tmp_path / "data", MINE)

    assert _mode(real) == 0o750
    assert list(real.iterdir()) == []


@pytest.mark.parametrize(("name", "kind"), [("workspaces", "file"), ("jobs", "file"), ("workbench.db", "folder")])
def test_a_part_of_the_wrong_kind_is_refused(name: str, kind: str, tmp_path: Path) -> None:
    """A file where a folder belongs, or a folder where the database belongs, stops the preparation."""
    if kind == "file":
        (tmp_path / name).write_bytes(b"")
    else:
        (tmp_path / name).mkdir()

    with pytest.raises(VolumeError, match=re.escape(f"{name} is not a")):
        prepare_volume(tmp_path, MINE)


def test_a_fifo_where_the_database_belongs_is_refused_without_blocking(tmp_path: Path) -> None:
    """Opening a FIFO for reading would wait for a writer; the check never opens one."""
    os.mkfifo(tmp_path / "workbench.db")

    with pytest.raises(VolumeError, match=r"workbench\.db is not a regular file"):
        prepare_volume(tmp_path, MINE)


def test_main_reports_a_refusal_in_one_short_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one-shot exits 1 with a message naming the part, never a traceback."""
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path / "missing"))

    assert pv.main() == 1

    err = capsys.readouterr().err
    assert err.startswith("workbench prepare-volume: ")
    assert err.count("\n") == 1
    assert "Traceback" not in err


def test_main_reports_an_unexpected_error_by_its_type_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An error the one-shot did not foresee is one line with its type: no traceback, no message, no path."""
    path = "/data/workspaces/user-a/chat-1/quarterly-figures.xlsx"

    def fail(data_dir: Path, owners: Owners | None = None) -> None:
        raise ValueError(f"cannot settle {path}")

    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(pv, "prepare_volume", fail)

    assert pv.main() == 1

    assert capsys.readouterr().err == "workbench prepare-volume: stopped by an unexpected error (ValueError)\n"


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="owning parts by other ids needs root on Linux")
def test_as_root_the_layout_belongs_to_the_api_and_to_root(tmp_path: Path) -> None:
    """The api owns workspaces, staging and the database; root owns the jobs folder and the volume root."""
    data = tmp_path / "data"
    data.mkdir()

    prepare_volume(data)

    assert (_ids(data), _mode(data)) == ((0, 10001), 0o1771)
    assert (_ids(data / "workspaces"), _mode(data / "workspaces")) == ((10001, 10001), 0o700)
    assert (_ids(data / "staging"), _mode(data / "staging")) == ((10001, 10001), 0o700)
    assert (_ids(data / "jobs"), _mode(data / "jobs")) == ((0, 0), 0o711)
    assert (_ids(data / "workbench.db"), _mode(data / "workbench.db")) == ((10001, 10001), 0o600)


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="owning parts by other ids needs root on Linux")
def test_as_root_main_prepares_the_configured_volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``python -m workbench.executor.prepare_volume`` reads ``WORKBENCH_DATA_DIR`` and exits 0."""
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))

    assert pv.main() == 0

    assert _ids(tmp_path / "jobs") == (0, 0)
    assert _ids(tmp_path / "staging") == (10001, 10001)


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="the sticky bit binds only unprivileged users; needs root on Linux")
def test_as_root_the_api_cannot_move_the_jobs_folder_away(public_tmp: Path) -> None:
    """The volume root is root's and sticky: the api may add its database journal but not replace ``jobs``."""
    data = public_tmp / "data"
    data.mkdir()
    prepare_volume(data)
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child reports through its exit code
        code = 0
        try:
            os.setgroups([])
            os.setgid(10001)
            os.setuid(10001)
            fd = os.open(data / "workbench.db-wal", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            try:
                os.rename(data / "jobs", data / "jobs-moved")
                code = 3
            except PermissionError:
                pass
        except BaseException:
            code = 4
        os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert (data / "jobs").is_dir()
    assert _ids(data / "workbench.db-wal") == (10001, 10001)
