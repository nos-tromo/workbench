"""The executor's supervisor: copy-in, a run in a slot, validated outputs out, and a slot pool that fails closed."""

import asyncio
import contextlib
import dataclasses
import logging
import os
import socket
import stat
import sys
import textwrap
import uuid
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from workbench.config import Settings, load_settings
from workbench.executor import supervisor
from workbench.executor.prepare_volume import Owners, prepare_volume
from workbench.executor.sandbox import Slot
from workbench.executor.supervisor import MAX_OUTPUTS, Supervisor, make_slots, startup_problem
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
spec = json.loads(spec_path.read_text())
result = {}
def tree(top):
    return sorted(str(p.relative_to(top)) for p in pathlib.Path(top).rglob("*"))
"""
EPILOGUE = """
spec_path.with_name("result.json").write_text(json.dumps(result))
"""


def _settings(data: Path, **env: str) -> Settings:
    """Executor settings for a volume at ``data``, with ``env`` on top."""
    values = {
        "WORKBENCH_DATA_DIR": str(data),
        "WORKBENCH_EXECUTOR_SOCKET": str(data / "executor.sock"),
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
    monkeypatch.setattr(supervisor, "job_argv", lambda spec: [sys.executable, "-I", "-c", code, str(spec)])


def _entries(folder: Path) -> list[str]:
    """Names in ``folder``."""
    return sorted(p.name for p in folder.iterdir())


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
    """Working folder and inputs are read-only to the job; ``out/``, ``tmp/`` and the job folder are private."""
    _chat(volume, {"q.csv": b"q", "reports/s.txt": b"s"})
    _runner(
        monkeypatch,
        """
        def mode(p):
            return oct(os.stat(p, follow_symlinks=False).st_mode & 0o7777)
        job = spec_path.parent
        result = {
            "cwd": os.getcwd() == str(job / "workspace" / spec["chat_id"]),
            "home": os.environ["HOME"] == os.getcwd(),
            "tmp": os.environ["TMPDIR"] == str(job / "tmp"),
            "modes": [mode(job), mode(job / "workspace"), mode("."), mode("q.csv"), mode("reports"),
                      mode("reports/s.txt"), mode("out"), mode(job / "tmp"), mode(spec_path)],
            "spec": spec,
            "top": sorted(os.listdir(job)),
        }
        """,
    )
    request = _request(input_files=["q.csv", "reports/s.txt"], args={"x": 1}, max_chars=100)

    answer = await Supervisor(_settings(volume), use_slots=False).run_job(request)

    assert answer.result["cwd"] and answer.result["home"] and answer.result["tmp"]
    assert answer.result["modes"] == ["0o700", "0o755", "0o755", "0o444", "0o755", "0o444", "0o700", "0o700", "0o444"]
    assert answer.result["spec"] == {
        "tool": "script",
        "args": {"x": 1},
        "chat_id": CHAT,
        "max_chars": 100,
        "file_mb": 50,
    }
    assert answer.result["top"] == ["spec.json", "tmp", "workspace"]


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
        'spec_path.with_name("result.json").write_text("not json")\nsys.exit(0)',
        'spec_path.with_name("result.json").write_text("[1, 2]")\nsys.exit(0)',
        'spec_path.with_name("result.json").write_bytes(b"{\\"x\\": \\"\\xff\\"}")\nsys.exit(0)',
        'spec_path.with_name("result.json").write_text("[" * 50000)\nsys.exit(0)',
        'os.mkfifo(spec_path.with_name("result.json"))\nsys.exit(0)',
        'os.mkdir(spec_path.with_name("result.json"))\nsys.exit(0)',
        'os.symlink(spec["args"]["decoy"], spec_path.with_name("result.json"))\nsys.exit(0)',
        'spec_path.with_name("result.json").write_text(json.dumps({"x": "y" * 70000}))\nsys.exit(0)',
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
    _runner(monkeypatch, 'spec_path.with_name("result.json").write_text("{}")\nsys.exit(3)')

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


async def test_a_job_folder_that_cannot_be_removed_is_closed_to_the_slot(
    volume: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If removal fails, the folder is handed back to root, mode 0700, so the slot's next job cannot enter it."""
    _chat(volume)
    _runner(monkeypatch, "pass")
    monkeypatch.setattr(supervisor.shutil, "rmtree", lambda path, **kwargs: kwargs["onexc"](os.rmdir, path, OSError()))
    request = _request()

    with caplog.at_level(logging.WARNING, logger="workbench.executor"):
        answer = await Supervisor(_settings(volume), use_slots=False).run_job(request)

    left = volume / "jobs" / request.job_id
    assert answer.status == "ok"
    assert (_ids(left), _mode(left)) == (ME, 0o700)
    assert [r.getMessage() for r in caplog.records] == ["a job folder could not be removed"]


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
    ) -> tuple[str, int]:
        """Leave a result as a job would, then end as told."""
        Path(argv[-1]).with_name("result.json").write_text('{"leaked": true}')
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


@pytest.mark.parametrize("memory", [None, "max", str(4 * 1536 * MB), str(8 * 1536 * MB)])
def test_a_well_set_up_host_passes_the_start_up_checks(memory: str | None, tmp_path: Path) -> None:
    """Root, every slot user present, and a memory limit of at least slots times job memory (or none)."""
    settings = _settings(tmp_path, WORKBENCH_EXECUTOR_SLOTS="4")
    memory_max = tmp_path / "absent" if memory is None else _memory_max(tmp_path, memory)

    problem = startup_problem(settings, passwd=_passwd(tmp_path, range(20001, 20017)), memory_max=memory_max, euid=0)

    assert problem is None


def test_too_little_container_memory_refuses_to_start(tmp_path: Path) -> None:
    """Four slots of 1536 MB need at least 6 GiB in the container's cgroup."""
    settings = _settings(tmp_path, WORKBENCH_EXECUTOR_SLOTS="4")

    problem = startup_problem(
        settings,
        passwd=_passwd(tmp_path, range(20001, 20017)),
        memory_max=_memory_max(tmp_path, str(4 * 1536 * MB - 1)),
        euid=0,
    )

    assert problem == "the container's memory limit is below 4 slots of 1536 MB"


def test_missing_slot_users_refuse_to_start(tmp_path: Path) -> None:
    """Every slot uid must be a user the image created."""
    settings = _settings(tmp_path, WORKBENCH_EXECUTOR_SLOTS="4")

    problem = startup_problem(
        settings, passwd=_passwd(tmp_path, range(20001, 20004)), memory_max=tmp_path / "absent", euid=0
    )

    assert problem == "1 of the 4 slot users is missing from the image"


def test_a_pool_beyond_its_sixteen_uids_refuses_to_start(tmp_path: Path) -> None:
    """``slot_uid_base + executor_slots - 1`` may not pass ``slot_uid_base + 15``."""
    settings = dataclasses.replace(_settings(tmp_path), executor_slots=17)

    problem = startup_problem(
        settings, passwd=_passwd(tmp_path, range(20001, 20018)), memory_max=tmp_path / "absent", euid=0
    )

    assert problem is not None
    assert "at most 16 slots" in problem


def test_a_supervisor_that_is_not_root_refuses_to_start(tmp_path: Path) -> None:
    """Without root it cannot switch to the slot uids."""
    problem = startup_problem(
        _settings(tmp_path), passwd=_passwd(tmp_path, range(20001, 20017)), memory_max=tmp_path / "x", euid=1000
    )

    assert problem == "the supervisor must run as root"


def test_main_refuses_to_start_with_one_short_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``python -m workbench.executor.supervisor`` exits 1 with the reason on stderr."""
    monkeypatch.setenv("WORKBENCH_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(supervisor, "startup_problem", lambda settings: "the supervisor must run as root")

    assert supervisor.main() == 1

    assert capsys.readouterr().err == "workbench executor: refusing to start: the supervisor must run as root\n"


def test_main_refuses_a_bad_setting(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A malformed environment stops it the same way."""
    monkeypatch.setenv("WORKBENCH_EXECUTOR_SLOTS", "99")

    assert supervisor.main() == 1

    err = capsys.readouterr().err
    assert err.startswith("workbench executor: WORKBENCH_EXECUTOR_SLOTS")
    assert err.count("\n") == 1


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
    """A prepared volume, owned by the test runner, with a short path for the socket."""
    prepare_volume(public_tmp, Owners(root=ME, api=ME))
    _chat(public_tmp)
    yield public_tmp


async def test_serve_answers_one_request_per_connection(served: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One request line in, one result line out, on a socket only its owner and group may use."""
    _runner(monkeypatch, 'result = {"tool": spec["tool"]}')
    sup = Supervisor(_settings(served), use_slots=False)
    request = _request()

    async with _serving(sup):
        answer = decode_result(await _ask(served / "executor.sock", encode(request)))
        again = decode_result(await _ask(served / "executor.sock", encode(_request())))

    assert (answer.job_id, answer.status, answer.result) == (request.job_id, "ok", {"tool": "script"})
    assert again.status == "ok"
    assert _mode(served / "executor.sock") == 0o660
    assert stat.S_ISSOCK(os.lstat(served / "executor.sock").st_mode)


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
            answer = decode_result(await _ask(served / "executor.sock", line))

    assert (answer.status, answer.message, answer.result, answer.outputs) == ("error", "malformed request", {}, [])
    assert "quarterly" not in caplog.text
    assert "chat-1" not in caplog.text
    assert _entries(served / "jobs") == []


async def test_a_stale_socket_is_replaced(served: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A socket left by an earlier run is removed before the new one is bound."""
    _runner(monkeypatch, "pass")
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(served / "executor.sock"))
    stale.close()

    async with _serving(Supervisor(_settings(served), use_slots=False)):
        answer = decode_result(await _ask(served / "executor.sock", encode(_request())))

    assert answer.status == "ok"


async def test_something_else_in_the_sockets_place_stops_serve(served: Path) -> None:
    """A file or symlink where the socket belongs is not removed; the supervisor does not start."""
    (served / "executor.sock").write_text("not a socket")

    with pytest.raises(RuntimeError, match="not a socket"):
        await Supervisor(_settings(served), use_slots=False).serve()

    assert (served / "executor.sock").read_text() == "not a socket"


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
            await _ask(served / "executor.sock", encode(_request()))

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
            answer = decode_result(await _ask(served / "executor.sock", encode(_request())))

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
        [20001, 20001, "0o700"],
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
    sup = Supervisor(_settings(root_volume))

    async with _serving(sup):
        answer = decode_result(await _ask(root_volume / "executor.sock", b"not json\n"))

    assert answer.status == "error"
    assert (_ids(root_volume / "executor.sock"), _mode(root_volume / "executor.sock")) == ((0, 10001), 0o660)


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
