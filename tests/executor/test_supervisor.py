"""The executor's supervisor: copy-in, a run in a slot, validated outputs out, and a slot pool that fails closed."""

import asyncio
import contextlib
import dataclasses
import logging
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, override

import pytest
import yaml

from tests.conftest import scaled
from tests.executor.conftest import IPC_SHAPE, SHARED_IPC_ONLY
from workbench.config import Settings, load_settings
from workbench.executor import supervisor
from workbench.executor.prepare_volume import Owners, prepare_volume
from workbench.executor.sandbox import Slot, SlotUse
from workbench.executor.supervisor import (
    MAX_OUTPUTS,
    NoUsableSlot,
    SocketRefused,
    Supervisor,
    make_slots,
    startup_problem,
)
from workbench.protocol import MAX_LINE, JobRequest, JobResult, Limits, OutputFile, decode_result, encode

pytestmark = pytest.mark.anyio

LINUX = sys.platform == "linux"
ROOT_ON_LINUX = LINUX and os.geteuid() == 0
#: Root in the executor image (its ``/workspace`` link), never a bare host: the tests that change ``/dev/zero`` need it.
IN_THE_IMAGE = ROOT_ON_LINUX and Path("/workspace").is_symlink()
ME = (os.getuid(), os.getgid())
CALLER, USER, CHAT = "open-webui", "user-a", "chat-1"
MB = 1024 * 1024

PRELUDE = """\
import json, os, pathlib, sys
spec_path = pathlib.Path(sys.argv[1])
result_path = pathlib.Path(sys.argv[2])
spec = json.loads(spec_path.read_text())
result = {}
def tree(top):
    return sorted(str(p.relative_to(top)) for p in pathlib.Path(top).rglob("*"))
"""
EPILOGUE = """
result_path.write_text(json.dumps(result))
"""


async def until(condition: Any, *, seconds: float = 10.0) -> None:
    """Let the loop run until ``condition()`` holds."""
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.01)


def _sock(data: Path) -> Path:
    """Where the socket of a volume at ``data`` lies: in a ``run`` folder beside it, as ``/run/workbench``."""
    return data.parent / "run" / "executor.sock"


def _settings(data: Path, **env: str) -> Settings:
    """Executor settings for a volume at ``data``, with ``env`` on top."""
    values = {
        "WORKBENCH_DATA_DIR": str(data),
        "WORKBENCH_EXECUTOR_SOCKET": str(_sock(data)),
        "WORKBENCH_EXECUTOR_SLOTS": "2",
        **env,
    }
    return load_settings(values, role="executor")


#: The real floor, for the tests of the floor itself.
FREE_SPACE_FLOOR = supervisor.free_space_floor


@pytest.fixture(autouse=True)
def _room_on_the_volume() -> Iterator[None]:
    """A floor of 0 bytes: a test's volume is a folder on the developer's disk, whose free space is not the test's.

    The supervisor still asks the volume before each job and in every pass; the tests of the floor put the real one
    back and stub what the volume answers. Its own patch, not the test's ``monkeypatch``: requested here, that would be
    set up before every other fixture and undone after them, and a test's patch of ``shutil`` would then outlive
    ``public_tmp``'s removal.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(supervisor, "free_space_floor", lambda size: 0)
        yield


#: A data volume no test of a watchdog's pass reads.
NOWHERE = Path("/nowhere")


def _own_cgroup(tmp_path: Path) -> Path:
    """A ``/proc/self/cgroup`` of a process in a cgroup namespace of its own, cgroup v2."""
    path = tmp_path / "own-cgroup"
    path.write_text("0::/\n")
    return path


class _Above:
    """A floor the volume is always above, for the tests of a watchdog's pass."""

    def below(self) -> bool:
        """Never below."""
        return False


@pytest.fixture
def volume(tmp_path: Path) -> Path:
    """A prepared volume owned by the test runner, as the supervisor without slots expects it."""
    data = tmp_path / "data"
    data.mkdir()
    prepare_volume(data, Owners(root=ME, api=ME))
    return data


@pytest.fixture
def root_volume(public_tmp: Path) -> Path:
    """A volume prepared as root, owned as in production, which the slot uids can reach."""
    data = public_tmp / "data"
    data.mkdir()
    prepare_volume(data)
    return data


def _chat(data: Path, files: dict[str, bytes] | None = None) -> Path:
    """The user's chat folder on the volume, holding ``files`` (paths below it)."""
    chat = data / "workspaces" / CALLER / USER / CHAT
    chat.mkdir(parents=True, exist_ok=True)
    for name, content in (files or {}).items():
        (chat / name).parent.mkdir(parents=True, exist_ok=True)
        (chat / name).write_bytes(content)
    return chat


def _request(**overrides: Any) -> JobRequest:
    """A job for the user's chat with roomy limits."""
    fields: dict[str, Any] = {
        "job_id": uuid.uuid4().hex,
        "tool": "script",
        "args": {},
        "caller": CALLER,
        "user_id": USER,
        "chat_id": CHAT,
        "input_files": [],
        "limits": Limits(wall_s=60, cpu_s=60, memory_mb=1024, file_mb=50),
        "max_chars": 16384,
        **overrides,
    }
    return JobRequest(**fields)


def _runner(monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Run ``body`` as the job instead of the job entry point; it fills ``result`` and may write ``out/``."""
    code = PRELUDE + textwrap.dedent(body) + EPILOGUE
    monkeypatch.setattr(
        supervisor, "job_argv", lambda spec, result: [sys.executable, "-I", "-c", code, str(spec), str(result)]
    )


def _entries(folder: Path) -> list[str]:
    """Names in ``folder``."""
    return sorted(p.name for p in folder.iterdir())


def _only(folder: Path) -> str:
    """The one name in ``folder``."""
    (name,) = _entries(folder)
    return name


def _mode(path: Path) -> int:
    """Permission bits of ``path``, without following a symlink."""
    return stat.S_IMODE(os.lstat(path).st_mode)


def _ids(path: Path) -> tuple[int, int]:
    """Owner and group of ``path``, without following a symlink."""
    st = os.lstat(path)
    return st.st_uid, st.st_gid


# --- copy in, run, stage out ------------------------------------------------------------------


async def test_run_job_copies_inputs_and_stages_outputs(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The job reads its copy of an input and writes ``out/r.csv``; the output is staged, the job folder removed."""
    _chat(volume, {"q.csv": b"region,total\nnorth,3\n"})
    _runner(
        monkeypatch,
        """
        rows = pathlib.Path("q.csv").read_text().splitlines()
        pathlib.Path("out/r.csv").write_text(rows[1].upper())
        result = {"rows": len(rows) - 1}
        """,
    )
    request = _request(input_files=["q.csv"])

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(request)

    staged = volume / "staging" / request.job_id / "r.csv"
    assert answer == JobResult(
        job_id=request.job_id,
        status="ok",
        result={"rows": 1},
        outputs=[OutputFile(name="r.csv", staged_path=str(staged), size=len(b"NORTH,3"))],
    )
    assert staged.read_bytes() == b"NORTH,3"
    assert _mode(staged) == 0o600
    assert _mode(staged.parent) == 0o700
    assert (volume / "workspaces") not in staged.parents
    assert _entries(volume / "jobs") == []


async def test_the_real_job_entry_point_runs_a_registered_runner(volume: Path) -> None:
    """Without a stand-in, the job is ``python -I -m workbench.executor.job`` and its result comes back."""
    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request(tool="echo", args={"x": 1}))

    assert (answer.status, answer.result, answer.outputs, answer.message) == ("ok", {"x": 1}, [], "")


async def test_a_runner_that_raised_is_an_error_with_its_text(volume: Path) -> None:
    """The job entry point turns a runner's exception into ``{"error": ...}``, which is an ``error`` status."""
    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request(tool="no-such-tool"))

    assert answer.status == "error"
    assert answer.message == "the tool failed"
    assert answer.result["error"].startswith("KeyError: ")


async def test_outputs_that_are_symlinks_folders_or_fifos_are_dropped(
    volume: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only regular files leave ``out/``; a symlink to a file outside is not followed, a FIFO does not block."""
    secret = tmp_path / "secret.txt"
    secret.write_text("not the job's")
    _chat(volume)
    _runner(
        monkeypatch,
        f"""
        pathlib.Path("out/r.csv").write_text("a")
        os.symlink({str(secret)!r}, "out/link.txt")
        os.mkdir("out/folder")
        pathlib.Path("out/folder/inner.csv").write_text("b")
        os.mkfifo("out/pipe")
        """,
    )

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request())

    assert answer.status == "ok"
    assert [o.name for o in answer.outputs] == ["r.csv"]
    assert secret.read_text() == "not the job's"
    assert _entries(volume / "jobs") == []


async def test_too_many_outputs_are_an_error_and_nothing_is_staged(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job may hand back at most 20 files; 21 refuses them all, and the job's result is kept."""
    _chat(volume)
    _runner(
        monkeypatch,
        f"""
        for n in range({MAX_OUTPUTS + 1}):
            pathlib.Path(f"out/part-{{n}}.csv").write_text("x")
        result = {{"wrote": {MAX_OUTPUTS + 1}}}
        """,
    )

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request())

    assert MAX_OUTPUTS == 20
    assert (answer.status, answer.message, answer.outputs) == ("error", "too many outputs: at most 20", [])
    assert answer.result == {"wrote": 21}
    assert _entries(volume / "staging") == []
    assert _entries(volume / "jobs") == []


async def test_exactly_twenty_outputs_are_staged(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The limit itself is allowed; symlinks and folders do not count towards it."""
    _chat(volume)
    _runner(
        monkeypatch,
        f"""
        for n in range({MAX_OUTPUTS}):
            pathlib.Path(f"out/part-{{n:02}}.csv").write_text("x")
        os.symlink("part-00.csv", "out/zz-link")
        os.mkdir("out/zz-folder")
        """,
    )

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request())

    assert answer.status == "ok"
    assert [o.name for o in answer.outputs] == [f"part-{n:02}.csv" for n in range(20)]


async def test_outputs_larger_than_the_upload_limit_in_all_are_refused(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outputs together may not exceed ``upload_max_mb``, the most the api would accept as one file."""
    _chat(volume)
    _runner(
        monkeypatch,
        """
        pathlib.Path("out/a.bin").write_bytes(b"x" * 600_000)
        pathlib.Path("out/b.bin").write_bytes(b"x" * 600_000)
        """,
    )

    answer = await Supervisor(_settings(volume, WORKBENCH_UPLOAD_MAX_MB="1"), use_slots=False).run_job(_request())

    assert (answer.status, answer.message, answer.outputs) == ("error", "the outputs are larger than 1 MB in all", [])
    assert _entries(volume / "staging") == []


async def test_outputs_with_names_the_workspace_refuses_are_dropped(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An output becomes a workspace file under its name, so the name must pass ``check_filename``."""
    _chat(volume)
    _runner(
        monkeypatch,
        """
        pathlib.Path("out/" + "x" * 201).write_text("too long a name")
        pathlib.Path("out/Übersicht März.csv").write_text("fine")
        """,
    )

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request())

    assert [o.name for o in answer.outputs] == ["Übersicht März.csv"]


async def test_outputs_with_more_than_one_name_are_dropped_and_counted(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard link can give ``out/`` a name for a file the job does not own; an output must be the job's alone."""
    _chat(volume, {"q.csv": b"q"})
    _runner(
        monkeypatch,
        """
        pathlib.Path("out/a.csv").write_text("a")
        os.link("out/a.csv", "out/b.csv")
        os.link("q.csv", "out/q-again.csv")
        pathlib.Path("out/r.csv").write_text("r")
        """,
    )

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request(input_files=["q.csv"]))

    assert (answer.status, [o.name for o in answer.outputs]) == ("ok", ["r.csv"])
    assert answer.message == "dropped 3 outputs: not the job's own file"
    assert _entries(volume / "staging" / _only(volume / "staging")) == ["r.csv"]


async def test_only_requested_inputs_are_copied(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``b.csv`` lies in the chat folder too, but the job asked only for ``a.csv``."""
    _chat(volume, {"a.csv": b"a", "b.csv": b"b"})
    _runner(monkeypatch, 'result = {"seen": tree(".")}')

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request(input_files=["a.csv"]))

    assert answer.result == {"seen": ["a.csv", "out"]}


async def test_nested_inputs_keep_their_folders(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An input in a folder of the chat is copied to the same path below the job's working folder."""
    _chat(volume, {"reports/2025/summary.txt": b"text", "q.csv": b"q"})
    _runner(monkeypatch, 'result = {"seen": tree("."), "text": pathlib.Path("reports/2025/summary.txt").read_text()}')

    request = _request(input_files=["reports/2025/summary.txt", "q.csv", "q.csv"])
    answer = await Supervisor(_settings(volume), use_slots=False).run_job(request)

    assert answer.result == {
        "seen": ["out", "q.csv", "reports", "reports/2025", "reports/2025/summary.txt"],
        "text": "text",
    }


async def test_inputs_that_are_not_regular_files_are_not_copied(
    volume: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symlinks (to a file or through a folder), FIFOs, folders and missing names are skipped; the job runs."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.csv").write_text("another user's")
    chat = _chat(volume, {"q.csv": b"q", "sub/inner.csv": b"i"})
    (chat / "link.csv").symlink_to(outside / "secret.csv")
    (chat / "linked").symlink_to(outside)
    os.mkfifo(chat / "pipe.csv")
    _runner(monkeypatch, 'result = {"seen": tree(".")}')
    inputs = ["q.csv", "link.csv", "linked/secret.csv", "pipe.csv", "sub", "gone.csv"]

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request(input_files=inputs))

    assert answer.status == "ok"
    assert answer.result == {"seen": ["out", "q.csv"]}


async def test_a_chat_folder_that_is_a_symlink_is_not_followed(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A user root or chat folder replaced by a symlink to another user's files yields no inputs."""
    other = volume / "workspaces" / CALLER / "user-b" / CHAT
    other.mkdir(parents=True)
    (other / "q.csv").write_text("user b's")
    (volume / "workspaces" / CALLER / USER).mkdir(parents=True)
    (volume / "workspaces" / CALLER / USER / CHAT).symlink_to(other)
    _runner(monkeypatch, 'result = {"seen": tree(".")}')

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request(input_files=["q.csv"]))

    assert answer.result == {"seen": ["out"]}


async def test_inputs_under_a_folder_named_out_are_not_copied(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``out/`` is where the job writes, so a chat folder of that name is not mixed into it."""
    _chat(volume, {"out/old.csv": b"old", "q.csv": b"q"})
    _runner(monkeypatch, 'result = {"seen": tree(".")}')

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(
        _request(input_files=["out/old.csv", "q.csv"])
    )

    assert answer.result == {"seen": ["out", "q.csv"]}
    assert answer.outputs == []


async def test_the_job_folder_has_its_layout_and_modes(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The job folder is root's, 0710, its group the job's; only ``home/`` (with ``tmp/``) and ``out/`` are the job's.

    The working folder and the inputs are read-only to the job; ``HOME``, ``TMPDIR`` and the result are in ``home/``.
    """
    _chat(volume, {"q.csv": b"q", "reports/s.txt": b"s"})
    _runner(
        monkeypatch,
        """
        def mode(p):
            return oct(os.stat(p, follow_symlinks=False).st_mode & 0o7777)
        job = spec_path.parent
        result = {
            "cwd": os.getcwd() == str(job / "workspace" / spec["chat_id"]),
            "home": os.environ["HOME"] == str(job / "home"),
            "tmp": os.environ["TMPDIR"] == str(job / "home" / "tmp"),
            "result": result_path == job / "home" / "result.json",
            "modes": [mode(job), mode(job / "workspace"), mode("."), mode("q.csv"), mode("reports"),
                      mode("reports/s.txt"), mode("out"), mode(job / "home"), mode(job / "home" / "tmp"),
                      mode(spec_path)],
            "group": os.stat(job).st_gid == os.getgid(),
            "spec": spec,
            "top": sorted(os.listdir(job)),
        }
        """,
    )
    request = _request(input_files=["q.csv", "reports/s.txt"], args={"x": 1}, max_chars=100)

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(request)

    assert answer.result["cwd"] and answer.result["home"] and answer.result["tmp"] and answer.result["result"]
    assert answer.result["modes"] == [
        "0o710", "0o755", "0o755", "0o444", "0o755", "0o444", "0o700", "0o700", "0o700", "0o444",
    ]  # fmt: skip
    assert answer.result["group"]
    assert answer.result["spec"] == {
        "tool": "script",
        "args": {"x": 1},
        "chat_id": CHAT,
        "max_chars": 100,
        "file_mb": 50,
    }
    assert answer.result["top"] == ["home", "spec.json", "workspace"]


async def test_limits_are_bounded_by_the_executors_own_settings(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The api cannot give a job more than the executor's memory check assumed, or more processes and files."""
    seen: list[Limits] = []
    _fake_run(monkeypatch, [None, None], seen=seen)
    big = Limits(wall_s=9999, cpu_s=9999, memory_mb=99999, file_mb=9999, nproc=5000, nofile=9000)
    small = Limits(wall_s=5, cpu_s=4, memory_mb=256, file_mb=2, nproc=8, nofile=32)
    sup = Supervisor(_settings(volume, WORKBENCH_JOB_WALL_S="30", WORKBENCH_JOB_MEMORY_MB="512"), use_slots=False)

    await sup.run_job(_request(limits=big))
    await sup.run_job(_request(limits=small))

    assert seen == [Limits(wall_s=30, cpu_s=30, memory_mb=512, file_mb=50, nproc=64, nofile=256), small]


# --- the job's result ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "sys.exit(0)",
        'result_path.write_text("not json")\nsys.exit(0)',
        'result_path.write_text("[1, 2]")\nsys.exit(0)',
        'result_path.write_bytes(b"{\\"x\\": \\"\\xff\\"}")\nsys.exit(0)',
        'result_path.write_text("[" * 50000)\nsys.exit(0)',
        "os.mkfifo(result_path)\nsys.exit(0)",
        "os.mkdir(result_path)\nsys.exit(0)",
        'os.symlink(spec["args"]["decoy"], result_path)\nsys.exit(0)',
        'result_path.write_text(json.dumps({"x": "y" * 70000}))\nsys.exit(0)',
    ],
    ids=["missing", "not-json", "not-a-dict", "not-utf8", "too-deep", "fifo", "folder", "symlink", "too-big"],
)
async def test_a_result_that_is_not_a_small_json_object_in_a_regular_file_is_refused(
    body: str, volume: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``result.json`` is read without following a symlink or blocking on a FIFO, bounded by ``max_chars``."""
    decoy = tmp_path / "decoy.json"
    decoy.write_text('{"from": "outside"}')
    _chat(volume)
    _runner(monkeypatch, body)
    request = _request(args={"decoy": str(decoy)}, max_chars=1)  # read at most 4 + 64 KiB bytes

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(request)

    assert (answer.status, answer.message, answer.result) == ("error", "the job left no readable result", {})
    assert _entries(volume / "jobs") == []


async def test_a_result_up_to_its_bound_is_read(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``max_chars * 4 + 64 KiB`` bytes are allowed."""
    _chat(volume)
    _runner(monkeypatch, 'result = {"x": "y" * (4 + 64 * 1024 - 10)}')

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request(max_chars=1))

    assert answer.status == "ok"
    assert len(answer.result["x"]) == 4 + 64 * 1024 - 10


async def test_a_failing_process_is_an_error(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A job that exits with an error leaves no result to trust, even if it wrote one."""
    _chat(volume)
    _runner(monkeypatch, 'result_path.write_text("{}")\nsys.exit(3)')

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request())

    assert (answer.status, answer.message, answer.result) == ("error", "the job failed", {})


async def test_a_job_past_its_wall_time_is_a_timeout(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The wall clock kills the job; its outputs are not staged."""
    _chat(volume)
    _runner(monkeypatch, 'pathlib.Path("out/r.csv").write_text("x")\nimport time\ntime.sleep(30)')
    request = _request(limits=Limits(wall_s=1, cpu_s=60, memory_mb=1024, file_mb=50))

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(request)

    assert (answer.status, answer.message, answer.outputs) == ("timeout", "the job ran past its wall time of 1 s", [])
    assert _entries(volume / "staging") == []
    assert _entries(volume / "jobs") == []


async def test_a_job_the_kernel_stopped_is_killed(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``SIGKILL`` (a resource limit) is ``killed`` with a short reason."""
    _chat(volume)
    _runner(monkeypatch, "import signal\nos.kill(os.getpid(), signal.SIGKILL)")

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request())

    assert (answer.status, answer.message) == ("killed", "the job was stopped on a resource limit")


# --- the job folder and the staging folder ---------------------------------------------------


async def test_removing_the_job_folder_follows_no_symlink(
    volume: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symlinks the job planted to folders outside are removed as links; what they point at stays."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    _chat(volume)
    _runner(
        monkeypatch,
        f"""
        os.symlink({str(outside)!r}, "out/escape")
        os.symlink({str(outside)!r}, spec_path.with_name("escape"))
        os.symlink({str(outside)!r}, os.environ["TMPDIR"] + "/escape")
        """,
    )

    await Supervisor(_settings(volume), use_slots=False).run_job(_request())

    assert (outside / "keep.txt").read_text() == "keep"
    assert _entries(volume / "jobs") == []


async def test_a_job_folder_that_cannot_be_removed_closes_and_retires_its_slot(
    volume: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A folder left behind holds a user's inputs: it goes back to root, and its slot is never reused."""
    _chat(volume)
    _runner(monkeypatch, "pass")
    monkeypatch.setattr(supervisor.shutil, "rmtree", lambda path, **kwargs: kwargs["onexc"](os.rmdir, path, OSError()))
    sup = Supervisor(_settings(volume, WORKBENCH_EXECUTOR_SLOTS="1"), use_slots=False)
    request = _request()

    with caplog.at_level(logging.WARNING, logger="workbench.executor"):
        answer = await sup.run_job(request)

    left = volume / "jobs" / request.job_id
    assert answer.status == "ok"
    assert (_ids(left), _mode(left)) == (ME, 0o700)
    assert [r.getMessage() for r in caplog.records] == ["a job folder could not be removed", "slot 0 retired"]
    assert (await sup.run_job(_request())).message == "no usable slot"


async def test_a_job_id_already_in_use_is_refused_and_its_folder_kept(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second job with an id gets an error; the folder of the first is neither reused nor removed."""
    _chat(volume)
    _runner(monkeypatch, "pass")
    request = _request()
    (volume / "jobs" / request.job_id).mkdir()
    (volume / "jobs" / request.job_id / "keep").write_text("keep")

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(request)

    assert (answer.status, answer.message) == ("error", "the job could not be set up")
    assert (volume / "jobs" / request.job_id / "keep").read_text() == "keep"


@pytest.mark.parametrize("job_id", ["..", "../staging", "x" * 32, ""])
async def test_a_request_built_without_validation_still_names_no_other_folder(
    job_id: str, volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job id is checked again where it becomes a folder name, whatever built the request."""
    _runner(monkeypatch, "pass")
    (volume / "staging" / "keep").write_text("keep")
    request = _request().model_copy(update={"job_id": job_id})

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(request)

    assert (answer.status, answer.message) == ("error", "the job could not be set up")
    assert _entries(volume / "staging") == ["keep"]
    assert _entries(volume / "jobs") == []


async def test_a_jobs_folder_that_is_a_symlink_is_not_used(
    volume: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is created through a ``jobs`` that points elsewhere."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (volume / "jobs").rmdir()
    (volume / "jobs").symlink_to(outside)
    _runner(monkeypatch, "pass")

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request())

    assert (answer.status, answer.message) == ("error", "the job could not be set up")
    assert _entries(outside) == []


async def test_a_jobs_folder_others_may_write_is_not_used(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A loosened ``jobs`` folder means the layout is not what the supervisor relies on."""
    (volume / "jobs").chmod(0o777)
    _runner(monkeypatch, "pass")

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request())

    assert (answer.status, answer.message) == ("error", "the job could not be set up")
    assert _entries(volume / "jobs") == []


@pytest.mark.parametrize("trap", ["symlink", "existing"])
async def test_staging_follows_no_symlink_and_overwrites_nothing(
    trap: str, volume: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The api owns ``staging``: a symlink or a folder already there for the job is refused, not written through."""
    outside = tmp_path / "outside"
    outside.mkdir()
    _chat(volume)
    _runner(monkeypatch, 'pathlib.Path("out/r.csv").write_text("x")')
    request = _request()
    if trap == "symlink":
        (volume / "staging" / request.job_id).symlink_to(outside)
    else:
        (volume / "staging" / request.job_id).mkdir()
        (volume / "staging" / request.job_id / "r.csv").write_text("the api's")

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(request)

    assert (answer.status, answer.message, answer.outputs) == ("error", "the outputs could not be staged", [])
    assert _entries(outside) == []
    if trap == "existing":
        assert (volume / "staging" / request.job_id / "r.csv").read_text() == "the api's"


async def test_a_staging_folder_that_is_a_symlink_is_not_used(
    volume: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``staging`` itself is opened without following it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (volume / "staging").rmdir()
    (volume / "staging").symlink_to(outside)
    _chat(volume)
    _runner(monkeypatch, 'pathlib.Path("out/r.csv").write_text("x")')

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request())

    assert (answer.status, answer.message) == ("error", "the outputs could not be staged")
    assert _entries(outside) == []


# --- slots: busy, reuse and retirement -------------------------------------------------------


def _fake_run(
    monkeypatch: pytest.MonkeyPatch,
    behaviours: list[BaseException | None],
    *,
    seen: list[Limits] | None = None,
    pools: list[Any] | None = None,
) -> None:
    """Replace the sandbox run: each call writes a result, then raises the next behaviour or succeeds."""

    async def fake(
        argv: list[str],
        cwd: Path,
        slot: Slot | None,
        limits: Limits,
        *,
        tmp_dir: Path | None = None,
        cleanup_pool: Any = None,
        **_more: Any,
    ) -> tuple[str, int]:
        """Leave a result as a job would, then end as told."""
        Path(argv[-1]).write_text('{"leaked": true}')
        if seen is not None:
            seen.append(limits)
        if pools is not None:
            pools.append(cleanup_pool)
        behaviour = behaviours.pop(0)
        if behaviour is not None:
            raise behaviour
        return "ok", 0

    monkeypatch.setattr(supervisor, "run_in_slot", fake)


async def test_busy_when_all_slots_run(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With one slot, a second job arriving while the first runs is answered ``busy`` at once."""
    _chat(volume)
    _runner(monkeypatch, "import time\ntime.sleep(1)")
    sup = Supervisor(_settings(volume, WORKBENCH_EXECUTOR_SLOTS="1"), use_slots=False)

    first, second = await asyncio.gather(sup.run_job(_request()), sup.run_job(_request()))

    assert sorted([first.status, second.status]) == ["busy", "ok"]
    busy = first if first.status == "busy" else second
    assert (busy.message, busy.result, busy.outputs) == ("every slot is busy", {}, [])
    assert (await sup.run_job(_request())).status == "ok"


async def test_a_slot_whose_cleanup_failed_is_retired(
    volume: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A slot that may still hold processes is never reused, its result never read, its retirement logged by index."""
    _chat(volume)
    _fake_run(monkeypatch, [RuntimeError("processes of slot 0 survived the cleanup")])
    sup = Supervisor(_settings(volume, WORKBENCH_EXECUTOR_SLOTS="1"), use_slots=False)
    request = _request()

    with caplog.at_level(logging.WARNING, logger="workbench.executor"):
        answer = await sup.run_job(request)

    assert (answer.status, answer.message, answer.result) == ("error", "the job's processes could not be stopped", {})
    assert [r.getMessage() for r in caplog.records] == ["slot 0 retired"]
    assert _entries(volume / "jobs") == []
    again = await sup.run_job(_request())
    assert (again.status, again.message) == ("busy", "no usable slot")


async def test_a_cancelled_job_retires_its_slot(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancellation can interrupt the cleanup, so the slot is not trusted again."""
    _chat(volume)
    _fake_run(monkeypatch, [asyncio.CancelledError()])
    sup = Supervisor(_settings(volume, WORKBENCH_EXECUTOR_SLOTS="1"), use_slots=False)

    with pytest.raises(asyncio.CancelledError):
        await sup.run_job(_request())

    assert (await sup.run_job(_request())).message == "no usable slot"


async def test_the_other_slots_keep_working_after_one_is_retired(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With two slots and one retired, jobs run one at a time and a second at once is ``busy``, not unusable."""
    _chat(volume)
    _fake_run(monkeypatch, [RuntimeError("x"), None, None, None])
    sup = Supervisor(_settings(volume), use_slots=False)

    await sup.run_job(_request())
    first = await sup.run_job(_request())
    both = await asyncio.gather(sup.run_job(_request()), sup.run_job(_request()))

    assert first.status == "ok"
    assert sorted((a.status, a.message) for a in both) == [("busy", "every slot is busy"), ("ok", "")]


async def test_a_slot_is_reused_after_a_clean_run(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One slot serves job after job while each cleanup succeeds."""
    _chat(volume)
    _fake_run(monkeypatch, [None, None, None])
    sup = Supervisor(_settings(volume, WORKBENCH_EXECUTOR_SLOTS="1"), use_slots=False)

    statuses = [(await sup.run_job(_request())).status for _ in range(3)]

    assert statuses == ["ok", "ok", "ok"]


async def test_cleanups_run_on_a_pool_of_their_own_with_a_thread_per_slot(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopped cleanups can hold a thread for 30 s; they never take the loop's default pool."""
    pools: list[Any] = []
    _chat(volume)
    _fake_run(monkeypatch, [None, None], pools=pools)
    sup = Supervisor(_settings(volume, WORKBENCH_EXECUTOR_SLOTS="3"), use_slots=False)

    await sup.run_job(_request())
    await sup.run_job(_request())

    assert isinstance(pools[0], ThreadPoolExecutor)
    assert pools[0] is pools[1]
    assert pools[0]._max_workers == 3


def test_slots_come_from_the_pool_and_never_run_as_root(tmp_path: Path) -> None:
    """Slot ``n`` runs as ``base + n``, uid and gid; a pool outside 1000-65533 or holding the api's uid is refused."""
    assert make_slots(_settings(tmp_path)) == (Slot(0, 20001, 20001), Slot(1, 20002, 20002))
    for env in (
        {"WORKBENCH_SLOT_UID_BASE": "1"},
        {"WORKBENCH_SLOT_UID_BASE": "65530", "WORKBENCH_EXECUTOR_SLOTS": "8"},
    ):
        with pytest.raises(ValueError, match="between 1000 and 65533"):
            make_slots(_settings(tmp_path, **env))
    with pytest.raises(ValueError, match="api's uid"):
        make_slots(_settings(tmp_path, WORKBENCH_SLOT_UID_BASE="9995", WORKBENCH_EXECUTOR_SLOTS="8"))
    with pytest.raises(ValueError, match="at most 16 slots"):
        make_slots(dataclasses.replace(_settings(tmp_path), executor_slots=17))


def test_a_supervisor_with_slots_refuses_a_bad_pool_at_construction(tmp_path: Path) -> None:
    """The pool is checked before any job could run in it."""
    with pytest.raises(ValueError):
        Supervisor(_settings(tmp_path, WORKBENCH_SLOT_UID_BASE="1"))


# --- start-up checks -------------------------------------------------------------------------


def _passwd(tmp_path: Path, uids: range) -> Path:
    """An ``/etc/passwd`` holding root and nologin users with ``uids``."""
    lines = ["root:x:0:0:root:/root:/bin/bash"]
    lines += [f"slot{uid}:x:{uid}:{uid}::/nonexistent:/usr/sbin/nologin" for uid in uids]
    path = tmp_path / "passwd"
    path.write_text("\n".join(lines) + "\n")
    return path


def _memory_max(tmp_path: Path, value: str) -> Path:
    """A cgroup ``memory.max`` file holding ``value``."""
    path = tmp_path / "memory.max"
    path.write_text(value + "\n")
    return path


MOUNTINFO = (
    "105 92 0:51 / /dev rw,nosuid - tmpfs tmpfs rw,size=65536k,mode=755\n"
    "109 105 0:54 / /dev/mqueue rw,nosuid,nodev,noexec,relatime - mqueue mqueue rw\n"
    "112 105 0:61 / /dev/shm rw,nosuid,nodev,noexec,relatime - tmpfs tmpfs rw,size=1024k,mode=755\n"
)
# A folder of root's that no one else may write, as /dev/shm must be: the file system's root is one
# on every host the tests run on.
ROOT_ONLY = Path("/")


def _mountinfo(tmp_path: Path, content: str = MOUNTINFO) -> Path:
    """A ``/proc/self/mountinfo`` holding ``content``."""
    path = tmp_path / "mountinfo"
    path.write_text(content)
    return path


# The supervisor's /proc/self/status under Docker's default seccomp profile, in part.
STATUS = "Name:\tpython\nUmask:\t0077\nState:\tR (running)\nNoNewPrivs:\t1\nSeccomp:\t2\nSeccomp_filters:\t1\n"


def _no_problem() -> str | None:
    """A seccomp probe that finds nothing wrong, for the checks that are not about it."""
    return None


def _status(tmp_path: Path, content: str = STATUS) -> Path:
    """A ``/proc/self/status`` holding ``content``."""
    path = tmp_path / "status"
    path.write_text(content)
    return path


def _sysctls(
    tmp_path: Path, hardlinks: str | None = "1", symlinks: str | None = "1", queues_max: str | None = "0"
) -> Path:
    """A ``/proc/sys/fs`` with the link protections and ``mqueue/queues_max`` as given; ``None`` leaves one out."""
    folder = tmp_path / "fs"
    (folder / "mqueue").mkdir(parents=True, exist_ok=True)
    settings = (
        ("protected_hardlinks", hardlinks),
        ("protected_symlinks", symlinks),
        ("mqueue/queues_max", queues_max),
    )
    for name, value in settings:
        if value is not None:
            (folder / name).write_text(value + "\n")
    return folder


@pytest.mark.parametrize("memory", [None, "max", str(4 * 1536 * MB), str(8 * 1536 * MB)])
def test_a_well_set_up_host_passes_the_start_up_checks(memory: str | None, tmp_path: Path) -> None:
    """Root, every slot user present, and a memory limit of at least slots times job memory (or none)."""
    settings = _settings(tmp_path, WORKBENCH_EXECUTOR_SLOTS="4")
    memory_max = tmp_path / "absent" if memory is None else _memory_max(tmp_path, memory)

    problem = startup_problem(
        settings,
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=memory_max,
        sysctls=_sysctls(tmp_path),
        mountinfo=_mountinfo(tmp_path),
        shm=ROOT_ONLY,
        tmp=ROOT_ONLY,
        status=_status(tmp_path),
        sockets=_no_problem,
        euid=0,
    )

    assert problem is None


def test_too_little_container_memory_refuses_to_start(tmp_path: Path) -> None:
    """Four slots of 1536 MB need at least 6 GiB in the container's cgroup."""
    settings = _settings(tmp_path, WORKBENCH_EXECUTOR_SLOTS="4")

    problem = startup_problem(
        settings,
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=_memory_max(tmp_path, str(4 * 1536 * MB - 1)),
        sysctls=_sysctls(tmp_path),
        mountinfo=_mountinfo(tmp_path),
        shm=ROOT_ONLY,
        tmp=ROOT_ONLY,
        status=_status(tmp_path),
        sockets=_no_problem,
        euid=0,
    )

    assert problem == "the container's memory limit is below 4 slots of 1536 MB"


def test_missing_slot_users_refuse_to_start(tmp_path: Path) -> None:
    """Every slot uid must be a user the image created."""
    settings = _settings(tmp_path, WORKBENCH_EXECUTOR_SLOTS="4")

    problem = startup_problem(
        settings,
        passwd=_passwd(tmp_path, range(20001, 20004)),
        memory_max=tmp_path / "absent",
        sysctls=_sysctls(tmp_path),
        mountinfo=_mountinfo(tmp_path),
        shm=ROOT_ONLY,
        tmp=ROOT_ONLY,
        status=_status(tmp_path),
        sockets=_no_problem,
        euid=0,
    )

    assert problem == "1 of the 4 slot users is missing from the image"


def test_a_pool_beyond_its_sixteen_uids_refuses_to_start(tmp_path: Path) -> None:
    """``slot_uid_base + executor_slots - 1`` may not pass ``slot_uid_base + 15``."""
    settings = dataclasses.replace(_settings(tmp_path), executor_slots=17)

    problem = startup_problem(
        settings,
        passwd=_passwd(tmp_path, range(20001, 20018)),
        memory_max=tmp_path / "absent",
        sysctls=_sysctls(tmp_path),
        mountinfo=_mountinfo(tmp_path),
        shm=ROOT_ONLY,
        tmp=ROOT_ONLY,
        status=_status(tmp_path),
        sockets=_no_problem,
        euid=0,
    )

    assert problem is not None
    assert "at most 16 slots" in problem


def test_a_supervisor_that_is_not_root_refuses_to_start(tmp_path: Path) -> None:
    """Without root it cannot switch to the slot uids."""
    problem = startup_problem(
        _settings(tmp_path),
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=tmp_path / "x",
        sysctls=_sysctls(tmp_path),
        mountinfo=_mountinfo(tmp_path),
        shm=ROOT_ONLY,
        tmp=ROOT_ONLY,
        status=_status(tmp_path),
        sockets=_no_problem,
        euid=1000,
    )

    assert problem == "the supervisor must run as root"


@pytest.mark.parametrize(
    ("hardlinks", "symlinks", "problem"),
    [
        ("0", "1", "fs.protected_hardlinks must be 1, so a job cannot link files it does not own"),
        (None, "1", "fs.protected_hardlinks must be 1, so a job cannot link files it does not own"),
        ("1", "0", "fs.protected_symlinks must be 1, so no one follows a job's links in shared folders"),
        ("1", "2", "fs.protected_symlinks must be 1, so no one follows a job's links in shared folders"),
    ],
)
def test_a_host_without_link_protection_refuses_to_start(
    hardlinks: str | None, symlinks: str | None, problem: str, tmp_path: Path
) -> None:
    """Root stages a job's outputs: a hard link to a file the job does not own must be impossible to make."""
    found = startup_problem(
        _settings(tmp_path),
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=tmp_path / "absent",
        sysctls=_sysctls(tmp_path, hardlinks, symlinks),
        mountinfo=_mountinfo(tmp_path),
        shm=ROOT_ONLY,
        tmp=ROOT_ONLY,
        status=_status(tmp_path),
        sockets=_no_problem,
        euid=0,
    )

    assert found == problem


@pytest.mark.parametrize(
    "content",
    [
        MOUNTINFO.replace("/dev/mqueue", "/dev/other"),
        MOUNTINFO.replace("- mqueue mqueue", "- tmpfs tmpfs"),
        "",
    ],
    ids=["not-mounted", "not-mqueue", "empty"],
)
def test_a_dev_mqueue_that_is_not_a_mounted_mqueue_refuses_to_start(content: str, tmp_path: Path) -> None:
    """Without the mount, POSIX message queues a job leaves can be neither listed nor removed."""
    problem = startup_problem(
        _settings(tmp_path),
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=tmp_path / "absent",
        sysctls=_sysctls(tmp_path),
        mountinfo=_mountinfo(tmp_path, content),
        shm=ROOT_ONLY,
        tmp=ROOT_ONLY,
        status=_status(tmp_path),
        sockets=_no_problem,
        euid=0,
    )

    assert problem == "/dev/mqueue must be a mounted mqueue file system, so the slots' queues can be cleared"


def test_unreadable_mount_information_refuses_to_start(tmp_path: Path) -> None:
    """What cannot be checked is not taken on trust."""
    problem = startup_problem(
        _settings(tmp_path),
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=tmp_path / "absent",
        sysctls=_sysctls(tmp_path),
        mountinfo=tmp_path / "absent-mountinfo",
        shm=ROOT_ONLY,
        tmp=ROOT_ONLY,
        status=_status(tmp_path),
        sockets=_no_problem,
        euid=0,
    )

    assert problem == "/dev/mqueue must be a mounted mqueue file system, so the slots' queues can be cleared"


SHM_REFUSED = "/dev/shm must be a folder of root's that no one else may write, so no job can leave anything there"


TMP_REFUSED = "/tmp must be a folder of root's that no one else may write, so no job can leave anything there"
TRAPS = ["world-writable", "group-writable", "not-root-owned", "symlink", "file", "missing"]


def _shared_folder(tmp_path: Path, trap: str, name: str) -> Path:
    """A ``/dev/shm`` or ``/tmp`` set up as ``trap`` names; as root, made root's unless the trap is its owner."""
    folder = tmp_path / name
    if trap == "missing":
        return folder
    if trap == "symlink":
        folder.symlink_to(ROOT_ONLY)
        return folder
    if trap == "file":
        folder.write_text("")
        folder.chmod(0o644)
        return folder
    folder.mkdir()
    folder.chmod({"world-writable": 0o1777, "group-writable": 0o775, "not-root-owned": 0o755}[trap])
    if trap == "not-root-owned" and os.geteuid() == 0:
        os.lchown(folder, 1000, 1000)
    return folder


@pytest.mark.parametrize("trap", TRAPS)
def test_a_dev_shm_slots_could_write_refuses_to_start(trap: str, tmp_path: Path) -> None:
    """Docker's default ``1777`` among them: a listing of a folder other slots write can be made to skip entries."""
    problem = startup_problem(
        _settings(tmp_path),
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=tmp_path / "absent",
        sysctls=_sysctls(tmp_path),
        mountinfo=_mountinfo(tmp_path),
        shm=_shared_folder(tmp_path, trap, "shm"),
        tmp=ROOT_ONLY,
        status=_status(tmp_path),
        sockets=_no_problem,
        euid=0,
    )

    assert problem == SHM_REFUSED


@pytest.mark.parametrize("trap", TRAPS)
def test_a_tmp_slots_could_write_refuses_to_start(trap: str, tmp_path: Path) -> None:
    """R30: jobs have a ``TMPDIR`` of their own, so ``/tmp`` is the last place slots could share; it must be root's."""
    problem = startup_problem(
        _settings(tmp_path),
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=tmp_path / "absent",
        sysctls=_sysctls(tmp_path),
        mountinfo=_mountinfo(tmp_path),
        shm=ROOT_ONLY,
        tmp=_shared_folder(tmp_path, trap, "tmp"),
        status=_status(tmp_path),
        sockets=_no_problem,
        euid=0,
    )

    assert problem == TMP_REFUSED


SECCOMP_REFUSED = (
    "the supervisor must run under a seccomp filter such as Docker's default profile, "
    "so no job can use the kernel's keyrings"
)


@pytest.mark.parametrize(
    "content",
    [
        STATUS.replace("Seccomp:\t2", "Seccomp:\t0"),
        STATUS.replace("Seccomp:\t2", "Seccomp:\t1"),
        STATUS.replace("Seccomp:\t2", "Seccomp:\t22"),
        STATUS.replace("Seccomp:\t2\n", ""),
        "Seccomp_filters:\t2\n",
        "",
        None,
    ],
    ids=["disabled", "strict", "other-value", "no-line", "only-a-longer-name", "empty", "missing"],
)
def test_a_supervisor_without_a_seccomp_filter_refuses_to_start(content: str | None, tmp_path: Path) -> None:
    """R31: without the default profile ``add_key`` and ``keyctl`` are open, and kernel keyrings outlive a job."""
    problem = startup_problem(
        _settings(tmp_path),
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=tmp_path / "absent",
        sysctls=_sysctls(tmp_path),
        mountinfo=_mountinfo(tmp_path),
        shm=ROOT_ONLY,
        tmp=ROOT_ONLY,
        status=tmp_path / "absent-status" if content is None else _status(tmp_path, content),
        sockets=_no_problem,
        euid=0,
    )

    assert problem == SECCOMP_REFUSED


@pytest.mark.parametrize(
    "content",
    [STATUS.replace("NoNewPrivs:\t1", "NoNewPrivs:\t0"), STATUS.replace("NoNewPrivs:\t1\n", "")],
    ids=["off", "no-line"],
)
def test_a_supervisor_without_no_new_privileges_refuses_to_start(content: str, tmp_path: Path) -> None:
    """R1 M-1: ``no-new-privileges`` is what keeps a setuid program inert for a job; the image has none either."""
    problem = startup_problem(
        _settings(tmp_path),
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=tmp_path / "absent",
        sysctls=_sysctls(tmp_path),
        mountinfo=_mountinfo(tmp_path),
        shm=ROOT_ONLY,
        tmp=ROOT_ONLY,
        status=_status(tmp_path, content),
        sockets=_no_problem,
        euid=0,
    )

    assert (
        problem == "the supervisor must run with no-new-privileges, so no job can gain rights through a setuid program"
    )


@pytest.mark.parametrize(
    ("pids", "passes"), [("max", True), ("320", True), ("4096", True), ("319", False), ("64", False)]
)
def test_the_containers_process_limit_must_hold_every_slot(pids: str, passes: bool, tmp_path: Path) -> None:
    """R1 M-2, as for memory: four slots of 64 processes and 64 for the supervisor and its helpers need 320."""
    limit = tmp_path / "pids.max"
    limit.write_text(pids + "\n")
    problem = startup_problem(
        _settings(tmp_path, WORKBENCH_EXECUTOR_SLOTS="4"),
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=tmp_path / "absent",
        sysctls=_sysctls(tmp_path),
        mountinfo=_mountinfo(tmp_path),
        shm=ROOT_ONLY,
        tmp=ROOT_ONLY,
        status=_status(tmp_path),
        pids_max=limit,
        sockets=_no_problem,
        euid=0,
    )

    assert problem == (None if passes else "the container's process limit is below 4 slots of 64 and 64 more")


def test_a_host_where_jobs_could_open_sockets_refuses_to_start(tmp_path: Path) -> None:
    """The socket filter is tried at start: what it finds wrong is the reason (R76)."""
    problem = startup_problem(
        _settings(tmp_path),
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=tmp_path / "absent",
        sysctls=_sysctls(tmp_path),
        mountinfo=_mountinfo(tmp_path),
        shm=ROOT_ONLY,
        tmp=ROOT_ONLY,
        status=_status(tmp_path),
        sockets=lambda: "the seccomp filter does not keep a process from opening an Internet socket",
        euid=0,
    )

    assert problem == "the seccomp filter does not keep a process from opening an Internet socket"


@pytest.mark.parametrize(
    "content",
    [
        STATUS,
        "Seccomp:\t2\nNoNewPrivs:\t1\n",
        "NoNewPrivs: 1\nSeccomp: 2\nSeccomp_filters:\t3\n",
        "NoNewPrivs:\t1\nSeccomp:\t2",
    ],
    ids=["docker-default", "only-the-line", "spaces-and-more-filters", "no-final-newline"],
)
def test_a_seccomp_filter_passes_the_start_up_check(content: str, tmp_path: Path) -> None:
    """Mode 2 is a filter; how many are stacked does not matter."""
    problem = startup_problem(
        _settings(tmp_path),
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=tmp_path / "absent",
        sysctls=_sysctls(tmp_path),
        mountinfo=_mountinfo(tmp_path),
        shm=ROOT_ONLY,
        tmp=ROOT_ONLY,
        status=_status(tmp_path, content),
        sockets=_no_problem,
        euid=0,
    )

    assert problem is None


@pytest.mark.parametrize(
    ("mode", "uid", "root_only"),
    [
        (stat.S_IFDIR | 0o755, 0, True),
        (stat.S_IFDIR | 0o700, 0, True),
        (stat.S_IFDIR | 0o1755, 0, True),
        (stat.S_IFDIR | 0o1777, 0, False),
        (stat.S_IFDIR | 0o775, 0, False),
        (stat.S_IFDIR | 0o757, 0, False),
        (stat.S_IFDIR | 0o755, 1000, False),
        (stat.S_IFLNK | 0o777, 0, False),
        (stat.S_IFREG | 0o644, 0, False),
    ],
    ids=["0755", "0700", "1755", "1777", "0775", "0757", "not-root-owned", "symlink", "file"],
)
def test_a_root_only_folder_is_roots_a_folder_and_writable_by_no_group_or_others(
    mode: int, uid: int, root_only: bool
) -> None:
    """Judged on the ``lstat`` alone, so the mode is checked even where the tests cannot make a folder of root's."""
    st = os.stat_result((mode, 1, 1, 2, uid, 0, 40, 0, 0, 0))

    assert supervisor._root_only(st) is root_only


@pytest.mark.skipif(os.geteuid() != 0, reason="making a folder of root's needs root")
def test_as_root_a_folder_of_roots_that_no_one_else_may_write_passes(tmp_path: Path) -> None:
    """The production ``/dev/shm`` and ``/tmp``: tmpfs folders of root's, modes ``0755`` and ``0700``."""
    shm, tmp = tmp_path / "shm", tmp_path / "tmp"
    shm.mkdir()
    shm.chmod(0o755)
    tmp.mkdir()
    tmp.chmod(0o700)

    problem = startup_problem(
        _settings(tmp_path),
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=tmp_path / "absent",
        sysctls=_sysctls(tmp_path),
        mountinfo=_mountinfo(tmp_path),
        shm=shm,
        tmp=tmp,
        status=_status(tmp_path),
        sockets=_no_problem,
        euid=0,
    )

    assert problem is None


@pytest.mark.parametrize("queues_max", ["256", "1", "", "zero", None])
def test_a_host_where_slots_could_make_posix_queues_refuses_to_start(queues_max: str | None, tmp_path: Path) -> None:
    """``fs.mqueue.queues_max`` must be 0 (Docker's default is 256); unreadable counts as not 0."""
    problem = startup_problem(
        _settings(tmp_path),
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=tmp_path / "absent",
        sysctls=_sysctls(tmp_path, queues_max=queues_max),
        mountinfo=_mountinfo(tmp_path),
        shm=ROOT_ONLY,
        tmp=ROOT_ONLY,
        status=_status(tmp_path),
        sockets=_no_problem,
        euid=0,
    )

    assert problem == "fs.mqueue.queues_max must be 0, so no job can make a POSIX message queue"


@pytest.mark.skipif(IPC_SHAPE is None, reason="needs a Linux /dev/shm set up as in production or as Docker's default")
def test_this_hosts_dev_shm_and_queue_limit_pass_only_in_the_production_shape(tmp_path: Path) -> None:
    """The real ``/dev/shm`` and ``fs.mqueue.queues_max``: Docker's default (``1777``, 256 queues) is refused."""
    queues_max = Path("/proc/sys/fs/mqueue/queues_max").read_text(encoding="ascii").strip()

    problem = startup_problem(
        _settings(tmp_path),
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=tmp_path / "absent",
        sysctls=_sysctls(tmp_path, queues_max=queues_max),
        mountinfo=_mountinfo(tmp_path),
        shm=Path("/dev/shm"),
        tmp=ROOT_ONLY,
        status=_status(tmp_path),
        sockets=_no_problem,
        euid=0,
    )

    assert problem == (None if IPC_SHAPE == "production" else SHM_REFUSED)


@pytest.mark.skipif(
    IPC_SHAPE != "production",
    reason="needs the production shape (make test-executor-root): /tmp a tmpfs of root's, mode 0700, "
    "and Docker's default seccomp profile",
)
def test_in_the_production_shape_this_hosts_tmp_and_seccomp_filter_pass(tmp_path: Path) -> None:
    """The real ``/tmp`` and ``/proc/self/status`` of the container the root tests run in."""
    problem = startup_problem(
        _settings(tmp_path),
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=tmp_path / "absent",
        sysctls=_sysctls(tmp_path),
        mountinfo=_mountinfo(tmp_path),
        shm=ROOT_ONLY,
        tmp=Path("/tmp"),
        status=Path("/proc/self/status"),
        euid=0,
    )

    assert problem is None


def test_main_exits_with_one_short_line_when_no_slot_is_left(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """With every slot retired the process ends, non-zero, so its restart brings fresh IPC and slots."""

    async def exhausted(self: Supervisor) -> None:
        """End as serving does once the last slot is retired."""
        raise NoUsableSlot("every slot is retired (2 of 2); exiting so the container restarts")

    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor, "startup_problem", lambda settings: None)
    monkeypatch.setattr(supervisor, "prepare_host", lambda settings: None)
    monkeypatch.setattr(supervisor, "make_slots", lambda settings: ())
    monkeypatch.setattr(Supervisor, "serve", exhausted)

    assert supervisor.main() == 1

    assert capsys.readouterr().err == (
        "workbench executor: every slot is retired (2 of 2); exiting so the container restarts\n"
    )


def test_main_refuses_to_start_with_one_short_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``python -m workbench.executor.supervisor`` exits 1 with the reason on stderr."""
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor, "startup_problem", lambda settings: "the supervisor must run as root")

    assert supervisor.main() == 1

    assert capsys.readouterr().err == "workbench executor: refusing to start: the supervisor must run as root\n"


def test_main_reports_a_socket_it_may_not_bind_in_one_short_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal found as ``serve()`` starts, such as an unsafe socket folder, ends the process the same way."""

    async def refuse(self: Supervisor) -> None:
        """Refuse as an unsafe socket folder does."""
        raise SocketRefused("the socket's folder must be root's alone")

    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor, "startup_problem", lambda settings: None)
    monkeypatch.setattr(supervisor, "prepare_host", lambda settings: None)
    monkeypatch.setattr(supervisor, "make_slots", lambda settings: ())
    monkeypatch.setattr(Supervisor, "serve", refuse)

    assert supervisor.main() == 1

    assert (
        capsys.readouterr().err == "workbench executor: refusing to start: the socket's folder must be root's alone\n"
    )


def test_main_refuses_a_bad_setting(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A malformed environment stops it the same way."""
    monkeypatch.setenv("WORKBENCH_EXECUTOR_SLOTS", "99")

    assert supervisor.main() == 1

    err = capsys.readouterr().err
    assert err.startswith("workbench executor: WORKBENCH_EXECUTOR_SLOTS")
    assert err.count("\n") == 1


#: A path an unexpected error might name: a synthetic file of a synthetic user.
SECRET_PATH = "/data/workspaces/user-a/chat-1/quarterly-figures.xlsx"


def _unexpected() -> PermissionError:
    """An error whose message names :data:`SECRET_PATH`, as a permission problem on the volume would."""
    return PermissionError(13, "Permission denied", SECRET_PATH)


def _holds_a_secret(text: str) -> bool:
    """Whether a log holds anything of the error's message."""
    return any(part in text for part in (SECRET_PATH, "quarterly-figures", "Permission denied", "Traceback"))


def test_main_reports_an_unexpected_error_by_its_type_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Whatever else ends serving is one line with the error's type: no traceback, no message, no path."""

    async def fail(self: Supervisor) -> None:
        """End serving as an unforeseen error would."""
        raise _unexpected()

    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor, "startup_problem", lambda settings: None)
    monkeypatch.setattr(supervisor, "prepare_host", lambda settings: None)
    monkeypatch.setattr(supervisor, "make_slots", lambda settings: ())
    monkeypatch.setattr(Supervisor, "serve", fail)

    assert supervisor.main() == 1

    err = capsys.readouterr().err
    assert err == "workbench executor: stopped by an unexpected error (PermissionError)\n"
    assert not _holds_a_secret(err)


async def test_an_unexpected_error_while_answering_a_connection_is_logged_by_its_type_alone(
    served: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An error outside the job's own handling closes the connection and is logged by its type alone.

    asyncio would log it with its traceback. (An ``OSError`` there is the api going away, and is not logged at all.)
    """

    def fail(answer: JobResult) -> bytes:
        raise ValueError(f"the answer for {SECRET_PATH} could not be encoded")

    _runner(monkeypatch, 'result = {"tool": spec["tool"]}')
    monkeypatch.setattr(supervisor, "_line", fail)
    with caplog.at_level(logging.DEBUG):
        async with _serving(Supervisor(_settings(served), use_slots=False)):
            assert await _ask(_sock(served), encode(_request())) == b""
            for _ in range(100):  # the handler logs once the connection is closed
                if "ValueError" in caplog.text:
                    break
                await asyncio.sleep(0.02)

    assert "a connection failed in the supervisor: ValueError" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert not _holds_a_secret(caplog.text)


async def test_a_job_that_fails_in_the_supervisor_is_logged_by_its_type_alone(
    served: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The job's answer says the executor failed; the log names the type, never the message."""

    async def fail(self: Supervisor, req: JobRequest) -> JobResult:
        raise _unexpected()

    monkeypatch.setattr(Supervisor, "run_job", fail)
    with caplog.at_level(logging.DEBUG):
        async with _serving(Supervisor(_settings(served), use_slots=False)):
            answer = decode_result(await _ask(_sock(served), encode(_request())))

    assert (answer.status, answer.message) == ("error", "the executor failed")
    assert "a job failed in the supervisor: PermissionError" in caplog.text
    assert not _holds_a_secret(caplog.text)


# --- the socket ------------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _serving(sup: Supervisor) -> AsyncIterator[None]:
    """Run ``serve()`` for the duration of the block."""
    task = asyncio.create_task(sup.serve())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _ask(path: Path, line: bytes) -> bytes:
    """Send one line to the socket and read the one line it answers."""
    for _ in range(250):
        try:
            reader, writer = await asyncio.open_unix_connection(str(path), limit=2 * MAX_LINE)
            break
        except (FileNotFoundError, ConnectionRefusedError):
            await asyncio.sleep(0.02)
    else:
        pytest.fail("the supervisor never listened")
    writer.write(line)
    answer = b""
    with contextlib.suppress(ConnectionError):
        answer = await reader.readline()
    writer.close()
    with contextlib.suppress(ConnectionError):
        await writer.wait_closed()
    return answer


@pytest.fixture
def served(public_tmp: Path) -> Iterator[Path]:
    """A prepared volume, owned by the test runner, and a private ``run`` folder with a short path for the socket."""
    data = public_tmp / "data"
    data.mkdir()
    prepare_volume(data, Owners(root=ME, api=ME))
    _sock(data).parent.mkdir(mode=0o755)
    _chat(data)
    yield data


async def test_serve_answers_one_request_per_connection(served: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One request line in, one result line out, on a socket only its owner and group may use."""
    _runner(monkeypatch, 'result = {"tool": spec["tool"]}')
    sup = Supervisor(_settings(served), use_slots=False)
    request = _request()

    async with _serving(sup):
        answer = decode_result(await _ask(_sock(served), encode(request)))
        again = decode_result(await _ask(_sock(served), encode(_request())))
        mode, kind = _mode(_sock(served)), os.lstat(_sock(served)).st_mode

    assert (answer.job_id, answer.status, answer.result) == (request.job_id, "ok", {"tool": "script"})
    assert again.status == "ok"
    assert mode == 0o660
    assert stat.S_ISSOCK(kind)


async def test_a_lone_surrogate_in_a_runners_answer_never_costs_the_run_its_outputs(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every string of the answer, keys and nested ones too, is sent with U+FFFD for a lone surrogate.

    The job writes a lone surrogate as an escape that JSON reads back; the answer could not be encoded before, and the
    run lost its outputs to "the job's result could not be encoded". A pair written as two escapes stays one character.
    """
    _runner(
        monkeypatch,
        """
        pathlib.Path("out/r.txt").write_text("x")
        result = {"text": "\\ud800", "\\udc80": ["a\\udfff", {"pair": "\\ud83d\\ude00"}]}
        """,
    )
    request = _request()

    async with _serving(Supervisor(_settings(served), use_slots=False)):
        answer = decode_result(await _ask(_sock(served), encode(request)))

    assert (answer.status, answer.message) == ("ok", "")
    assert answer.result == {"text": "�", "�": ["a�", {"pair": "\U0001f600"}]}
    assert [output.name for output in answer.outputs] == ["r.txt"]
    assert (served / "staging" / request.job_id / "r.txt").read_text() == "x"


@pytest.mark.parametrize(
    "line",
    [
        b"not json\n",
        b'{"job_id": "quarterly-figures.xlsx"}\n',
        b"x" * (MAX_LINE + 10) + b"\n",
        encode(_request()).replace(b'"chat-1"', b'"../chat-1"'),
    ],
    ids=["not-json", "wrong-shape", "too-long", "unsafe-path"],
)
async def test_a_malformed_request_gets_an_error_line_and_nothing_is_logged(
    line: bytes, served: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A request that does not decode is answered with a short error; its contents reach no log."""
    sup = Supervisor(_settings(served), use_slots=False)

    with caplog.at_level(logging.DEBUG):
        async with _serving(sup):
            answer = decode_result(await _ask(_sock(served), line))

    assert (answer.status, answer.message, answer.result, answer.outputs) == ("error", "malformed request", {}, [])
    assert "quarterly" not in caplog.text
    assert "chat-1" not in caplog.text
    assert _entries(served / "jobs") == []


async def test_serve_ends_when_its_last_slot_is_retired(served: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The request that retired the last slot is still answered; then serving ends with ``NoUsableSlot``."""
    _fake_run(monkeypatch, [RuntimeError("processes of slot 0 survived the cleanup")])
    sup = Supervisor(_settings(served, WORKBENCH_EXECUTOR_SLOTS="1"), use_slots=False)

    serving = asyncio.create_task(sup.serve())
    answer = decode_result(await _ask(_sock(served), encode(_request())))
    with pytest.raises(NoUsableSlot) as ended:
        await asyncio.wait_for(serving, 10)

    assert answer.message == "the job's processes could not be stopped"
    assert str(ended.value) == "every slot is retired (1 of 1); exiting so the container restarts"
    assert not _sock(served).exists()


async def test_serve_goes_on_while_a_slot_is_usable(served: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One retired slot of two leaves the executor serving on the other."""
    _fake_run(monkeypatch, [RuntimeError("x"), None])
    sup = Supervisor(_settings(served), use_slots=False)

    async with _serving(sup):
        first = decode_result(await _ask(_sock(served), encode(_request())))
        second = decode_result(await _ask(_sock(served), encode(_request())))

    assert (first.status, second.status) == ("error", "ok")


async def test_a_stale_socket_is_replaced(served: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A socket left by an earlier run is removed before the new one is bound."""
    _runner(monkeypatch, "pass")
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(_sock(served)))
    stale.close()

    async with _serving(Supervisor(_settings(served), use_slots=False)):
        answer = decode_result(await _ask(_sock(served), encode(_request())))

    assert answer.status == "ok"


async def test_the_socket_gets_its_mode_as_it_is_made_not_through_its_path(served: Path) -> None:
    """``0660`` comes from the umask in force at ``bind``; no mode is set later through a path."""
    real_chmod = os.chmod
    os.chmod = lambda *args, **kwargs: pytest.fail("chmod by path")
    previous = os.umask(0o002)
    try:
        async with _serving(Supervisor(_settings(served), use_slots=False)):
            answer = decode_result(await _ask(_sock(served), b"not json\n"))
            mode = _mode(_sock(served))
    finally:
        restored = os.umask(previous)
        os.chmod = real_chmod

    assert answer.status == "error"
    assert mode == 0o660
    assert restored == 0o002


@pytest.mark.parametrize("trap", ["group-writable", "others-writable", "symlink"])
async def test_a_socket_folder_others_could_change_stops_serve(trap: str, served: Path) -> None:
    """The socket's folder must be root's alone, so no one can swap the socket under the supervisor."""
    run = _sock(served).parent
    real = served.parent / "real-run"
    if trap == "symlink":
        run.rmdir()
        real.mkdir(mode=0o755)
        run.symlink_to(real)
    else:
        run.chmod(0o775 if trap == "group-writable" else 0o757)

    with pytest.raises(RuntimeError, match="the socket's folder must be root's alone"):
        await asyncio.wait_for(Supervisor(_settings(served), use_slots=False).serve(), 10)

    assert not _sock(served).exists()
    assert not real.exists() or _entries(real) == []


async def test_something_else_in_the_sockets_place_stops_serve(served: Path) -> None:
    """A file or symlink where the socket belongs is not removed; the supervisor does not start."""
    (_sock(served)).write_text("not a socket")

    with pytest.raises(RuntimeError, match="not a socket"):
        await asyncio.wait_for(Supervisor(_settings(served), use_slots=False).serve(), 10)

    assert (_sock(served)).read_text() == "not a socket"


async def test_serve_removes_its_socket_when_it_ends(served: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A socket left behind would look like a serving supervisor to whoever only looks at the file."""
    _runner(monkeypatch, "pass")

    async with _serving(Supervisor(_settings(served), use_slots=False)):
        answer = decode_result(await _ask(_sock(served), encode(_request())))
        assert stat.S_ISSOCK(os.lstat(_sock(served)).st_mode)

    assert answer.status == "ok"
    assert not _sock(served).exists()


async def test_serve_leaves_what_is_not_its_socket_when_it_ends(served: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """At the end too, only a socket in the socket's place is removed, never a file put there meanwhile."""
    _runner(monkeypatch, "pass")

    async with _serving(Supervisor(_settings(served), use_slots=False)):
        await _ask(_sock(served), encode(_request()))
        _sock(served).unlink()
        _sock(served).write_text("not a socket")

    assert _sock(served).read_text() == "not a socket"


async def test_serve_leaves_another_supervisors_socket_when_it_ends(
    served: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A socket bound in its place meanwhile, as a supervisor started after this one binds its own, stays."""
    _runner(monkeypatch, "pass")
    other = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        async with _serving(Supervisor(_settings(served), use_slots=False)):
            await _ask(_sock(served), encode(_request()))
            _sock(served).unlink()
            other.bind(str(_sock(served)))
            other.listen()
            theirs = os.lstat(_sock(served))
        left = os.lstat(_sock(served))
    finally:
        other.close()

    assert (left.st_dev, left.st_ino) == (theirs.st_dev, theirs.st_ino)


def _health_probe(path: Path) -> str:
    """The executor's healthcheck from ``docker/compose.yaml``, aimed at ``path`` instead of the real socket."""
    compose = yaml.safe_load((Path(__file__).resolve().parents[2] / "docker" / "compose.yaml").read_text("utf-8"))
    test = compose["services"]["executor"]["healthcheck"]["test"]
    assert test[:3] == ["CMD", "python", "-c"]
    assert "/run/workbench/executor.sock" in test[3]
    return str(test[3]).replace("/run/workbench/executor.sock", str(path))


@pytest.mark.skipif(not Path("/proc/net/unix").exists(), reason="the healthcheck reads Linux's /proc/net/unix")
def test_the_healthcheck_passes_only_while_a_socket_listens_on_its_path() -> None:
    """Run as compose runs it: a socket left by an earlier run, or one not listening yet, is not a healthy executor.

    The probe never connects: a connection without a request line would be a malformed request to the supervisor.
    """
    folder = Path(tempfile.mkdtemp(prefix="wb"))  # short: a socket's path has at most 108 bytes
    sockets = [socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) for _ in range(3)]
    listening, bound, stale = sockets
    try:
        listening.bind(str(folder / "listening"))
        listening.listen()
        bound.bind(str(folder / "bound"))
        stale.bind(str(folder / "stale"))
        stale.listen()
        stale.close()  # its file stays, as a killed supervisor's does
        (folder / "file").write_bytes(b"")
        answers = {
            name: subprocess.run(
                [sys.executable, "-c", _health_probe(folder / name)], capture_output=True, timeout=30
            ).returncode
            for name in ("listening", "bound", "stale", "file", "missing")
        }
        assert stat.S_ISSOCK(os.lstat(folder / "stale").st_mode)
    finally:
        for sock in sockets:
            sock.close()
        shutil.rmtree(folder)
    assert answers == {"listening": 0, "bound": 1, "stale": 1, "file": 1, "missing": 1}


async def test_job_folders_left_by_an_earlier_run_are_removed_at_start(
    served: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A crash leaves copies of users' files in ``jobs``; the next start removes them, counting, never naming."""
    _runner(monkeypatch, "pass")
    stale = served / "jobs" / uuid.uuid4().hex
    (stale / "workspace" / CHAT).mkdir(parents=True)
    (stale / "workspace" / CHAT / "q.csv").write_text("left over")
    (served / "jobs" / "not-a-job").mkdir()

    with caplog.at_level(logging.INFO, logger="workbench.executor"):
        async with _serving(Supervisor(_settings(served), use_slots=False)):
            await _ask(_sock(served), encode(_request()))

    assert _entries(served / "jobs") == ["not-a-job"]
    assert [r.getMessage() for r in caplog.records] == ["removed 1 job folder left from an earlier run"]


async def test_a_loosened_jobs_folder_is_reported_at_start_and_left_alone(
    served: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The sweep removes nothing from a ``jobs`` folder others may write, and says so; jobs then fail to set up."""
    stale = served / "jobs" / uuid.uuid4().hex
    stale.mkdir()
    (served / "jobs").chmod(0o777)

    with caplog.at_level(logging.INFO, logger="workbench.executor"):
        async with _serving(Supervisor(_settings(served), use_slots=False)):
            answer = decode_result(await _ask(_sock(served), encode(_request())))

    assert answer.message == "the job could not be set up"
    assert stale.is_dir()
    assert [r.getMessage() for r in caplog.records] == ["the jobs folder is missing or not root's alone"]


# --- as root, with real slots (Linux) --------------------------------------------------------


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_a_slot_the_job_reaches_only_its_own_folder(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The job runs as slot 0 with no groups: inputs read-only, ``out/`` writable, nothing else on the volume."""
    chat = _chat(root_volume, {"q.csv": b"q"})
    for path in (root_volume / "workspaces" / CALLER, chat.parent, chat, chat / "q.csv"):
        os.chown(path, 10001, 10001)
    _runner(
        monkeypatch,
        f"""
        def owner(p):
            st = os.stat(p, follow_symlinks=False)
            return [st.st_uid, st.st_gid, oct(st.st_mode & 0o7777)]
        def attempt(action):
            try:
                action()
                return "ok"
            except PermissionError:
                return "denied"
        job = spec_path.parent
        result = {{
            "ids": [list(os.getresuid()), list(os.getresgid()), os.getgroups()],
            "nice": os.getpriority(os.PRIO_PROCESS, 0),
            "owners": [owner(job), owner("."), owner("q.csv"), owner("out"), owner(os.environ["TMPDIR"])],
            "new_input": attempt(lambda: open("new.csv", "w").close()),
            "change_input": attempt(lambda: open("q.csv", "a").close()),
            "write_out": attempt(lambda: pathlib.Path("out/r.csv").write_text("r")),
            "workspaces": attempt(lambda: os.listdir({str(root_volume / "workspaces")!r})),
            "staging": attempt(lambda: os.listdir({str(root_volume / "staging")!r})),
            "jobs": attempt(lambda: os.listdir({str(root_volume / "jobs")!r})),
        }}
        """,
    )
    request = _request(input_files=["q.csv"])

    answer = await Supervisor(_settings(root_volume)).run_job(request)

    assert answer.status == "ok", answer
    assert answer.result["ids"] == [[20001] * 3, [20001] * 3, []]
    assert answer.result["nice"] >= 10
    assert answer.result["owners"] == [
        [0, 20001, "0o710"],
        [0, 0, "0o755"],
        [0, 0, "0o444"],
        [20001, 20001, "0o700"],
        [20001, 20001, "0o700"],
    ]
    denied = ("new_input", "change_input", "workspaces", "staging", "jobs")
    assert {key: answer.result[key] for key in denied} == dict.fromkeys(denied, "denied")
    assert answer.result["write_out"] == "ok"
    staged = root_volume / "staging" / request.job_id
    assert (_ids(staged), _mode(staged)) == ((10001, 10001), 0o700)
    assert (_ids(staged / "r.csv"), _mode(staged / "r.csv")) == ((10001, 10001), 0o600)
    assert _entries(root_volume / "jobs") == []


#: The job's half of a barrier the test holds, for two jobs that must look at each other while both run.
#:
#: Each job leaves ``ready`` in its home and waits for the test's ``go``; then it looks (``look()``, which the test
#: defines), leaves ``checked`` and waits for ``done``, so neither ends, and its folder goes, before the other has
#: looked. A ``time.sleep`` did not hold that on a slower host: one job's folder was gone when the other looked (CI,
#: x86_64), and the ``FileNotFoundError`` ended the job as "the job failed". Anything the job raises is its result, so
#: the answer says what went wrong.
TWO_JOBS = """
import time, traceback
gate = pathlib.Path({gate!r})
home = pathlib.Path(os.environ["HOME"])
others = pathlib.Path({jobs!r}) / spec["args"]["other"]
def attempt(action):
    try:
        action()
        return "ok"
    except PermissionError:
        return "denied"
def wait_for(name):
    deadline = time.monotonic() + 60
    while not (gate / name).exists():
        if time.monotonic() > deadline:
            raise TimeoutError("no " + name)
        time.sleep(0.02)
try:
    mine = before()
    (home / "ready").touch()
    wait_for("go")
    result = {{"mine": mine, **look()}}
    (home / "checked").touch()
    wait_for("done")
except BaseException:
    result = {{"crash": traceback.format_exc(limit=4)}}
"""


async def _two_jobs(sup: Supervisor, first: JobRequest, second: JobRequest, root_volume: Path) -> list[JobResult]:
    """Run two jobs at once, holding the barrier of ``TWO_JOBS``; whatever happens, both end and are answered."""
    gate = root_volume.parent / "gate"
    homes = [root_volume / "jobs" / request.job_id / "home" for request in (first, second)]
    running = asyncio.gather(sup.run_job(first), sup.run_job(second))
    for mark, opens in (("ready", "go"), ("checked", "done")):

        def reached(mark: str = mark) -> bool:
            return running.done() or all((home / mark).exists() for home in homes)

        with contextlib.suppress(TimeoutError):
            await until(reached, seconds=scaled(30))
        (gate / opens).touch()
    return list(await running)


def _two_job_runner(monkeypatch: pytest.MonkeyPatch, root_volume: Path, body: str) -> None:
    """Run ``body``, which defines ``before()`` and ``look()``, inside the barrier of ``TWO_JOBS``."""
    gate = root_volume.parent / "gate"
    gate.mkdir()
    gate.chmod(0o755)
    barrier = TWO_JOBS.format(gate=str(gate), jobs=str(root_volume / "jobs"))
    _runner(monkeypatch, textwrap.dedent(body) + barrier)


def _crossed(first: JobRequest, second: JobRequest) -> tuple[JobRequest, JobRequest]:
    """The two requests, each naming the other's job in its arguments."""
    return (
        first.model_copy(update={"args": {"other": second.job_id}}),
        second.model_copy(update={"args": {"other": first.job_id}}),
    )


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_two_running_jobs_cannot_reach_each_other(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Jobs in different slots cannot enter each other's folder, even knowing its id, while both run."""
    _chat(root_volume, {"q.csv": b"q"})
    _two_job_runner(
        monkeypatch,
        root_volume,
        """
        def before():
            return {}
        def look():
            return {
                "list": attempt(lambda: os.listdir(others)),
                "read": attempt(lambda: (others / "workspace" / spec["chat_id"] / "q.csv").read_bytes()),
            }
        """,
    )
    first, second = _crossed(_request(input_files=["q.csv"]), _request(input_files=["q.csv"]))

    answers = await _two_jobs(Supervisor(_settings(root_volume)), first, second, root_volume)

    assert [a.result for a in answers] == [{"mine": {}, "list": "denied", "read": "denied"}] * 2, answers


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_the_real_entry_point_runs_in_a_slot(root_volume: Path) -> None:
    """``python -I -m workbench.executor.job`` starts as the slot and answers."""
    _chat(root_volume)

    answer = await Supervisor(_settings(root_volume)).run_job(_request(tool="echo", args={"x": 1}))

    assert (answer.status, answer.result) == ("ok", {"x": 1})


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="setting the socket's group needs root on Linux")
async def test_as_root_the_socket_belongs_to_the_api_group(root_volume: Path) -> None:
    """Mode 0660, owner root, group 10001: the api may connect, the slot uids may not."""
    _sock(root_volume).parent.mkdir(mode=0o755)
    sup = Supervisor(_settings(root_volume))

    async with _serving(sup):
        answer = decode_result(await _ask(_sock(root_volume), b"not json\n"))
        owned = (_ids(_sock(root_volume)), _mode(_sock(root_volume)))

    assert answer.status == "error"
    assert owned == ((0, 10001), 0o660)


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_a_job_folder_that_cannot_be_removed_belongs_to_root(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slot's next job, perhaps another user's, must not reach a folder left behind."""
    _chat(root_volume)
    _runner(monkeypatch, "pass")
    monkeypatch.setattr(supervisor.shutil, "rmtree", lambda path, **kwargs: kwargs["onexc"](os.rmdir, path, OSError()))
    request = _request()

    await Supervisor(_settings(root_volume)).run_job(request)

    left = root_volume / "jobs" / request.job_id
    assert (_ids(left), _mode(left)) == ((0, 0), 0o700)


# The SysV halves run in every shape; the POSIX halves need slots that may write /dev/shm and make
# queues, which only Docker's default allows (R28).
LEAVE_SYSV = """
import ctypes, struct
libc = ctypes.CDLL(None, use_errno=True)
libc.shmat.restype = ctypes.c_void_p
shm = libc.shmget(0x5742, 4096, 0o1000 | 0o600)
at = libc.shmat(shm, None, 0)
ctypes.memmove(at, b"user-a: sysv note\\0", 18)
libc.shmdt(ctypes.c_void_p(at))
msq = libc.msgget(0x5743, 0o1000 | 0o600)
sent = libc.msgsnd(msq, ctypes.create_string_buffer(struct.pack("q6s", 1, b"user-a")), 6, 0)
sem = libc.semget(0x5744, 1, 0o1000 | 0o600)
result = {"uid": os.getuid(), "made": [shm >= 0, msq >= 0, sent == 0, sem >= 0]}
"""
LEAVE_IPC = (
    LEAVE_SYSV
    + """
pathlib.Path("/dev/shm/wb-drop").write_text("user-a: synthetic figures")
pathlib.Path("/dev/shm/wb-dir").mkdir()
pathlib.Path("/dev/shm/wb-dir/note").write_text("user-a: synthetic note")
mq = libc.mq_open(b"/wb-q", os.O_CREAT | os.O_RDWR, 0o600, None)
queued = libc.mq_send(mq, b"user-a", 6, 0)
result["made"] += [mq >= 0, queued == 0]
"""
)
LOOK_FOR_SYSV = """
import ctypes
libc = ctypes.CDLL(None, use_errno=True)
result = {
    "uid": os.getuid(),
    "sysv": [libc.shmget(0x5742, 0, 0), libc.msgget(0x5743, 0), libc.semget(0x5744, 0, 0)],
}
"""
LOOK_FOR_IPC = (
    LOOK_FOR_SYSV
    + """
tops = ("/dev/shm", "/dev/mqueue")
mine = [n for top in tops for n in os.listdir(top) if os.lstat(f"{top}/{n}").st_uid == os.getuid()]
result |= {
    "files": [os.path.exists("/dev/shm/wb-drop"), os.path.exists("/dev/shm/wb-dir")],
    "mq": libc.mq_open(b"/wb-q", os.O_RDONLY),
    "mine": mine,
}
"""
)


@SHARED_IPC_ONLY
@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_the_next_job_in_a_slot_finds_nothing_of_the_last(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IPC outlives processes: what user a's job leaves in shared memory and queues is gone before user b's runs."""
    _chat(root_volume)
    (root_volume / "workspaces" / CALLER / "user-b" / CHAT).mkdir(parents=True)
    sup = Supervisor(_settings(root_volume, WORKBENCH_EXECUTOR_SLOTS="1"))

    _runner(monkeypatch, LEAVE_IPC)
    first = await sup.run_job(_request())
    _runner(monkeypatch, LOOK_FOR_IPC)
    second = await sup.run_job(_request(user_id="user-b"))

    assert first.status == "ok", first
    assert first.result == {"uid": 20001, "made": [True] * 6}
    assert second.status == "ok", second
    assert second.result == {"uid": 20001, "files": [False, False], "sysv": [-1, -1, -1], "mq": -1, "mine": []}


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_the_next_job_in_a_slot_finds_no_sysv_ipc_of_the_last(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SysV half, in the production shape too: a filled segment, a queued message and a semaphore set are gone."""
    _chat(root_volume)
    (root_volume / "workspaces" / CALLER / "user-b" / CHAT).mkdir(parents=True)
    sup = Supervisor(_settings(root_volume, WORKBENCH_EXECUTOR_SLOTS="1"))

    _runner(monkeypatch, LEAVE_SYSV)
    first = await sup.run_job(_request())
    _runner(monkeypatch, LOOK_FOR_SYSV)
    second = await sup.run_job(_request(user_id="user-b"))

    assert (first.status, first.result) == ("ok", {"uid": 20001, "made": [True] * 4})
    assert (second.status, second.result) == ("ok", {"uid": 20001, "sysv": [-1, -1, -1]})


TRY_TO_LEAVE_IPC = """
import ctypes, errno, multiprocessing
libc = ctypes.CDLL(None, use_errno=True)
libc.shmat.restype = ctypes.c_void_p
made = {}
for name, make in (
    ("file", lambda: open("/dev/shm/wb-drop", "x")),
    ("folder", lambda: os.mkdir("/dev/shm/wb-dir")),
    ("semaphore", multiprocessing.Lock),
):
    try:
        make()
        made[name] = "made"
    except OSError as exc:
        made[name] = type(exc).__name__
queue = libc.mq_open(b"/wb-q", os.O_CREAT | os.O_RDWR, 0o600, None)
made["queue"] = "made" if queue >= 0 else errno.errorcode[ctypes.get_errno()]
shm = libc.shmget(0x5745, 4096, 0o1000 | 0o600)
at = libc.shmat(shm, None, 0)
ctypes.memmove(at, b"user-a: sysv note\\0", 18)
libc.shmdt(ctypes.c_void_p(at))
result = {"uid": os.getuid(), "made": made, "sysv": shm >= 0}
"""
LOOK_FOR_ANY_IPC = """
import ctypes
libc = ctypes.CDLL(None, use_errno=True)
result = {
    "uid": os.getuid(),
    "shm": os.listdir("/dev/shm"),
    "mqueue": os.listdir("/dev/mqueue"),
    "sysv": libc.shmget(0x5745, 0, 0),
}
"""


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
@pytest.mark.skipif(
    IPC_SHAPE != "production",
    reason="needs the production shape: /dev/shm root's alone (--tmpfs /dev/shm:size=1m,mode=0755) "
    "and --sysctl fs.mqueue.queues_max=0",
)
async def test_as_root_in_the_production_shape_a_job_can_leave_nothing_in_dev_shm_or_dev_mqueue(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R28: a slot may add no name to ``/dev/shm`` nor make a POSIX queue; its SysV IPC is gone before the next job."""
    _chat(root_volume)
    (root_volume / "workspaces" / CALLER / "user-b" / CHAT).mkdir(parents=True)
    sup = Supervisor(_settings(root_volume, WORKBENCH_EXECUTOR_SLOTS="1"))

    _runner(monkeypatch, TRY_TO_LEAVE_IPC)
    first = await sup.run_job(_request())
    _runner(monkeypatch, LOOK_FOR_ANY_IPC)
    second = await sup.run_job(_request(user_id="user-b"))

    refused = dict.fromkeys(("file", "folder", "semaphore"), "PermissionError") | {"queue": "ENOSPC"}
    assert (first.status, first.result) == ("ok", {"uid": 20001, "made": refused, "sysv": True})
    assert (second.status, second.result) == ("ok", {"uid": 20001, "shm": [], "mqueue": [], "sysv": -1})


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_outputs_the_job_does_not_own_or_that_have_other_names_are_dropped(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files in ``out/`` that are not the slot's, or that name a file linked elsewhere, never reach staging."""
    _chat(root_volume)
    _runner(
        monkeypatch,
        """
        import time
        pathlib.Path("out/r.csv").write_text("r")
        pathlib.Path("out/ready").write_text("")
        for _ in range(200):
            if pathlib.Path("out/placed").exists():
                break
            time.sleep(0.05)
        os.unlink("out/ready")
        os.unlink("out/placed")
        """,
    )
    request = _request()
    out = root_volume / "jobs" / request.job_id / "workspace" / CHAT / "out"
    database = root_volume / "workbench.db"
    job = asyncio.create_task(Supervisor(_settings(root_volume)).run_job(request))
    for _ in range(200):
        if (out / "ready").exists():
            break
        await asyncio.sleep(0.05)
    (out / "foreign.csv").write_text("root's, not the job's")
    os.link(database, out / "linked.bin")
    (out / "placed").write_text("")

    answer = await job

    assert (answer.status, [o.name for o in answer.outputs]) == ("ok", ["r.csv"])
    assert answer.message == "dropped 2 outputs: not the job's own file"
    assert os.stat(database).st_nlink == 1


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="owning a folder by another uid needs root on Linux")
async def test_as_root_a_socket_folder_the_api_owns_stops_serve(root_volume: Path) -> None:
    """The api must not own the socket's folder, or it could replace the socket the supervisor binds."""
    _sock(root_volume).parent.mkdir(mode=0o755)
    os.chown(_sock(root_volume).parent, 10001, 10001)

    with pytest.raises(RuntimeError, match="the socket's folder must be root's alone"):
        await asyncio.wait_for(Supervisor(_settings(root_volume)).serve(), 10)


HAND_OVER_JOB = """
import ctypes, struct, time
libc = ctypes.CDLL(None, use_errno=True)
target, how, end = int(spec["args"]["target"]), spec["args"]["how"], time.time() + float(spec["args"]["secs"])
me = os.getuid()
def owner(ident, uid):
    state = ctypes.create_string_buffer(512)
    if libc.shmctl(ident, 2, state) == 0:
        struct.pack_into("I", state, 4, uid)
        libc.shmctl(ident, 1, state)
live = []
while time.time() < end:
    if how == "give":
        ident = libc.shmget(0, 4096, 0o1000 | 0o600)
        if ident >= 0:
            owner(ident, target)
            live.append(ident)
        if len(live) > 64:
            libc.shmctl(live.pop(0), 0, None)
    else:
        if not live:
            live = [libc.shmget(0, 4096, 0o1000 | 0o600) for _ in range(16)]
        for ident in live:
            owner(ident, target)
            owner(ident, me)
result = {"uid": me}
"""
QUICK = 'result = {"uid": os.getuid()}'


@pytest.mark.parametrize("how", ["give", "flip"])
@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_a_job_in_another_slot_cannot_retire_a_slot_through_ipc_set(
    how: str, root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handing segments to another slot's uid, or flipping their owner back and forth, retires nothing."""
    _chat(root_volume)
    sup = Supervisor(_settings(root_volume))
    _runner(monkeypatch, HAND_OVER_JOB)
    attacker = asyncio.create_task(sup.run_job(_request(args={"target": 20002, "how": how, "secs": 6})))
    await asyncio.sleep(1)
    _runner(monkeypatch, QUICK)

    victims = [await sup.run_job(_request()) for _ in range(5)]
    attack = await attacker
    both = await asyncio.gather(sup.run_job(_request()), sup.run_job(_request()))

    assert [(v.status, v.result) for v in victims] == [("ok", {"uid": 20002})] * 5
    assert attack.status == "ok"
    assert sorted(a.result["uid"] for a in both) == [20001, 20002]


TRAPS_JOB = """
os.mkdir("/dev/shm/wb-locked")
pathlib.Path("/dev/shm/wb-locked/note").write_text("synthetic note of user a")
os.chmod("/dev/shm/wb-locked", 0)
os.mkdir("/dev/shm/wb-deep")
os.chdir("/dev/shm/wb-deep")
for _ in range(2100):
    os.mkdir("d")
    os.chdir("d")
pathlib.Path("note").write_text("synthetic note of user a")
result = {"uid": os.getuid()}
"""
LOOK_FOR_TRAPS = 'result = {"uid": os.getuid(), "left": sorted(os.listdir("/dev/shm"))}'


@SHARED_IPC_ONLY
@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_a_job_cannot_retire_its_own_slot_with_a_locked_or_deep_folder(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder its owner made unreadable and a chain deeper than ``PATH_MAX`` are both cleared."""
    _chat(root_volume)
    (root_volume / "workspaces" / CALLER / "user-b" / CHAT).mkdir(parents=True)
    sup = Supervisor(_settings(root_volume, WORKBENCH_EXECUTOR_SLOTS="1"))

    _runner(monkeypatch, TRAPS_JOB)
    first = await sup.run_job(_request())
    _runner(monkeypatch, LOOK_FOR_TRAPS)
    second = await sup.run_job(_request(user_id="user-b"))

    assert (first.status, first.result) == ("ok", {"uid": 20001})
    assert (second.status, second.result) == ("ok", {"uid": 20001, "left": []})


HOLD_JOB = """
import ctypes, time
libc = ctypes.CDLL(None, use_errno=True)
libc.shmat.restype = ctypes.c_void_p
end = time.time() + 20
while time.time() < end and (ident := libc.shmget(0x57D0, 0, 0)) < 0:
    time.sleep(0.01)
libc.shmat(ident, None, 0)
pathlib.Path(spec["args"]["held"]).write_text("held")
time.sleep(float(spec["args"]["secs"]))
result = {"uid": os.getuid()}
"""
SHARE_JOB = """
import ctypes, time
libc = ctypes.CDLL(None, use_errno=True)
libc.shmat.restype = ctypes.c_void_p
ident = libc.shmget(0x57D0, 4096, 0o1000 | 0o666)
ctypes.memmove(libc.shmat(ident, None, 0), b"synthetic note of user a\\0", 25)
end = time.time() + 20
while time.time() < end and not os.path.exists(spec["args"]["held"]):
    time.sleep(0.01)
result = {"uid": os.getuid(), "shmid": ident}
"""
ATTACH_JOB = """
import ctypes, struct
libc = ctypes.CDLL(None, use_errno=True)
libc.shmat.restype = ctypes.c_void_p
ident, failed = int(spec["args"]["shmid"]), (None, ctypes.c_void_p(-1).value)
plain = libc.shmat(ident, None, 0)
perm = ctypes.create_string_buffer(512)
struct.pack_into("III", perm, 4, os.getuid(), os.getgid(), os.getuid())
struct.pack_into("I", perm, 20, 0o600)
restored = libc.shmctl(ident, 1, perm)
at = libc.shmat(ident, None, 0)
content = None if at in failed else ctypes.string_at(at, 4096)
result = {"uid": os.getuid(), "plain": plain not in failed, "restored": restored == 0, "zeros": content == bytes(4096)}
"""


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_a_segment_another_slot_keeps_attached_neither_retires_the_slot_nor_reaches_its_next_job(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The segment is emptied, marked for destruction and closed; even reopened by its creator it holds nothing."""
    _chat(root_volume)
    (root_volume / "workspaces" / CALLER / "user-c" / CHAT).mkdir(parents=True)
    held = Path(tempfile.mkdtemp(dir=root_volume.parent))
    held.chmod(0o777)
    sup = Supervisor(_settings(root_volume))

    _runner(monkeypatch, HOLD_JOB)
    holder = asyncio.create_task(sup.run_job(_request(args={"held": str(held / "held"), "secs": 4})))
    await asyncio.sleep(0.5)
    _runner(monkeypatch, SHARE_JOB)
    sharer = await sup.run_job(_request(args={"held": str(held / "held")}))
    _runner(monkeypatch, ATTACH_JOB)
    after = await sup.run_job(_request(user_id="user-c", args={"shmid": sharer.result["shmid"]}))
    holding = await holder

    assert (sharer.status, sharer.result["uid"]) == ("ok", 20002)
    assert (after.status, after.result) == ("ok", {"uid": 20002, "plain": False, "restored": True, "zeros": True})
    assert (holding.status, holding.result) == ("ok", {"uid": 20001})


# --- the job's own files, the watchdog and the leftovers (R75, R76) -----------------------------------------------


async def test_a_result_with_a_second_name_is_refused(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``result.json`` must be the job's alone, as an output must: a hard link could name a file it may not read."""
    _chat(volume)
    _runner(
        monkeypatch,
        """
        result = {"x": 1}
        other = result_path.with_name("other.json")
        other.write_text(json.dumps(result))
        os.link(other, result_path)
        sys.exit(0)
        """,
    )

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request())

    assert (answer.status, answer.message) == ("error", "the job left no readable result")


@pytest.mark.parametrize(
    ("body", "settings", "message"),
    [
        (
            "pathlib.Path(os.environ['TMPDIR'], 'big').write_bytes(os.urandom(3 * 1024 * 1024))",
            {"WORKBENCH_WORKSPACE_MAX_MB": "1"},
            "the job was stopped: it wrote more than 1 MB",
        ),
        (
            "[open(f'out/f{n}', 'w').close() for n in range(10001)]",
            {},
            "the job was stopped: it wrote more than 10 000 files and folders",
        ),
        (
            "here = os.open(os.environ['HOME'], os.O_RDONLY)\nfor _ in range(260):\n"
            "    os.mkdir('d', dir_fd=here)\n    deeper = os.open('d', os.O_RDONLY, dir_fd=here)\n"
            "    os.close(here)\n    here = deeper",
            {},
            "the job was stopped: its folders nest more than 256 deep",
        ),
    ],
    ids=["disk", "entries", "depth"],
)
async def test_a_job_past_a_limit_of_its_folders_is_stopped_and_told_which(
    body: str, settings: dict[str, str], message: str, volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The watchdog measures ``home/`` and ``out/`` about twice a second and stops the job well before its wall time."""
    _chat(volume)
    _runner(monkeypatch, body + "\nimport time\ntime.sleep(60)\n")
    began = time.monotonic()

    answer = await Supervisor(_settings(volume, **settings), use_slots=False).run_job(_request())

    assert (answer.status, answer.message) == ("killed", message)
    assert time.monotonic() - began < 30
    assert _entries(volume / "jobs") == []


async def test_a_job_within_its_limits_is_not_stopped(volume: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A job that writes a little and takes longer than a few measures ends by itself."""
    _chat(volume)
    _runner(
        monkeypatch,
        """
        import time
        pathlib.Path("out/r.csv").write_bytes(b"x" * 100_000)
        time.sleep(1.5)
        result = {"done": True}
        """,
    )

    answer = await Supervisor(_settings(volume, WORKBENCH_WORKSPACE_MAX_MB="1"), use_slots=False).run_job(_request())

    assert (answer.status, answer.result) == ("ok", {"done": True})


async def test_a_job_ends_at_its_wall_time_while_every_worker_of_the_loops_pool_is_held(
    volume: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R86 N2: the wall clock is kept on the loop; the watchdog has a thread of its own. Neither waits on that pool.

    The pool runs the set-up, the staging and the removals of every slot; once the job runs, both its workers are held.
    The job beats into a file; once its wall time has passed, it beats no more, while the pool is still held.
    """
    _chat(volume)
    beats = tmp_path / "beats"
    _runner(
        monkeypatch,
        f"""
        import time
        while True:
            pathlib.Path({str(beats)!r}).write_text(repr(time.time()))
            time.sleep(0.05)
        """,
    )
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=2))
    release = threading.Event()
    real = Supervisor._set_up

    def set_up_then_hold_the_pool(self: Supervisor, *args: Any) -> Any:
        job = real(self, *args)
        for _ in range(2):  # both workers, once this one is back
            loop.call_soon_threadsafe(loop.run_in_executor, None, release.wait, 60)
        return job

    monkeypatch.setattr(Supervisor, "_set_up", set_up_then_hold_the_pool)
    limits = Limits(wall_s=2, cpu_s=60, memory_mb=1024, file_mb=50)
    running = asyncio.create_task(Supervisor(_settings(volume), use_slots=False).run_job(_request(limits=limits)))
    try:
        await until(beats.exists, seconds=scaled(10))
        await asyncio.sleep(2 + scaled(2))
        last = beats.read_text()
        await asyncio.sleep(1)
        assert beats.read_text() == last, "the job still ran past its wall time"
        assert not running.done()  # the staging that follows waits for the pool
    finally:
        release.set()
    answer = await running
    assert (answer.status, answer.message) == ("timeout", "the job ran past its wall time of 2 s")


async def test_a_job_the_watchdog_cannot_measure_is_stopped_and_its_slot_retired(
    volume: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R86 N3: a measure that fails stops the job (fail closed) and retires its slot; the log has the type alone."""
    _chat(volume)
    _runner(monkeypatch, "import time\ntime.sleep(60)\n")

    def unreadable(tops: Any, **bounds: Any) -> Any:
        raise _unexpected()

    monkeypatch.setattr(supervisor, "measure", unreadable)
    sup = Supervisor(_settings(volume), use_slots=False)
    limits = Limits(wall_s=5, cpu_s=60, memory_mb=1024, file_mb=50)
    began = time.monotonic()

    with caplog.at_level(logging.DEBUG, logger="workbench.executor"):
        answer = await sup.run_job(_request(limits=limits))

    assert (answer.status, answer.message) == (
        "killed",
        "the job was stopped: its use of memory and disk could not be measured",
    )
    assert time.monotonic() - began < scaled(4)
    assert sup._retired == {0}
    assert sup._free == [1]
    assert "the watchdog could not measure the job of slot 0 (PermissionError)" in caplog.text
    assert not _holds_a_secret(caplog.text)
    assert _entries(volume / "jobs") == []


class _Judged:
    """A stand-in for a job's watch: its slot's uid and what judging it does."""

    def __init__(self, index: int, uid: int | None, verdict: str | None = None, error: Exception | None = None) -> None:
        """Judge to ``verdict``, or raise ``error``."""
        self.index, self.uid = index, uid
        self.verdict, self.error = verdict, error
        self.seen: list[SlotUse | None] = []

    def judge(self, use: SlotUse | None) -> str | None:
        """Remember the use, then judge."""
        self.seen.append(use)
        if self.error is not None:
            raise self.error
        return self.verdict


def test_one_pass_reads_proc_once_for_every_slot_and_hands_each_job_its_own_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R86 N6: one look at ``/proc`` for all slots per pass, not one per slot; the per-slot sums stay apart."""
    looks: list[set[int]] = []

    def use_of(uids: set[int]) -> dict[int, SlotUse]:
        looks.append(set(uids))
        return {uid: SlotUse(memory=uid) for uid in uids}

    monkeypatch.setattr(supervisor, "slots_use", use_of)
    monkeypatch.setattr(supervisor, "unattributed_shmem", lambda data_dir: 0)
    first, second, third = _Judged(0, 20001), _Judged(1, 20002, "it wrote more than 1 MB"), _Judged(2, None)

    verdicts = supervisor._Watchdog(_Above(), NOWHERE).check([first, second, third])  # type: ignore[list-item]

    assert looks == [{20001, 20002}]
    assert (first.seen, second.seen, third.seen) == ([SlotUse(memory=20001)], [SlotUse(memory=20002)], [None])
    assert verdicts == [(second, "it wrote more than 1 MB")]


def test_a_pass_that_cannot_read_proc_stops_every_job(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R86 N3: fail closed for every job when the shared look fails, and for one job when its own measure does."""

    def broken(uids: set[int]) -> dict[int, SlotUse]:
        raise _unexpected()

    one, two = _Judged(0, 20001), _Judged(1, 20002, error=_unexpected())
    watchdog = supervisor._Watchdog(_Above(), NOWHERE)  # type: ignore[arg-type]
    monkeypatch.setattr(supervisor, "slots_use", lambda uids: {uid: SlotUse() for uid in uids})
    monkeypatch.setattr(supervisor, "unattributed_shmem", lambda data_dir: 0)
    with caplog.at_level(logging.DEBUG, logger="workbench.executor"):
        assert watchdog.check([one, two]) == [(two, supervisor.UNMEASURED)]  # type: ignore[list-item]
        monkeypatch.setattr(supervisor, "slots_use", broken)
        assert watchdog.check([one, two]) == [  # type: ignore[list-item]
            (one, supervisor.UNMEASURED),
            (two, supervisor.UNMEASURED),
        ]
    assert "the watchdog could not measure the job of slot 1 (PermissionError)" in caplog.text
    assert "the watchdog could not read the slots' processes (PermissionError)" in caplog.text
    assert not _holds_a_secret(caplog.text)


async def test_a_watchdog_that_dies_stops_its_jobs_and_the_next_job_is_watched_by_a_new_one(
    volume: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R89: an error that escapes the watchdog's loop fails closed: its jobs stop unmeasured, their slots are retired.

    The next job starts a new thread, which watches it: past its disk limit, it is stopped.
    """
    _chat(volume)
    _runner(monkeypatch, "import time\ntime.sleep(60)\n")
    real = supervisor._Watchdog.check
    passes: list[int] = []

    def dies_once(self: Any, watches: list[Any]) -> list[tuple[Any, str]]:
        passes.append(len(watches))
        if len(passes) == 1:
            raise MemoryError
        return real(self, watches)

    monkeypatch.setattr(supervisor._Watchdog, "check", dies_once)
    sup = Supervisor(_settings(volume, WORKBENCH_WORKSPACE_MAX_MB="1"), use_slots=False)
    limits = Limits(wall_s=20, cpu_s=20, memory_mb=1024, file_mb=50)
    began = time.monotonic()

    with caplog.at_level(logging.DEBUG, logger="workbench.executor"):
        first = await sup.run_job(_request(limits=limits))

    assert (first.status, first.message) == (
        "killed",
        "the job was stopped: its use of memory and disk could not be measured",
    )
    assert time.monotonic() - began < scaled(5)
    assert sup._retired == {0}
    assert "the watchdog stopped (MemoryError); stopping the 1 job it watched" in caplog.messages

    _runner(
        monkeypatch,
        "pathlib.Path(os.environ['TMPDIR'], 'big').write_bytes(os.urandom(3 * 1024 * 1024))\n"
        "import time\ntime.sleep(60)\n",
    )
    second = await sup.run_job(_request(limits=limits))
    assert (second.status, second.message) == ("killed", "the job was stopped: it wrote more than 1 MB")
    assert len(passes) >= 2


async def test_an_error_in_the_dying_watchdogs_handler_leaves_no_job_it_took_unwatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R89: the stops go out before the log line, each on its own; neither a failing post nor log stops them."""
    loop = asyncio.get_running_loop()

    class ClosedLoop:
        def call_soon_threadsafe(self, *args: Any) -> None:
            raise ValueError("synthetic: this post fails")

    first = types.SimpleNamespace(loop=ClosedLoop(), stop=loop.create_future())
    second = types.SimpleNamespace(loop=loop, stop=loop.create_future())
    watchdog = supervisor._Watchdog(_Above(), NOWHERE, interval_s=0.01)  # type: ignore[arg-type]
    watchdog._watches = [first, second]  # type: ignore[list-item]

    def dies(self: Any, watches: list[Any]) -> list[tuple[Any, str]]:
        raise MemoryError

    def no_log(*args: Any, **kwargs: Any) -> None:
        raise MemoryError

    monkeypatch.setattr(supervisor._Watchdog, "check", dies)
    monkeypatch.setattr(supervisor._log, "error", no_log)
    thread = threading.Thread(target=watchdog._run)
    thread.start()
    try:
        await until(second.stop.done, seconds=scaled(5))
    finally:
        thread.join(scaled(5))

    assert second.stop.result() == supervisor.UNMEASURED
    assert not thread.is_alive()
    assert (watchdog._thread, watchdog._watches) == (None, [])


async def test_a_job_whose_watchdog_cannot_start_never_runs_and_the_next_job_starts_one(
    volume: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R89, c5d75fb: a thread that cannot start leaves the watchdog able to start the next; the answer says the truth.

    Nothing ran, so the answer is not "the job's processes could not be stopped"; the job's folder goes and its slot
    stays free.
    """
    _chat(volume)
    ran = volume.parent / "ran"
    _runner(monkeypatch, f"pathlib.Path({str(ran)!r}).touch()\nresult = {{'ran': True}}\n")
    real = supervisor._Watchdog._new_thread

    class NoThread:
        def start(self) -> None:
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(supervisor._Watchdog, "_new_thread", lambda self: NoThread())
    sup = Supervisor(_settings(volume), use_slots=False)

    with caplog.at_level(logging.DEBUG, logger="workbench.executor"):
        refused = await sup.run_job(_request())

    assert (refused.status, refused.message) == ("error", "the job could not be watched, so it did not run")
    assert not ran.exists()
    assert _entries(volume / "jobs") == []
    assert (sorted(sup._free), sup._retired) == ([0, 1], set())
    assert "a job could not be watched and did not run (RuntimeError)" in caplog.messages

    monkeypatch.setattr(supervisor._Watchdog, "_new_thread", real)
    again = await sup.run_job(_request())
    assert (again.status, again.result) == ("ok", {"ran": True})


def test_shared_memory_outside_every_jobs_count_stops_every_job(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R90: past the margin, every running job stops, the innocent with the rest; the log has counts alone.

    Without a slot (tests, dev) the container's shared memory is not asked: such jobs share the supervisor's uid.
    """
    hidden = {"bytes": supervisor.SHMEM_MARGIN + 1}
    looks: list[int] = []

    def unattributed(data_dir: Path) -> int:
        assert data_dir == NOWHERE
        looks.append(hidden["bytes"])
        return hidden["bytes"]

    monkeypatch.setattr(supervisor, "unattributed_shmem", unattributed)
    monkeypatch.setattr(supervisor, "slots_use", lambda uids: {uid: SlotUse() for uid in uids})
    one, two = _Judged(0, 20001), _Judged(1, 20002, "it wrote more than 1 MB")
    watchdog = supervisor._Watchdog(_Above(), NOWHERE)  # type: ignore[arg-type]

    with caplog.at_level(logging.DEBUG, logger="workbench.executor"):
        assert watchdog.check([one, two]) == [(one, supervisor.HIDDEN), (two, supervisor.HIDDEN)]  # type: ignore[list-item]
        hidden["bytes"] = supervisor.SHMEM_MARGIN
        assert watchdog.check([one, two]) == [(two, "it wrote more than 1 MB")]  # type: ignore[list-item]
        assert watchdog.check([_Judged(2, None)]) == []  # type: ignore[list-item]

    assert looks == [supervisor.SHMEM_MARGIN + 1, supervisor.SHMEM_MARGIN]
    assert caplog.messages == ["stopped 2 running jobs: 64 MiB of shared memory is outside any job's count"]


def test_a_job_stopped_for_hidden_shared_memory_is_refused_with_its_code(volume: Path) -> None:
    """R90: the answer is a refusal the api answers 503 and records as ``refused: hidden_memory``."""
    sup = Supervisor(_settings(volume), use_slots=False)
    limits = Limits(wall_s=5, cpu_s=5, memory_mb=256, file_mb=1)

    answer = sup._answer_job(_request(), None, "killed", limits, supervisor.HIDDEN)  # type: ignore[arg-type]

    assert (answer.status, answer.result, answer.message) == (
        "error",
        {"error": "hidden_memory: memory outside any job's count"},
        "the job was stopped: shared memory outside any job's count",
    )


def test_a_container_whose_shared_memory_cannot_be_read_does_not_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R90: the bound needs cgroup v2's ``memory.stat``; without its ``shmem`` the supervisor refuses to start."""
    limits = _Limits(8192, 8192)
    monkeypatch.setattr(supervisor.resource, "getrlimit", limits.get)
    monkeypatch.setattr(supervisor.resource, "setrlimit", limits.set)
    monkeypatch.setattr(supervisor, "_close_dev_zero", lambda path: True)
    good, bad = tmp_path / "good.stat", tmp_path / "bad.stat"
    good.write_text("anon 1\nshmem 0\n")
    bad.write_text("anon 1\n")
    own = _own_cgroup(tmp_path)
    settings = _settings(Path("/nowhere"))

    assert supervisor.prepare_host(settings, memory_stat=good, self_cgroup=own) is None
    for memory_stat in (bad, tmp_path / "absent"):
        assert supervisor.prepare_host(settings, memory_stat=memory_stat, self_cgroup=own) == (
            "the container's memory.stat (cgroup v2) must give its shared memory, so the watchdog can bound it"
        )


@pytest.mark.parametrize(
    "content",
    ["0::/system.slice/docker-0123.scope\n", "12:memory:/docker/0123\n0::/\n", ""],
    ids=["host-namespace", "cgroup-v1", "empty"],
)
def test_a_supervisor_outside_a_cgroup_namespace_of_its_own_does_not_start(
    content: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R91: under ``cgroupns=host`` (or cgroup v1) ``/sys/fs/cgroup`` is not the container's: refused at start."""
    limits = _Limits(8192, 8192)
    monkeypatch.setattr(supervisor.resource, "getrlimit", limits.get)
    monkeypatch.setattr(supervisor.resource, "setrlimit", limits.set)
    monkeypatch.setattr(supervisor, "_close_dev_zero", lambda path: True)
    good = tmp_path / "good.stat"
    good.write_text("shmem 0\n")
    cgroup = tmp_path / "cgroup"
    cgroup.write_text(content)

    for self_cgroup in (cgroup, tmp_path / "absent"):
        assert supervisor.prepare_host(_settings(Path("/nowhere")), memory_stat=good, self_cgroup=self_cgroup) == (
            "the supervisor must be in a cgroup namespace of its own (cgroup v2), so it reads the container's memory"
        )


@pytest.mark.skipif(not IN_THE_IMAGE, reason="the executor image's own cgroup, as root")
def test_as_root_the_container_has_a_cgroup_namespace_and_memory_stat_of_its_own() -> None:
    """R91: what ``prepare_host`` requires holds in the container the root tests run in, as in compose's."""
    assert Path("/proc/self/cgroup").read_text().strip() == "0::/"
    assert supervisor.shmem_charged(supervisor.MEMORY_STAT) >= 0


async def test_a_watch_judges_its_slots_memory_and_score_before_its_folders(tmp_path: Path) -> None:
    """R86 N7: a lowered OOM score is a broken limit; memory past the job's share is too."""
    home, out = tmp_path / "home", tmp_path / "out"
    home.mkdir()
    out.mkdir()
    fds = [os.open(folder, os.O_RDONLY | os.O_DIRECTORY) for folder in (home, out)]
    job = supervisor._Job(
        name="0" * 32, jobs=-1, folder=-1, out=fds[1], home=fds[0], path=tmp_path, cwd=tmp_path, runner=ME[0]
    )
    limits = Limits(wall_s=5, cpu_s=5, memory_mb=256, file_mb=1)
    watch = supervisor._Watch(job, 0, Slot(0, 20001, 20001), limits, disk_mb=1)
    try:
        assert watch.judge(SlotUse(memory=256 * MB)) is None
        assert watch.judge(SlotUse(memory=256 * MB + 1)) == "its processes used more than 256 MB of memory together"
        assert watch.judge(SlotUse(memory=0, lowered=True)) == "one of its processes lowered its OOM score"
        (home / "big").write_bytes(os.urandom(2 * MB))
        assert watch.judge(SlotUse()) == "it wrote more than 1 MB"
    finally:
        watch.close()
        for fd in fds:
            os.close(fd)
    assert watch.judge(SlotUse(lowered=True)) is None


GIB = 1024 * MB


def _space(free: int, size: int = 100 * GIB) -> os.statvfs_result:
    """What ``statvfs`` says of a volume of ``size`` bytes with ``free`` of them free to anyone, in 4 KiB blocks."""
    return os.statvfs_result((4096, 4096, size // 4096, free // 4096, free // 4096, 1000, 500, 500, 0, 255))


def test_the_floor_is_a_gibibyte_or_a_twentieth_of_the_volume_whichever_is_more() -> None:
    """R84: a fixed rule, not a setting: 1 GiB on a volume of up to 20 GiB, 5 % on a larger one."""
    assert [FREE_SPACE_FLOOR(size * GIB) for size in (1, 10, 20, 100, 2000)] == [GIB, GIB, GIB, 5 * GIB, 100 * GIB]


class _Volume:
    """A stand-in for ``os.statvfs`` whose answer the test sets, counting the calls."""

    def __init__(self, free: int) -> None:
        """Answer ``free`` bytes free of 100 GiB."""
        self.free = free
        self.asked = 0

    def __call__(self, path: Any) -> os.statvfs_result:
        """The volume's space now."""
        self.asked += 1
        return _space(self.free)


async def test_below_the_free_space_floor_no_job_starts(
    volume: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R84: the refusal is the runner's ``storage_full`` code, which the api answers 507; nothing of the job is made."""
    monkeypatch.setattr(supervisor, "free_space_floor", FREE_SPACE_FLOOR)
    space = _Volume(free=4 * GIB)  # below 5 GiB, the floor of 100 GiB
    monkeypatch.setattr(supervisor.os, "statvfs", space)
    _chat(volume, {"q.csv": b"q"})
    _runner(monkeypatch, "result = {'ran': True}")
    sup = Supervisor(_settings(volume), use_slots=False)
    request = _request(input_files=["q.csv"])

    with caplog.at_level(logging.DEBUG, logger="workbench.executor"):
        first = await sup.run_job(request)
        second = await sup.run_job(_request())

    for answer in (first, second):
        assert (answer.status, answer.result, answer.message) == (
            "error",
            {"error": "storage_full: storage is full"},
            "the data volume is full",
        )
    assert _entries(volume / "jobs") == []
    assert (sup._free, sup._retired) == ([0, 1], set())
    assert caplog.messages == ["the data volume is below its free-space floor: running jobs stop and no job starts"]

    space.free = 6 * GIB
    with caplog.at_level(logging.DEBUG, logger="workbench.executor"):
        again = await sup.run_job(request)
    assert (again.status, again.result) == ("ok", {"ran": True})
    assert caplog.messages[-1] == "the data volume is above its free-space floor again: jobs start"
    assert request.job_id not in caplog.text


async def test_a_running_job_stops_when_the_volume_falls_below_its_floor(
    volume: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R84: the watchdog asks the volume in every pass; below the floor it stops every running job, by count in the log.

    A job can fill the volume with a file it removed and keeps open: no folder the watchdog walks holds it, but the
    volume's free space counts it.
    """
    monkeypatch.setattr(supervisor, "free_space_floor", FREE_SPACE_FLOOR)
    space = _Volume(free=50 * GIB)
    monkeypatch.setattr(supervisor.os, "statvfs", space)
    _chat(volume)
    started = volume.parent / "started"
    started.mkdir()
    _runner(
        monkeypatch, f"import time\n(pathlib.Path({str(started)!r}) / spec_path.parent.name).touch()\ntime.sleep(60)\n"
    )
    sup = Supervisor(_settings(volume), use_slots=False)
    limits = Limits(wall_s=30, cpu_s=30, memory_mb=1024, file_mb=50)

    with caplog.at_level(logging.DEBUG, logger="workbench.executor"):
        running = [asyncio.create_task(sup.run_job(_request(limits=limits))) for _ in range(2)]
        await until(lambda: len(_entries(started)) == 2, seconds=scaled(10))
        began = time.monotonic()
        space.free = GIB
        answers = await asyncio.gather(*running)

    assert time.monotonic() - began < scaled(4)
    for answer in answers:
        assert (answer.status, answer.result) == ("error", {"error": "storage_full: storage is full"})
    assert "stopped 2 running jobs: the data volume is below its free-space floor" in caplog.messages
    assert _entries(volume / "jobs") == []
    assert sorted(sup._free) == [0, 1]


async def test_a_volume_that_cannot_be_asked_for_its_free_space_runs_no_job(
    volume: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Fail closed: the job is not set up, and the log names the error's type alone."""

    def broken(path: Any) -> os.statvfs_result:
        raise _unexpected()

    monkeypatch.setattr(supervisor.os, "statvfs", broken)
    _chat(volume)
    _runner(monkeypatch, "result = {'ran': True}")

    with caplog.at_level(logging.DEBUG, logger="workbench.executor"):
        answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request())

    assert (answer.status, answer.message) == ("error", "the job could not be set up")
    assert "the data volume's free space could not be read (PermissionError)" in caplog.text
    assert not _holds_a_secret(caplog.text)


async def test_a_failed_run_removes_the_job_folder_off_the_event_loop(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R1 M-3: what the job left can be millions of entries; the removal runs in a thread, never on the loop."""
    _chat(volume)
    _fake_run(monkeypatch, [RuntimeError("processes of slot 0 survived the cleanup")])
    threads: list[str] = []
    real = Supervisor._remove

    def remove(self: Supervisor, job: Any, watch: Any = None) -> bool:
        threads.append(threading.current_thread().name)
        return real(self, job, watch)

    monkeypatch.setattr(Supervisor, "_remove", remove)

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(_request())

    assert answer.message == "the job's processes could not be stopped"
    assert threads and threading.main_thread().name not in threads


async def test_a_job_folder_whose_removal_failed_is_tried_again(
    volume: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R2 I5: the folder holds copies of a user's files; it is tried again on a timer, and the log has counts only."""
    _chat(volume, {"q.csv": b"q"})
    _runner(monkeypatch, "result = {}")
    sup = Supervisor(_settings(volume), use_slots=False)
    real = Supervisor._rmtree
    failing = {"left": True}

    def rmtree(self: Supervisor, name: str, parent: int, *, quiet: bool = False) -> bool:
        if failing["left"]:
            failing["left"] = False
            return False
        return real(self, name, parent, quiet=quiet)

    monkeypatch.setattr(Supervisor, "_rmtree", rmtree)
    request = _request(input_files=["q.csv"])
    await sup.run_job(request)
    assert _entries(volume / "jobs") == [request.job_id]
    monkeypatch.setattr(supervisor, "LEFTOVER_RETRY_S", 0.01)

    with caplog.at_level(logging.INFO, logger="workbench.executor"):
        retrying = asyncio.create_task(sup._retry_leftovers())
        await until(lambda: _entries(volume / "jobs") == [])
        retrying.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await retrying

    assert "removed 1 of 1 job folder a failed removal left" in caplog.text
    assert request.job_id not in caplog.text


class _SlowSet(set[str]):
    """A set whose iteration stops after its first name, until told to go on: a retry in the middle of its copy."""

    def __init__(self, *names: str) -> None:
        """Hold ``names``."""
        super().__init__(names)
        self.reading = threading.Event()
        self.go_on = threading.Event()

    @override
    def __iter__(self) -> Iterator[str]:
        """Yield the first name, then wait (at most 2 s) before the rest."""
        names = super().__iter__()
        yield next(names)
        self.reading.set()
        self.go_on.wait(2)
        yield from names


async def test_a_removal_that_fails_while_the_leftovers_are_tried_again_waits_for_their_copy(
    volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R86 N5: removals add to the leftovers in worker threads while the retry copies them in another; neither breaks.

    Without a lock the retry's copy fails with "Set changed size during iteration", and the retry comes 300 s later.
    With it, the removal waits for the copy, then adds its folder for the next retry.
    """
    sup = Supervisor(_settings(volume), use_slots=False)
    leftovers = _SlowSet("a" * 32, "b" * 32)
    sup._leftovers = leftovers
    monkeypatch.setattr(Supervisor, "_rmtree", lambda self, name, parent, *, quiet=False: False)
    request = _request()
    job = await asyncio.to_thread(sup._set_up, request, None, sup._bounded(request.limits))

    retry = asyncio.create_task(asyncio.to_thread(sup._remove_leftovers))
    await asyncio.to_thread(leftovers.reading.wait, 5)
    removal = asyncio.create_task(asyncio.to_thread(sup._remove, job))
    await asyncio.sleep(0.2)  # the removal adds its folder now, or waits for the copy
    leftovers.go_on.set()

    assert await retry == 2  # the two names it copied are on no disk, so they are gone
    assert await removal is False
    assert sorted(leftovers) == [request.job_id]


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_a_job_cannot_open_its_folder_to_another_slot(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R76: the job folder is root's, 0710; a job that opens ``home/`` and ``out/`` to all opens them to nobody."""
    _chat(root_volume, {"q.csv": b"q"})
    _two_job_runner(
        monkeypatch,
        root_volume,
        """
        def before():
            job = spec_path.parent
            mine = {
                "chmod_job": attempt(lambda: os.chmod(job, 0o777)),
                "list_job": attempt(lambda: os.listdir(job)),
                "add_to_job": attempt(lambda: (job / "x").write_text("x")),
                "chmod_home": attempt(lambda: os.chmod(job / "home", 0o777)),
                "chmod_out": attempt(lambda: os.chmod("out", 0o777)),
            }
            (job / "home" / "secret").write_text("user a's figures")
            os.chmod(job / "home" / "secret", 0o666)
            return mine
        def look():
            return {
                "their_secret": attempt(lambda: (others / "home" / "secret").read_text()),
                "their_out": attempt(lambda: os.listdir(others / "workspace" / spec["chat_id"] / "out")),
                "their_job": attempt(lambda: os.listdir(others)),
            }
        """,
    )
    first, second = _crossed(_request(input_files=["q.csv"]), _request(input_files=["q.csv"]))

    answers = await _two_jobs(Supervisor(_settings(root_volume)), first, second, root_volume)

    for answer in answers:
        assert answer.status == "ok", answers
        assert answer.result["mine"] == {
            "chmod_job": "denied",
            "list_job": "denied",
            "add_to_job": "denied",
            "chmod_home": "ok",
            "chmod_out": "ok",
        }, answers
        assert {key: answer.result.get(key) for key in ("their_secret", "their_out", "their_job")} == dict.fromkeys(
            ("their_secret", "their_out", "their_job"), "denied"
        ), answers


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_a_jobs_processes_together_are_held_to_its_memory(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R75: each process stays under ``RLIMIT_AS``, but three of them together pass the job's memory, and it stops.

    Private memory on a real kernel: each child's own pages are in its resident set, and the sum of the slot's
    processes passes the job's share.
    """
    _chat(root_volume)
    _runner(
        monkeypatch,
        """
        import time
        for _ in range(3):
            if os.fork() == 0:
                held = bytearray(120 * 1024 * 1024)
                for at in range(0, len(held), 4096):
                    held[at] = 1
                time.sleep(60)
                os._exit(0)
        time.sleep(60)
        """,
    )
    began = time.monotonic()

    answer = await Supervisor(_settings(root_volume, WORKBENCH_JOB_MEMORY_MB="256")).run_job(_request())

    assert (answer.status, answer.message) == (
        "killed",
        "the job was stopped: its processes used more than 256 MB of memory together",
    )
    assert time.monotonic() - began < scaled(40)


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_segments_a_job_filled_and_left_to_its_children_count_against_it(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R85: each child keeps a SysV segment its parent filled and then detached; no resident set holds those pages.

    Each segment is attached (``nattch`` 1, the child's), and before R85 the watchdog left attached segments out. Each
    process stays under ``RLIMIT_AS``; the three segments together pass the job's memory, and it stops.
    """
    _chat(root_volume)
    _runner(
        monkeypatch,
        """
        import ctypes, time
        libc = ctypes.CDLL(None, use_errno=True)
        libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
        libc.shmat.restype = ctypes.c_void_p
        libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        libc.shmdt.argtypes = [ctypes.c_void_p]
        size = 100 * 1024 * 1024
        for _ in range(3):
            ident = libc.shmget(0, size, 0o1600)
            at = libc.shmat(ident, None, 0)
            ctypes.memset(at, 1, size)
            if os.fork() == 0:
                time.sleep(60)
                os._exit(0)
            libc.shmdt(at)
        time.sleep(60)
        """,
    )
    limits = Limits(wall_s=30, cpu_s=30, memory_mb=1024, file_mb=50)
    began = time.monotonic()

    answer = await Supervisor(_settings(root_volume, WORKBENCH_JOB_MEMORY_MB="256")).run_job(_request(limits=limits))

    assert (answer.status, answer.message) == (
        "killed",
        "the job was stopped: its processes used more than 256 MB of memory together",
    )
    assert time.monotonic() - began < scaled(20)


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
@pytest.mark.parametrize(
    ("room_mb", "pause_s", "status"),
    [(64, 0.0, "error"), (None, 0.5, "timeout")],
    ids=["the-floor-first", "the-wall-time-first"],
)
async def test_as_root_a_job_that_writes_files_it_removed_ends_by_the_floor_or_its_wall_time(
    room_mb: int | None, pause_s: float, status: str, root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R84: files a job removes and keeps open are in no folder the watchdog walks, but they fill the volume.

    Each of its processes may hold 256 such files of 50 MB, 64 processes a job. The floor is set 64 MB below what the
    volume has free now, so the job reaches it fast; or out of reach, with a job that writes slowly, which ends at its
    wall time instead.
    """
    st = os.statvfs(root_volume)
    if room_mb is None:
        floor = 0
    else:
        floor = st.f_bavail * st.f_frsize - room_mb * MB
    monkeypatch.setattr(supervisor, "free_space_floor", lambda size: floor)
    _chat(root_volume)
    _runner(
        monkeypatch,
        f"""
        import time
        chunk = os.urandom(4 * 1024 * 1024)
        held = []
        while True:
            name = pathlib.Path(os.environ["TMPDIR"], f"gone{{len(held)}}")
            fd = os.open(name, os.O_WRONLY | os.O_CREAT, 0o600)
            os.unlink(name)
            held.append(fd)
            for _ in range(12):
                os.write(fd, chunk)
                time.sleep({pause_s})
        """,
    )
    limits = Limits(wall_s=5, cpu_s=30, memory_mb=1024, file_mb=50)
    began = time.monotonic()

    answer = await Supervisor(_settings(root_volume)).run_job(_request(limits=limits))

    assert answer.status == status, answer
    if status == "error":
        assert answer.result == {"error": "storage_full: storage is full"}
        assert time.monotonic() - began < scaled(4)
    assert _entries(root_volume / "jobs") == []


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_a_job_that_lowers_its_oom_score_is_stopped(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R86 N7: a job may lower its score from 1000 to the container's floor; the watchdog then stops it."""
    _chat(root_volume)
    _runner(
        monkeypatch,
        """
        import time
        if os.fork() == 0:
            pathlib.Path("/proc/self/oom_score_adj").write_text("0")
            time.sleep(60)
            os._exit(0)
        time.sleep(60)
        """,
    )
    limits = Limits(wall_s=30, cpu_s=30, memory_mb=1024, file_mb=50)
    began = time.monotonic()

    answer = await Supervisor(_settings(root_volume)).run_job(_request(limits=limits))

    assert (answer.status, answer.message) == (
        "killed",
        "the job was stopped: one of its processes lowered its OOM score",
    )
    assert time.monotonic() - began < scaled(20)


@pytest.fixture
def dev_zero_closed() -> Iterator[None]:
    """``/dev/zero`` as the supervisor leaves it at start (``prepare_host``), and as it was after the test.

    Only in the executor image (``IN_THE_IMAGE``): its ``/dev`` is the container's own, never a host's.
    """
    assert IN_THE_IMAGE
    before = stat.S_IMODE(os.lstat("/dev/zero").st_mode)
    assert supervisor._close_dev_zero()
    try:
        yield
    finally:
        os.chmod("/dev/zero", before)


@pytest.mark.skipif(not IN_THE_IMAGE, reason="the executor image's own /dev/zero, as root")
async def test_as_root_a_read_only_shared_map_of_dev_zero_holds_no_memory(
    root_volume: Path, dev_zero_closed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R88: with ``/dev/zero`` closed, a slot gets only a read-only descriptor; its shared map holds nothing when read.

    Measured in the container's cgroup (``memory.stat`` ``shmem``) and the process's ``RssShmem``, while the map is
    held. The same job fills a SysV segment of the same size by reading it, which both counters see: a shared map of
    ``/dev/zero`` opened for writing would do the same (shmem allocates a page on a read fault too).
    """
    _chat(root_volume)
    _runner(
        monkeypatch,
        """
        import ctypes, mmap
        size, page = 200 * 1024 * 1024, mmap.PAGESIZE
        def shmem():
            for line in open("/sys/fs/cgroup/memory.stat"):
                key, value = line.split()
                if key == "shmem":
                    return int(value)
        def rss_shmem():
            for line in open("/proc/self/status"):
                if line.startswith("RssShmem:"):
                    return int(line.split()[1]) * 1024
        def read_all(view):
            return sum(view[at] for at in range(0, size, page))
        try:
            os.open("/dev/zero", os.O_RDWR)
            writable = "ok"
        except PermissionError:
            writable = "denied"
        fd = os.open("/dev/zero", os.O_RDONLY)
        before = shmem()
        zero = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ)
        read = read_all(zero)
        held = (shmem() - before, rss_shmem())
        zero.close()
        libc = ctypes.CDLL(None, use_errno=True)
        libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
        libc.shmat.restype = ctypes.c_void_p
        libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        before = shmem()
        at = libc.shmat(libc.shmget(0, size, 0o1600), None, 0)
        read_all((ctypes.c_ubyte * size).from_address(at))
        segment = (shmem() - before, rss_shmem())
        result = {"writable": writable, "read": read, "zero": held, "segment": segment}
        """,
    )

    answer = await Supervisor(_settings(root_volume)).run_job(_request())

    assert answer.status == "ok", answer
    mb = 1024 * 1024
    (zero_shmem, zero_rss), (segment_shmem, segment_rss) = answer.result["zero"], answer.result["segment"]
    assert (answer.result["writable"], answer.result["read"]) == ("denied", 0)
    assert zero_shmem < 16 * mb and zero_rss < 16 * mb, answer.result
    assert segment_shmem > 180 * mb and segment_rss > 180 * mb, answer.result


@pytest.fixture
def dev_zero_open() -> Iterator[None]:
    """``/dev/zero`` writable by all, as on a host where the supervisor cannot close it (userns-remap, rootless)."""
    assert IN_THE_IMAGE
    before = stat.S_IMODE(os.lstat("/dev/zero").st_mode)
    os.chmod("/dev/zero", 0o666)
    try:
        yield
    finally:
        os.chmod("/dev/zero", before)


@pytest.mark.skipif(not IN_THE_IMAGE, reason="the executor image's own /dev/zero, as root")
async def test_as_root_shared_memory_a_job_hides_where_dev_zero_stays_writable_stops_it(
    root_volume: Path, dev_zero_open: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R90: the job fills a writable shared map of ``/dev/zero``, leaves it to a child and unmaps it: no count shows it.

    The container's ``shmem`` does, past the margin: the job stops within a pass or two, refused ``hidden_memory``.
    Before it, the idle container is far below the margin (its baseline is 0 bytes).
    """
    idle = supervisor.unattributed_shmem()
    _chat(root_volume)
    _runner(
        monkeypatch,
        """
        import mmap, time
        size = 200 * 1024 * 1024
        fd = os.open("/dev/zero", os.O_RDWR)
        zero = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        sum(zero[at] for at in range(0, size, mmap.PAGESIZE))
        if os.fork() == 0:
            time.sleep(60)
            os._exit(0)
        zero.close()
        os.close(fd)
        time.sleep(60)
        """,
    )
    limits = Limits(wall_s=30, cpu_s=30, memory_mb=1024, file_mb=50)
    began = time.monotonic()

    answer = await Supervisor(_settings(root_volume)).run_job(_request(limits=limits))

    assert idle < supervisor.SHMEM_MARGIN // 4, idle
    assert (answer.status, answer.result) == ("error", {"error": "hidden_memory: memory outside any job's count"})
    assert time.monotonic() - began < scaled(10)
    assert supervisor.unattributed_shmem() < supervisor.SHMEM_MARGIN // 4  # the slot's cleanup freed it


@pytest.mark.skipif(not IN_THE_IMAGE, reason="the executor image's own /dev/zero, as root")
async def test_as_root_a_job_has_no_socket_and_its_libraries_still_work(
    root_volume: Path, dev_zero_closed: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R76, R81, R85: no socket, no shared anonymous memory; the image's libraries, threads, asyncio and ctypes work.

    No Internet, Unix or netlink socket, no abstract name, no memfd, no shared anonymous map, and ``/dev/zero`` cannot
    be opened for writing. A Unix pair, a private anonymous map, a read-only shared map of ``/dev/zero``, pandas,
    numpy, matplotlib, openpyxl, python-docx, threads, asyncio and a ctypes callback all work; the OOM score is 1000.
    """
    _chat(root_volume)
    _runner(
        monkeypatch,
        """
        import asyncio, ctypes, errno, io, mmap, socket, threading
        def attempt(action):
            try:
                action()
                return "ok"
            except OSError as exc:
                return errno.errorcode.get(exc.errno, str(exc.errno))
        def abstract():
            one, two = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
            one.bind(b"\\0workbench-test")
        def passcred():
            one, two = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
            one.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        libc = ctypes.CDLL(None, use_errno=True)
        def secret():
            if libc.syscall(447, 0) < 0:
                raise OSError(ctypes.get_errno(), "memfd_secret")
        def zero_shared(prot):
            fd = os.open("/dev/zero", os.O_RDONLY)
            try:
                mmap.mmap(fd, 4096, mmap.MAP_SHARED, prot)
            finally:
                os.close(fd)
        async def nothing():
            return "ok"
        import numpy, pandas, matplotlib.pyplot as plt, openpyxl, docx
        figure = io.BytesIO()
        plt.plot([1, 2, 3])
        plt.savefig(figure, format="png")
        book = openpyxl.Workbook()
        book.active["A1"] = 3
        sheet = io.BytesIO()
        book.save(sheet)
        letter = docx.Document()
        letter.add_paragraph("harbour report")
        page = io.BytesIO()
        letter.save(page)
        callback = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int)(lambda x: x * 2)
        done = []
        worker = threading.Thread(target=lambda: done.append(int(numpy.linalg.inv(numpy.eye(3) * 2).trace() * 4)))
        worker.start()
        worker.join()
        result = {
            "inet": attempt(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM)),
            "inet6": attempt(lambda: socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)),
            "unix": attempt(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)),
            "netlink": attempt(lambda: socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 2)),
            "abstract": attempt(abstract),
            "passcred": attempt(passcred),
            "memfd": attempt(lambda: os.memfd_create("x")),
            "memfd_secret": attempt(secret),
            "shared_anonymous": attempt(lambda: mmap.mmap(-1, 4096)),
            "dev_zero_for_writing": attempt(lambda: os.close(os.open("/dev/zero", os.O_RDWR))),
            "dev_zero_shared_writable": attempt(lambda: zero_shared(mmap.PROT_READ | mmap.PROT_WRITE)),
            "pair": attempt(lambda: socket.socketpair()),
            "private_anonymous": attempt(lambda: mmap.mmap(-1, 4096, flags=mmap.MAP_PRIVATE)),
            "dev_zero_shared_readable": attempt(lambda: zero_shared(mmap.PROT_READ)),
            "asyncio": asyncio.run(nothing()),
            "pandas": int(pandas.DataFrame({"a": [1, 2, 3]})["a"].sum()),
            "libraries": [figure.getvalue()[1:4].decode(), sheet.getvalue()[:2].decode(), page.getvalue()[:2].decode()],
            "thread_numpy": done,
            "callback": callback(21),
            "oom_score_adj": pathlib.Path("/proc/self/oom_score_adj").read_text().strip(),
        }
        """,
    )

    answer = await Supervisor(_settings(root_volume)).run_job(_request())

    assert answer.status == "ok", answer
    refused = (
        "inet",
        "inet6",
        "unix",
        "netlink",
        "abstract",
        "passcred",
        "memfd",
        "memfd_secret",
        "shared_anonymous",
        "dev_zero_for_writing",
        "dev_zero_shared_writable",
    )
    assert {key: answer.result[key] for key in refused} == dict.fromkeys(refused, "EACCES")
    works = ("pair", "private_anonymous", "dev_zero_shared_readable", "asyncio")
    assert {key: answer.result[key] for key in works} == dict.fromkeys(works, "ok")
    assert {
        key: answer.result[key] for key in ("pandas", "libraries", "thread_numpy", "callback", "oom_score_adj")
    } == {
        "pandas": 6,
        "libraries": ["PNG", "PK", "PK"],
        "thread_numpy": [6],
        "callback": 42,
        "oom_score_adj": "1000",
    }


@pytest.mark.skipif(
    not (ROOT_ON_LINUX and Path("/workspace").is_symlink()), reason="the executor image's own files, as root"
)
def test_the_executor_image_has_no_setuid_or_setgid_program() -> None:
    """R1 M-1: the image strips them; ``no-new-privileges`` would keep them inert, and the supervisor requires it."""
    found = subprocess.run(
        ["find", "/", "-xdev", "-perm", "/6000", "-type", "f"], capture_output=True, text=True, check=False
    )
    assert found.stdout.split() == []
    assert not Path("/app/contract").exists() and not Path("/app/docs").exists()  # R1 M-10: .dockerignore


class _Limits:
    """A stand-in for ``getrlimit`` and ``setrlimit`` of open files, starting at ``soft`` and ``hard``."""

    def __init__(self, soft: int, hard: int) -> None:
        """Start at the given limits."""
        self.soft, self.hard = soft, hard
        self.calls: list[tuple[int, tuple[int, int]]] = []

    def get(self, which: int) -> tuple[int, int]:
        """The limits now."""
        return self.soft, self.hard

    def set(self, which: int, limits: tuple[int, int]) -> None:
        """Take the new limits, as the kernel does up to the hard one."""
        self.calls.append((which, limits))
        self.soft = limits[0]


def test_the_supervisor_raises_its_soft_limit_of_open_files_to_the_hard_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watchdog holds a descriptor per level of a running job's folders; a host may start containers at 1024."""
    limits = _Limits(1024, 524288)
    monkeypatch.setattr(supervisor.resource, "getrlimit", limits.get)
    monkeypatch.setattr(supervisor.resource, "setrlimit", limits.set)

    assert supervisor._open_files_problem(4) is None

    assert limits.calls == [(supervisor.resource.RLIMIT_NOFILE, (524288, 524288))]


@pytest.mark.parametrize(("slots", "hard"), [(4, 1024), (16, 4096)])
def test_a_supervisor_that_may_open_too_few_files_for_its_slots_refuses_to_start(
    slots: int, hard: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R86 N3: every slot's walk of its job folder, one descriptor per level, and a margin must fit the hard limit."""
    limits = _Limits(hard, hard)
    monkeypatch.setattr(supervisor.resource, "getrlimit", limits.get)
    monkeypatch.setattr(supervisor.resource, "setrlimit", limits.set)
    need = slots * (supervisor.MAX_JOB_DEPTH + 2) + supervisor.NOFILE_MARGIN

    monkeypatch.setattr(supervisor, "_close_dev_zero", lambda path: True)

    problem = supervisor.prepare_host(_settings(Path("/nowhere"), WORKBENCH_EXECUTOR_SLOTS=str(slots)))

    assert problem == f"the supervisor may open {hard} files, fewer than the {need} its {slots} slots need"


def _mountinfo_of(tmp_path: Path, *points: str) -> Path:
    """A ``/proc/self/mountinfo`` whose mounts are at ``points``."""
    path = tmp_path / "mountinfo-of"
    path.write_text("".join(f"{n} 1 0:{n} / {point} rw - tmpfs tmpfs rw\n" for n, point in enumerate(points, 20)))
    return path


def test_dev_zero_is_closed_to_others_writes_at_start(tmp_path: Path) -> None:
    """R85: a node writable by all becomes ``0644``; one that already is stays; a missing one is no route at all."""
    node = tmp_path / "zero"
    node.write_bytes(b"")
    node.chmod(0o666)
    mounts = _mountinfo_of(tmp_path, "/", "/dev")

    assert supervisor._close_dev_zero(node, root=ME[0], mountinfo=mounts) is True
    assert _mode(node) == 0o644
    assert supervisor._close_dev_zero(node, root=ME[0], mountinfo=mounts) is True
    assert supervisor._close_dev_zero(tmp_path / "absent", root=ME[0], mountinfo=mounts) is True


def test_a_dev_zero_that_cannot_be_closed_is_left_as_it_is_with_a_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """R88: under userns-remap or rootless Docker the node is the host's, bind-mounted and not root's; never changed.

    A link, another owner, a mount of its own (or a mount table that cannot be read), or a mode that cannot be
    changed: the node stays as it is, a warning says why by a fixed text or a type, and nothing refuses to start.
    """
    node = tmp_path / "zero"
    node.write_bytes(b"")
    node.chmod(0o666)
    link = tmp_path / "link"
    link.symlink_to(node)

    def refuse(path: Any, mode: int) -> None:
        raise PermissionError(1, "Operation not permitted", str(path))

    with caplog.at_level(logging.WARNING, logger="workbench.executor"):
        assert supervisor._close_dev_zero(link, root=ME[0], mountinfo=_mountinfo_of(tmp_path, "/")) is False
        assert supervisor._close_dev_zero(node, root=ME[0] + 1, mountinfo=_mountinfo_of(tmp_path, "/")) is False
        assert supervisor._close_dev_zero(node, root=ME[0], mountinfo=_mountinfo_of(tmp_path, "/", str(node))) is False
        assert supervisor._close_dev_zero(node, root=ME[0], mountinfo=tmp_path / "unreadable") is False
        monkeypatch.setattr(supervisor.os, "chmod", refuse)
        assert supervisor._close_dev_zero(node, root=ME[0], mountinfo=_mountinfo_of(tmp_path, "/")) is False

    assert _mode(node) == 0o666
    open_route = "/dev/zero stays writable by the slots, who may map shared memory through it"
    assert caplog.messages == [
        f"{open_route}: it is not root's own node (a user-namespaced daemon?)",
        f"{open_route}: it is not root's own node (a user-namespaced daemon?)",
        f"{open_route}: it is a mount of its own, the host's node, which the supervisor leaves alone",
        f"{open_route}: it is a mount of its own, the host's node, which the supervisor leaves alone",
        f"{open_route}: its mode could not be changed (PermissionError)",
    ]
    assert str(tmp_path) not in caplog.text


def test_a_dev_zero_that_cannot_be_closed_never_stops_the_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R88: the change is best effort. The chmod is refused here, so this host's node is never changed."""
    limits = _Limits(8192, 8192)
    monkeypatch.setattr(supervisor.resource, "getrlimit", limits.get)
    monkeypatch.setattr(supervisor.resource, "setrlimit", limits.set)

    def refuse(path: Any, mode: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(supervisor.os, "chmod", refuse)
    memory_stat = tmp_path / "memory.stat"
    memory_stat.write_text("shmem 0\n")
    own = _own_cgroup(tmp_path)

    assert supervisor.prepare_host(_settings(Path("/nowhere")), memory_stat=memory_stat, self_cgroup=own) is None


@pytest.mark.skipif(not IN_THE_IMAGE, reason="the executor image's own /dev, as root")
def test_as_root_this_containers_dev_zero_can_be_closed(dev_zero_closed: None) -> None:
    """R85: Docker's ``/dev`` is the container's tmpfs and the node its own (no mount of its own); root changes it."""
    st = os.lstat("/dev/zero")
    assert stat.S_ISCHR(st.st_mode) and (st.st_uid, stat.S_IMODE(st.st_mode)) == (0, 0o644)
    assert "/dev/zero" not in (supervisor._mount_points(Path("/proc/self/mountinfo")) or {"/dev/zero"})


def test_the_open_files_four_slots_need_pass_a_soft_limit_of_1024() -> None:
    """Why the soft limit is raised at all: four slots at 256 levels already need more than many hosts give."""
    assert supervisor.open_files_needed(4) > 1024
