"""One tool core: the specs of each form, the start check of each caller's list, and the answers both doors give."""

import dataclasses
from typing import Any, cast

import pytest
from fastapi import Depends, FastAPI, HTTPException

from tests.api.conftest import DOCINT_KEY, KEY
from workbench.api.auth import Caller, CallerRegistry, require_tool
from workbench.api.errors import MAX_ERRORS, compact, describe
from workbench.api.files import DisplayedFile
from workbench.api.jobs import Busy, RequestTooLarge
from workbench.api.openapi import TOOL_TAG
from workbench.api.registry import (
    FORM_TOOLS,
    INLINE_TOOLS,
    MCP_WORKSPACE_TOOLS,
    WORKSPACE_TOOLS,
    Files,
    Registry,
    ToolSpec,
    allowed_tools,
    workspace_specs,
)
from workbench.api.store import Conflict, NotFound, QuotaExceeded, StoreError
from workbench.api.tools import FileList, PythonRun, RunPythonRequest
from workbench.config import ConfigError, Settings
from workbench.paths import PathError
from workbench.utils.audit_event import AuditWriteError

# --- the specs ------------------------------------------------------------------------------------------------------


def test_the_registry_equals_the_tool_routes(app: FastAPI) -> None:
    """The nine workspace specs are the routes tagged as tools, each titled and described, none served on MCP."""
    specs = workspace_specs(app)
    assert len(specs) == 9 and {s.name for s in specs} == WORKSPACE_TOOLS
    assert all(s.form == "workspace" and s.run is None and s.title and s.description for s in specs)
    assert all(s.input_schema is None for s in specs)
    by_name = {s.name: s for s in specs}
    assert (by_name["run_python"].input_model, by_name["run_python"].output_model) == (RunPythonRequest, PythonRun)
    assert (by_name["list_files"].input_model, by_name["list_files"].output_model) == (None, FileList)
    assert (by_name["display_file"].input_model, by_name["display_file"].output_model) == (None, DisplayedFile)
    registry: Registry = app.state.registry
    open_webui = cast(CallerRegistry, app.state.callers).get("open-webui")
    assert [s.name for s in registry.for_caller(open_webui)] == sorted(WORKSPACE_TOOLS)


def _spec(name: str, form: Files) -> ToolSpec:
    """A synthetic spec named ``name``, of the form ``form``."""
    return ToolSpec(name=name, title=name, description=f"tool {name}", input_model=None, output_model=None, form=form)


def test_a_caller_sees_the_tools_of_its_form_in_name_order() -> None:
    """A caller finds the specs of its own file form that its list names, in the order of their names."""
    b, a, c = _spec("b", "workspace"), _spec("a", "workspace"), _spec("c", "inline")
    registry = Registry([b, a, c])
    terminal = Caller("open-webui", KEY, frozenset({"a", "b", "c"}))
    inline = Caller("docint", DOCINT_KEY, frozenset({"c"}), door="mcp", files="inline")
    assert registry.for_caller(terminal) == [a, b]
    assert registry.for_caller(inline) == [c]
    assert registry.find(inline, "a") is None
    assert registry.find(inline, "c") is c
    assert registry.find(terminal, "c") is None
    assert registry.find(dataclasses.replace(terminal, tools=frozenset({"b"})), "a") is None
    with pytest.raises(ValueError, match="two tools"):
        Registry([a, _spec("a", "workspace")])


@pytest.mark.parametrize("tools", [frozenset({"list_files"}), frozenset({"run_python", "nowhere"})])
def test_a_tool_without_the_callers_form_stops_the_start(settings: Settings, tools: frozenset[str]) -> None:
    """Docint brings its data with each call: a workspace tool, or a name no tool has, on its list stops the start.

    The message names the variable and the tools it may list, never a name the variable was set to.
    """
    with pytest.raises(ConfigError, match="DOCINT_WORKBENCH_TOOLS") as refused:
        CallerRegistry.from_settings(dataclasses.replace(settings, docint_tools=tools))
    assert [name for name in tools - INLINE_TOOLS if name in str(refused.value)] == []


def test_allowed_tools_by_door_and_form() -> None:
    """The terminal door serves every workspace tool; the MCP door all but ``display_file``, or the inline tools."""
    assert allowed_tools("terminal", "workspace") == WORKSPACE_TOOLS
    assert allowed_tools("mcp", "workspace") == WORKSPACE_TOOLS - {"display_file"} == MCP_WORKSPACE_TOOLS
    assert allowed_tools("mcp", "inline") == INLINE_TOOLS


def test_the_tools_of_each_form_are_read_only() -> None:
    """The start check's sets cannot be changed while the api runs."""
    with pytest.raises(TypeError):
        cast(dict[str, frozenset[str]], FORM_TOOLS)["inline"] = WORKSPACE_TOOLS


def _write_table() -> dict[str, str]:
    """A synthetic tool route's endpoint."""
    return {}


@pytest.mark.parametrize(("guard", "starts"), [(None, False), ("write_document", False), ("write_table", True)])
def test_a_tool_route_without_its_own_guard_stops_the_start(guard: str | None, starts: bool) -> None:
    """A tool route must enforce the caller's list under its own name: no guard, or another tool's, stops the start."""
    app = FastAPI()
    app.add_api_route(
        "/tools/write_table",
        _write_table,
        methods=["POST"],
        tags=[TOOL_TAG],
        operation_id="write_table",
        summary="Write a table",
        description="Write a table into a new file of this conversation.",
        dependencies=[] if guard is None else [Depends(require_tool(guard))],
    )
    if starts:
        assert [spec.name for spec in workspace_specs(app)] == ["write_table"]
        return
    with pytest.raises(ValueError, match="write_table"):
        workspace_specs(app)


# --- the answers ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "answer"),
    [
        (PathError("invalid file name"), (400, "invalid file name")),
        (NotFound("x"), (404, "not found")),
        (Conflict("already exists"), (409, "already exists")),
        (QuotaExceeded("quota exceeded"), (507, "quota exceeded")),
        (AuditWriteError("x"), (500, "audit unavailable")),
        (Busy("queue_full"), (503, "busy, retry later")),
        (RequestTooLarge("x"), (422, "request too large")),
        (StoreError("x"), (500, "storage error")),
        (HTTPException(422, "the code could not be completed: time"), (422, "the code could not be completed: time")),
        (RuntimeError("/data/workspaces/user-a"), (500, "internal error")),
    ],
)
def test_describe_is_what_the_handlers_answer(exc: Exception, answer: tuple[int, str]) -> None:
    """Each error gets the status and the short fixed text the terminal door answers it with."""
    assert describe(exc) == answer


def test_compact_keeps_where_and_why_never_the_value() -> None:
    """At most 20 errors, each its type, location and message: never the input or its context; a long part is cut."""
    long_key = "column-" + "x" * 393
    errors = [
        {
            "type": "missing",
            "loc": ("body", long_key if n == 0 else "rows", n),
            "msg": "Field required",
            "input": "passages.json holds the harbour figures",
            "ctx": {"limit": 50},
        }
        for n in range(25)
    ]
    body = compact(errors)
    detail = cast(list[dict[str, Any]], body["detail"])
    assert len(detail) == MAX_ERRORS == 20
    assert body["more_errors"] == 5
    assert all(set(error) == {"type", "loc", "msg"} for error in detail)
    first = detail[0]["loc"][1]
    assert first.endswith(" [cut]") and first.startswith("column-xxx")
    assert detail[1] == {"type": "missing", "loc": ["body", "rows", 1], "msg": "Field required"}
    assert "more_errors" not in compact(errors[1:3])
