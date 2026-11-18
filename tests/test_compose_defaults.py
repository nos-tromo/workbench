"""The operator surface, read as files; nothing here starts a container or touches a volume.

Compose's settings and shape, the images' bases, ``.env.example``, the bundle script and the Makefile's targets.
"""

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.conftest import _time_scale
from workbench.api.audit import OPERATIONS
from workbench.config import GHOSTWRITER_DEFAULT_TOOLS, load_settings

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ROOT / "docker"

#: The spec's defaults (its §9, the plan's Global Constraints), by their exact names.
SPEC_DEFAULTS = {
    "WORKBENCH_RETENTION_DAYS": "7",
    "WORKBENCH_RETENTION_INTERVAL_S": "300",
    "WORKBENCH_EXECUTOR_SLOTS": "4",
    "WORKBENCH_QUEUE_MAX": "16",
    "WORKBENCH_QUEUE_WAIT_S": "60",
    "WORKBENCH_JOBS_PER_USER": "1",
    "WORKBENCH_JOB_WALL_S": "120",
    "WORKBENCH_JOB_MEMORY_MB": "1536",
    "WORKBENCH_RESULT_MAX_KB": "16",
    "WORKBENCH_UPLOAD_MAX_MB": "50",
    "WORKBENCH_WORKSPACE_MAX_MB": "500",
}

#: docint's variables in the api's environment: the key, its tools and its two limits (the spec's §3.8).
CALLER_DEFAULTS = {
    "DOCINT_WORKBENCH_KEY": "",
    "DOCINT_WORKBENCH_TOOLS": "run_python",
    "DOCINT_WORKBENCH_WALL_S": "60",
    "DOCINT_WORKBENCH_SLOTS": "2",
}

#: Ghostwriter's variables in the api's environment: the key, its tools and its two limits.
EIGHT_LIST = (
    "list_files,read_document,search_files,run_python,inspect_template,fill_template,write_table,write_document"
)
GHOSTWRITER_DEFAULTS = {
    "GHOSTWRITER_WORKBENCH_KEY": "",
    "GHOSTWRITER_WORKBENCH_TOOLS": EIGHT_LIST,
    "GHOSTWRITER_WORKBENCH_WALL_S": "120",
    "GHOSTWRITER_WORKBENCH_SLOTS": "2",
}

#: The executor supervisor's capabilities (docs/sandbox.md).
EXECUTOR_CAPABILITIES = {"SETUID", "SETGID", "CHOWN", "FOWNER", "DAC_OVERRIDE", "KILL"}

#: What prepare_volume needs as root: hand parts over (CHOWN), set the modes of parts it does not own (FOWNER), and
#: open the api's 0700 folders and 0600 database, read-only (DAC_READ_SEARCH).
VOLUME_PERMISSIONS_CAPABILITIES = {"CHOWN", "FOWNER", "DAC_READ_SEARCH"}

#: Tied to the mounts and the image's users (R32): never set through compose.
FIXED_BY_THE_IMAGES = ("WORKBENCH_DATA_DIR", "WORKBENCH_EXECUTOR_SOCKET", "WORKBENCH_SLOT_UID_BASE")

SERVICES = ("volume-permissions", "api", "executor")

_DEFAULT = re.compile(r"\$\{([A-Z0-9_]+):-([^}]*)\}")
#: Either form of a default: ``${VAR:-default}``, or the colon-less ``${VAR-default}``, which keeps a value set empty.
_ANY_DEFAULT = re.compile(r"\$\{([A-Z0-9_]+):?-([^}]*)\}")


def compose(name: str = "compose.yaml") -> dict[str, Any]:
    """Read a compose file of ``docker/``, its anchors and merge keys resolved."""
    return yaml.safe_load((DOCKER / name).read_text(encoding="utf-8"))


def default_of(value: object) -> tuple[str, str]:
    """Split a ``${VAR:-default}`` value into its variable and default."""
    found = _DEFAULT.fullmatch(str(value))
    assert found is not None, f"{value!r} is not ${{VAR:-default}}"
    return found.group(1), found.group(2)


def example_values() -> dict[str, str]:
    """The values ``.env.example`` shows: its ``KEY=value`` lines and its ``# KEY=value`` defaults."""
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    found = (re.fullmatch(r"(?:# )?([A-Z][A-Z0-9_]*)=(.*)", line) for line in lines)
    return {match.group(1): match.group(2) for match in found if match is not None}


# --- settings -------------------------------------------------------------------------------------------------


def test_compose_defaults_match_the_spec() -> None:
    """Both roles get every setting as ``${VAR:-default}``; each default is the spec's, the code's and the example's."""
    services = compose()["services"]
    code = load_settings({}, role="executor")
    for name in ("api", "executor"):
        settings = {key: value for key, value in services[name]["environment"].items() if key.startswith("WORKBENCH_")}
        assert set(settings) == set(SPEC_DEFAULTS), name
        for key, value in settings.items():
            assert default_of(value) == (key, SPEC_DEFAULTS[key]), (name, key)
    assert {key: str(getattr(code, key.removeprefix("WORKBENCH_").lower())) for key in SPEC_DEFAULTS} == SPEC_DEFAULTS
    example = example_values()
    assert {key: example.get(key) for key in SPEC_DEFAULTS} == SPEC_DEFAULTS


def test_the_api_alone_gets_the_callers_variables() -> None:
    """Docint's variables and the purpose rule reach the api, not the executor, which reads none of them."""
    spec = compose()
    api = spec["services"]["api"]["environment"]
    assert {key: default_of(api[key]) for key in CALLER_DEFAULTS} == {
        key: (key, value) for key, value in CALLER_DEFAULTS.items()
    }
    assert api["AUDIT_REQUIRE_PURPOSE"] == "${AUDIT_REQUIRE_PURPOSE-query,disclose,combine,modify,collect}"
    names = {*CALLER_DEFAULTS, "AUDIT_REQUIRE_PURPOSE"}
    assert names.isdisjoint(spec["services"]["executor"]["environment"]) and names.isdisjoint(spec["x-settings"])


def test_the_api_alone_gets_ghostwriters_variables() -> None:
    """Ghostwriter's key, tools and limits reach the api, not the executor and not the shared block."""
    spec = compose()
    api = spec["services"]["api"]["environment"]
    assert {key: default_of(api[key]) for key in GHOSTWRITER_DEFAULTS} == {
        key: (key, value) for key, value in GHOSTWRITER_DEFAULTS.items()
    }
    assert set(GHOSTWRITER_DEFAULTS).isdisjoint(spec["services"]["executor"]["environment"])
    assert set(GHOSTWRITER_DEFAULTS).isdisjoint(spec["x-settings"])


def test_the_purpose_default_covers_every_recorded_ghostwriter_tool() -> None:
    """Each action Ghostwriter's tools, its upload and its view are recorded under needs a purpose; ``delete`` does not.

    ``chat.delete`` is Ghostwriter's own retention too, which states none, so it never gets a 428.
    """
    api = compose()["services"]["api"]["environment"]
    default = _ANY_DEFAULT.fullmatch(api["AUDIT_REQUIRE_PURPOSE"])
    assert default is not None
    required = set(default.group(2).split(","))
    tools = {OPERATIONS[tool][0] for tool in GHOSTWRITER_DEFAULT_TOOLS if tool in OPERATIONS}
    assert tools | {OPERATIONS["upload"][0], OPERATIONS["view"][0]} <= required
    assert OPERATIONS["chat_delete"][0] not in required
    settings = load_settings(
        {"GHOSTWRITER_WORKBENCH_KEY": "a" * 32, "AUDIT_REQUIRE_PURPOSE": default.group(2)}, role="api"
    )
    assert settings.require_purpose == required


def test_the_settings_tied_to_the_images_stay_out_of_compose() -> None:
    """The data folder, the socket and the slot uids follow the mounts and the image's users, not ``.env``."""
    for name in ("compose.yaml", "compose.override.yaml", "compose.test.yaml"):
        text = (DOCKER / name).read_text(encoding="utf-8")
        assert [key for key in FIXED_BY_THE_IMAGES if key in text] == [], name
    assert [key for key in FIXED_BY_THE_IMAGES if key in example_values()] == []


def test_every_compose_default_is_the_examples() -> None:
    """Each default in the production and dev shapes, colon-less ones too, is the value ``.env.example`` shows."""
    example = example_values()
    for name in ("compose.yaml", "compose.override.yaml"):
        for key, value in _ANY_DEFAULT.findall((DOCKER / name).read_text(encoding="utf-8")):
            if not key.endswith("_VERSION"):
                assert example.get(key) == value, (name, key)


def test_the_executor_cpu_limit_says_how_to_fit_a_smaller_host() -> None:
    """Docker refuses a CPU limit above the host's CPUs: the example says to lower it."""
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    above = text.split("# WORKBENCH_EXECUTOR_CPUS=", 1)[0].rsplit("\n\n", 1)[1]
    assert "refuses" in above and "CPUs" in above and "or less" in above


# --- the executor ---------------------------------------------------------------------------------------------


def test_executor_has_no_network_and_only_the_listed_capabilities() -> None:
    """No network at all, and root with exactly the six capabilities the supervisor needs."""
    executor = compose()["services"]["executor"]
    assert executor["network_mode"] == "none"
    assert "networks" not in executor and "ports" not in executor and "expose" not in executor
    assert set(executor["cap_add"]) == EXECUTOR_CAPABILITIES
    assert len(executor["cap_add"]) == len(EXECUTOR_CAPABILITIES)
    assert executor["user"] == "0:0"
    assert executor.get("privileged", False) is False


def test_executor_memory_cannot_swap_past_its_limit() -> None:
    """Memory plus swap is the memory limit: slots x job memory + 512 MiB, and 64 processes a slot + 64."""
    executor = compose()["services"]["executor"]
    assert default_of(executor["mem_limit"]) == ("WORKBENCH_EXECUTOR_MEM_LIMIT", "6656m")
    assert executor["memswap_limit"] == executor["mem_limit"]
    slots, job_mb = int(SPEC_DEFAULTS["WORKBENCH_EXECUTOR_SLOTS"]), int(SPEC_DEFAULTS["WORKBENCH_JOB_MEMORY_MB"])
    assert 6656 == slots * job_mb + 512
    assert default_of(executor["pids_limit"]) == ("WORKBENCH_EXECUTOR_PIDS_LIMIT", str(slots * 64 + 64))
    assert default_of(executor["cpus"]) == ("WORKBENCH_EXECUTOR_CPUS", "4")


def test_the_api_is_bounded_in_memory_and_processes() -> None:
    """The api holds every user's requests: its memory, swap included, and its processes have limits (R74)."""
    api = compose()["services"]["api"]
    assert default_of(api["mem_limit"]) == ("WORKBENCH_API_MEM_LIMIT", "2g")
    assert api["memswap_limit"] == api["mem_limit"]
    assert default_of(api["pids_limit"]) == ("WORKBENCH_API_PIDS_LIMIT", "128")
    assert "/tmp:size=512m" in api["tmpfs"]


def test_the_executor_image_strips_setuid_and_setgid_bits() -> None:
    """R1 M-1: no setuid or setgid program in the runtime image, before it is the supervisor's."""
    text = (DOCKER / "Dockerfile.executor").read_text(encoding="utf-8")
    runtime = text.split(" AS runtime", 1)[1].split("\nFROM ", 1)[0]
    assert "RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} +" in runtime
    assert runtime.index("chmod a-s") < runtime.index("USER 0:0")


def test_the_images_leave_out_what_neither_runs() -> None:
    """R1 M-10: the tests, the docs and the build helpers stay out of both images."""
    ignored = set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())
    assert {"docs/", "**/tests/", "docker/", "make/", "Makefile"} <= ignored


def test_ci_widens_the_tests_time_bounds_and_the_root_run_takes_the_factor_in() -> None:
    """R78: CI sets WORKBENCH_TEST_TIME_SCALE=3 for pytest and for the root run, which hands it to the container."""
    ci = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    assert "WORKBENCH_TEST_TIME_SCALE=3" in ci["jobs"]["ci"]["with"]["test-env"].split()
    assert ci["jobs"]["executor-root"]["env"]["WORKBENCH_TEST_TIME_SCALE"] == "3"
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    run = makefile.split("EXECUTOR_ROOT_RUN    :=", 1)[1].split("\nEXECUTOR_ROOT_PYTEST", 1)[0]
    assert "-e WORKBENCH_TEST_TIME_SCALE " in run


def test_the_time_factor_is_one_unless_set_and_never_below_one() -> None:
    """A blank or missing factor is 1; anything that is not a number of at least 1 stops the tests at once."""
    assert _time_scale(None) == _time_scale("  ") == 1.0
    assert (_time_scale("3"), _time_scale("4.5")) == (3.0, 4.5)
    for bad in ("0.5", "-1", "fast", "nan"):
        with pytest.raises(ValueError):
            _time_scale(bad)


def _health_probe() -> str:
    """The executor healthcheck's Python."""
    test = compose()["services"]["executor"]["healthcheck"]["test"]
    assert test[:3] == ["CMD", "python", "-c"]
    return str(test[3])


def test_the_executor_is_healthy_once_its_socket_listens() -> None:
    """The probe asks the kernel for a listening socket on the path; it never connects.

    A connection without a request line would be a malformed request to the supervisor. The probe is run against
    listening, stale and unbound sockets in ``tests/executor/test_supervisor.py``, on Linux, ``make
    test-executor-root`` included.
    """
    probe = _health_probe()
    assert "/run/workbench/executor.sock" in probe
    assert str(load_settings({}, role="executor").executor_socket) == "/run/workbench/executor.sock"
    assert "/proc/net/unix" in probe and "'00010000'" in probe
    assert "connect" not in probe and "socket" not in probe.replace("executor.sock", "")
    depends = compose()["services"]["api"]["depends_on"]
    assert depends == {
        "volume-permissions": {"condition": "service_completed_successfully"},
        "executor": {"condition": "service_healthy"},
    }


# --- networks, volumes, ports, restarts -----------------------------------------------------------------------


def test_api_joins_only_workbench_net() -> None:
    """The api is ``workbench-api`` on ``workbench-net``, port 8000; nothing joins inference-net or data-net."""
    spec = compose()
    api = spec["services"]["api"]
    assert api["networks"] == {"workbench-net": {"aliases": ["workbench-api"]}}
    assert api["expose"] == ["8000"]
    assert spec["networks"] == {"workbench-net": {"external": True, "name": "${WORKBENCH_NET:-workbench-net}"}}
    assert spec["services"]["volume-permissions"]["network_mode"] == "none"
    text = (DOCKER / "compose.yaml").read_text(encoding="utf-8")
    assert "inference-net" not in text and "data-net" not in text


def test_workspace_volume_is_external() -> None:
    """State lives on the external ``workbench-data``, at ``/data`` in every service; ``down -v`` cannot remove it."""
    spec = compose()
    assert spec["volumes"]["workbench-data"] == {"external": True, "name": "workbench-data"}
    for name in SERVICES:
        assert "workbench-data:/data" in spec["services"][name]["volumes"], name


def test_only_the_dev_overlay_publishes_a_port() -> None:
    """Production publishes nothing; ``compose.override.yaml`` publishes the api alone."""
    assert [name for name, service in compose()["services"].items() if "ports" in service] == []
    override = compose("compose.override.yaml")["services"]
    assert list(override) == ["api"]
    assert override["api"]["ports"] == ["127.0.0.1:${WORKBENCH_HOST_PORT:-8100}:8000"]


def test_restart_policies() -> None:
    """The api and the executor come back; the one-shot never reruns on its own."""
    services = compose()["services"]
    assert {name: services[name]["restart"] for name in SERVICES} == {
        "volume-permissions": "no",
        "api": "unless-stopped",
        "executor": "unless-stopped",
    }


# --- hardening ------------------------------------------------------------------------------------------------


def test_every_service_is_hardened() -> None:
    """Deploy ADR 0001: a read-only root, no privilege escalation, no capability but those added back, a /tmp tmpfs."""
    for name, service in compose()["services"].items():
        assert service["read_only"] is True, name
        assert "no-new-privileges:true" in service["security_opt"], name
        assert service["cap_drop"] == ["ALL"], name
        assert any(str(entry).split(":", 1)[0] == "/tmp" for entry in service["tmpfs"]), name
    assert "cap_add" not in compose()["services"]["api"]


def test_no_core_dumps() -> None:
    """A core file of the api or the executor could hold a call's data: both services set the core limit to 0."""
    services = compose()["services"]
    assert {name: services[name]["ulimits"] for name in ("api", "executor")} == {
        "api": {"core": 0},
        "executor": {"core": 0},
    }


def test_volume_permissions_has_only_what_prepare_volume_needs() -> None:
    """Root with three capabilities, no network, running prepare_volume once."""
    service = compose()["services"]["volume-permissions"]
    assert set(service["cap_add"]) == VOLUME_PERMISSIONS_CAPABILITIES
    assert len(service["cap_add"]) == len(VOLUME_PERMISSIONS_CAPABILITIES)
    assert service["user"] == "0:0"
    assert service["command"] == ["python", "-m", "workbench.executor.prepare_volume"]


@pytest.mark.parametrize("dockerfile", ["Dockerfile.api", "Dockerfile.executor"])
def test_both_images_pin_their_bases_by_tag_and_digest(dockerfile: str) -> None:
    """Every ``FROM`` names a stage of the file, or an image by its tag and its sha256 digest."""
    stages: set[str] = set()
    bases: list[str] = []
    for line in (DOCKER / dockerfile).read_text(encoding="utf-8").splitlines():
        found = re.fullmatch(r"FROM (\S+)(?: AS (\S+))?", line)
        if found is None:
            continue
        image, stage = found.groups()
        if image not in stages:
            assert re.fullmatch(r"[\w.\-/]+:[\w.\-]+@sha256:[0-9a-f]{64}", image), image
            bases.append(image.split("@", 1)[0])
        if stage:
            stages.add(stage)
    assert "docker.io/library/python:3.12-slim-trixie" in bases


def _requirement_names(requirements: list[str]) -> set[str]:
    """The distribution names of a list of requirements, lower-cased."""
    return {re.split(r"[\s<>=!~;\[]", requirement, maxsplit=1)[0].lower() for requirement in requirements}


def _shipped_syncs(dockerfile: str) -> list[str]:
    """Every ``uv sync`` of the stages the production image is built from: the last stage and what it takes from."""
    text = (DOCKER / dockerfile).read_text(encoding="utf-8").replace("\\\n", " ")
    stages: list[tuple[str | None, str, list[str]]] = []
    for line in text.splitlines():
        found = re.fullmatch(r"FROM (\S+)(?: AS (\S+))?", line.strip())
        if found is not None:
            stages.append((found.group(2), found.group(1), []))
        elif stages and line.strip() and not line.lstrip().startswith("#"):
            stages[-1][2].append(line.strip())
    by_name = {name: stage for stage in stages if (name := stage[0]) is not None}
    shipped: list[tuple[str | None, str, list[str]]] = []
    todo = [stages[-1]]
    while todo:
        stage = todo.pop()
        if stage in shipped:
            continue
        shipped.append(stage)
        sources = [stage[1], *(m for line in stage[2] for m in re.findall(r"--from=(\S+)", line))]
        todo.extend(by_name[source] for source in sources if source in by_name)
    return [line for _, _, lines in shipped for line in lines if "uv sync" in line]


def test_the_sdk_stays_in_the_dev_group() -> None:
    """The MCP SDK tests the door and never ships: it is in the dev group alone, and each image syncs without it."""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "mcp" in _requirement_names(project["dependency-groups"]["dev"])
    assert "mcp" not in _requirement_names(project["project"]["dependencies"])
    for extra in project["project"]["optional-dependencies"].values():
        assert "mcp" not in _requirement_names(extra)
    for dockerfile in ("Dockerfile.api", "Dockerfile.executor"):
        syncs = _shipped_syncs(dockerfile)
        assert len(syncs) == 2, dockerfile
        assert [sync for sync in syncs if "--no-dev" not in sync] == [], dockerfile
    every_line = (DOCKER / "Dockerfile.executor").read_text(encoding="utf-8").replace("\\\n", " ").splitlines()
    assert [line for line in every_line if "uv sync" in line and "--no-dev" not in line], "the test stage's is left out"


def test_the_test_stack_keeps_nothing_of_a_developers() -> None:
    """Its own project, volume, network and images: never ``workbench-data`` or ``workbench-net``."""
    test = compose("compose.test.yaml")
    assert test["name"] == "workbench-test" != compose()["name"]
    assert test["volumes"] == {"workbench-data": {"external": False, "name": "workbench-test-data"}}
    assert test["networks"] == {"workbench-net": {"external": False, "name": "workbench-test-net"}}
    assert {name: test["services"][name]["image"].rsplit(":", 1)[1] for name in SERVICES} == dict.fromkeys(
        SERVICES, "test"
    )


def test_dependabot_watches_only_folders_that_exist() -> None:
    """Each update entry names a folder of the repo: none is left for a stack that went."""
    updates = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))["updates"]
    assert [u["directory"] for u in updates if not (ROOT / u["directory"].lstrip("/")).is_dir()] == []


# --- the bundle script ----------------------------------------------------------------------------------------


def test_the_bundle_script_parses() -> None:
    """``bash -n``: the script is valid bash."""
    done = subprocess.run(["bash", "-n", "scripts/bundle_images.sh"], cwd=ROOT, capture_output=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert os.access(ROOT / "scripts" / "bundle_images.sh", os.X_OK)


def test_the_bundle_ships_both_images_compose_runs() -> None:
    """The images are compose's own, both built here; their tag is the version the script sets, and none is pulled.

    The script takes the images from ``compose config --images`` and saves those without a registry, the locally
    built ones; compose names exactly ``workbench-api`` and ``workbench-executor``, so both ship and no pulled
    runtime image is left out.
    """
    script = (ROOT / "scripts" / "bundle_images.sh").read_text(encoding="utf-8")
    assert ". scripts/bundle-lib.sh" in script
    assert "bundle_checkout_release workbench" in script and "bundle_version workbench;" in script
    assert "-f docker/compose.yaml" in script and "config --images" in script
    assert '"workbench-built-${VER}.tar.gz"' in script
    assert "BUNDLE_PULLED" not in script
    images = {name: service["image"] for name, service in compose()["services"].items()}
    assert images == {
        "volume-permissions": "workbench-executor:${WORKBENCH_VERSION:-latest}",
        "api": "workbench-api:${WORKBENCH_VERSION:-latest}",
        "executor": "workbench-executor:${WORKBENCH_VERSION:-latest}",
    }
    built = {name for name, service in compose()["services"].items() if "build" in service}
    assert {images[name] for name in built} == set(images.values())


# --- the Makefile ---------------------------------------------------------------------------------------------


def _make(*args: str, stdin: str = "", env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run make in the repository, never with a ``.env`` of the developer's in play (``-n`` or a fake docker)."""
    return subprocess.run(
        ["make", "--no-print-directory", *args],
        cwd=ROOT,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )


def _targets() -> set[str]:
    """Every target the Makefile and the vendored common.mk declare."""
    found: set[str] = set()
    for path in (ROOT / "Makefile", ROOT / "make" / "common.mk"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(".PHONY:"):
                found.update(line.split(":", 1)[1].split())
    return found


def test_every_target_has_a_help_line() -> None:
    """``make help`` names every target."""
    shown = set(re.findall(r"^  make ([a-z][a-z-]*)", _make("help").stdout, flags=re.MULTILINE))
    assert _targets() - shown == set()
    assert {"nuke", "health"} <= _targets()


def test_nuke_removes_this_projects_volume_and_nothing_else() -> None:
    """Without running it: ``down`` of this project, then ``docker volume rm workbench-data``; no other target can."""
    planned = _make("-n", "nuke").stdout
    assert "down" in planned and "docker volume rm workbench-data" in planned
    recipes = [
        line
        for path in (ROOT / "Makefile", ROOT / "make" / "common.mk")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("\t") and "volume rm" in line
    ]
    assert recipes == ["\tdocker volume rm $(VOLUMES)"]


def test_nuke_asks_for_the_word_workbench_and_stops_at_anything_else(tmp_path: Path) -> None:
    """A wrong answer, or none, ends it before any docker command; a stand-in docker on the PATH proves it."""
    calls = tmp_path / "calls"
    fake = tmp_path / "bin" / "docker"
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n', encoding="utf-8")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}"}
    for answer in ("nuke\n", "Workbench\n", "workbench-data\n", ""):
        done = _make("nuke", stdin=answer, env=env)
        assert done.returncode != 0, answer
        assert "aborted" in done.stdout + done.stderr, answer
        assert not calls.exists(), answer
    assert "Type 'workbench' to confirm" in _make("-n", "nuke").stdout


def test_nuke_answered_workbench_takes_the_project_down_then_removes_its_volume(tmp_path: Path) -> None:
    """The confirming answer runs exactly the two commands, in order.

    The stand-in docker is the only docker the run can find: the PATH is its folder and a folder of links to the few
    tools make needs, checked before the run, so no answer here could reach a real one.
    """
    calls = tmp_path / "calls"
    fake = tmp_path / "bin" / "docker"
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n', encoding="utf-8")
    fake.chmod(0o755)
    tools = tmp_path / "tools"
    tools.mkdir()
    for tool in ("make", "tr", "cat", "date", "git"):
        if (found := shutil.which(tool)) is not None:
            (tools / tool).symlink_to(found)
    path = os.pathsep.join([str(fake.parent), str(tools)])
    assert shutil.which("docker", path=path) == str(fake)
    assert [folder for folder in path.split(os.pathsep) if shutil.which("docker", path=folder)] == [str(fake.parent)]
    done = _make("nuke", stdin="workbench\n", env={**os.environ, "PATH": path})
    assert done.returncode == 0, done.stdout + done.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "compose --env-file .env -f docker/compose.yaml down",
        "volume rm workbench-data",
    ]


def test_the_examples_workbench_settings_are_all_commented_out() -> None:
    """``# KEY=value`` shows a default without setting it, so a host that copies the file still follows compose's."""
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    found = [re.match(r"\s*(#\s*)?(WORKBENCH_[A-Z0-9_]*)=", line) for line in lines]
    settings = [match for match in found if match is not None]
    assert len(settings) >= len(SPEC_DEFAULTS)
    assert [match.group(2) for match in settings if match.group(1) is None] == []


def test_no_callers_key_is_required() -> None:
    """The api starts with either key: neither is ``[required]`` or active; docint's is a shared secret."""
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if line.startswith("# [") and "[required]" in line] == []
    assert "# Required" not in lines
    assert "DOCINT_WORKBENCH_KEY=" not in lines
    assert lines[lines.index("# DOCINT_WORKBENCH_KEY=") - 1] == "# [secret] [shared: docint WORKBENCH_API_KEY]"


def test_the_ghostwriter_lines_of_env_example() -> None:
    """Ghostwriter's key is shared and commented out, never active or required; its tools are the code's own."""
    lines = (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
    assert (
        lines[lines.index("# GHOSTWRITER_WORKBENCH_KEY=") - 1] == "# [secret] [shared: ghostwriter WORKBENCH_API_KEY]"
    )
    assert lines[lines.index("# WORKBENCH_RETENTION_DAYS=7") - 1] == "# [shared: open-webui-service, ghostwriter]"
    assert not any(line.startswith("GHOSTWRITER_WORKBENCH_KEY=") for line in lines)
    assert not any("[required]" in line for line in lines if not line.startswith("#   "))
    assert f"# GHOSTWRITER_WORKBENCH_TOOLS={EIGHT_LIST}" in lines
    assert set(EIGHT_LIST.split(",")) == GHOSTWRITER_DEFAULT_TOOLS
    assert "# --- ghostwriter ---" in lines


def test_health_shows_the_containers_and_asks_the_api() -> None:
    """``ps``, then the api's ``/healthz`` from inside its container: it publishes no port in production."""
    planned = _make("-n", "health").stdout
    assert " ps " in planned
    assert "exec -T api" in planned and "/healthz" in planned


def test_the_root_executor_tests_can_read_the_healthcheck() -> None:
    """The image leaves ``docker/`` out: the root run mounts it, read-only, so the probe's test runs there too."""
    planned = _make("-n", "test-executor-root").stdout
    assert planned.count('/docker:/app/docker:ro"') == 2


def test_the_isolation_stack_is_torn_down_on_every_way_out() -> None:
    """A trap takes the test project down, volumes included, on success, failure and Ctrl-C; nothing else."""
    planned = _make("-n", "test-isolation").stdout
    assert "trap " in planned and "EXIT" in planned and "INT" in planned
    downs = re.findall(r"docker compose ([^;']*?) down -v", planned)
    assert downs and all("-p workbench-test " in down for down in downs)
    assert "--env-file" not in planned


def test_the_load_stack_is_torn_down_on_every_way_out() -> None:
    """As the isolation stack: a trap takes the test project down, volumes included; ``.env`` stays out."""
    planned = _make("-n", "test-load").stdout
    assert "trap " in planned and "EXIT" in planned and "INT" in planned
    downs = re.findall(r"docker compose ([^;']*?) down -v", planned)
    assert downs and all("-p workbench-test " in down for down in downs)
    assert "--env-file" not in planned


@pytest.mark.parametrize("target", ["test-isolation", "test-load"])
def test_the_stack_tests_collect_their_own_folder_with_the_executors_libraries(target: str) -> None:
    """CI run 1: a runner's environment has the dev group only; collecting every module failed on openpyxl and docx.

    Each target collects its own folder, whose modules build their files with openpyxl and python-docx, and syncs the
    executor extra for them.
    """
    name = target.removeprefix("test-")
    planned = " ".join(_make("-n", target).stdout.replace("\\\n", " ").split())
    assert f"uv run --locked --extra executor pytest tests/{name} -m {name} " in planned


def test_the_test_stacks_images_are_built_and_never_pulled() -> None:
    """No registry has the ``:test`` images; Compose would try one before building and print its refusal.

    The services that build them say ``build``; the one that only reuses one (the volume's one-shot) says ``never``,
    as its image is there once the build is.
    """
    test = compose("compose.test.yaml")
    policies = {name: service.get("pull_policy") for name, service in test["services"].items()}
    assert policies == {"api": "build", "executor": "build", "volume-permissions": "never"}
