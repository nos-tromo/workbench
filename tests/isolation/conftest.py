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
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

PROJECT = os.environ.get("WORKBENCH_TEST_PROJECT", "workbench-test")
API_URL = os.environ.get("WORKBENCH_TEST_API", "http://127.0.0.1:18100")
COMPOSE_FILE = Path(__file__).resolve().parents[2] / "docker" / "compose.yaml"
_SETTLE_S = 60.0

#: The MCP revision docint and Ghostwriter speak, and the prefix of the client's keys in ``params._meta``.
MCP_REVISION = "2026-07-28"
_META = "io.modelcontextprotocol/"
#: A synthetic purpose: ``AUDIT_REQUIRE_PURPOSE`` asks one of the MCP door's tools and Ghostwriter's file routes.
PURPOSE = "case_ref=case-0001&reason=isolation+test"


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


def listening_probe() -> list[str]:
    """The executor's healthcheck, as ``docker/compose.yaml`` has it: a socket listens on the supervisor's path.

    Returns:
        The command, without compose's ``CMD``.
    """
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    test = compose["services"]["executor"]["healthcheck"]["test"]
    assert test[0] == "CMD", test
    return list(test[1:])


def _settled(container: str) -> str | None:
    """Tell why the executor is not yet serving, if it is not.

    Args:
        container: The executor's id.

    Returns:
        ``None`` once it runs, has never restarted and its healthcheck's probe finds the socket listening; else the
        reason. A socket file alone is not enough: it outlives a supervisor that was killed.
    """
    info = inspect(container)
    if info["RestartCount"]:
        return f"the executor restarted {info['RestartCount']} times"
    if not info["State"]["Running"]:
        return f"the executor is {info['State']['Status']}"
    found = subprocess.run(["docker", "exec", container, *listening_probe()], capture_output=True, timeout=60)
    return None if found.returncode == 0 else "no socket listens on the executor's path"


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
def api() -> Iterator[httpx.Client]:
    """A client of the api on its published test port; it holds no key, as the healthcheck and the scraper hold none."""
    with httpx.Client(base_url=API_URL, timeout=300) as client:
        yield client


def _tools_call(
    key: str, tool: str, arguments: dict[str, Any], *, user: str, session: str, extra: dict[str, str] | None = None
) -> httpx.Response:
    """Call a tool on the MCP door: a modern ``tools/call`` with :data:`PURPOSE`.

    Args:
        key: The caller's key.
        tool: The tool's name.
        arguments: The tool's arguments.
        user: The user pseudonym the caller would send.
        session: The session pseudonym the caller would send.
        extra: More headers the caller sends, if any.

    Returns:
        The api's answer.
    """
    headers = {
        **(extra or {}),
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": MCP_REVISION,
        "Mcp-Method": "tools/call",
        "Mcp-Name": tool,
        "X-User-Id": user,
        "X-Session-Id": session,
        "X-Audit-Purpose": PURPOSE,
    }
    meta = {f"{_META}protocolVersion": MCP_REVISION, f"{_META}clientCapabilities": {}}
    params = {"name": tool, "arguments": arguments, "_meta": meta}
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
    return httpx.post(f"{API_URL}/mcp", json=message, headers=headers, timeout=300)


def mcp_call(
    api_info: dict[str, Any], code: str, data: list[dict[str, str]], *, user: str = "a" * 64, session: str = "b" * 64
) -> httpx.Response:
    """Run ``code`` over ``data`` through docint's door: a modern ``tools/call`` of inline ``run_python``.

    Args:
        api_info: ``docker inspect`` of the api's container, whose environment holds docint's key.
        code: The Python to run.
        data: The data entries, each ``{"name": ..., "content": ...}``.
        user: The user pseudonym docint would send.
        session: The session pseudonym docint would send.

    Returns:
        The api's answer.
    """
    key = environment(api_info)["DOCINT_WORKBENCH_KEY"]
    return _tools_call(key, "run_python", {"code": code, "data": data}, user=user, session=session)


def ghostwriter(
    api_info: dict[str, Any], method: str, path: str, *, chat: str, user: str = "a" * 64, **kwargs: Any
) -> httpx.Response:
    """Call a file route as Ghostwriter does: its key, the user and the chat pseudonyms, and :data:`PURPOSE`.

    Args:
        api_info: ``docker inspect`` of the api's container, whose environment holds Ghostwriter's key.
        method: The HTTP method.
        path: The route's path.
        chat: The chat pseudonym, sent as ``X-Session-Id``.
        user: The user pseudonym, sent as ``X-User-Id``.
        **kwargs: What ``httpx`` takes besides.

    Returns:
        The api's answer.
    """
    key = environment(api_info)["GHOSTWRITER_WORKBENCH_KEY"]
    headers = {"Authorization": f"Bearer {key}", "X-User-Id": user, "X-Session-Id": chat, "X-Audit-Purpose": PURPOSE}
    return httpx.request(method, f"{API_URL}{path}", headers=headers, timeout=300, **kwargs)


def ghostwriter_mcp(
    api_info: dict[str, Any], tool: str, arguments: dict[str, Any], *, chat: str, user: str = "a" * 64
) -> httpx.Response:
    """Call a workspace tool through Ghostwriter's door: a modern ``tools/call``, as :func:`mcp_call` builds one.

    It says it takes images (``X-Accept-Images: 1``), as Ghostwriter 0.3.0 does.

    Args:
        api_info: ``docker inspect`` of the api's container, whose environment holds Ghostwriter's key.
        tool: The tool's name.
        arguments: The tool's arguments.
        chat: The chat pseudonym, sent as ``X-Session-Id``.
        user: The user pseudonym, sent as ``X-User-Id``.

    Returns:
        The api's answer.
    """
    key = environment(api_info)["GHOSTWRITER_WORKBENCH_KEY"]
    return _tools_call(key, tool, arguments, user=user, session=chat, extra={"X-Accept-Images": "1"})


def run_python(api_info: dict[str, Any], code: str, *, user: str = "user-a", chat: str = "chat-1") -> dict[str, Any]:
    """Run ``code`` through Ghostwriter's ``run_python`` in a synthetic user's chat, as the model would.

    Args:
        api_info: ``docker inspect`` of the api's container, whose environment holds Ghostwriter's key.
        code: The Python to run.
        user: The user id Ghostwriter would send.
        chat: The chat id Ghostwriter would send.

    Returns:
        The call's result (:func:`mcp_result`).
    """
    return mcp_result(ghostwriter_mcp(api_info, "run_python", {"code": code}, chat=chat, user=user))


def mcp_result(response: httpx.Response) -> dict[str, Any]:
    """Take the ``result`` of a JSON-RPC answer of the MCP door.

    Args:
        response: The api's answer.

    Returns:
        The result: ``content``, ``structuredContent`` and ``isError``.
    """
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" not in body, body
    return body["result"]


def stdout_of(result: dict[str, Any]) -> str:
    """Take what a ``run_python`` that ran printed.

    Args:
        result: The call's result.

    Returns:
        The job's standard output, stripped.
    """
    assert result["isError"] is False, result
    return str(result["structuredContent"]["stdout"]).strip()
