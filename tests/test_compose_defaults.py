"""The operator surface, read as files; nothing here starts a container or touches a volume.

Compose's settings and shape, the images' bases, ``.env.example``, the bundle script and the Makefile's targets.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from workbench.config import load_settings

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

#: The executor supervisor's capabilities (docs/sandbox.md).
EXECUTOR_CAPABILITIES = {"SETUID", "SETGID", "CHOWN", "FOWNER", "DAC_OVERRIDE", "KILL"}

#: What prepare_volume needs as root: hand parts over (CHOWN), set the modes of parts it does not own (FOWNER), and
#: open the api's 0700 folders and 0600 database, read-only (DAC_READ_SEARCH).
VOLUME_PERMISSIONS_CAPABILITIES = {"CHOWN", "FOWNER", "DAC_READ_SEARCH"}

#: Tied to the mounts and the image's users (R32): never set through compose.
FIXED_BY_THE_IMAGES = ("WORKBENCH_DATA_DIR", "WORKBENCH_EXECUTOR_SOCKET", "WORKBENCH_SLOT_UID_BASE")

SERVICES = ("volume-permissions", "api", "executor")

_DEFAULT = re.compile(r"\$\{([A-Z0-9_]+):-([^}]*)\}")


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


def test_the_settings_tied_to_the_images_stay_out_of_compose() -> None:
    """The data folder, the socket and the slot uids follow the mounts and the image's users, not ``.env``."""
    for name in ("compose.yaml", "compose.override.yaml", "compose.test.yaml"):
        text = (DOCKER / name).read_text(encoding="utf-8")
        assert [key for key in FIXED_BY_THE_IMAGES if key in text] == [], name
    assert [key for key in FIXED_BY_THE_IMAGES if key in example_values()] == []


def test_every_compose_default_is_the_examples() -> None:
    """Each ``${VAR:-default}`` in the production and dev shapes is the value ``.env.example`` shows."""
    example = example_values()
    for name in ("compose.yaml", "compose.override.yaml"):
        for key, value in _DEFAULT.findall((DOCKER / name).read_text(encoding="utf-8")):
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
    assert override["api"]["ports"] == ["${WORKBENCH_HOST_PORT:-8100}:8000"]


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


def test_the_test_stack_keeps_nothing_of_a_developers() -> None:
    """Its own project, volume, network and images: never ``workbench-data`` or ``workbench-net``."""
    test = compose("compose.test.yaml")
    assert test["name"] == "workbench-test" != compose()["name"]
    assert test["volumes"] == {"workbench-data": {"external": False, "name": "workbench-test-data"}}
    assert test["networks"] == {"workbench-net": {"external": False, "name": "workbench-test-net"}}
    assert {name: test["services"][name]["image"].rsplit(":", 1)[1] for name in SERVICES} == dict.fromkeys(
        SERVICES, "test"
    )


class _ComposeLoader(yaml.SafeLoader):
    """Reads compose's ``!reset`` tag as the pair ``("!reset", value)``."""


_ComposeLoader.add_constructor("!reset", lambda loader, node: ("!reset", loader.construct_sequence(node)))  # type: ignore[arg-type]


def contract_stack(name: str = "compose.yaml") -> dict[str, Any]:
    """Read a file of ``contract/``: the stack, or the override it includes last."""
    return yaml.load((ROOT / "contract" / name).read_text(encoding="utf-8"), Loader=_ComposeLoader)


#: The shape of the Open WebUI pin: a release tag and a digest. The pin itself lives in contract/compose.yaml only, so a
#: Dependabot bump there passes this check and reaches the contract job, which then runs the new image.
OPEN_WEBUI_IMAGE = re.compile(r"ghcr\.io/open-webui/open-webui:v[0-9]+\.[0-9]+\.[0-9]+@sha256:[0-9a-f]{64}")

#: Open WebUI's production switches that bear on the contract (open-webui-service docker/compose.yaml), and the two
#: the connection adds.
CONTRACT_SWITCHES = {
    "ENABLE_PERSISTENT_CONFIG": "false",
    "AUDIT_LOG_LEVEL": "METADATA",
    "ENABLE_AUDIT_STDOUT": "true",
    "ENABLE_AUDIT_GET_REQUESTS": "true",
    "AUDIT_EXCLUDED_PATHS": "/terminals",
    "ENABLE_TOOLS": "false",
    "ENABLE_FUNCTIONS": "false",
    "ENABLE_CODE_INTERPRETER": "false",
    "ENABLE_CODE_EXECUTION": "false",
    "ENABLE_TOOL_SERVERS": "true",
}


def test_the_contract_stack_keeps_nothing_of_a_developers_or_the_test_stacks() -> None:
    """Its own project, volume and network on top of the test stack; the api publishes nothing, Open WebUI loopback.

    The test stack is changed by an override file that ends the ``include``'s path list, Compose's documented way;
    the including file only adds services, since redefining an included resource there is an error.
    """
    stack = contract_stack()
    assert stack["name"] == "workbench-contract"
    assert stack["include"] == [
        {"path": ["../docker/compose.yaml", "../docker/compose.test.yaml", "workbench.override.yaml"]}
    ]
    assert set(stack) == {"name", "include", "services"}
    assert set(stack["services"]) == {"open-webui", "stub-model"}
    override = contract_stack("workbench.override.yaml")
    assert override["volumes"] == {"workbench-data": {"name": "workbench-contract-data"}}
    assert override["networks"] == {"workbench-net": {"name": "workbench-contract-net"}}
    assert set(override["services"]) == {"api"}
    assert override["services"]["api"]["ports"] == ("!reset", [])
    open_webui = stack["services"]["open-webui"]
    assert open_webui["ports"] == ["127.0.0.1:18180:8080"]
    assert "volumes" not in open_webui
    assert open_webui["networks"] == ["workbench-net"]


def test_the_contract_stacks_stub_model_is_hardened_and_publishes_nothing() -> None:
    """The test-only model: the api's test image, the script read-only, hardened as the services are, no port."""
    stub = contract_stack()["services"]["stub-model"]
    assert stub["image"] == "workbench-api:test"
    assert "build" not in stub and "ports" not in stub
    assert stub["volumes"] == ["./stub_model.py:/stub/stub_model.py:ro"]
    assert stub["networks"] == ["workbench-net"]
    assert stub["read_only"] is True
    assert stub["security_opt"] == ["no-new-privileges:true"]
    assert stub["cap_drop"] == ["ALL"]
    assert stub["tmpfs"] == ["/tmp:size=1m"]
    assert "cap_add" not in stub and "user" not in stub  # the image's own uid, 10001
    assert (ROOT / "contract" / "stub_model.py").is_file()


def test_the_contract_runs_open_webui_as_production_does() -> None:
    """Open WebUI by tag and digest, hardened as in production, its switches, one bearer connection, Dependabot's.

    Hardened as open-webui-service runs it: uid 10001, a read-only root, every capability dropped, no privilege
    escalation.

    The image's value is contract/compose.yaml's alone, so a Dependabot bump passes here and reaches the contract job.
    Its one provider is an OpenAI-compatible endpoint, as in production, here the stub model; Ollama stays off.
    """
    open_webui = contract_stack()["services"]["open-webui"]
    assert OPEN_WEBUI_IMAGE.fullmatch(open_webui["image"]), open_webui["image"]
    assert open_webui["user"] == "10001:10001"
    assert open_webui["read_only"] is True
    assert open_webui["cap_drop"] == ["ALL"]
    assert "cap_add" not in open_webui
    assert open_webui["security_opt"] == ["no-new-privileges:true"]
    env = open_webui["environment"]
    assert {key: env.get(key) for key in CONTRACT_SWITCHES} == CONTRACT_SWITCHES
    assert (env["ENABLE_OPENAI_API"], env["OPENAI_API_BASE_URL"], env["ENABLE_OLLAMA_API"]) == (
        "true",
        "http://stub-model:9000/v1",
        "false",
    )
    assert open_webui["depends_on"]["stub-model"] == {"condition": "service_healthy"}
    key = "${WORKBENCH_CONTRACT_KEY:?make test-contract sets it}"
    (connection,) = json.loads(env["TERMINAL_SERVER_CONNECTIONS"].replace(key, "synthetic-key"))
    assert (connection["id"], connection["url"], connection["auth_type"]) == (
        "workbench",
        "http://workbench-api:8000",
        "bearer",
    )
    assert connection["key"] == "synthetic-key"
    assert connection["config"] == {
        "chat_uploads": "filesystem",
        "access_grants": [{"principal_type": "user", "principal_id": "*", "permission": "read"}],
    }
    updates = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))["updates"]
    assert [u["directory"] for u in updates if u["package-ecosystem"] == "docker-compose"] == ["/contract"]


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


@pytest.mark.parametrize(
    ("target", "project"), [("test-load", "workbench-test"), ("test-contract", "workbench-contract")]
)
def test_the_load_and_contract_stacks_are_torn_down_on_every_way_out(target: str, project: str) -> None:
    """As the isolation stack: a trap takes the target's own project down, volumes included; ``.env`` stays out."""
    planned = _make("-n", target).stdout
    assert "trap " in planned and "EXIT" in planned and "INT" in planned
    downs = re.findall(r"docker compose ([^;']*?) down -v", planned)
    assert downs and all(f"-p {project} " in down for down in downs)
    assert "--env-file" not in planned
    assert f"pytest -m {target.removeprefix('test-')} " in planned


def test_the_contract_makes_its_key_and_secret_for_each_run() -> None:
    """Nothing to keep or leak: the caller key and Open WebUI's forwarding secret exist only in the run's shell."""
    planned = _make("-n", "test-contract").stdout
    for name in ("WORKBENCH_CONTRACT_KEY", "WORKBENCH_CONTRACT_JWT_SECRET"):
        assert f'{name}="$(openssl rand -hex 32)"' in planned
