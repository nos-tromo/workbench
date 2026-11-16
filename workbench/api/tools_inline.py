"""The inline tools: data that comes with the call, outputs that go back in its answer, nothing kept.

Version 2 has one, ``run_python``, served to docint on the MCP door (:data:`INLINE_SPECS`). The data is written into
the job's folder (:class:`~workbench.protocol.InlineFile`) and goes with it; the outputs are read back from staging by
type and cap (:meth:`~workbench.api.jobs.JobQueue.submit_inline`) and returned, never registered, so docint has no
workspace. Open WebUI's ``run_python``, a route in :mod:`workbench.api.tools`, is a tool of the other form.

The call is recorded once the job has answered and before anything of the answer goes out (:class:`InlineTaker`), as
``combine`` / ``tool.run_python.inline`` on ``inline_data``: the count of entries and their bytes, the job, how many
outputs came back and how many did not. Never the data or a digest of it, a name, the code, ``stdout`` or a
refusal's text. A call that must state a purpose and states none is recorded as ``denied`` and runs nothing.
"""

import base64
import dataclasses
import urllib.parse
from collections.abc import Sequence
from typing import Any, Final

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from workbench.api.audit import AuditLog
from workbench.api.auth import Caller
from workbench.api.jobs import (
    MCP_QUEUE_WAIT_S,
    RETURN_TYPES,
    Busy,
    CallLimits,
    SkippedOutput,
    TakenFile,
    TakenOutputs,
    suffix_of,
)
from workbench.api.registry import PurposeRequired, ToolEnv, ToolOutcome, ToolSpec
from workbench.api.tools import MAX_CODE, MAX_HINT, NotCompleted, _capped, _join, fit_python, python_args, read_code_run
from workbench.models import Principal
from workbench.protocol import MAX_INLINE_BYTES, MAX_INLINE_FILES, InlineFile, JobResult, check_inline_inputs
from workbench.utils.audit_event import Purpose

#: What a data entry may be named: a letter or digit, up to 99 more of letters, digits, ``_``, ``.`` and ``-``, and
#: one of the suffixes docint sends.
DATA_NAME: Final = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.(json|csv|txt|md)$"
#: Most data entries one call brings: the protocol's own bound.
MAX_DATA: Final = MAX_INLINE_FILES
#: Most bytes, in UTF-8, all data entries of one call hold together: the protocol's own bound.
MAX_DATA_BYTES: Final = MAX_INLINE_BYTES
#: The least ``time_limit_s`` a call may ask for.
MIN_TIME_LIMIT_S: Final = 5

#: The event key of an inline run.
_KEY: Final = "run_python_inline"
#: What a skipped output's type is when it is not one that comes back.
_UNKNOWN_TYPE: Final = "application/octet-stream"
_TIME_LIMIT: Final = (
    "Seconds the code may run, from 5 to the most this service allows; leave it out for that most. The code gets "
    "9/10 of it, so that stderr can say it timed out."
)


class InlineData(BaseModel):
    """One data entry: a text the call brings, written as a read-only file of the job's folder.

    Attributes:
        name: The file's name.
        content: Its text, written as UTF-8.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str = Field(
        pattern=DATA_NAME,
        description="The file's name in the current folder, such as passages.json: ending .json, .csv, .txt or .md.",
    )
    content: str = Field(repr=False, description="The file's text.")


class InlinePythonRequest(BaseModel):
    """Arguments of inline ``run_python``, validated with the caller's wall cap as ``context={"wall_cap": ...}``.

    Attributes:
        code: The Python to run, as a script.
        data: The data, each entry a file of the current folder.
        time_limit_s: Seconds the job may run, or ``None`` for the caller's cap.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    code: str = Field(
        min_length=1,
        max_length=MAX_CODE,
        description=(
            "The Python code to run, as a script; at most 50000 characters. Print what you need to see, and write "
            "every result file directly into out/."
        ),
    )
    data: list[InlineData] = Field(
        default_factory=list,
        max_length=MAX_DATA,
        description="The data, at most 8 entries and 512 KiB in all: each is a read-only file of the current folder.",
    )
    time_limit_s: int | None = Field(default=None, strict=True, description=_TIME_LIMIT)

    @field_validator("data")
    @classmethod
    def _check_data(cls, value: list[InlineData]) -> list[InlineData]:
        """Accept the entries the job's protocol accepts (:func:`~workbench.protocol.check_inline_inputs`).

        Args:
            value: The entries.

        Returns:
            ``value``, unchanged.

        Raises:
            ValueError: A name is repeated, a text cannot be encoded as UTF-8, or the texts are too large together.
        """
        check_inline_inputs(value)
        return value

    @field_validator("time_limit_s")
    @classmethod
    def _check_time_limit(cls, value: int | None, info: ValidationInfo) -> int | None:
        """Accept a time limit from :data:`MIN_TIME_LIMIT_S` to the caller's wall cap; never clamp one.

        Args:
            value: The time limit, or ``None``.
            info: Carries the caller's wall cap as ``context["wall_cap"]``.

        Returns:
            ``value``, unchanged.

        Raises:
            ValueError: It is outside that range, or no wall cap came with the validation to check it by.
        """
        if value is None:
            return value
        cap = info.context.get("wall_cap") if isinstance(info.context, dict) else None
        if not isinstance(cap, int):
            raise ValueError("time_limit_s is checked against the caller's wall cap, and none was given")
        if not MIN_TIME_LIMIT_S <= value <= cap:
            raise ValueError(f"time_limit_s must be from {MIN_TIME_LIMIT_S} to {cap}")
        return value


def input_schema(caller: Caller) -> dict[str, Any]:
    """The arguments' JSON schema for a caller: ``time_limit_s`` runs from 5 to its wall cap.

    Args:
        caller: The authenticated caller.

    Returns:
        :class:`InlinePythonRequest`'s schema, with ``time_limit_s`` a plain integer of the caller's range.
    """
    schema = InlinePythonRequest.model_json_schema()
    limit: dict[str, Any] = {"type": "integer", "minimum": MIN_TIME_LIMIT_S}
    if caller.wall_s is not None:
        limit["maximum"] = caller.wall_s
    limit["description"] = _TIME_LIMIT
    schema["properties"]["time_limit_s"] = limit
    return schema


class OutputInfo(BaseModel):
    """One file the code wrote into ``out/``, and whether it came back.

    Attributes:
        name: Its name, as the code wrote it.
        mime: Its media type.
        bytes: Its size in bytes.
        returned: Whether it came back with this answer.
    """

    name: str = Field(description="Its name in out/, as the code wrote it.")
    mime: str = Field(description="Its media type.")
    bytes: int = Field(description="Its size in bytes.")
    returned: bool = Field(description="True if it came back with this answer; the hint says why one did not.")


class InlinePythonResult(BaseModel):
    """What inline ``run_python`` answers; sent with its ``None`` fields.

    Attributes:
        stdout: What the code printed.
        stderr: Its warnings and errors.
        truncated: Whether stdout or stderr was cut in the middle.
        job_id: The id of the run.
        files: Every file the code wrote: those that came back, then those that did not.
        hint: What to tell the user, or how to go on.
        reason: Always ``None`` on a result; an error result names its reason there.
    """

    stdout: str = Field(description="What the code printed.")
    stderr: str = Field(description="Its warnings and errors; a traceback ends with the error.")
    truncated: bool = Field(description="True if stdout or stderr was cut in the middle to fit the result limit.")
    job_id: str = Field(description="The id of this run; cite it as the source of what the code computed.")
    files: list[OutputInfo] = Field(description="Every file written into out/: those that came back first.")
    hint: str | None = Field(max_length=MAX_HINT, description="What to tell the user, or how to go on.")
    reason: None = Field(default=None, description="None on a result; an error result names its reason here.")


def output_uri(job_id: str, name: str) -> str:
    """Name an output that came back as a resource.

    Args:
        job_id: The job's id.
        name: The output's name, as the code wrote it.

    Returns:
        ``workbench://jobs/<job-id>/out/<name>``, the name percent-encoded.
    """
    return f"workbench://jobs/{job_id}/out/{urllib.parse.quote(name, safe='')}"


def _stated(purpose: Purpose | None) -> bool:
    """Say whether a call stated a purpose.

    Args:
        purpose: The purpose the call stated, or ``None``.

    Returns:
        Whether it holds a case reference or a reason.
    """
    return purpose is not None and bool(purpose.to_dict())


@dataclasses.dataclass(frozen=True)
class InlineTaker:
    """Records an inline run before anything of its answer goes out (:class:`~workbench.api.jobs.Taker`).

    Attributes:
        audit: The audit log.
        p: The caller, user and session.
        purpose: The purpose the call stated, or ``None``.
        request_id: The call's request id, or ``None``.
        count: How many data entries came with the call.
        input_bytes: Their bytes in UTF-8, together.
    """

    audit: AuditLog
    p: Principal
    purpose: Purpose | None
    request_id: str | None
    count: int
    input_bytes: int

    def take(self, result: JobResult, taken: TakenOutputs) -> None:
        """Write the run's event: ``ok`` for an ``ok`` job whose answer reads, else ``error``; a refusal by its token.

        An answer that will be an error result returns no output, so it counts every output as dropped.

        Args:
            result: The executor's answer.
            taken: The outputs that come back and those that do not.
        """
        refused: str | None = None
        try:
            read_code_run(result)
        except NotCompleted:
            refused = "not_completed"
        except HTTPException:
            refused = "refused"
        returned = 0 if refused else len(taken.files)
        criteria: dict[str, object] = {
            "job": result.job_id,
            "input_bytes": self.input_bytes,
            "outputs": returned,
            "dropped": len(taken.files) + len(taken.skipped) - returned,
        }
        if refused is not None:
            criteria["refused"] = refused
        self.audit.record(
            _KEY,
            self.p,
            target_kind="inline_data",
            count=self.count,
            outcome="ok" if result.status == "ok" and refused is None else "error",
            criteria=criteria,
            purpose=self.purpose,
            request_id=self.request_id,
        )


def _files(n: int) -> str:
    """Count files in words.

    Args:
        n: How many.

    Returns:
        ``1 file`` or ``<n> files``.
    """
    return f"{n} {'file' if n == 1 else 'files'}"


def _skip_hint(skipped: Sequence[SkippedOutput]) -> list[str]:
    """Say, once per reason, how many outputs did not come back and what to do about it.

    Args:
        skipped: The outputs that did not come back.

    Returns:
        One sentence per reason that occurred, in a fixed order.
    """
    counts: dict[str, int] = {}
    for skip in skipped:
        kind = "image" if suffix_of(skip.name) == ".png" else "text"
        reason = f"size-{kind}" if skip.reason == "size" else skip.reason
        counts[reason] = counts.get(reason, 0) + 1
    sentences = {
        "type": "{} of another type did not come back: only .png, .csv, .txt, .md and .json come back.",
        "count": "{} did not come back: at most 8 files come back.",
        "size-image": (
            "{} over 512 KiB did not come back: an image comes back up to 512 KiB, so save it smaller, for example "
            "plt.savefig(..., dpi=100)."
        ),
        "size-text": "{} over 256 KiB did not come back: a text comes back up to 256 KiB.",
        "signature": "{} named .png did not come back: a file that is not a PNG cannot be named .png.",
        "encoding": "{} did not come back, not being UTF-8: write text files as UTF-8.",
        "total": "{} did not come back: the files that come back hold at most 1 MiB in all.",
        "unreadable": "{} could not be read and did not come back.",
    }
    return [text.format(_files(counts[reason])) for reason, text in sentences.items() if reason in counts]


def _returned_note(result: JobResult, note: str | None) -> str | None:
    """Say what the executor's note means on this door, where nothing is kept: which files did not come back.

    Args:
        result: The executor's answer, as :func:`~workbench.api.tools.read_code_run` read it.
        note: The executor's bare note, or ``None``.

    Returns:
        The sentence, or ``None`` for an ``ok`` job without a note.
    """
    if result.status == "ok":
        return None if note is None else f"Not every file in out/ came back: {note}."
    return f"No file in out/ came back: {note or 'the outputs could not be returned'}."


def _info(skip: SkippedOutput) -> OutputInfo:
    """Describe an output that did not come back.

    Args:
        skip: The output.

    Returns:
        Its entry in ``files``, typed by its suffix where that is a type that comes back.
    """
    return OutputInfo(
        name=skip.name, mime=RETURN_TYPES.get(suffix_of(skip.name), _UNKNOWN_TYPE), bytes=skip.size, returned=False
    )


def _block(job_id: str, file: TakenFile) -> dict[str, Any]:
    """The content block of an output that came back: a chart for the user to see, a text as a resource.

    Args:
        job_id: The job's id.
        file: The output.

    Returns:
        An ``image`` block for a PNG, a ``resource`` block with the text for anything else.
    """
    if file.mime == RETURN_TYPES[".png"]:
        return {
            "type": "image",
            "data": base64.b64encode(file.data).decode("ascii"),
            "mimeType": file.mime,
            "annotations": {"audience": ["user"]},
        }
    return {
        "type": "resource",
        "resource": {"uri": output_uri(job_id, file.name), "mimeType": file.mime, "text": file.data.decode("utf-8")},
    }


async def run_python_inline(body: InlinePythonRequest, env: ToolEnv) -> ToolOutcome:
    """Run the code on the data that came with the call and answer with what it printed and the outputs.

    Args:
        body: The arguments, validated with the caller's wall cap.
        env: The caller, the app's collaborators and what the call's headers say.

    Returns:
        The answer and one content block per output that came back, in return order.

    Raises:
        PurposeRequired: ``combine`` needs a purpose and the call stated none; the ``denied`` event is written and no
            job starts.
        Busy: The queue refused the call, or there is no queue.
        RequestTooLarge: The data does not fit the executor's line once encoded; nothing is recorded.
        NotCompleted: The code could not be completed; the event is written.
        HTTPException: The runner refused or failed, or its answer could not be read; the event is written.
        AuditWriteError: The event could not be written; nothing of the answer goes out.
    """
    p, caller, settings = env.p, env.caller, env.settings
    if "combine" in settings.require_purpose and not _stated(env.purpose):
        criteria = {"refused": "purpose_required", "container": f"chat:{p.chat_id}"}
        await run_in_threadpool(
            env.audit.record,
            _KEY,
            p,
            target_kind=None,
            outcome="denied",
            criteria=criteria,
            request_id=env.request_id,
        )
        raise PurposeRequired
    cap = caller.wall_s or settings.job_wall_s
    wall = min(body.time_limit_s or cap, cap, settings.job_wall_s)
    if env.jobs is None:
        raise Busy("executor_busy")
    inline = [InlineFile(name=entry.name, content=entry.content) for entry in body.data]
    taker = InlineTaker(
        env.audit,
        p,
        env.purpose,
        env.request_id,
        count=len(inline),
        input_bytes=sum(len(entry.content.encode("utf-8")) for entry in inline),
    )
    job, result, taken = await env.jobs.submit_inline(
        p, "run_python", python_args(body.code, wall), inline, taker, CallLimits(wall, MCP_QUEUE_WAIT_S, caller.slots)
    )
    run, note = read_code_run(result)
    files = [OutputInfo(name=f.name, mime=f.mime, bytes=len(f.data), returned=True) for f in taken.files]
    files += [_info(skip) for skip in sorted(taken.skipped, key=lambda skip: skip.name)]
    answer = InlinePythonResult(
        stdout=run.stdout,
        stderr=run.stderr,
        truncated=run.truncated,
        job_id=job.id,
        files=files,
        hint=_capped(_join(run.hint, _returned_note(result, note), *_skip_hint(taken.skipped)), MAX_HINT),
    )
    answer = fit_python(answer, settings.result_max_kb, keep_none=True)
    return ToolOutcome(structured=answer.model_dump(mode="json"), blocks=[_block(job.id, f) for f in taken.files])


RUN_PYTHON: Final = ToolSpec(
    name="run_python",
    title="Run Python on inline data",
    description=(
        "Run Python code on the data given with this call and get back what it printed and the files it wrote. "
        "Each data entry is a file in the current folder, under its name, read-only. Write every result file "
        "directly into the out/ folder: tables as CSV, for example df.to_csv('out/counts.csv', index=False), and "
        "charts as PNG, for example plt.savefig('out/chart.png', dpi=100). Only .png, .csv, .txt, .md and .json "
        "files come back, at most 8: an image up to 512 KiB, a text up to 256 KiB, 1 MiB in all. files lists every "
        "file written, with returned false and a hint for one that did not come back; nothing is kept after the "
        "call. Installed: pandas, numpy, scipy, matplotlib. There is no network and nothing can be installed. Use "
        "threads, not multiprocessing. The code runs as a script with a time limit; an error or timeout is in "
        "stderr. stdout and stderr are cut in the middle when long (truncated is then true), so print summaries, "
        "not whole tables. Cite job_id as the source of figures the code computed."
    ),
    input_model=InlinePythonRequest,
    output_model=InlinePythonResult,
    form="inline",
    run=run_python_inline,
    input_schema=input_schema,
)

#: Every inline tool; their names are :data:`~workbench.api.registry.INLINE_TOOLS`.
INLINE_SPECS: Final = (RUN_PYTHON,)
