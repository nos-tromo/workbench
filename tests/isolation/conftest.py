"""The running test stack (``make test-isolation``): its containers, as ``docker inspect`` sees them, and the api.

The stack is ``docker/compose.yaml`` with ``docker/compose.test.yaml`` on top, under its own
project name, with a volume and a network of its own, so a developer's data is never touched.
These tests only look at it; ``make test-isolation`` brings it up and takes it down.
"""

import json
import os
import subprocess
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

PROJECT = os.environ.get("WORKBENCH_TEST_PROJECT", "workbench-test")
API_URL = os.environ.get("WORKBENCH_TEST_API", "http://127.0.0.1:18100")
SOCKET = "/run/workbench/executor.sock"
_SETTLE_S = 60.0


def docker(*args: str) -> str:
    """Run the docker CLI and return what it printed.

    Args:
        *args: The arguments after ``docker``.

    Returns:
        Its standard output.

    Raises:
        subprocess.CalledProcessError: The command failed.
    """
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=120).stdout


def container_of(service: str) -> str:
    """Find the one container of a service of the test project.

    Args:
        service: The compose service.

    Returns:
        Its id.
    """
    ids = docker(
        "ps",
        "--all",
        "--quiet",
        "--filter",
        f"label=com.docker.compose.project={PROJECT}",
        "--filter",
        f"label=com.docker.compose.service={service}",
    ).split()
    if len(ids) != 1:
        pytest.fail(f"expected one {service} container of project {PROJECT}, found {len(ids)}: run make test-isolation")
    return ids[0]


def inspect(container: str) -> dict[str, Any]:
    """Read ``docker inspect`` of a container.

    Args:
        container: Its id.

    Returns:
        The container's record.
    """
    return json.loads(docker("inspect", container))[0]


def environment(info: dict[str, Any]) -> dict[str, str]:
    """Read a container's environment from its record.

    Args:
        info: ``docker inspect`` of the container.

    Returns:
        Its variables by name.
    """
    return dict(entry.partition("=")[::2] for entry in info["Config"]["Env"] or [])


def _settled(container: str) -> str | None:
    """Tell why the executor is not yet serving, if it is not.

    Args:
        container: The executor's id.

    Returns:
        ``None`` once it runs, has never restarted and its socket is there; else the reason.
    """
    info = inspect(container)
    if info["RestartCount"]:
        return f"the executor restarted {info['RestartCount']} times"
    if not info["State"]["Running"]:
        return f"the executor is {info['State']['Status']}"
    probe = f"import os, stat, sys; sys.exit(not stat.S_ISSOCK(os.lstat({SOCKET!r}).st_mode))"
    found = subprocess.run(["docker", "exec", container, "python", "-c", probe], capture_output=True, timeout=60)
    return None if found.returncode == 0 else "the executor's socket is not there"


@pytest.fixture(scope="session")
def executor() -> str:
    """The executor's container, once its supervisor serves; it fails with the executor's last log lines else."""
    container = container_of("executor")
    deadline = time.monotonic() + _SETTLE_S
    while (problem := _settled(container)) is not None:
        if time.monotonic() > deadline or "restarted" in problem:
            logs = subprocess.run(["docker", "logs", "--tail", "20", container], capture_output=True, text=True)
            pytest.fail(f"{problem}; its last lines:\n{logs.stdout}{logs.stderr}")
        time.sleep(1)
    return container


@pytest.fixture(scope="session")
def api_info() -> dict[str, Any]:
    """``docker inspect`` of the api's container."""
    return inspect(container_of("api"))


@pytest.fixture(scope="session")
def api(api_info: dict[str, Any]) -> Iterator[httpx.Client]:
    """A client of the api on its published test port, holding the test stack's synthetic caller key."""
    key = environment(api_info)["OPEN_WEBUI_WORKBENCH_KEY"]
    with httpx.Client(base_url=API_URL, headers={"Authorization": f"Bearer {key}"}, timeout=300) as client:
        yield client


def run_python(api: httpx.Client, code: str, *, user: str = "user-a", chat: str = "chat-1") -> httpx.Response:
    """Run ``code`` through the ``run_python`` tool as a synthetic user.

    Args:
        api: The api client.
        code: The Python to run.
        user: The user id Open WebUI would send.
        chat: The chat id Open WebUI would send.

    Returns:
        The api's answer.
    """
    return api.post("/tools/run_python", json={"code": code}, headers={"X-User-Id": user, "X-Session-Id": chat})


def stdout_of(response: httpx.Response) -> str:
    """Take what a successful ``run_python`` printed.

    Args:
        response: The api's answer.

    Returns:
        The job's standard output, stripped.
    """
    assert response.status_code == 200, response.text
    return str(response.json()["stdout"]).strip()
