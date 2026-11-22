"""Every tool, described once, by its form: what a caller may call is read from here.

A tool has a form, as a caller's files do (:class:`~workbench.api.auth.Caller`): a ``workspace`` tool works on the files
a user keeps in a chat folder, an ``inline`` tool on data that comes with the call and leaves with its answer. The
workspace tools are the rows of :data:`~workbench.api.tool_table.WORKSPACE_TABLE`, which
:mod:`workbench.api.tools_mcp` makes specs with an adapter that calls each row's function. A caller finds the tools of
its own form that its list names (:class:`Registry`), and the api does not start with a list that names any other
(:func:`allowed_tools`). A call that takes no images (no ``X-Accept-Images: 1``,
:func:`~workbench.api.auth.accepts_images`) finds none of the tools that only show images, and is told of the others
what an earlier workbench told it: ``read_document`` in 0.6's words, ``write_presentation`` in 0.8.0's
(:attr:`ToolSpec.text_description`).

``Caller`` is imported for type checking only, since :mod:`workbench.api.auth` imports this module.
"""

import dataclasses
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import BaseModel

from workbench.api.audit import AuditLog
from workbench.api.audit import PurposeRequired as PurposeRequired
from workbench.api.jobs import JobQueue
from workbench.api.store import Store
from workbench.api.tool_table import WORKSPACE_TABLE
from workbench.config import Settings
from workbench.models import Principal
from workbench.utils.audit_event import Purpose

if TYPE_CHECKING:
    from workbench.api.auth import Caller

#: Where a caller's files live, and so the form of the tools it may use: in its users' workspaces, or only inside the
#: call that brings them.
Files = Literal["workspace", "inline"]

#: Every workspace tool, by name: the rows of :data:`~workbench.api.tool_table.WORKSPACE_TABLE`.
WORKSPACE_TOOLS: Final = frozenset(tool.name for tool in WORKSPACE_TABLE)

#: Every inline tool, by name.
INLINE_TOOLS: Final = frozenset({"run_python"})

#: The tools of each file form, read-only.
FORM_TOOLS: Final[Mapping[Files, frozenset[str]]] = MappingProxyType(
    {"workspace": WORKSPACE_TOOLS, "inline": INLINE_TOOLS}
)


def allowed_tools(files: Files) -> frozenset[str]:
    """Name the tools a caller's list may hold: those of its file form.

    Args:
        files: The caller's file form.

    Returns:
        The tools of that form.
    """
    return FORM_TOOLS[files]


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
        images: Whether the call takes images (``X-Accept-Images: 1``): without, no tool sends one.
    """

    p: Principal
    caller: "Caller"
    settings: Settings
    store: Store
    audit: AuditLog
    jobs: JobQueue | None
    purpose: Purpose | None
    request_id: str | None
    images: bool


@dataclass(frozen=True)
class ToolOutcome:
    """A tool's answer before the door wraps it.

    Attributes:
        structured: The answer, as the fields of the tool's output model.
        blocks: The content blocks that go with it.
        text: The text block's text, or ``None`` for the structured answer as compact JSON.
    """

    structured: dict[str, Any]
    blocks: list[dict[str, Any]]
    text: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    """A tool the MCP door serves.

    Attributes:
        name: Its name.
        title: A short title.
        description: What it does, written for the model.
        input_model: The model of its arguments, or ``None`` when it takes no body.
        output_model: The model of its answer, or ``None``.
        form: Whether it works on a workspace's files or on data that comes with the call.
        run: Runs it on arguments its input model has validated.
        input_schema: The arguments' JSON schema for a caller.
        output_schema: The answer's JSON schema for a caller, in place of ``output_model``'s own.
        images_only: Whether it only shows images (``view_file``), and so is listed and called only on a call that
            takes them.
        text_description: What a call that takes no images is told of it instead of ``description``, or ``None`` for
            the same: ``read_document``'s text of workbench 0.6, which promises no image, and ``write_presentation``'s
            of 0.8.0, which names no ``view_file``.
    """

    name: str
    title: str
    description: str
    input_model: type[BaseModel] | None
    output_model: type[BaseModel] | None
    form: Files
    run: Callable[[Any, ToolEnv], Awaitable[ToolOutcome]]
    input_schema: Callable[["Caller"], dict[str, Any]]
    output_schema: Callable[["Caller"], dict[str, Any]] | None = None
    images_only: bool = False
    text_description: str | None = None

    def seen(self, *, images: bool) -> "ToolSpec | None":
        """The tool as a call is told of it.

        Args:
            images: Whether the call takes images.

        Returns:
            The spec itself on a call that takes images; on one that does not, ``None`` for a tool that only shows
            images, else the spec with its ``text_description``, if it has one, as its description.
        """
        if images:
            return self
        if self.images_only:
            return None
        if self.text_description is None:
            return self
        return dataclasses.replace(self, description=self.text_description)


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

    def for_caller(self, caller: "Caller", *, images: bool) -> list[ToolSpec]:
        """List the tools a caller may call on one call, as the call is told of them.

        Args:
            caller: The authenticated caller.
            images: Whether the call takes images (:meth:`ToolSpec.seen`).

        Returns:
            The specs of the caller's file form whose names are on its list, as the call sees them, in name order.
        """
        found = [
            seen
            for (form, name), spec in self._specs.items()
            if form == caller.files and name in caller.tools and (seen := spec.seen(images=images)) is not None
        ]
        return sorted(found, key=lambda spec: spec.name)

    def find(self, caller: "Caller", name: str, *, images: bool) -> ToolSpec | None:
        """Find a tool a caller may call on one call.

        Args:
            caller: The authenticated caller.
            name: The tool's name.
            images: Whether the call takes images (:meth:`ToolSpec.seen`).

        Returns:
            The spec of that name in the caller's file form as the call sees it, or ``None`` if there is none, it is
            not on its list, or it only shows images and the call takes none.
        """
        if name not in caller.tools:
            return None
        spec = self._specs.get((caller.files, name))
        return None if spec is None else spec.seen(images=images)
