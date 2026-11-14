"""What isolates a job, checked on the running test stack: the executor's configuration, then jobs that try to escape.

The configuration tests read ``docker inspect`` and look from inside the running executor, so
they prove what the kernel enforces, not only what the compose file asks for. The tests that
run code go through the ``run_python`` tool, as the model would.
"""

import io
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import openpyxl
import pytest
from docx import Document

from tests.fixtures import make_templates as mt
from tests.isolation.conftest import docker, environment, inspect, run_python, stdout_of

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
    assert "OPEN_WEBUI_WORKBENCH_KEY" not in env


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


def test_no_network(api: httpx.Client) -> None:
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
    assert stdout_of(run_python(api, code)) == "no network"


def test_other_workspaces_are_unreadable(api: httpx.Client) -> None:
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
    assert stdout_of(run_python(api, code)) == "refused"


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


def test_other_jobs_are_unreadable(api: httpx.Client) -> None:
    """Two jobs at once, each finding the other's folder from its command line: neither may list it."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda user: run_python(api, FIND_THE_OTHER_JOB, user=user), ["user-a", "user-b"]))

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


def test_a_job_that_opens_its_folders_to_all_opens_them_to_no_other_job(api: httpx.Client) -> None:
    """R76: two jobs at once open their ``home/`` to everyone and try a loopback listener; neither reaches the other."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda user: run_python(api, OPEN_UP_AND_LISTEN, user=user), ["user-a", "user-b"]))

    assert [stdout_of(answer) for answer in answers] == ["no socket,refused,refused"] * 2


def test_inputs_are_read_only(api: httpx.Client) -> None:
    """The job's copy of an upload is root's; only ``out/`` takes writes."""
    upload = api.post(
        "/files/upload",
        params={"directory": "/workspace/chat-1"},
        headers={"X-User-Id": "user-a", "X-Session-Id": "chat-1"},
        files={"file": ("in.csv", b"quarter,figure\nq1,10\n")},
    )
    assert upload.status_code == 200, upload.text
    code = """
try:
    open("in.csv", "w").write("changed")
except PermissionError:
    print("input refused")
open("out/x", "w").write("synthetic")
print("output written")
"""
    assert stdout_of(run_python(api, code)).splitlines() == ["input refused", "output written"]


def test_no_secrets_in_environment(api: httpx.Client) -> None:
    """The caller key is in no job's environment, by name or by value."""
    key = api.headers["Authorization"].removeprefix("Bearer ")
    env = json.loads(stdout_of(run_python(api, "import json, os; print(json.dumps(dict(os.environ)))")))

    assert "OPEN_WEBUI_WORKBENCH_KEY" not in env
    assert not any(key in value for value in env.values())


def test_fork_bomb_is_contained(api: httpx.Client) -> None:
    """The job ends as killed, error or timeout; the next job runs normally.

    At the slot's process limit ``fork`` fails, so the code itself usually ends in that error, which the answer
    carries as its traceback; else the executor stopped the job, on a resource limit or its wall time.
    """
    bomb = run_python(api, "import os\nwhile True:\n    os.fork()\n")

    if bomb.status_code == 200:
        assert bomb.json()["stderr"].rstrip().endswith("BlockingIOError: [Errno 11] Resource temporarily unavailable")
    else:
        detail = bomb.json()["detail"]
        assert (bomb.status_code, detail.split(": ")[0]) == (422, "the code could not be completed"), bomb.text
        assert "resource limit" in detail or "wall time" in detail, detail
    assert stdout_of(run_python(api, 'print("still here")')) == "still here"


def test_memory_hog_is_killed_with_a_clear_message(api: httpx.Client) -> None:
    """More than the job's memory ends the job, and the answer says it was memory."""
    hog = run_python(api, "blob = bytearray(8 * 1024 ** 3)\nprint(len(blob))")

    assert "memory" in hog.text.lower()
    assert stdout_of(run_python(api, 'print("still here")')) == "still here"


def test_wall_time_limit(api: httpx.Client) -> None:
    """A job that never ends is stopped at its wall time, and the answer says so."""
    sleeper = run_python(api, "import time\ntime.sleep(10_000)")

    assert "wall time" in sleeper.text.lower()


def test_code_that_ignores_its_soft_limit_is_killed_at_the_wall_time(api: httpx.Client) -> None:
    """The runner's own limit rides on ``SIGALRM``; code that ignores the signal is stopped by the executor (R40).

    The answer is the executor's, through the api: the job ran past its wall time, the test stack's 20 s.
    """
    code = "import signal, time\nsignal.signal(signal.SIGALRM, signal.SIG_IGN)\ntime.sleep(10_000)"
    stubborn = run_python(api, code)

    assert (stubborn.status_code, stubborn.json()) == (
        422,
        {"detail": "the code could not be completed: the job ran past its wall time of 20 s"},
    )
    assert stdout_of(run_python(api, 'print("still here")')) == "still here"


def _upload(api: httpx.Client, name: str, data: bytes, *, user: str = "user-a", chat: str = "chat-1") -> str:
    """Upload a synthetic file into a chat; return the name the workspace gave it."""
    answer = api.post(
        "/files/upload",
        params={"directory": f"/workspace/{chat}"},
        headers={"X-User-Id": user, "X-Session-Id": chat},
        files={"file": (name, data)},
    )
    assert answer.status_code == 200, answer.text
    return str(answer.json()["path"]).rsplit("/", 1)[1]


def test_absolute_virtual_paths_work_in_python(api: httpx.Client) -> None:
    """``/workspace/<chat>/x`` is the job's own copy of ``x``, through the image's ``/workspace`` link."""
    name = _upload(api, "quarterly-figures.csv", b"quarter,figure\nq1,10\nq2,32\n")
    code = f"import pandas as pd\nprint(pd.read_csv({f'/workspace/chat-1/{name}'!r})['figure'].sum())"
    assert stdout_of(run_python(api, code)) == "42"


def test_a_failing_read_shows_only_virtual_paths(api: httpx.Client) -> None:
    """The traceback of a file that is not there names it as the code did, never the job's real folder."""
    code = "import pandas as pd, pathlib\nprint(pathlib.Path.cwd())\npd.read_csv('/workspace/chat-1/missing.csv')"
    answer = run_python(api, code)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["stdout"].strip() == "/workspace/chat-1"
    assert body["stderr"].rstrip().endswith("No such file or directory: '/workspace/chat-1/missing.csv'")
    assert "/data" not in answer.text
    assert "hint" not in body


def test_another_chats_files_are_not_there_and_the_answer_says_why(api: httpx.Client) -> None:
    """A file of the user's other chat is not in this job; the answer tells the model only this chat's are."""
    name = _upload(api, "other-chat.csv", b"a,b\n1,2\n", chat="chat-2")
    answer = run_python(api, f"open({f'/workspace/chat-2/{name}'!r})")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert "FileNotFoundError" in body["stderr"]
    assert "only this chat's files are available" in body["hint"].lower()


# --- the template tools, end to end ------------------------------------------------------------------------------

TEMPLATES_CHAT = "chat-templates"
TEMPLATE_HEADERS = {"X-User-Id": "user-a", "X-Session-Id": TEMPLATES_CHAT}


def _template_tool(api: httpx.Client, tool: str, body: dict[str, Any]) -> dict[str, Any]:
    """Call a template tool in the templates' chat, and take its answer."""
    answer = api.post(f"/tools/{tool}", json=body, headers=TEMPLATE_HEADERS)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _download(api: httpx.Client, path: str) -> bytes:
    """Fetch a file's bytes through the file browser."""
    answer = api.get("/files/view", params={"path": path}, headers=TEMPLATE_HEADERS)
    assert answer.status_code == 200, answer.text
    return answer.content


def test_templates_are_inspected_and_filled_in_the_executor(api: httpx.Client, tmp_path: Path) -> None:
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
    names = {name: _upload(api, name, path.read_bytes(), chat=TEMPLATES_CHAT) for name, path in built.items()}
    where = {name: f"/workspace/{TEMPLATES_CHAT}/{stored}" for name, stored in names.items()}

    kinds = {
        name: _template_tool(api, "inspect_template", {"path": path})["template_kind"] for name, path in where.items()
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
        filled = _template_tool(api, "fill_template", {"path": where[name], "values": form})
        assert filled["path"].endswith(".docx"), filled
        assert (filled["filled"], filled["warnings"]) == (3, [])
        document = Document(io.BytesIO(_download(api, filled["path"])))
        rows = [[cell.text for cell in row.cells] for row in document.tables[-1].rows]
        assert rows[0] == ["field", "value", "source kind", "reference"]
        assert rows[1] == ["customer", "Invented Harbour Ltd", "user", "said in the chat"]

    letter = {
        "customer": {"value": "Invented Harbour Ltd", "source": said},
        "rows": {"value": [{"item": "Dredging", "total": 118}], "source": {"kind": "assistant"}},
    }
    filled = _template_tool(api, "fill_template", {"path": where["letter.docx"], "values": letter})
    document = Document(io.BytesIO(_download(api, filled["path"])))
    assert document.paragraphs[0].text == "Customer: Invented Harbour Ltd"
    assert [cell.text for cell in document.tables[0].rows[1].cells] == ["Dredging", "118"]

    summary = {
        "total": {"value": 469.5, "source": {"kind": "assistant"}},
        "rows": {"value": [["Dredging", 118], ["Piers", "=1+1"]], "source": {"kind": "assistant"}},
    }
    filled = _template_tool(api, "fill_template", {"path": where["summary.xlsx"], "values": summary})
    assert filled["path"] == f"/workspace/{TEMPLATES_CHAT}/summary-filled.xlsx"
    book = openpyxl.load_workbook(io.BytesIO(_download(api, filled["path"])))
    sheet = book["Summary"]
    assert (sheet["B2"].value, sheet["A6"].value, sheet["B7"].value) == (469.5, "Dredging", "=1+1")
    assert sheet["B7"].data_type == "s"
    assert [cell.value for cell in book["Sources"][1]] == ["field", "value", "source kind", "reference"]


# --- the output tools, end to end --------------------------------------------------------------------------------

OUTPUTS_CHAT = "chat-outputs"
OUTPUT_HEADERS = {"X-User-Id": "user-a", "X-Session-Id": OUTPUTS_CHAT}


def _output_tool(api: httpx.Client, tool: str, body: dict[str, Any]) -> dict[str, Any]:
    """Call an output tool in the outputs' chat, and take its answer."""
    answer = api.post(f"/tools/{tool}", json=body, headers=OUTPUT_HEADERS)
    assert answer.status_code == 200, answer.text
    return answer.json()


def _fetched(api: httpx.Client, path: str) -> bytes:
    """Fetch a file's bytes from the outputs' chat through the file browser."""
    answer = api.get("/files/view", params={"path": path}, headers=OUTPUT_HEADERS)
    assert answer.status_code == 200, answer.text
    return answer.content


def test_tables_and_documents_are_written_in_the_executor(api: httpx.Client) -> None:
    """A workbook, a CSV file and a Word document, each written by a real job and read back from the workspace (R45).

    The jobs run as slot uids with a read-only root and no network, in the shipped image, so this also proves that
    xlsxwriter, markdown-it-py and python-docx are there and work as the job runs them.
    """
    columns = ["item", "total", "note"]
    rows = [["Dredging", 118, "=1+1"], ["Piers", 351.5, "Grüße"], ["Total", None]]
    workbook = _output_tool(
        api, "write_table", {"name": "quarterly-figures", "format": "xlsx", "columns": columns, "rows": rows}
    )
    assert workbook == {"path": f"/workspace/{OUTPUTS_CHAT}/quarterly-figures.xlsx", "rows": 3}
    sheet = openpyxl.load_workbook(io.BytesIO(_fetched(api, workbook["path"])))["Sheet1"]
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
        api, "write_table", {"name": "quarterly-figures", "format": "csv", "columns": columns, "rows": rows}
    )
    assert table["path"] == f"/workspace/{OUTPUTS_CHAT}/quarterly-figures.csv"
    assert "1 cell" in table["hint"]
    raw = _fetched(api, table["path"])
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
    written = _output_tool(api, "write_document", {"name": "report", "format": "docx", "markdown": markdown})
    assert written == {"path": f"/workspace/{OUTPUTS_CHAT}/report.docx"}
    document = Document(io.BytesIO(_fetched(api, written["path"])))
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


def test_the_retention_dry_run_runs_in_the_api_container(api_info: dict[str, Any], api: httpx.Client) -> None:
    """What ``make retention-dry-run`` runs, in the shipped api image as uid 10001 on a read-only root: counts only.

    The test stack's volume is its own, so a fresh upload is there to be counted as kept.
    """
    name = _upload(api, "retention-dry-run-sample.txt", b"synthetic", chat="chat-retention")
    printed = docker("exec", api_info["Id"], "python", "-m", "workbench.api.retention", "dry-run")
    assert "nothing is recorded, deleted or changed" in printed
    assert "Files are deleted 7 days after they were made." in printed
    assert "upload: none due; " in printed
    assert name not in printed
    assert "chat-retention" not in printed
