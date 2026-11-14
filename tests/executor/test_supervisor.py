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
import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.executor.conftest import IPC_SHAPE, SHARED_IPC_ONLY
from workbench.config import Settings, load_settings
from workbench.executor import supervisor
from workbench.executor.prepare_volume import Owners, prepare_volume
from workbench.executor.sandbox import Slot
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


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_two_running_jobs_cannot_reach_each_other(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Jobs in different slots cannot enter each other's folder, even knowing its id."""
    _chat(root_volume, {"q.csv": b"q"})
    _runner(
        monkeypatch,
        f"""
        import time
        other = pathlib.Path({str(root_volume / "jobs")!r}) / spec["args"]["other"]
        def attempt(action):
            try:
                action()
                return "ok"
            except PermissionError:
                return "denied"
        time.sleep(1)
        result = {{
            "list": attempt(lambda: os.listdir(other)),
            "read": attempt(lambda: (other / "workspace" / spec["chat_id"] / "q.csv").read_bytes()),
        }}
        """,
    )
    first, second = _request(input_files=["q.csv"]), _request(input_files=["q.csv"])
    first = first.model_copy(update={"args": {"other": second.job_id}})
    second = second.model_copy(update={"args": {"other": first.job_id}})
    sup = Supervisor(_settings(root_volume))

    answers = await asyncio.gather(sup.run_job(first), sup.run_job(second))

    assert [a.result for a in answers] == [{"list": "denied", "read": "denied"}] * 2


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


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_a_job_cannot_open_its_folder_to_another_slot(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R76: the job folder is root's, 0710; a job that opens ``home/`` and ``out/`` to all opens them to nobody."""
    _chat(root_volume, {"q.csv": b"q"})
    _runner(
        monkeypatch,
        f"""
        import time
        def attempt(action):
            try:
                action()
                return "ok"
            except PermissionError:
                return "denied"
        job = spec_path.parent
        mine = {{
            "chmod_job": attempt(lambda: os.chmod(job, 0o777)),
            "list_job": attempt(lambda: os.listdir(job)),
            "add_to_job": attempt(lambda: (job / "x").write_text("x")),
            "chmod_home": attempt(lambda: os.chmod(job / "home", 0o777)),
            "chmod_out": attempt(lambda: os.chmod("out", 0o777)),
        }}
        (job / "home" / "secret").write_text("user a's figures")
        os.chmod(job / "home" / "secret", 0o666)
        time.sleep(1)
        other = pathlib.Path({str(root_volume / "jobs")!r}) / spec["args"]["other"]
        result = {{
            "mine": mine,
            "their_secret": attempt(lambda: (other / "home" / "secret").read_text()),
            "their_out": attempt(lambda: os.listdir(other / "workspace" / spec["chat_id"] / "out")),
            "their_job": attempt(lambda: os.listdir(other)),
        }}
        """,
    )
    first, second = _request(input_files=["q.csv"]), _request(input_files=["q.csv"])
    first = first.model_copy(update={"args": {"other": second.job_id}})
    second = second.model_copy(update={"args": {"other": first.job_id}})

    sup = Supervisor(_settings(root_volume))

    answers = await asyncio.gather(sup.run_job(first), sup.run_job(second))

    for answer in answers:
        assert answer.status == "ok", answer
        assert answer.result["mine"] == {
            "chmod_job": "denied",
            "list_job": "denied",
            "add_to_job": "denied",
            "chmod_home": "ok",
            "chmod_out": "ok",
        }
        assert {key: answer.result[key] for key in ("their_secret", "their_out", "their_job")} == dict.fromkeys(
            ("their_secret", "their_out", "their_job"), "denied"
        )


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_a_jobs_processes_together_are_held_to_its_memory(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R75: each process stays under ``RLIMIT_AS``, but three of them together pass the job's memory, and it stops."""
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
    assert time.monotonic() - began < 40


@pytest.mark.skipif(not ROOT_ON_LINUX, reason="switching to slot uids needs root on Linux")
async def test_as_root_a_job_has_no_socket_and_its_libraries_still_work(
    root_volume: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R76: no Internet, Unix or netlink socket, no abstract name; a Unix pair, asyncio, pandas and the OOM score do."""
    _chat(root_volume)
    _runner(
        monkeypatch,
        """
        import asyncio, errno, socket
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
        async def nothing():
            return "ok"
        import pandas
        result = {
            "inet": attempt(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM)),
            "inet6": attempt(lambda: socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)),
            "unix": attempt(lambda: socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)),
            "netlink": attempt(lambda: socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 2)),
            "abstract": attempt(abstract),
            "passcred": attempt(passcred),
            "memfd": attempt(lambda: os.memfd_create("x")),
            "pair": attempt(lambda: socket.socketpair()),
            "asyncio": asyncio.run(nothing()),
            "pandas": int(pandas.DataFrame({"a": [1, 2, 3]})["a"].sum()),
            "oom_score_adj": pathlib.Path("/proc/self/oom_score_adj").read_text().strip(),
        }
        """,
    )

    answer = await Supervisor(_settings(root_volume)).run_job(_request())

    assert answer.status == "ok", answer
    refused = ("inet", "inet6", "unix", "netlink", "abstract", "passcred", "memfd")
    assert {key: answer.result[key] for key in refused} == dict.fromkeys(refused, "EACCES")
    assert {key: answer.result[key] for key in ("pair", "asyncio", "pandas", "oom_score_adj")} == {
        "pair": "ok",
        "asyncio": "ok",
        "pandas": 6,
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
