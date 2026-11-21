"""The workspace tools on the MCP door, for a caller whose files are kept in the chat's workspace: Ghostwriter.

Each tool is a row of :data:`~workbench.api.tool_table.WORKSPACE_TABLE` and runs the row's function
(:func:`workspace_run`). The adapter passes each parameter by its name, and swaps in what the door decides: the audit
log bound to the call's purpose and request id, which refuses a recorded tool whose purpose rule the call does not
meet, as docint's inline tool is refused; the caller's limits on the queue (:class:`CallJobs`) and a settings copy
whose job wall leaves :data:`PREVIEW_RESERVE_S` for the previews the job reads after its runner.

The answer is the function's own, with its ``None`` fields as the inline door sends them, so that it holds to the
output schema the door lists; plus ``outputs``: every file the call wrote, by reference (:class:`OutputRef`), with the
table preview the executor read for a csv or xlsx output. The text block is that answer without the previews, so a
model fed the text gets no table rows it did not ask for; the caller's cards read ``structuredContent``, and the
tools' texts tell the model the user sees each new file as a card.
"""

import dataclasses
import inspect
import posixpath
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from functools import partial
from typing import Any, Final

from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, create_model

from workbench.api.audit import OPERATIONS
from workbench.api.auth import Caller
from workbench.api.jobs import MCP_QUEUE_WAIT_S, Busy, CallLimits, JobQueue, Keeper
from workbench.api.mcp_wire import _compact_json
from workbench.api.registry import ToolEnv, ToolOutcome, ToolSpec
from workbench.api.renders import WithBlocks
from workbench.api.tool_table import WORKSPACE_TABLE
from workbench.models import DerivedRecord, FileRecord, JobRecord, Principal
from workbench.protocol import JobResult, OutputFile, TablePreview

#: Seconds of a job's wall its runner leaves for the previews: their 2 s, and one C call that cannot be interrupted.
PREVIEW_RESERVE_S: Final = 5


class NoArguments(BaseModel):
    """The arguments of a tool whose route takes no body, ``list_files``: none."""

    # The docstring is the schema's description in Ghostwriter's tool list, frozen there: it stays as it is.
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
    """A keeper that notes the preview of each output it is asked to keep, then records as the tool's keeper does.

    Attributes:
        keeper: The tool's keeper.
        previews: Where each output's preview goes, by the id planned for it.
    """

    keeper: Keeper
    previews: dict[str, TablePreview | None]

    def record(self, result: JobResult, planned: Sequence[str]) -> Sequence[str]:
        """Note each planned id's preview, then have the tool's keeper record the job.

        Args:
            result: The executor's answer.
            planned: One id per output of an ``ok`` answer, in order.

        Returns:
            What the tool's keeper keeps.
        """
        outputs: list[OutputFile] = result.outputs if result.status == "ok" else []
        self.previews.update(zip(planned, (output.preview for output in outputs), strict=True))
        return self.keeper.record(result, planned)

    def again(self, ids: Sequence[str]) -> None:
        """Have the tool's keeper record outputs again as errors.

        Args:
            ids: Their file ids.
        """
        self.keeper.again(ids)


class CallJobs:
    """The queue as one MCP call sees it: within the caller's limits, with previews, keeping what the call made.

    Its renders (:meth:`submit_renders`) run on the user's own turn, and are derived files of their source, never files
    the call wrote: :meth:`outputs` never names them.
    """

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
            keeper: The tool's keeper; the previews of the outputs it is asked to keep are noted first.

        Returns:
            What :meth:`JobQueue.submit` returns.
        """
        noting = None if keeper is None else _Previewing(keeper, self._previews)
        job, result, made = await self._queue.submit(p, tool, args, input_files, noting, call=self.call, previews=True)
        self._made.extend(made)
        return job, result, made

    async def submit_renders(
        self, p: Principal, source: FileRecord, args: dict[str, Any], keeper: Keeper
    ) -> tuple[JobRecord, JobResult, list[DerivedRecord]]:
        """Render a picture or pages of a PDF as :meth:`JobQueue.submit_renders` does, within the call's limits.

        It waits for the user's own turn; the renders are not the call's outputs.

        Args:
            p: The caller, user and chat the job runs for: the source's own.
            source: The file to render.
            args: The runner's other arguments.
            keeper: Records the job before any render becomes visible, and picks the renders to keep.

        Returns:
            What :meth:`JobQueue.submit_renders` returns.
        """
        return await self._queue.submit_renders(p, source, args, keeper, call=self.call)

    def source_lock(self, source_id: str) -> AbstractAsyncContextManager[None]:
        """Hold the lock of one source's renders, waiting at most the call's wait, as :meth:`JobQueue.source_lock`.

        Args:
            source_id: The source's id.

        Returns:
            The lock, held while the context runs; entering it raises ``Busy("wait_timeout")`` after the wait.
        """
        return self._queue.source_lock(source_id, self.call.wait_s)

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


def _model_schema(model: type[BaseModel], caller: Caller) -> dict[str, Any]:
    """A model's JSON schema, the same for every caller.

    Args:
        model: The model.
        caller: The caller; unused.

    Returns:
        The schema.
    """
    return model.model_json_schema()


def with_outputs(model: type[BaseModel]) -> type[BaseModel]:
    """Extend a tool's answer by the files the call wrote.

    Args:
        model: The tool's answer.

    Returns:
        A model of its fields and ``outputs``, a list of :class:`OutputRef`.
    """
    return create_model(
        f"{model.__name__}WithOutputs", __base__=model, outputs=(list[OutputRef], Field(default_factory=list))
    )


def workspace_run(endpoint: Callable[..., Any], key: str) -> Callable[[Any, ToolEnv], Awaitable[ToolOutcome]]:
    """Make the MCP door's ``run`` of a workspace tool, which calls the tool's function.

    Args:
        endpoint: The tool's function.
        key: The tool's name; the purpose rule checks it, and its events are recorded, only if it is a key of the
            audit's operations (``list_files`` is not).

    Returns:
        The adapter: it passes the function each parameter it takes, by name, and answers the function's answer, with
        its ``None`` fields, plus ``outputs``, its text without the previews.
    """
    takes = frozenset(inspect.signature(endpoint).parameters)
    asynchronous = inspect.iscoroutinefunction(endpoint)

    async def run(body: Any, env: ToolEnv) -> ToolOutcome:
        """Run the tool for the call, within the caller's limits.

        Args:
            body: The arguments, validated by the tool's input model.
            env: The caller, the app's collaborators and what the call's headers say.

        Returns:
            The answer, its outputs and its text.

        Raises:
            PurposeRequired: The tool's audit action needs a stated purpose and the call stated none; the ``denied``
                event is written and nothing runs.
            Busy: The tool needs the queue and the app has none.
            RuntimeError: The principal is not confined to one chat, as a workspace caller's always is.
            Exception: What the tool raised.
        """
        if not env.p.confined:
            raise RuntimeError(f"{key} runs only for a principal confined to one chat")
        settings, caller = env.settings, env.caller
        audit = env.audit.bound(env.purpose, env.request_id, required=settings.require_purpose)
        if key in OPERATIONS:
            await run_in_threadpool(audit.check, key, env.p)
        wall = min(caller.wall_s or settings.job_wall_s, settings.job_wall_s)
        if "jobs" in takes and env.jobs is None:
            raise Busy("executor_busy")
        jobs = None if env.jobs is None else CallJobs(env.jobs, CallLimits(wall, MCP_QUEUE_WAIT_S, caller.slots))
        offered: dict[str, Any] = {
            "body": body,
            "p": env.p,
            "settings": dataclasses.replace(settings, job_wall_s=wall - PREVIEW_RESERVE_S),
            "store": env.store,
            "audit": audit,
            "jobs": jobs,
            "images": env.images,
        }
        given = {name: offered[name] for name in takes}
        answer = await endpoint(**given) if asynchronous else await run_in_threadpool(endpoint, **given)
        blocks: list[dict[str, Any]] = []
        if isinstance(answer, WithBlocks):
            answer, blocks = answer.answer, answer.blocks
        outputs: list[OutputRef] = [] if jobs is None else jobs.outputs()
        structured = {
            **answer.model_dump(mode="json"),
            "outputs": [output.model_dump(mode="json") for output in outputs],
        }
        told = [
            {name: value for name, value in output.items() if name != "preview"} for output in structured["outputs"]
        ]
        return ToolOutcome(structured, blocks, _compact_json({**structured, "outputs": told}))

    return run


def workspace_tools() -> tuple[ToolSpec, ...]:
    """Describe the workspace tools the MCP door serves, one spec per row of the table.

    Returns:
        A spec per row of :data:`~workbench.api.tool_table.WORKSPACE_TABLE`, in its order: its ``run`` calls the row's
        function (:func:`workspace_run`), its input model is the row's or :class:`NoArguments`, its output model the
        row's with ``outputs`` (:func:`with_outputs`), and both schemas are those models' own, the same on a call that
        takes images and on one that does not; what the row says of images is the spec's too.
    """
    specs: list[ToolSpec] = []
    for tool in WORKSPACE_TABLE:
        input_model = tool.input_model or NoArguments
        output_model = with_outputs(tool.output_model)
        specs.append(
            ToolSpec(
                name=tool.name,
                title=tool.title,
                description=tool.description,
                input_model=input_model,
                output_model=output_model,
                form="workspace",
                run=workspace_run(tool.function, tool.name),
                input_schema=partial(_model_schema, input_model),
                output_schema=partial(_model_schema, output_model),
                images_only=tool.images_only,
                text_description=tool.text_description,
            )
        )
    return tuple(specs)
