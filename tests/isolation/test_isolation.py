"""What isolates a job, checked on the running test stack: the executor's configuration, then jobs that try to escape.

The configuration tests read ``docker inspect`` and look from inside the running executor, so
they prove what the kernel enforces, not only what the compose file asks for. The tests that
run code go through the ``run_python`` tool, as the model would, on Ghostwriter's door or docint's.
"""

import base64
import io
import json
import stat
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import openpyxl
import pytest
from docx import Document

from tests.fixtures import make_templates as mt
from tests.isolation.conftest import (
    docker,
    environment,
    ghostwriter,
    ghostwriter_mcp,
    inspect,
    mcp_call,
    mcp_result,
    run_python,
    stdout_of,
)

pytestmark = pytest.mark.isolation

MIB = 1024 * 1024
CAPABILITIES = {"SETUID", "SETGID", "CHOWN", "FOWNER", "DAC_OVERRIDE", "KILL"}
# The bounding set of those six: CHOWN 0, DAC_OVERRIDE 1, FOWNER 3, KILL 5, SETGID 6, SETUID 7.
CAPABILITY_MASK = f"{(1 << 0) | (1 << 1) | (1 << 3) | (1 << 5) | (1 << 6) | (1 << 7):016x}"
PIDS_LIMIT = 320  # R19: 4 slots x 64 processes + 64 for the supervisor and its helpers
SLOT_UIDS = range(20001, 20017)
API_UID = 10001


def _capabilities(names: list[str] | None) -> set[str]:
    """Capability names as compose writes them, whichever form Docker reports."""
    return {name.upper().removeprefix("CAP_") for name in names or []}


def _options(text: str) -> set[str]:
    """The options of a tmpfs mount, in any order."""
    return set(text.split(","))


INSIDE = r"""
import json, os, pathlib, pwd, stat
def owner_and_mode(path):
    st = os.lstat(path)
    return {"uid": st.st_uid, "gid": st.st_gid, "mode": stat.S_IMODE(st.st_mode), "kind": stat.S_IFMT(st.st_mode)}
def status(pid):
    lines = pathlib.Path(f"/proc/{pid}/status").read_text().splitlines()
    return dict(line.split(":", 1) for line in lines if ":" in line)
def is_up(name):
    flags = pathlib.Path(f"/sys/class/net/{name}/flags")
    return flags.exists() and int(flags.read_text(), 16) & 1  # IFF_UP
def mounts():
    rows = (line.split() for line in pathlib.Path("/proc/self/mounts").read_text().splitlines())
    return {row[1]: [row[2], sorted(set(row[3].split(",")) & {"nosuid", "nodev", "noexec"})] for row in rows}
def argv(pid):
    try:
        return pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:  # the process ended after the listing
        return []
supervisors = [
    pid for pid in os.listdir("/proc") if pid.isdigit() and argv(pid)[1:3] == [b"-m", b"workbench.executor.supervisor"]
]
found = {
    "shm": owner_and_mode("/dev/shm"),
    "tmp": owner_and_mode("/tmp"),
    "run": owner_and_mode("/run/workbench"),
    "socket": owner_and_mode("/run/workbench/executor.sock"),
    "mounts": {path: mounts().get(path) for path in ("/tmp", "/dev/shm", "/dev/mqueue")},
    "queues_max": pathlib.Path("/proc/sys/fs/mqueue/queues_max").read_text().strip(),
    "shm_rmid_forced": pathlib.Path("/proc/sys/kernel/shm_rmid_forced").read_text().strip(),
    "up": sorted(name for name in os.listdir("/sys/class/net") if is_up(name)),
    "routes": pathlib.Path("/proc/net/route").read_text().splitlines()[1:],
    "ipv6_routes": sorted({row.split()[-1] for row in pathlib.Path("/proc/net/ipv6_route").read_text().splitlines()}),
    "workspace": os.readlink("/workspace"),
    "pid1": pathlib.Path("/proc/1/comm").read_text().strip(),
    "slot_shells": sorted({pwd.getpwuid(uid).pw_shell for uid in range(20001, 20017)}),
    "slot_groups": [pwd.getpwuid(uid).pw_gid for uid in range(20001, 20017)],
    "supervisors": [
        {key: " ".join(status(pid)[key].split()) for key in ("Uid", "Seccomp", "NoNewPrivs", "CapEff", "CapBnd")}
        for pid in supervisors
    ],
}
print(json.dumps(found))
"""


def test_executor_is_isolated_by_configuration(executor: str) -> None:
    """No network, six capabilities, a read-only root, ``init``, the pids and memory limits, R28's IPC shape, R30."""
    info = inspect(executor)
    host = info["HostConfig"]
    env = environment(info)
    slots = int(env.get("WORKBENCH_EXECUTOR_SLOTS", "4"))
    job_memory_mb = int(env.get("WORKBENCH_JOB_MEMORY_MB", "1536"))

    assert host["NetworkMode"] == "none"
    assert _capabilities(host["CapAdd"]) == CAPABILITIES
    assert _capabilities(host["CapDrop"]) == {"ALL"}
    assert host["Privileged"] is False
    assert host["ReadonlyRootfs"] is True
    assert host["Init"] is True
    assert host["PidsLimit"] == PIDS_LIMIT
    assert host["Memory"] >= slots * job_memory_mb * MIB
    assert host["NanoCpus"] > 0
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert not any("seccomp" in option or "apparmor=unconfined" in option for option in host["SecurityOpt"])
    assert host["IpcMode"] in ("private", "shareable")
    assert host["PidMode"] == ""
    assert {path: _options(options) for path, options in host["Tmpfs"].items()} == {
        "/dev/shm": {"size=1m", "mode=0755"},
        "/tmp": {"size=16m", "mode=0700"},
    }
    assert host["Sysctls"] == {"fs.mqueue.queues_max": "0", "kernel.shm_rmid_forced": "1"}
    assert info["Config"]["User"] == "0:0"
    assert [name for name in env if name.endswith("_WORKBENCH_KEY")] == []


def test_the_running_executor_has_what_its_configuration_asks_for(executor: str) -> None:
    """Seen from inside: root-only ``/dev/shm`` and ``/tmp``, no queues, loopback only, the supervisor confined."""
    found: dict[str, Any] = json.loads(docker("exec", executor, "python", "-I", "-c", INSIDE))

    folder = stat.S_IFDIR
    assert found["shm"] == {"uid": 0, "gid": 0, "mode": 0o755, "kind": folder}
    assert found["tmp"] == {"uid": 0, "gid": 0, "mode": 0o700, "kind": folder}
    assert (found["run"]["uid"], found["run"]["kind"], found["run"]["mode"] & 0o022) == (0, folder, 0)
    assert found["socket"] == {"uid": 0, "gid": API_UID, "mode": 0o660, "kind": stat.S_IFSOCK}
    sealed = ["nodev", "noexec", "nosuid"]
    assert found["mounts"] == {
        "/tmp": ["tmpfs", sealed],
        "/dev/shm": ["tmpfs", sealed],
        "/dev/mqueue": ["mqueue", sealed],
    }
    assert (found["queues_max"], found["shm_rmid_forced"]) == ("0", "1")
    # The kernel may add fallback tunnel devices to every network namespace; none is up.
    assert (found["up"], found["routes"], found["ipv6_routes"]) == (["lo"], [], ["lo"])
    assert found["workspace"] == "/proc/self/cwd/.."
    assert found["pid1"] == "docker-init"
    assert found["slot_shells"] == ["/usr/sbin/nologin"]
    assert found["slot_groups"] == list(SLOT_UIDS)
    assert found["supervisors"] == [
        {"Uid": "0 0 0 0", "Seccomp": "2", "NoNewPrivs": "1", "CapEff": CAPABILITY_MASK, "CapBnd": CAPABILITY_MASK}
    ]


def test_the_api_runs_unprivileged_on_its_network_alone(api_info: dict[str, Any], api: httpx.Client) -> None:
    """Uid 10001, no capability, a read-only root, one network where it is ``workbench-api``; ``/healthz`` answers."""
    host = api_info["HostConfig"]
    networks = api_info["NetworkSettings"]["Networks"]

    assert api_info["Config"]["User"] == f"{API_UID}:{API_UID}"
    assert _capabilities(host["CapDrop"]) == {"ALL"}
    assert not host["CapAdd"]
    assert host["ReadonlyRootfs"] is True
    assert "no-new-privileges:true" in host["SecurityOpt"]
    assert len(networks) == 1
    assert "workbench-api" in next(iter(networks.values()))["Aliases"]
    assert api.get("/healthz").json() == {"ok": True}


# --- jobs that try to escape -------------------------------------------------------------------


def test_no_network(api_info: dict[str, Any]) -> None:
    """A job reaches no address, not even by IP."""
    code = """
import socket
try:
    socket.create_connection(("1.1.1.1", 53), 2)
except OSError:
    print("no network")
else:
    print("connected")
"""
    assert stdout_of(run_python(api_info, code)) == "no network"


def test_other_workspaces_are_unreadable(api_info: dict[str, Any]) -> None:
    """The users' files are the api's alone."""
    code = """
import os
try:
    os.listdir("/data/workspaces")
except PermissionError:
    print("refused")
else:
    print("listed")
"""
    assert stdout_of(run_python(api_info, code)) == "refused"


FIND_THE_OTHER_JOB = """
import os, pathlib, time
mine = str(pathlib.Path.cwd().parents[1]).encode()
others = set()
deadline = time.monotonic() + 30
while not others and time.monotonic() < deadline:
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            argv = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\\0")
        except OSError:
            continue
        others |= {os.path.dirname(a).decode() for a in argv if a.endswith(b"/spec.json") and not a.startswith(mine)}
    time.sleep(0.2)
outcomes = []
for folder in sorted(others):
    try:
        os.listdir(folder)
        outcomes.append("listed")
    except PermissionError:
        outcomes.append("refused")
print(",".join(outcomes) or "none found")
time.sleep(3)
"""


def test_other_jobs_are_unreadable(api_info: dict[str, Any]) -> None:
    """Two jobs at once, each finding the other's folder from its command line: neither may list it."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        users = ["user-a", "user-b"]
        answers = list(pool.map(lambda user: run_python(api_info, FIND_THE_OTHER_JOB, user=user), users))

    assert [stdout_of(answer) for answer in answers] == ["refused", "refused"]


OPEN_UP_AND_LISTEN = """
import os, pathlib, socket, time
job = pathlib.Path.cwd().parents[1]
os.chmod(job / "home", 0o777)
os.chmod("out", 0o777)
(job / "home" / "marker").write_text("synthetic")
os.chmod(job / "home" / "marker", 0o666)
outcomes = []
try:
    socket.socket(socket.AF_INET, socket.SOCK_STREAM).bind(("127.0.0.1", 47000))
    outcomes.append("listening")
except PermissionError:
    outcomes.append("no socket")
mine = str(job).encode()
others = set()
deadline = time.monotonic() + 30
while not others and time.monotonic() < deadline:
    for pid in filter(str.isdigit, os.listdir("/proc")):
        try:
            argv = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\\0")
        except OSError:
            continue
        others |= {os.path.dirname(a).decode() for a in argv if a.endswith(b"/spec.json") and not a.startswith(mine)}
    time.sleep(0.2)
for folder in sorted(others):
    other = pathlib.Path(folder)
    for attempt in (lambda: (other / "home" / "marker").read_text(), lambda: os.listdir(other / "home")):
        try:
            attempt()
            outcomes.append("reached")
        except PermissionError:
            outcomes.append("refused")
print(",".join(outcomes))
time.sleep(3)
"""


def test_a_job_that_opens_its_folders_to_all_opens_them_to_no_other_job(api_info: dict[str, Any]) -> None:
    """R76: two jobs at once open their ``home/`` to everyone and try a loopback listener; neither reaches the other."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        users = ["user-a", "user-b"]
        answers = list(pool.map(lambda user: run_python(api_info, OPEN_UP_AND_LISTEN, user=user), users))

    assert [stdout_of(answer) for answer in answers] == ["no socket,refused,refused"] * 2


def test_inputs_are_read_only(api_info: dict[str, Any]) -> None:
    """The job's copy of an upload is root's; only ``out/`` takes writes."""
    _upload(api_info, "in.csv", b"quarter,figure\nq1,10\n")
    code = """
try:
    open("in.csv", "w").write("changed")
except PermissionError:
    print("input refused")
open("out/x", "w").write("synthetic")
print("output written")
"""
    assert stdout_of(run_python(api_info, code)).splitlines() == ["input refused", "output written"]


def test_no_secrets_in_environment(api_info: dict[str, Any]) -> None:
    """No caller's key is in a job's environment, by name or by value."""
    keys = {name: value for name, value in environment(api_info).items() if name.endswith("_WORKBENCH_KEY") and value}
    assert {"DOCINT_WORKBENCH_KEY", "GHOSTWRITER_WORKBENCH_KEY"} <= set(keys)
    env = json.loads(stdout_of(run_python(api_info, "import json, os; print(json.dumps(dict(os.environ)))")))

    assert not set(keys) & set(env)
    assert not any(key in value for key in keys.values() for value in env.values())


def test_fork_bomb_is_contained(api_info: dict[str, Any]) -> None:
    """The job ends as killed, error or timeout; the next job runs normally.

    At the slot's process limit ``fork`` fails, so the code itself usually ends in that error, which the answer
    carries as its traceback; else the executor stopped the job, on a resource limit or its wall time.
    """
    bomb = run_python(api_info, "import os\nwhile True:\n    os.fork()\n")

    if not bomb["isError"]:
        stderr = bomb["structuredContent"]["stderr"]
        assert stderr.rstrip().endswith("BlockingIOError: [Errno 11] Resource temporarily unavailable")
    else:
        detail = bomb["content"][0]["text"]
        assert (bomb["structuredContent"], detail.split(": ")[0]) == (
            {"reason": "not_completed"},
            "the code could not be completed",
        ), bomb
        assert "resource limit" in detail or "wall time" in detail, detail
    assert stdout_of(run_python(api_info, 'print("still here")')) == "still here"


#: Run inside the executor's container: what it holds now, and the shared memory no job's count shows.
SHMEM_NOW = (
    "from workbench.executor import sandbox, supervisor; "
    "print(sandbox.unattributed_shmem(), sandbox.shmem_charged(sandbox.MEMORY_STAT), supervisor.SHMEM_MARGIN)"
)


def test_the_running_executor_holds_no_shared_memory_outside_any_jobs_count(
    executor: str, api_info: dict[str, Any]
) -> None:
    """R90: the baseline the watchdog bounds, on the real stack: none idle, and none after a job that used numpy."""
    for _ in range(2):
        hidden, charged, margin = (int(word) for word in docker("exec", executor, "python", "-c", SHMEM_NOW).split())
        assert hidden < margin // 4, (hidden, charged)
        assert stdout_of(run_python(api_info, "import numpy\nprint(int(numpy.ones(10).sum()))")) == "10"


def test_memory_hog_is_killed_with_a_clear_message(api_info: dict[str, Any]) -> None:
    """More than the job's memory ends the job, and the answer says it was memory."""
    hog = run_python(api_info, "blob = bytearray(8 * 1024 ** 3)\nprint(len(blob))")

    assert "memory" in json.dumps(hog).lower()
    assert stdout_of(run_python(api_info, 'print("still here")')) == "still here"


def test_wall_time_limit(api_info: dict[str, Any]) -> None:
    """A job that never ends is stopped at its wall time, and the answer says so."""
    sleeper = run_python(api_info, "import time\ntime.sleep(10_000)")

    assert "wall time" in json.dumps(sleeper).lower()


def test_code_that_ignores_its_soft_limit_is_killed_at_the_wall_time(api_info: dict[str, Any]) -> None:
    """The runner's own limit rides on ``SIGALRM``; code that ignores the signal is stopped by the executor (R40).

    The answer is the executor's, through the api: the job ran past its wall time, Ghostwriter's 20 s on the stack.
    """
    code = "import signal, time\nsignal.signal(signal.SIGALRM, signal.SIG_IGN)\ntime.sleep(10_000)"
    stubborn = run_python(api_info, code)

    assert (stubborn["isError"], stubborn["structuredContent"]) == (True, {"reason": "not_completed"}), stubborn
    assert stubborn["content"] == [
        {"type": "text", "text": "the code could not be completed: the job ran past its wall time of 20 s"}
    ]
    assert stdout_of(run_python(api_info, 'print("still here")')) == "still here"


def _upload(api_info: dict[str, Any], name: str, data: bytes, *, user: str = "user-a", chat: str = "chat-1") -> str:
    """Upload a synthetic file into a chat as Ghostwriter does; return the name the workspace gave it."""
    params = {"directory": f"/workspace/{chat}"}
    answer = ghostwriter(
        api_info, "POST", "/files/upload", chat=chat, user=user, params=params, files={"file": (name, data)}
    )
    assert answer.status_code == 200, answer.text
    return str(answer.json()["path"]).rsplit("/", 1)[1]


def test_absolute_virtual_paths_work_in_python(api_info: dict[str, Any]) -> None:
    """``/workspace/<chat>/x`` is the job's own copy of ``x``, through the image's ``/workspace`` link."""
    name = _upload(api_info, "quarterly-figures.csv", b"quarter,figure\nq1,10\nq2,32\n")
    code = f"import pandas as pd\nprint(pd.read_csv({f'/workspace/chat-1/{name}'!r})['figure'].sum())"
    assert stdout_of(run_python(api_info, code)) == "42"


def test_a_failing_read_shows_only_virtual_paths(api_info: dict[str, Any]) -> None:
    """The traceback of a file that is not there names it as the code did, never the job's real folder."""
    code = "import pandas as pd, pathlib\nprint(pathlib.Path.cwd())\npd.read_csv('/workspace/chat-1/missing.csv')"
    answer = run_python(api_info, code)
    assert answer["isError"] is False, answer
    body = answer["structuredContent"]
    assert body["stdout"].strip() == "/workspace/chat-1"
    assert body["stderr"].rstrip().endswith("No such file or directory: '/workspace/chat-1/missing.csv'")
    assert "/data" not in json.dumps(answer)
    assert body["hint"] is None


def test_another_chats_files_are_not_there_and_the_answer_says_why(api_info: dict[str, Any]) -> None:
    """A file of the user's other chat is not in this job; the answer tells the model only this chat's are."""
    name = _upload(api_info, "other-chat.csv", b"a,b\n1,2\n", chat="chat-2")
    answer = run_python(api_info, f"open({f'/workspace/chat-2/{name}'!r})")
    assert answer["isError"] is False, answer
    body = answer["structuredContent"]
    assert "FileNotFoundError" in body["stderr"]
    assert "only this chat's files are available" in body["hint"].lower()


# --- the template tools, end to end ------------------------------------------------------------------------------

TEMPLATES_CHAT = "chat-templates"


def _tool(api_info: dict[str, Any], chat: str, tool: str, body: dict[str, Any]) -> dict[str, Any]:
    """Call a workspace tool through Ghostwriter's door in ``chat``, and take its answer without its ``outputs``.

    The fields the answer leaves empty are left out too, as the tool's own measure leaves them.
    """
    answer = mcp_result(ghostwriter_mcp(api_info, tool, body, chat=chat, user="user-a"))
    assert answer["isError"] is False, answer
    return {
        name: value for name, value in answer["structuredContent"].items() if name != "outputs" and value is not None
    }


def _download(api_info: dict[str, Any], chat: str, path: str) -> bytes:
    """Fetch a file's bytes through Ghostwriter's view of ``chat``."""
    answer = ghostwriter(api_info, "GET", "/files/view", chat=chat, user="user-a", params={"path": path})
    assert answer.status_code == 200, answer.text
    return answer.content


def test_templates_are_inspected_and_filled_in_the_executor(api_info: dict[str, Any], tmp_path: Path) -> None:
    """A Word form, the same as a .dotx, a {{ }} document and a workbook: inspected and filled by real jobs (R45).

    The jobs run as slot uids with a read-only root and no network, in the shipped image, so this also proves that
    python-docx, docxtpl, Jinja and openpyxl are there and work as the job runs them.
    """
    built = {
        "form.docx": mt.make_controls_docx(tmp_path / "form.docx"),
        "letter.docx": mt.make_jinja_docx(tmp_path / "letter.docx"),
        "summary.xlsx": mt.make_placeholder_xlsx(tmp_path / "summary.xlsx"),
    }
    built["form.dotx"] = mt.as_dotx(built["form.docx"], tmp_path / "form.dotx")
    names = {name: _upload(api_info, name, path.read_bytes(), chat=TEMPLATES_CHAT) for name, path in built.items()}
    where = {name: f"/workspace/{TEMPLATES_CHAT}/{stored}" for name, stored in names.items()}

    kinds = {
        name: _tool(api_info, TEMPLATES_CHAT, "inspect_template", {"path": path})["template_kind"]
        for name, path in where.items()
    }
    assert kinds == {
        "form.docx": "content_controls",
        "form.dotx": "content_controls",
        "letter.docx": "placeholders",
        "summary.xlsx": "placeholders",
    }

    said = {"kind": "user", "note": "said in the chat"}
    form = {
        "customer": {"value": "Invented Harbour Ltd", "source": said},
        "status": {"value": "closed", "source": {"kind": "assistant"}},
        "approved": {"value": True, "source": {"kind": "assistant"}},
    }
    for name in ("form.docx", "form.dotx"):
        filled = _tool(api_info, TEMPLATES_CHAT, "fill_template", {"path": where[name], "values": form})
        assert filled["path"].endswith(".docx"), filled
        assert (filled["filled"], filled["warnings"]) == (3, [])
        document = Document(io.BytesIO(_download(api_info, TEMPLATES_CHAT, filled["path"])))
        rows = [[cell.text for cell in row.cells] for row in document.tables[-1].rows]
        assert rows[0] == ["field", "value", "source kind", "reference"]
        assert rows[1] == ["customer", "Invented Harbour Ltd", "user", "said in the chat"]

    letter = {
        "customer": {"value": "Invented Harbour Ltd", "source": said},
        "rows": {"value": [{"item": "Dredging", "total": 118}], "source": {"kind": "assistant"}},
    }
    filled = _tool(api_info, TEMPLATES_CHAT, "fill_template", {"path": where["letter.docx"], "values": letter})
    document = Document(io.BytesIO(_download(api_info, TEMPLATES_CHAT, filled["path"])))
    assert document.paragraphs[0].text == "Customer: Invented Harbour Ltd"
    assert [cell.text for cell in document.tables[0].rows[1].cells] == ["Dredging", "118"]

    summary = {
        "total": {"value": 469.5, "source": {"kind": "assistant"}},
        "rows": {"value": [["Dredging", 118], ["Piers", "=1+1"]], "source": {"kind": "assistant"}},
    }
    filled = _tool(api_info, TEMPLATES_CHAT, "fill_template", {"path": where["summary.xlsx"], "values": summary})
    assert filled["path"] == f"/workspace/{TEMPLATES_CHAT}/summary-filled.xlsx"
    book = openpyxl.load_workbook(io.BytesIO(_download(api_info, TEMPLATES_CHAT, filled["path"])))
    sheet = book["Summary"]
    assert (sheet["B2"].value, sheet["A6"].value, sheet["B7"].value) == (469.5, "Dredging", "=1+1")
    assert sheet["B7"].data_type == "s"
    assert [cell.value for cell in book["Sources"][1]] == ["field", "value", "source kind", "reference"]


# --- the output tools, end to end --------------------------------------------------------------------------------

OUTPUTS_CHAT = "chat-outputs"


def _output_tool(api_info: dict[str, Any], tool: str, body: dict[str, Any]) -> dict[str, Any]:
    """Call an output tool in the outputs' chat, and take its answer."""
    return _tool(api_info, OUTPUTS_CHAT, tool, body)


def _fetched(api_info: dict[str, Any], path: str) -> bytes:
    """Fetch a file's bytes from the outputs' chat."""
    return _download(api_info, OUTPUTS_CHAT, path)


def test_tables_and_documents_are_written_in_the_executor(api_info: dict[str, Any]) -> None:
    """A workbook, a CSV file and a Word document, each written by a real job and read back from the workspace (R45).

    The jobs run as slot uids with a read-only root and no network, in the shipped image, so this also proves that
    xlsxwriter, markdown-it-py and python-docx are there and work as the job runs them.
    """
    columns = ["item", "total", "note"]
    rows = [["Dredging", 118, "=1+1"], ["Piers", 351.5, "Grüße"], ["Total", None]]
    workbook = _output_tool(
        api_info, "write_table", {"name": "quarterly-figures", "format": "xlsx", "columns": columns, "rows": rows}
    )
    assert workbook == {"path": f"/workspace/{OUTPUTS_CHAT}/quarterly-figures.xlsx", "rows": 3}
    sheet = openpyxl.load_workbook(io.BytesIO(_fetched(api_info, workbook["path"])))["Sheet1"]
    assert [[cell.value for cell in row] for row in sheet.iter_rows()] == [
        columns,
        ["Dredging", 118, "=1+1"],
        ["Piers", 351.5, "Grüße"],
        ["Total", None, None],
    ]
    assert sheet["C2"].data_type == "s"
    assert sheet["A1"].font.b
    assert sheet.freeze_panes == "A2"

    table = _output_tool(
        api_info, "write_table", {"name": "quarterly-figures", "format": "csv", "columns": columns, "rows": rows}
    )
    assert table["path"] == f"/workspace/{OUTPUTS_CHAT}/quarterly-figures.csv"
    assert "1 cell" in table["hint"]
    raw = _fetched(api_info, table["path"])
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8-sig").splitlines() == [
        "item,total,note",
        "Dredging,118,'=1+1",
        "Piers,351.5,Grüße",
        "Total,,",
    ]

    markdown = (
        "# Quarterly report\n\nDredging **118**.\n\n- north pier\n- south pier\n\n"
        "| Item | Total |\n|---|---|\n| Piers | 351 |\n"
    )
    written = _output_tool(api_info, "write_document", {"name": "report", "format": "docx", "markdown": markdown})
    assert written == {"path": f"/workspace/{OUTPUTS_CHAT}/report.docx"}
    document = Document(io.BytesIO(_fetched(api_info, written["path"])))
    assert [(p.style.name if p.style is not None else "", p.text) for p in document.paragraphs] == [
        ("Heading 1", "Quarterly report"),
        ("Normal", "Dredging 118."),
        ("List Bullet", "north pier"),
        ("List Bullet", "south pier"),
    ]
    assert [[cell.text for cell in row.cells] for row in document.tables[0].rows] == [
        ["Item", "Total"],
        ["Piers", "351"],
    ]


def test_the_retention_dry_run_runs_in_the_api_container(api_info: dict[str, Any]) -> None:
    """What ``make retention-dry-run`` runs, in the shipped api image as uid 10001 on a read-only root: counts only.

    The test stack's volume is its own, so a fresh upload is there to be counted as kept.
    """
    name = _upload(api_info, "retention-dry-run-sample.txt", b"synthetic", chat="chat-retention")
    printed = docker("exec", api_info["Id"], "python", "-m", "workbench.api.retention", "dry-run")
    assert "nothing is recorded, deleted or changed" in printed
    assert "Files are deleted 7 days after they were made." in printed
    assert "upload: none due; " in printed
    assert name not in printed
    assert "chat-retention" not in printed


# --- docint's inline jobs ------------------------------------------------------------------------------------------

MARKER = "synthetic-harbour-marker-7f3a"
INLINE_OPERATION = "tool.run_python.inline"

CHART_OF_THE_DATA = r"""
import json, pathlib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
passages = json.loads(pathlib.Path("passages.json").read_text())
print(len(passages))
pathlib.Path("out/counts.csv").write_text("n,text\n" + "".join(f"{p['n']},{p['text']}\n" for p in passages))
figure, axes = plt.subplots()
axes.bar([p["n"] for p in passages], [len(p["text"]) for p in passages])
figure.savefig("out/chart.png")
"""

#: Run in the executor, as root: what the volume holds of a job, and how many of its regular files hold the marker.
LEFT_ON_THE_VOLUME = r"""
import json, os, stat, sys
marker = sys.argv[1].encode()
holding = 0
for folder, _, names in os.walk("/data"):
    for name in names:
        path = os.path.join(folder, name)
        if stat.S_ISREG(os.lstat(path).st_mode):
            with open(path, "rb") as f:
                holding += marker in f.read()
left = {"jobs": os.listdir("/data/jobs"), "staging": os.listdir("/data/staging")}
print(json.dumps({**left, "docint": os.path.exists("/data/workspaces/docint"), "holding": holding}))
"""

#: Run in the api: how many job records docint has, read-only.
DOCINT_JOBS = (
    "import sqlite3; db = sqlite3.connect('file:/data/workbench.db?mode=ro', uri=True); "
    "print(db.execute(\"SELECT COUNT(*) FROM jobs WHERE caller = 'docint'\").fetchone()[0])"
)


def _logs(container: str) -> str:
    """Read everything a container has logged, its standard output and its standard error."""
    found = subprocess.run(["docker", "logs", container], check=True, capture_output=True, text=True, timeout=120)
    return found.stdout + found.stderr


def _logged(container: str, text: str, seconds: float = 30.0) -> str:
    """Read a container's log again until it holds ``text``, or the time is up; docker may copy a line late."""
    deadline = time.monotonic() + seconds
    while text not in (found := _logs(container)) and time.monotonic() < deadline:
        time.sleep(0.5)
    return found


def test_an_inline_job_leaves_nothing_on_the_volume(api_info: dict[str, Any], executor: str) -> None:
    """The data and outputs of docint's job come back in the answer and stay nowhere; only its record is kept.

    No job or staging folder is left and docint has no workspace; no file on the volume and no line of either
    container's log holds the data.
    """
    before = int(docker("exec", api_info["Id"], "python", "-I", "-c", DOCINT_JOBS))
    data = [{"name": "passages.json", "content": json.dumps([{"n": 1, "text": MARKER}])}]
    answer = mcp_result(mcp_call(api_info, CHART_OF_THE_DATA, data))

    assert answer["isError"] is False, answer["structuredContent"]
    assert answer["structuredContent"]["stdout"].strip() == "1"
    assert [block["type"] for block in answer["content"]] == ["text", "image", "resource"]
    _, image, resource = answer["content"]
    assert image["mimeType"] == "image/png"
    assert base64.b64decode(image["data"]).startswith(b"\x89PNG\r\n\x1a\n")
    assert MARKER in resource["resource"]["text"]

    left = json.loads(docker("exec", executor, "python", "-I", "-c", LEFT_ON_THE_VOLUME, MARKER))
    assert left == {"jobs": [], "staging": [], "docint": False, "holding": 0}
    assert int(docker("exec", api_info["Id"], "python", "-I", "-c", DOCINT_JOBS)) == before + 1
    api_logs = _logged(api_info["Id"], INLINE_OPERATION)
    assert INLINE_OPERATION in api_logs and MARKER not in api_logs
    assert MARKER not in _logs(executor)


def test_no_core_dumps_in_either_container(api_info: dict[str, Any], executor: str) -> None:
    """Compose's ``ulimits: core: 0`` holds in the api and the executor; an inline job runs with a core limit of 0."""
    for info in (api_info, inspect(executor)):
        assert info["HostConfig"]["Ulimits"] == [{"Name": "core", "Hard": 0, "Soft": 0}]
    answer = mcp_result(mcp_call(api_info, "import resource; print(resource.getrlimit(resource.RLIMIT_CORE))", []))
    assert answer["isError"] is False, answer["structuredContent"]
    assert answer["structuredContent"]["stdout"].strip() == "(0, 0)"


# --- Ghostwriter's chats -------------------------------------------------------------------------------------------

CHAT_A, CHAT_B = "c" * 64, "e" * 64
#: The user of the chat delete, whose chat holds that test's files alone.
DELETING_USER = "d" * 64
NOT_FOUND = (404, {"detail": "not found"})
GHOSTWRITER_MARKER = "synthetic-ghostwriter-marker-5c1e"
FIGURES = f"quarter,figure\nq1,10\n{GHOSTWRITER_MARKER},32\n".encode()
TABLE: dict[str, Any] = {
    "name": "counts",
    "format": "csv",
    "columns": ["author", "posts"],
    "rows": [["user-a", 12], ["user-b", 7]],
}
PREVIEW = {
    "columns": ["author", "posts"],
    "rows": [["user-a", "12"], ["user-b", "7"]],
    "row_count": 2,
    "truncated": False,
}

TABLE_AND_CHART = r"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
table = pd.DataFrame({"author": ["user-a", "user-b"], "posts": [12, 7]})
table.to_csv("out/t.csv", index=False)
figure, axes = plt.subplots()
axes.bar(table["author"], table["posts"])
figure.savefig("out/chart.png")
"""

#: Run in the api: what is left of one Ghostwriter chat on the volume and in the database, read-only.
LEFT_OF_THE_CHAT = r"""
import json, os, sqlite3, sys
user, chat = sys.argv[1], sys.argv[2]
owner = ("ghostwriter", user, chat)
db = sqlite3.connect("file:/data/workbench.db?mode=ro", uri=True)
mine = "WHERE caller = ? AND user_id = ? AND chat_id = ?"
print(json.dumps({
    "folder": os.path.exists(f"/data/workspaces/ghostwriter/{user}/{chat}"),
    "files": db.execute(f"SELECT COUNT(*) FROM files {mine}", owner).fetchone()[0],
    "jobs": db.execute(f"SELECT COUNT(*) FROM jobs {mine}", owner).fetchone()[0],
    "staging": os.listdir("/data/staging"),
}))
"""


def _ghostwriter_upload(api_info: dict[str, Any], *, chat: str, user: str = "a" * 64) -> httpx.Response:
    """Upload :data:`FIGURES` as ``quarterly-figures.csv`` into Ghostwriter's chat ``chat``."""
    files = {"file": ("quarterly-figures.csv", FIGURES)}
    answer = ghostwriter(
        api_info, "POST", "/files/upload", chat=chat, user=user, params={"directory": f"/workspace/{chat}"}, files=files
    )
    assert answer.status_code == 200, answer.text
    return answer


def _audit_events(container: str) -> list[dict[str, Any]]:
    """Read a container's audit events so far: the JSON lines of its standard output."""
    lines = docker("logs", container).splitlines()
    return [event for line in lines if line.startswith("{") and "operation" in (event := json.loads(line))]


def test_another_chats_path_is_not_found(api_info: dict[str, Any]) -> None:
    """A path into another chat of the same user is not found: on the file routes, and through a tool.

    In its own chat the file is viewed and read; named from another chat it is the plain 404 on the routes, and an
    error result through ``read_document``, ``refused`` and ``not found``, as any refusal.
    """
    path = f"/workspace/{CHAT_A}/quarterly-figures.csv"
    _ghostwriter_upload(api_info, chat=CHAT_A)
    own = ghostwriter(api_info, "GET", "/files/view", chat=CHAT_A, params={"path": path})
    assert (own.status_code, own.content) == (200, FIGURES)
    read = mcp_result(ghostwriter_mcp(api_info, "read_document", {"path": path}, chat=CHAT_A))
    assert read["isError"] is False, read["structuredContent"]

    own_list = ghostwriter(api_info, "GET", "/files/list", chat=CHAT_A, params={"directory": f"/workspace/{CHAT_A}"})
    assert own_list.status_code == 200, own_list.text
    assert "quarterly-figures.csv" in [entry["name"] for entry in own_list.json()["entries"]]

    viewed = ghostwriter(api_info, "GET", "/files/view", chat=CHAT_B, params={"path": path})
    assert (viewed.status_code, viewed.json()) == NOT_FOUND
    listed = ghostwriter(api_info, "GET", "/files/list", chat=CHAT_B, params={"directory": f"/workspace/{CHAT_A}"})
    assert (listed.status_code, listed.json()) == NOT_FOUND
    refused = mcp_result(ghostwriter_mcp(api_info, "read_document", {"path": path}, chat=CHAT_B))
    assert (refused["isError"], refused["structuredContent"]) == (True, {"reason": "refused"})
    assert refused["content"] == [{"type": "text", "text": "not found"}]


def test_a_real_preview_comes_back(api_info: dict[str, Any]) -> None:
    """The executor reads each table's preview in the job: ``write_table``'s csv and xlsx, ``run_python``'s csv.

    The chart ``run_python`` writes beside its table is an output too, with no preview.
    """
    for kind in ("csv", "xlsx"):
        wrote = mcp_result(ghostwriter_mcp(api_info, "write_table", {**TABLE, "format": kind}, chat=CHAT_A))
        assert wrote["isError"] is False, wrote["structuredContent"]
        (output,) = wrote["structuredContent"]["outputs"]
        assert (output["name"], output["preview"]) == (f"counts.{kind}", PREVIEW)

    ran = mcp_result(ghostwriter_mcp(api_info, "run_python", {"code": TABLE_AND_CHART}, chat=CHAT_A))
    assert ran["isError"] is False, ran["structuredContent"]
    outputs = ran["structuredContent"]["outputs"]
    assert {output["name"]: output["preview"] for output in outputs} == {"t.csv": PREVIEW, "chart.png": None}
    assert {output["name"]: output["mime"] for output in outputs} == {"t.csv": "text/csv", "chart.png": "image/png"}


def test_a_chat_delete_leaves_nothing(api_info: dict[str, Any], executor: str) -> None:
    """Ghostwriter deletes a chat it uploaded into, wrote a table in and ran Python in: its four files, and the rest.

    Before, the chat has its folder, four file rows and two job rows; after, none of them, and nothing is staged. The
    api's log holds the delete's event, by the ids of the four files.
    """
    user = DELETING_USER
    uploaded = _ghostwriter_upload(api_info, chat=CHAT_A, user=user).json()
    wrote = mcp_result(ghostwriter_mcp(api_info, "write_table", TABLE, chat=CHAT_A, user=user))
    ran = mcp_result(ghostwriter_mcp(api_info, "run_python", {"code": TABLE_AND_CHART}, chat=CHAT_A, user=user))
    assert (wrote["isError"], ran["isError"]) == (False, False), (wrote, ran)
    made = [output["id"] for answer in (wrote, ran) for output in answer["structuredContent"]["outputs"]]

    def left() -> dict[str, Any]:
        return json.loads(docker("exec", api_info["Id"], "python", "-I", "-c", LEFT_OF_THE_CHAT, user, CHAT_A))

    assert left() == {"folder": True, "files": 4, "jobs": 2, "staging": []}
    deleted = ghostwriter(api_info, "DELETE", "/files/chat", chat=CHAT_A, user=user)
    assert (deleted.status_code, deleted.json()) == (200, {"deleted": 4})
    assert left() == {"folder": False, "files": 0, "jobs": 0, "staging": []}

    _logged(api_info["Id"], "chat.delete")
    (event,) = [e for e in _audit_events(api_info["Id"]) if e["operation"] == "chat.delete"]
    assert (event["action"], event["outcome"], event["actor"]) == ("delete", "ok", {"user": user, "via": "service"})
    assert sorted(event["target"].pop("ids")) == sorted([uploaded["id"], *made])
    assert event["target"] == {"kind": "file", "count": 4, "container": f"chat:{CHAT_A}"}
