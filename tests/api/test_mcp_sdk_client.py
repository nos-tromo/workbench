"""The official MCP SDK's client against the door, on loopback: the modern era from discovery to a tool call.

The SDK is in the dev group only and never in an image. The api is served by uvicorn on a socket bound to
``127.0.0.1`` in a thread, behind a recorder of each request's ``MCP-Protocol-Version``; the client reaches nothing
else (no proxy from the environment), so nothing leaves the host.
"""

import asyncio
import contextlib
import json
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx2
import jsonschema
import pytest
import uvicorn
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.types import Receive, Scope, Send

from tests.api.conftest import DOCINT_KEY
from tests.api.test_tools_documents import Scripted
from tests.api.test_tools_python import ran
from tests.conftest import scaled
from workbench import __version__
from workbench.api.audit import AuditLog
from workbench.api.jobs import JobQueue
from workbench.api.main import create_app
from workbench.api.metrics import Metrics
from workbench.api.store import Store
from workbench.config import Settings

PASSAGES = {"name": "passages.json", "content": '[{"n": 1, "author": "user-a"}, {"n": 2, "author": "user-b"}]'}
ARGS = {
    "code": "import pandas as pd\nprint(pd.read_json('passages.json')['author'].nunique(), 'authors')",
    "data": [PASSAGES],
    "time_limit_s": 30,
}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
COUNTS = b"author,posts\nuser-a,12\nuser-b,7\n"


@pytest.fixture
def app_with_scripted_executor(settings: Settings, store: Store, audit: AuditLog) -> FastAPI:
    """The real app over an executor that answers as code that printed ``2 authors`` and wrote a chart and a table."""
    executor = Scripted(settings.data_dir)
    executor.payload = ran("2 authors")
    executor.files = {"chart.png": PNG, "counts.csv": COUNTS}
    return create_app(
        settings, store=store, audit=audit, jobs=JobQueue(settings, executor, store, Metrics(settings.data_dir))
    )


@contextlib.contextmanager
def served(app: FastAPI) -> Iterator[tuple[str, list[str | None]]]:
    """Serve ``app`` with uvicorn on loopback, in a thread, recording each request's ``MCP-Protocol-Version``."""
    recorded: list[str | None] = []

    async def recorder(scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            found = [value for name, value in scope["headers"] if name == b"mcp-protocol-version"]
            recorded.append(found[0].decode("latin-1") if found else None)
        await app(scope, receive, send)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(recorder, lifespan="on", log_level="warning", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + scaled(10)
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}/mcp", recorded
    finally:
        server.should_exit = True
        thread.join(scaled(10))
        assert not thread.is_alive(), "uvicorn did not shut down"
        sock.close()


async def converse(url: str) -> tuple[str, Any, list[Any], Any]:
    """Discover, list the tools and call ``run_python`` as docint's client would, with the SDK's own client.

    Returns:
        The revision the client settled on, the server it names, the tools listed and the call's result.
    """
    headers = {"Authorization": f"Bearer {DOCINT_KEY}", "X-User-Id": "user-a", "X-Session-Id": "chat-1"}
    async with httpx2.AsyncClient(headers=headers, trust_env=False, timeout=scaled(30)) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            listed = await client.list_tools()
            called = await client.call_tool("run_python", ARGS)
            return client.protocol_version, client.server_info, listed.tools, called


def test_the_official_client_speaks_the_modern_era(app_with_scripted_executor: FastAPI) -> None:
    """Discovery at 2026-07-28 with no ``initialize`` to fall back on; the listed tool runs and validates."""
    with served(app_with_scripted_executor) as (url, recorded):
        version, server, tools, result = asyncio.run(asyncio.wait_for(converse(url), scaled(60)))
    assert version == "2026-07-28"
    assert (server.name, server.version) == ("workbench", __version__)
    assert [tool.name for tool in tools] == ["run_python"]
    (tool,) = tools
    assert tool.input_schema["properties"]["time_limit_s"]["maximum"] == 60
    jsonschema.validate(ARGS, tool.input_schema)
    assert result.is_error is False and result.structured_content["stdout"] == "2 authors"
    assert [file["name"] for file in result.structured_content["files"]] == ["chart.png", "counts.csv"]
    assert [block.type for block in result.content] == ["text", "image", "resource"]
    assert json.loads(result.content[0].text) == result.structured_content
    assert recorded and set(recorded) == {"2026-07-28"}
