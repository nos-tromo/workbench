"""The api's view of the measure of a document: its length in its reader's units, as Ghostwriter asks it.

``POST /files/measure`` (:func:`workbench.api.files.measure_file`) answers how long one file of a chat is: how many
pages, slides, paragraphs, rows or lines its reader counts in it (``total``), and how many characters a full read of it
shows (``chars``, counted only up to the executor's cap, with ``capped`` saying it stopped there). Ghostwriter warns on
a long document's tile with it, once the file is uploaded. The api parses no file: the count is made by the readers
``read_document`` uses, in a ``measure_file`` job of the executor, and :func:`measured` runs that job and sends on what
it counted.

- A picture or a clip is no document and has no text to measure: it is refused by its name, with no job and no event
  (:data:`NOT_A_DOCUMENT`).
- Every answer is the work of one job, recorded (``query`` / ``file.measure``, by the file's id and no count of it)
  before it goes out. The counts, and a refusal that the file alone decides (``unsupported_format``, and an
  ``unreadable_file``: the readers' refusals are made by the file at the job's fixed limits), are remembered by the
  store (:meth:`~workbench.api.store.Store.remember_measure`), so a file is measured once: the same call again is
  answered from memory, with no job and no event, and a refusal again is recorded, as the refusal of a prepare is, as
  an ``error`` with ``remembered``. A run that ran out of time (``read_timeout``), a file that went
  (``file_not_found``), a busy executor and any failure are never remembered, so the next call tries again.
- The job runs on the caller's measure gate (:meth:`~workbench.api.jobs.JobQueue.submit`), so that a slow measure never
  stands in front of a prepare, a conversion or a tool call.

Nothing here holds a name, a path or a text: the memory, the events and the logs have ids, counts and codes.
"""

from typing import Final, Self

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field, model_validator

from workbench.api.audit import AuditLog
from workbench.api.jobs import CallLimits, JobQueue
from workbench.api.store import Store
from workbench.api.tools import _parse, _payload, record_answer, refusal
from workbench.config import Settings
from workbench.models import FileRecord, Principal
from workbench.renders import READ_UNITS, ReaderKind, ReadUnit, kind_of

#: What a picture or a clip is told: they are not documents, whatever their names.
NOT_A_DOCUMENT: Final = "unsupported_format: a picture or a clip is not a document and has no text to measure"

#: The runner's codes whose refusals are remembered of a measure, at once: the readers refuse a file by what it is, at
#: the job's fixed limits. A ``read_timeout``, a ``file_not_found``, a busy executor or a failure is never remembered.
REMEMBERED: Final = frozenset({"unsupported_format", "unreadable_file"})

#: The kinds of file that are no documents, by :func:`~workbench.renders.kind_of`.
_NOT_DOCUMENTS: Final = frozenset({"image", "media"})

#: The status of a refusal: the file is what it is.
_REFUSED_STATUS: Final = 422


class MeasureRequest(BaseModel):
    """Body of ``POST /files/measure``: the file and nothing else.

    Attributes:
        path: The file's virtual path, in the request's chat.
    """

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=1024)


class Measured(BaseModel):
    """What a measure answers: the length of a document in the unit its reader counts.

    Attributes:
        path: The file's virtual path.
        kind: The family of document its reader takes it for.
        unit: What ``total`` counts: the unit of ``kind`` (:data:`~workbench.renders.READ_UNITS`).
        total: How many units there are, whole whatever ``capped`` says.
        chars: How many characters a full read shows, counted up to the executor's cap.
        capped: Whether the count of characters stopped at the cap, so that ``chars`` is the cap and the text is longer.
    """

    path: str
    kind: ReaderKind
    unit: ReadUnit
    total: int = Field(ge=0)
    chars: int = Field(ge=0)
    capped: bool

    @model_validator(mode="after")
    def _unit_of_the_kind(self) -> Self:
        """Refuse an answer whose unit is not the one its kind is counted in.

        Returns:
            The answer.

        Raises:
            ValueError: ``unit`` is not ``READ_UNITS[kind]``.
        """
        if self.unit != READ_UNITS[self.kind]:
            raise ValueError("unit is not the unit of the kind")
        return self


def _record_remembered(audit: AuditLog, p: Principal, source: FileRecord, detail: str) -> None:
    """Record a refusal answered from memory as its job's was: an ``error`` naming the file, by its code.

    Args:
        audit: The audit log, bound to the call.
        p: The caller, user and chat of the source.
        source: The file refused.
        detail: The refusal remembered; its code alone is recorded.

    Raises:
        AuditWriteError: The event could not be written; the refusal must not go out.
    """
    criteria: dict[str, object] = {"refused": detail.partition(": ")[0], "remembered": True}
    audit.record("measure", p, file_ids=[source.id], outcome="error", criteria=criteria)


async def measured(
    p: Principal,
    source: FileRecord,
    *,
    settings: Settings,
    store: Store,
    audit: AuditLog,
    queue: JobQueue,
    call: CallLimits,
) -> Measured:
    """Measure one file of the chat: from what the store remembers of it, else in one job on the measure gate.

    The caller has checked the call's purpose for ``query``. The job is recorded once it has answered, before anything
    of the answer, or of its refusal, goes out.

    Args:
        p: The caller, user and chat of the source.
        source: The file, as the store found it for its owner.
        settings: The api's settings; ``job_wall_s`` gives the runner's time, 3/4 of it.
        store: The store, which remembers the counts, or the refusal the file decides.
        audit: The audit log, bound to the call.
        queue: The app's job queue.
        call: The call's wall, its wait for the measure gate and the caller's slots.

    Returns:
        The file's path, kind, unit, total, characters and whether they stopped at the cap.

    Raises:
        HTTPException: ``422`` for a picture or a clip (:data:`NOT_A_DOCUMENT`), and for the readers' refusal of the
            file, word for word; what :func:`~workbench.api.tools._payload` raises for any other failure of the job; a
            ``500`` for an answer that cannot be read.
        AuditWriteError: The event could not be written; nothing goes out.
        Busy: The gate's line or the queue is full, or no turn or slot came in time.
    """
    if kind_of(source.name) in _NOT_DOCUMENTS:
        raise HTTPException(status_code=_REFUSED_STATUS, detail=NOT_A_DOCUMENT)
    held = store.measure_of(source)
    if isinstance(held, str):
        await run_in_threadpool(_record_remembered, audit, p, source, held)
        raise HTTPException(status_code=_REFUSED_STATUS, detail=held)
    if held is not None:
        return _parse(Measured, {**held, "path": source.virtual_path})
    args = {"path": source.name, "budget_s": max(1, settings.job_wall_s * 3 // 4)}
    _, result, _ = await queue.submit(p, "measure_file", args, [source.name], call=call, gate="measure")
    payload = {**result.result, "path": source.virtual_path}
    await run_in_threadpool(record_answer, audit, "measure", p, [source.id], {}, result, Measured, payload)
    try:
        _payload(result, "file")
    except HTTPException as refused:
        if refusal(result) in REMEMBERED:
            store.remember_measure(source, str(refused.detail))
        raise
    answer = _parse(Measured, payload)
    store.remember_measure(source, answer.model_dump(mode="json", exclude={"path"}))
    return answer
