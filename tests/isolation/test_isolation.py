"""What isolates a job, checked on the running test stack: the executor's configuration, then jobs that try to escape.

The configuration tests read ``docker inspect`` and look from inside the running executor, so
they prove what the kernel enforces, not only what the compose file asks for. The tests that
run code go through the ``run_python`` tool, which Task 12 adds; until then they are expected
to fail (``xfail(strict=True)``), and Task 12 removes the marks.
"""

import json
import stat
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest

from tests.isolation.conftest import docker, environment, inspect, run_python, stdout_of

pytestmark = pytest.mark.isolation

MIB = 1024 * 1024
CAPABILITIES = {"SETUID", "SETGID", "CHOWN", "FOWNER", "DAC_OVERRIDE", "KILL"}
# The bounding set of those six: CHOWN 0, DAC_OVERRIDE 1, FOWNER 3, KILL 5, SETGID 6, SETUID 7.
CAPABILITY_MASK = f"{(1 << 0) | (1 << 1) | (1 << 3) | (1 << 5) | (1 << 6) | (1 << 7):016x}"
PIDS_LIMIT = 320  # R19: 4 slots x 64 processes + 64 for the supervisor and its helpers
SLOT_UIDS = range(20001, 20017)
API_UID = 10001
NEEDS_RUN_PYTHON = pytest.mark.xfail(reason="needs run_python (Task 12)", strict=True)


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
    return pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
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


# --- jobs that try to escape (need run_python, Task 12) ---------------------------------------


@NEEDS_RUN_PYTHON
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


@NEEDS_RUN_PYTHON
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


@NEEDS_RUN_PYTHON
def test_other_jobs_are_unreadable(api: httpx.Client) -> None:
    """Two jobs at once, each finding the other's folder from its command line: neither may list it."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        answers = list(pool.map(lambda user: run_python(api, FIND_THE_OTHER_JOB, user=user), ["user-a", "user-b"]))

    assert [stdout_of(answer) for answer in answers] == ["refused", "refused"]


@NEEDS_RUN_PYTHON
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


@NEEDS_RUN_PYTHON
def test_no_secrets_in_environment(api: httpx.Client) -> None:
    """The caller key is in no job's environment, by name or by value."""
    key = api.headers["Authorization"].removeprefix("Bearer ")
    env = json.loads(stdout_of(run_python(api, "import json, os; print(json.dumps(dict(os.environ)))")))

    assert "OPEN_WEBUI_WORKBENCH_KEY" not in env
    assert not any(key in value for value in env.values())


@NEEDS_RUN_PYTHON
def test_fork_bomb_is_contained(api: httpx.Client) -> None:
    """The job ends as killed, error or timeout; the next job runs normally."""
    bomb = run_python(api, "import os\nwhile True:\n    os.fork()\n")

    assert bomb.status_code != 200 or "error" in bomb.json()
    assert stdout_of(run_python(api, 'print("still here")')) == "still here"


@NEEDS_RUN_PYTHON
def test_memory_hog_is_killed_with_a_clear_message(api: httpx.Client) -> None:
    """More than the job's memory ends the job, and the answer says it was memory."""
    hog = run_python(api, "blob = bytearray(8 * 1024 ** 3)\nprint(len(blob))")

    assert "memory" in hog.text.lower()
    assert stdout_of(run_python(api, 'print("still here")')) == "still here"


@NEEDS_RUN_PYTHON
def test_wall_time_limit(api: httpx.Client) -> None:
    """A job that never ends is stopped at its wall time, and the answer says so."""
    sleeper = run_python(api, "import time\ntime.sleep(10_000)")

    assert "wall time" in sleeper.text.lower()
