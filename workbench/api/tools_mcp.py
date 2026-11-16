"""The workspace tools on the MCP door, for a caller whose files are kept in the chat's workspace: Ghostwriter.

Each tool the MCP door serves (:data:`~workbench.api.registry.MCP_WORKSPACE_TOOLS`) runs its route's own function, so
both doors run the same code (:func:`workspace_run`). The adapter passes each parameter by its name, and swaps in what
the door decides: the caller's limits on the queue (:class:`CallJobs`) and a settings copy whose job wall leaves
:data:`PREVIEW_RESERVE_S` for the previews the job reads after its runner.

The answer is the route's own, plus ``outputs``: every file the call wrote, by reference (:class:`OutputRef`), with the
table preview the executor read for a csv or xlsx output. The text block is that answer without the previews, so a
model fed the text gets no table rows it did not ask for; the caller's cards read ``structuredContent``. The tools'
texts tell the model the user sees each new file as a card, since ``display_file``, Open WebUI's card, is not served
here (:data:`MCP_TEXT`); the terminal door's texts stay as they are.
"""

import dataclasses
import inspect
import posixpath
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from typing import Any, Final

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, create_model

from workbench.api.auth import Caller
from workbench.api.jobs import MCP_QUEUE_WAIT_S, Busy, CallLimits, JobQueue, Keeper
from workbench.api.mcp_wire import _compact_json
from workbench.api.openapi import tool_routes
from workbench.api.registry import MCP_WORKSPACE_TOOLS, ToolEnv, ToolOutcome, ToolSpec, workspace_specs
from workbench.models import FileRecord, JobRecord, Principal
from workbench.protocol import JobResult, OutputFile, TablePreview

#: Seconds of a job's wall its runner leaves for the previews: their 2 s, and one C call that cannot be interrupted.
PREVIEW_RESERVE_S: Final = 5

#: The phrases of the tools' texts that name ``display_file``, and what the MCP door says instead, in order.
MCP_TEXT: Final = (
    ("show a file with display_file", "the user sees each file in out/ as a card in the chat"),
    ("Show the result with display_file.", "The user sees the new file as a card in the chat."),
    ("show it with display_file", "the user sees it as a card in the chat"),
)


class NoArguments(BaseModel):
    """The arguments of a tool whose route takes no body, ``list_files``: none."""

    model_config = ConfigDict(extra="forbid")


class OutputRef(BaseModel):
    """A file the call wrote, by reference: never its bytes.

    Attributes:
        id: The file's id.
        path: Its absolute path.
        name: Its name, as it was written.
        mime: Its media type.
        size: Its size in bytes.
        preview: The header and first rows of a csv or xlsx file, or ``None``.
    """

    id: str = Field(description="The file's id.")
    path: str = Field(description="The file's absolute path; use it as is in other tools.")
    name: str = Field(description="The file's name, as it was written.")
    mime: str = Field(description="Its media type.")
    size: int = Field(description="Its size in bytes.")
    preview: TablePreview | None = Field(description="The header and first rows of a csv or xlsx file, else null.")


@dataclasses.dataclass(frozen=True)
class _Previewing:
    """A keeper that notes the preview of each output it is asked to keep, then records as the route's keeper does.

    Attributes:
        keeper: The route's keeper.
        previews: Where each output's preview goes, by the id planned for it.
    """

    keeper: Keeper
    previews: dict[str, TablePreview | None]

    def record(self, result: JobResult, planned: Sequence[str]) -> Sequence[str]:
        """Note each planned id's preview, then have the route's keeper record the job.

        Args:
            result: The executor's answer.
            planned: One id per output of an ``ok`` answer, in order.

        Returns:
            What the route's keeper keeps.
        """
        outputs: list[OutputFile] = result.outputs if result.status == "ok" else []
        self.previews.update(zip(planned, (output.preview for output in outputs), strict=True))
        return self.keeper.record(result, planned)

    def again(self, ids: Sequence[str]) -> None:
        """Have the route's keeper record outputs again as errors.

        Args:
            ids: Their file ids.
        """
        self.keeper.again(ids)


class CallJobs:
    """The queue as one MCP call sees it: within the caller's limits, with previews, keeping what the call made."""

    def __init__(self, queue: JobQueue, call: CallLimits) -> None:
        """Wrap the queue for one call.

        Args:
            queue: The app's job queue.
            call: The call's wall, wait and caller slots.
        """
        self._queue = queue
        self.call = call
        self._made: list[FileRecord] = []
        self._previews: dict[str, TablePreview | None] = {}

    async def submit(
        self,
        p: Principal,
        tool: str,
        args: dict[str, Any],
        input_files: Sequence[str],
        keeper: Keeper | None = None,
    ) -> tuple[JobRecord, JobResult, list[FileRecord]]:
        """Run a tool in the executor as :meth:`JobQueue.submit` does, within the call's limits and with previews.

        Args:
            p: The caller, user and chat the job runs for.
            tool: Name of the runner to run.
            args: The runner's arguments.
            input_files: Paths relative to the chat folder that the job may read.
            keeper: The route's keeper; the previews of the outputs it is asked to keep are noted first.

        Returns:
            What :meth:`JobQueue.submit` returns.
        """
        noting = None if keeper is None else _Previewing(keeper, self._previews)
        job, result, made = await self._queue.submit(p, tool, args, input_files, noting, call=self.call, previews=True)
        self._made.extend(made)
        return job, result, made

    def outputs(self) -> list[OutputRef]:
        """Name every file the call made, in the order made.

        Returns:
            Each file by reference, with the preview noted for it, or ``None``.
        """
        return [
            OutputRef(
                id=rec.id,
                path=rec.virtual_path,
                name=posixpath.basename(rec.name),
                mime=rec.mime,
                size=rec.size,
                preview=self._previews.get(rec.id),
            )
            for rec in self._made
        ]


def mcp_text(text: str) -> str:
    """Say a tool's text in the MCP door's words (:data:`MCP_TEXT`).

    Args:
        text: The text.

    Returns:
        The text, each phrase replaced in order.
    """
    for phrase, replacement in MCP_TEXT:
        text = text.replace(phrase, replacement)
    return text


def mcp_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Say every ``description`` of a JSON schema in the MCP door's words, at any depth.

    Args:
        schema: The schema; it is not changed.

    Returns:
        A copy with each description passed through :func:`mcp_text`.
    """

    def worded(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: mcp_text(value) if key == "description" and isinstance(value, str) else worded(value)
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [worded(item) for item in node]
        return node

    worded_schema: dict[str, Any] = worded(schema)
    return worded_schema


def _model_schema(model: type[BaseModel], caller: Caller) -> dict[str, Any]:
    """A model's JSON schema in the MCP door's words, the same for every caller.

    Args:
        model: The model.
        caller: The caller; unused.

    Returns:
        The schema.
    """
    return mcp_schema(model.model_json_schema())


def with_outputs(model: type[BaseModel]) -> type[BaseModel]:
    """Extend a tool's REST answer by the files the call wrote.

    Args:
        model: The route's answer.

    Returns:
        A model of its fields and ``outputs``, a list of :class:`OutputRef`.
    """
    return create_model(
        f"{model.__name__}WithOutputs", __base__=model, outputs=(list[OutputRef], Field(default_factory=list))
    )


def workspace_run(endpoint: Callable[..., Any], key: str) -> Callable[[Any, ToolEnv], Awaitable[ToolOutcome]]:
    """Make the MCP door's ``run`` of a workspace tool, which calls the tool's route function.

    Args:
        endpoint: The route function.
        key: The tool, a key of the audit's operations.

    Returns:
        The adapter: it passes the route each parameter it takes, by name, and answers the route's answer plus
        ``outputs``, its text without the previews.
    """
    takes = frozenset(inspect.signature(endpoint).parameters)
    asynchronous = inspect.iscoroutinefunction(endpoint)

    async def run(body: Any, env: ToolEnv) -> ToolOutcome:
        """Run the route for the call, within the caller's limits.

        Args:
            body: The arguments, validated by the tool's input model.
            env: The caller, the app's collaborators and what the call's headers say.

        Returns:
            The answer, its outputs and its text.

        Raises:
            Busy: The route needs the queue and the app has none.
            Exception: What the route raised.
        """
        settings, caller = env.settings, env.caller
        wall = min(caller.wall_s or settings.job_wall_s, settings.job_wall_s)
        if "jobs" in takes and env.jobs is None:
            raise Busy("executor_busy")
        jobs = None if env.jobs is None else CallJobs(env.jobs, CallLimits(wall, MCP_QUEUE_WAIT_S, caller.slots))
        offered: dict[str, Any] = {
            "body": body,
            "p": env.p,
            "settings": dataclasses.replace(settings, job_wall_s=wall - PREVIEW_RESERVE_S),
            "store": env.store,
            "audit": env.audit,
            "jobs": jobs,
        }
        given = {name: offered[name] for name in takes}
        answer = await endpoint(**given) if asynchronous else await run_in_threadpool(endpoint, **given)
        outputs: list[OutputRef] = [] if jobs is None else jobs.outputs()
        structured = {
            **answer.model_dump(mode="json", exclude_none=True),
            "outputs": [output.model_dump(mode="json") for output in outputs],
        }
        told = [
            {name: value for name, value in output.items() if name != "preview"} for output in structured["outputs"]
        ]
        return ToolOutcome(structured, [], _compact_json({**structured, "outputs": told}))

    return run


def workspace_tools(app: FastAPI) -> tuple[ToolSpec, ...]:
    """Describe the app's workspace tools, those the MCP door serves with their adapters and schemas.

    Args:
        app: The application, with its routers in.

    Returns:
        Every spec of :func:`~workbench.api.registry.workspace_specs`. Each one named in
        :data:`~workbench.api.registry.MCP_WORKSPACE_TOOLS` gets its ``run``, its input model (:class:`NoArguments`
        for a route without a body), its output model with ``outputs``, both schemas in the MCP door's words, and its
        description in those words. ``display_file`` stays as it is.

    Raises:
        ValueError: A tool the MCP door serves has no route function or no output model.
    """
    endpoints = {route.operation_id: route.endpoint for route in tool_routes(app)}
    specs: list[ToolSpec] = []
    for spec in workspace_specs(app):
        if spec.name not in MCP_WORKSPACE_TOOLS:
            specs.append(spec)
            continue
        endpoint = endpoints.get(spec.name)
        if endpoint is None or spec.output_model is None:
            raise ValueError(f"the tool route {spec.name} needs a function and a response model to be served on MCP")
        input_model = spec.input_model or NoArguments
        output_model = with_outputs(spec.output_model)
        specs.append(
            dataclasses.replace(
                spec,
                run=workspace_run(endpoint, spec.name),
                input_model=input_model,
                input_schema=partial(_model_schema, input_model),
                output_model=output_model,
                output_schema=partial(_model_schema, output_model),
                description=mcp_text(spec.description),
            )
        )
    return tuple(specs)
