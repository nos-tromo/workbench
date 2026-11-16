"""The official MCP SDK's client against Ghostwriter's door, on loopback: the eight workspace tools, listed and called.

The api runs the real runners (the in-process executor of ``tests/api/test_tools_templates.py``), so the table
preview of a written csv is the executor's own. The client reaches nothing but the socket ``served`` binds.
"""

import asyncio
from pathlib import Path
from typing import Any, Final

import httpx2
import jsonschema
import pytest
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from tests.api.conftest import GHOSTWRITER_KEY
from tests.api.test_files import AuditLines
from tests.api.test_mcp_sdk_client import served
from tests.api.test_tools_templates import Local
from tests.callers import EIGHT
from tests.conftest import scaled
from workbench.api.audit import AuditLog
from workbench.api.jobs import JobQueue
from workbench.api.main import create_app
from workbench.api.metrics import Metrics
from workbench.api.store import Store
from workbench.config import Settings

TABLE: Final[dict[str, Any]] = {
    "name": "counts",
    "format": "csv",
    "columns": ["author", "posts"],
    "rows": [["user-a", 12], ["user-b", 7]],
}
HEADERS: Final = {
    "Authorization": f"Bearer {GHOSTWRITER_KEY}",
    "X-User-Id": "user-a",
    "X-Session-Id": "chat-1",
    "X-Audit-Purpose": "reason=ghostwriter+chat",
    "X-Request-ID": "req-1",
}


@pytest.fixture
def app_with_local_executor(settings: Settings, store: Store, audit: AuditLog, tmp_path: Path) -> FastAPI:
    """The real app over an executor that runs the real runners in this process."""
    executor = Local(settings.data_dir, tmp_path / "jobs")
    return create_app(
        settings, store=store, audit=audit, jobs=JobQueue(settings, executor, store, Metrics(settings.data_dir))
    )


async def converse(url: str) -> tuple[list[Any], Any, Any]:
    """List the tools, write a table and list the files, as Ghostwriter's client would, with the SDK's own client.

    Returns:
        The tools listed, the ``write_table`` result and the ``list_files`` result.
    """
    async with httpx2.AsyncClient(headers=HEADERS, trust_env=False, timeout=scaled(30)) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            listed = await client.list_tools()
            written = await client.call_tool("write_table", TABLE)
            files = await client.call_tool("list_files", {})
            return listed.tools, written, files


def test_the_official_client_lists_and_calls_the_workspace_tools(
    app_with_local_executor: FastAPI, audit_lines: AuditLines
) -> None:
    """Eight tools, each with schemas the validator accepts; a table written with its preview, then listed."""
    with served(app_with_local_executor) as (url, recorded):
        tools, written, files = asyncio.run(asyncio.wait_for(converse(url), scaled(60)))
    assert [tool.name for tool in tools] == sorted(EIGHT)
    for tool in tools:
        jsonschema.Draft202012Validator.check_schema(tool.input_schema)
        assert tool.output_schema is not None, tool.name
        jsonschema.Draft202012Validator.check_schema(tool.output_schema)
    (write_table,) = [tool for tool in tools if tool.name == "write_table"]
    assert written.is_error is False
    assert write_table.output_schema is not None
    jsonschema.validate(written.structured_content, write_table.output_schema)
    assert written.structured_content["outputs"][0]["preview"]["columns"] == ["author", "posts"]
    assert [block.type for block in written.content] == ["text"]
    assert files.is_error is False
    assert files.structured_content["files"][0]["path"] == "/workspace/chat-1/counts.csv"
    assert files.structured_content["outputs"] == []
    assert recorded and set(recorded) == {"2026-07-28"}
    (event,) = [e for e in audit_lines() if e["operation"] == "tool.write_table"]
    assert event["purpose"] == {"reason": "ghostwriter chat"}
    assert event["request_id"] == "req-1"
