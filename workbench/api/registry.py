"""Every tool, described once, by its form: what a caller may call is read from here.

A tool has a form, as a caller's files do (:class:`~workbench.api.auth.Caller`): a ``workspace`` tool works on the files
a user keeps in a chat folder, an ``inline`` tool on data that comes with the call and leaves with its answer. The
workspace tools are the terminal door's routes tagged :data:`~workbench.api.openapi.TOOL_TAG`, described as specs when
the app is made (:func:`workspace_specs`); they are not served on the MCP door in version 2. A caller finds the tools of
its own form that its list names (:class:`Registry`), and the api does not start with a list that names any other
(:data:`FORM_TOOLS`). Each workspace tool route must also enforce that list itself, through a :class:`ToolGuard` named
after it, or the api does not start either.

``Caller`` is imported for type checking only, since :mod:`workbench.api.auth` imports this module.
"""

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal

from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from pydantic import BaseModel

from workbench.api.audit import AuditLog
from workbench.api.jobs import JobQueue
from workbench.api.openapi import tool_routes
from workbench.api.store import Store
from workbench.config import Settings
from workbench.models import Principal
from workbench.utils.audit_event import Purpose

if TYPE_CHECKING:
    from workbench.api.auth import Caller

#: Where a caller's files live, and so the form of the tools it may use: in its users' workspaces, or only inside the
#: call that brings them.
Files = Literal["workspace", "inline"]

#: Every workspace tool, by ``operationId``: the terminal door's tool routes.
WORKSPACE_TOOLS: Final = frozenset(
    {
        "list_files",
        "read_document",
        "search_files",
        "run_python",
        "inspect_template",
        "fill_template",
        "write_table",
        "write_document",
        "display_file",
    }
)

#: Every inline tool, by name.
INLINE_TOOLS: Final = frozenset({"run_python"})

#: The tools of each file form, read-only. A caller's list must lie within its own form's.
FORM_TOOLS: Final[Mapping[Files, frozenset[str]]] = MappingProxyType(
    {"workspace": WORKSPACE_TOOLS, "inline": INLINE_TOOLS}
)


@dataclass(frozen=True)
class ToolEnv:
    """What a tool runs with on the MCP door: the app's collaborators and what the call's headers say.

    Attributes:
        p: The caller, user and chat the call is for.
        caller: The authenticated caller.
        settings: The api's settings.
        store: Files and job records on the data volume.
        audit: The audit chain.
        jobs: The job queue, or ``None`` when the app has none.
        purpose: The purpose the call stated, or ``None``.
        request_id: The call's request id, or ``None``.
    """

    p: Principal
    caller: "Caller"
    settings: Settings
    store: Store
    audit: AuditLog
    jobs: JobQueue | None
    purpose: Purpose | None
    request_id: str | None


@dataclass(frozen=True)
class ToolOutcome:
    """A tool's answer before the door wraps it.

    Attributes:
        structured: The answer, as the fields of the tool's output model.
        blocks: The content blocks that go with it.
    """

    structured: dict[str, Any]
    blocks: list[dict[str, Any]]


class PurposeRequired(Exception):
    """A purpose the call had to state was not stated; the ``denied`` event is already written."""


class ToolGuard:
    """The dependency a workspace tool route enforces the caller's list with, tagged with the tool it guards.

    :func:`~workbench.api.auth.require_tool` makes it; :func:`workspace_specs` refuses a tool route without one named
    after the route.

    Attributes:
        tool: The ``operationId`` of the tool it guards.
    """

    def __init__(self, tool: str) -> None:
        """Tag the guard.

        Args:
            tool: The ``operationId`` of the tool it guards.
        """
        self.tool = tool


@dataclass(frozen=True)
class ToolSpec:
    """A tool, whichever door serves it.

    A workspace spec has ``run`` and ``input_schema`` ``None``: it is a route, not served on MCP in version 2.

    Attributes:
        name: Its name; a workspace tool's is its route's ``operationId``.
        title: A short title.
        description: What it does, written for the model.
        input_model: The model of its arguments, or ``None`` when it takes no body.
        output_model: The model of its answer, or ``None``.
        form: Whether it works on a workspace's files or on data that comes with the call.
        run: Runs it on arguments its input model has validated.
        input_schema: The arguments' JSON schema for a caller, where it depends on the caller.
    """

    name: str
    title: str
    description: str
    input_model: type[BaseModel] | None
    output_model: type[BaseModel] | None
    form: Files
    run: Callable[[Any, ToolEnv], Awaitable[ToolOutcome]] | None = None
    input_schema: Callable[["Caller"], dict[str, Any]] | None = None


def _model(candidate: object) -> type[BaseModel] | None:
    """Keep ``candidate`` if it is a pydantic model class.

    Args:
        candidate: A route's body type or response model.

    Returns:
        The model, or ``None`` for anything else.
    """
    return candidate if isinstance(candidate, type) and issubclass(candidate, BaseModel) else None


def _guarded(dependant: Dependant) -> set[str]:
    """Name the tools the guards in a dependency tree guard.

    Args:
        dependant: A route's dependencies, or one of them.

    Returns:
        The ``tool`` of every :class:`ToolGuard` in the tree.
    """
    names: set[str] = set()
    for sub in dependant.dependencies:
        if isinstance(sub.call, ToolGuard):
            names.add(sub.call.tool)
        names |= _guarded(sub)
    return names


def workspace_specs(app: FastAPI) -> tuple[ToolSpec, ...]:
    """Describe each tool route of ``app`` as a workspace spec.

    Args:
        app: The application, with its routers in.

    Returns:
        A spec per route tagged :data:`~workbench.api.openapi.TOOL_TAG`: its ``operation_id``, ``summary`` and
        ``description``, its body model or ``None``, and its ``response_model`` or ``None``.

    Raises:
        ValueError: If a tool route has no ``operation_id``, ``summary`` or ``description``, or its guards are not
            exactly one tool's, its own: without that, it would serve a caller whose list does not name it.
    """
    specs: list[ToolSpec] = []
    for route in tool_routes(app):
        name, title, description = route.operation_id, route.summary, route.description
        if not (name and title and description):
            raise ValueError(f"the tool route {route.path} needs an operation_id, a summary and a description")
        if _guarded(route.dependant) != {name}:
            raise ValueError(f"the tool route {route.path} needs require_tool({name!r}) and no other tool's guard")
        body = route.body_field
        specs.append(
            ToolSpec(
                name=name,
                title=title,
                description=description,
                input_model=_model(None if body is None else body.field_info.annotation),
                output_model=_model(route.response_model),
                form="workspace",
            )
        )
    return tuple(specs)


class Registry:
    """The tools the api serves, by form and name."""

    def __init__(self, specs: Iterable[ToolSpec]) -> None:
        """Remember ``specs``.

        Args:
            specs: Every tool, each name once within its form.

        Raises:
            ValueError: If two specs of one form have the same name.
        """
        self._specs: dict[tuple[str, str], ToolSpec] = {}
        for spec in specs:
            if (spec.form, spec.name) in self._specs:
                raise ValueError(f"two tools of the {spec.form} form are named {spec.name}")
            self._specs[spec.form, spec.name] = spec

    def for_caller(self, caller: "Caller") -> list[ToolSpec]:
        """List the tools a caller may call.

        Args:
            caller: The authenticated caller.

        Returns:
            The specs of the caller's file form whose names are on its list, in name order.
        """
        found = [spec for (form, name), spec in self._specs.items() if form == caller.files and name in caller.tools]
        return sorted(found, key=lambda spec: spec.name)

    def find(self, caller: "Caller", name: str) -> ToolSpec | None:
        """Find a tool a caller may call.

        Args:
            caller: The authenticated caller.
            name: The tool's name.

        Returns:
            The spec of that name in the caller's file form, or ``None`` if there is none or it is not on its list.
        """
        if name not in caller.tools:
            return None
        return self._specs.get((caller.files, name))
