"""One tool core: the specs of each form, the start check of each caller's list, and the answers the MCP door gives."""

import dataclasses
import inspect
from typing import Any, Final, cast

import pytest
from fastapi import FastAPI, HTTPException

from tests.api.conftest import DOCINT_KEY, GHOSTWRITER_KEY
from tests.callers import EIGHT
from workbench.api.auth import Caller, CallerRegistry
from workbench.api.errors import MAX_ERRORS, compact, describe
from workbench.api.jobs import Busy, RequestTooLarge
from workbench.api.registry import (
    FORM_TOOLS,
    INLINE_TOOLS,
    WORKSPACE_TOOLS,
    Files,
    Registry,
    ToolEnv,
    ToolOutcome,
    ToolSpec,
    allowed_tools,
)
from workbench.api.store import Conflict, NotFound, QuotaExceeded, StoreError
from workbench.api.tool_table import WORKSPACE_TABLE
from workbench.api.tools import FileList, PythonRun, RunPythonRequest
from workbench.api.tools_mcp import NoArguments, workspace_tools
from workbench.config import ConfigError, Settings
from workbench.paths import PathError
from workbench.utils.audit_event import AuditWriteError

#: Every parameter a tool function may take: what the MCP door's adapter passes by name.
KNOWN: Final = frozenset({"body", "p", "settings", "store", "audit", "jobs"})

# --- the table ------------------------------------------------------------------------------------------------------


def test_the_table_holds_the_eight_tools_in_name_order() -> None:
    """The workspace tools are the table's rows, one per tool Ghostwriter may call, by name."""
    assert [tool.name for tool in WORKSPACE_TABLE] == sorted(EIGHT)


def test_each_function_takes_only_known_parameters() -> None:
    """The adapter passes each parameter by its name, so a tool's function may take no other."""
    for tool in WORKSPACE_TABLE:
        assert set(inspect.signature(tool.function).parameters) <= KNOWN, tool.name


# --- the specs ------------------------------------------------------------------------------------------------------


def test_the_workspace_specs_are_the_tables_rows(app: FastAPI) -> None:
    """One spec per row, titled and described as the row, run on the MCP door, its models the row's."""
    specs = workspace_tools()
    assert [s.name for s in specs] == [tool.name for tool in WORKSPACE_TABLE]
    assert {s.name for s in specs} == WORKSPACE_TOOLS
    for spec, tool in zip(specs, WORKSPACE_TABLE, strict=True):
        assert (spec.title, spec.description, spec.form) == (tool.title, tool.description, "workspace")
        assert spec.output_schema is not None
        assert spec.output_model is not None and spec.output_model.__bases__ == (tool.output_model,)
    by_name = {tool.name: tool for tool in WORKSPACE_TABLE}
    assert (by_name["run_python"].input_model, by_name["run_python"].output_model) == (RunPythonRequest, PythonRun)
    assert (by_name["list_files"].input_model, by_name["list_files"].output_model) == (None, FileList)
    assert {s.name: s.input_model for s in specs}["list_files"] is NoArguments
    registry: Registry = app.state.registry
    ghostwriter = cast(CallerRegistry, app.state.callers).get("ghostwriter")
    assert [s.name for s in registry.for_caller(ghostwriter)] == sorted(WORKSPACE_TOOLS)


async def _never(_body: Any, _env: ToolEnv) -> ToolOutcome:
    """A run no test calls: these specs are only found and listed."""
    raise AssertionError("not run")


def _spec(name: str, form: Files) -> ToolSpec:
    """A synthetic spec named ``name``, of the form ``form``."""
    return ToolSpec(
        name=name,
        title=name,
        description=f"tool {name}",
        input_model=None,
        output_model=None,
        form=form,
        run=_never,
        input_schema=lambda _caller: {"type": "object"},
    )


def test_a_caller_sees_the_tools_of_its_form_in_name_order() -> None:
    """A caller finds the specs of its own file form that its list names, in the order of their names."""
    b, a, c = _spec("b", "workspace"), _spec("a", "workspace"), _spec("c", "inline")
    registry = Registry([b, a, c])
    workspace = Caller("ghostwriter", GHOSTWRITER_KEY, frozenset({"a", "b", "c"}))
    inline = Caller("docint", DOCINT_KEY, frozenset({"c"}), files="inline")
    assert registry.for_caller(workspace) == [a, b]
    assert registry.for_caller(inline) == [c]
    assert registry.find(inline, "a") is None
    assert registry.find(inline, "c") is c
    assert registry.find(workspace, "c") is None
    assert registry.find(dataclasses.replace(workspace, tools=frozenset({"b"})), "a") is None
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


def test_allowed_tools_by_form() -> None:
    """A caller with workspace files may list the eight workspace tools; one whose data comes inline, the inline."""
    assert allowed_tools("workspace") == WORKSPACE_TOOLS == EIGHT
    assert allowed_tools("inline") == INLINE_TOOLS


def test_the_tools_of_each_form_are_read_only() -> None:
    """The start check's sets cannot be changed while the api runs."""
    with pytest.raises(TypeError):
        cast(dict[str, frozenset[str]], FORM_TOOLS)["inline"] = WORKSPACE_TOOLS


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
    """Each error gets the status and the short fixed text the file routes answer it with, and the MCP door's text."""
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
