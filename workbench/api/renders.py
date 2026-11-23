"""The api's view of the renders of a picture, a PDF or an Office file: prepared once, made on demand, read back.

A render is a derived file of its source (:meth:`~workbench.api.store.Store.register_derived`), made by the executor's
``prepare_file`` job through the queue (:meth:`~workbench.api.jobs.JobQueue.submit_renders`). An Office file (a text
document or a deck) is converted once to a PDF by a ``convert_office`` job
(:meth:`~workbench.api.jobs.JobQueue.submit_conversion`), kept as its derived file of role ``pdf``, and its pages or
slides are drawn from that PDF as a PDF's are; a later page reuses it, and one that went is converted again. This module
decides which jobs run and reads their renders back. It decodes and converts nothing, and imports no imaging library and
nothing of LibreOffice: a render, like the PDF, is checked by its signature and its size alone
(:mod:`workbench.renders`).

- :func:`prepared` answers a file's :class:`Manifest`. The first time, one job draws the first pages and finds the
  facts (kind, unit, page count, pages without text), which the whole render of page 1 keeps; an Office file is
  converted first, in the same turn. Every later call answers from the rows, with no job, no event and no wait for the
  source's lock.
  A clip (a video or an audio file) is never read by a job: its prepare runs one job over the two artifacts Nextext
  made of it, kept as its derived files, on the caller's prepare gate, and keeps the transcript and the frames that come
  back (:func:`media_manifest_of`). A clip with no artifact is answered at once, with no job and no event.
- :func:`renders_for` answers the renders of some pages at a strip count, and makes those that are missing, unless
  the source's renders leave no room for one more.
- :func:`read_render` reads one back, or nothing when it is not what its row says.
- :func:`image_block`, :func:`noun_of`, :func:`label`, :func:`shown_image` and :func:`page_window` are what the tools
  that show images share, with the caps of what one answer shows.

Every job is recorded before any file it made is visible (``modify`` / ``file.prepare``, by the source's id and then
each render's, or the PDF's), and one call at a time makes the renders of a source (:meth:`RenderJobs.source_lock`), so
two prepares of one file run one job (an Office file's two) and keep one set of renders. A call that finds the lock
held waits for it up to ``SOURCE_LOCK_WAIT_S``, as long as it would have spent converting the file itself, and then
finds what the holder made. The jobs of one call run in one turn of its gate (:meth:`RenderJobs.turn`), taken inside
the source's lock, never the other way round; a turn that converts an Office file is of the caller's conversion gate,
whoever runs it, so that a caller converts one file at a time and a picture's prepare never waits behind it. The rows
are read again after every job: a render whose row is not there is never answered, whatever the job said it made.

A file the runner refuses for what it is (``unsupported_format``, or an ``unreadable_file`` of :data:`FILE_DECIDED`), at
its prepare or its conversion, is remembered so by the store (:meth:`~workbench.api.store.Store.remember_refusal`), and
every later prepare or render of it is answered with the same refusal at once, with no job, recorded as the refusal
was. Any other ``unreadable_file`` (a write that failed in the job's sandbox, a conversion that ended for no reason the
file gives, a text the api does not know) is remembered only when it comes twice in a row, a job that went well between
starting the count again; out of time, a busy executor or a failure is never remembered.
"""

import base64
import dataclasses
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from typing import Any, Final, Literal, Protocol, override

from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ValidationError

from workbench.api.audit import AuditLog
from workbench.api.jobs import SOURCE_LOCK_WAIT_S, CallLimits, Gate, JobQueue, Keeper, Turn
from workbench.api.store import NotFound, QuotaExceeded, RendersFull, StorageFull, Store, StoreError
from workbench.api.tools import OutputKeeper, ShownImage, _parse, _payload, _readable, judged, refusal
from workbench.config import Settings
from workbench.models import DerivedRecord, FileRecord, JobRecord, Principal
from workbench.paths import PathError
from workbench.protocol import JobResult
from workbench.renders import (
    ARTIFACT_ROLES,
    DERIVED_MAX_BYTES,
    MAX_STRIPS,
    NOTHING_FOUND,
    RENDER_MAX_BYTES,
    RENDERS_PER_JOB,
    SIGNATURES,
    ConvertAnswer,
    PagedUnit,
    RenderAnswer,
    RenderFacts,
    Unit,
    kind_of,
    signature_ok,
    unit_of,
)

_log = logging.getLogger(__name__)

#: Most images one ``view_file`` answer shows.
MAX_VIEW_IMAGES: Final = 8
#: Most images one ``read_document`` answer shows.
MAX_READ_IMAGES: Final = 4
#: Most raw bytes of the images of one answer (2.5 MiB): as base64 they stay under Ghostwriter's 4 MiB answer cap.
MAX_SHOWN_BYTES: Final = 2_621_440

#: The hint of a file that is neither a picture, a PDF nor an Office file that is drawn.
NOT_SHOWN: Final = "This kind of file, such as a spreadsheet, is not shown as pictures; read it with read_document."

#: The text of the 500 for a render the store lost for a reason it cannot name (:func:`_lost`).
NOT_KEPT: Final = "the renders of this file could not be kept"

#: The roles of the derived files that are renders of a picture, a PDF or an Office file.
_RENDER_ROLES: Final = frozenset({"image", "page"})

#: The roles of what a clip's prepare job makes of its raw artifacts (:data:`~workbench.renders.ARTIFACT_ROLES`).
_TRANSCRIPT_ROLE: Final = "transcript"
_FRAME_ROLE: Final = "frame"

#: The role of the PDF an Office file was converted to, which is a derived file of it but no render.
_CONVERTED_ROLE: Final = "pdf"

#: The runner's codes whose refusals the api remembers of a prepare or a conversion, so that each later prepare or
#: render of the file is answered with it at once: every ``unsupported_format`` at once, an ``unreadable_file`` at once
#: when its text is one of :data:`FILE_DECIDED` and else when it comes twice in a row (ruling R22). A ``read_timeout``,
#: a busy executor or a failure is never remembered.
REMEMBERED: Final = frozenset({"unreadable_file", "unsupported_format"})

#: The ``unreadable_file`` refusals the file alone decides, word for word as the runners give them, remembered at once:
#: damaged or not what its name says, too large to read within the memory limit, password-protected (a PDF, an Office
#: file or an old binary one), not opened by LibreOffice, more or larger pictures than can be drawn within the memory
#: limit (also when a conversion redone in parts runs out of its time: ruling R23), a picture over 120 megapixels, a PDF
#: with no pages, a picture that cannot be made small enough, a converted PDF past 32 MB, a clip none of whose
#: artifacts could be read (the artifacts decide it, a new one is a new input: the route that keeps it forgets it).
#:
#: These are copies, never imports (ruling R25): the api imports nothing of the executor, whose modules that hold them
#: load the runners, Pillow or pdfium, none of which the api's image has. A pin test in ``tests/api/test_renders.py``
#: (``test_the_refusals_remembered_at_once_are_the_runners_own_words``) imports the runners' constants and asserts
#: this set equals them, so a text changed on either side fails CI.
#:
#: Any other text, such as a job that could not write inside its sandbox, a conversion that ended for no reason the
#: file gives, or a text added to a runner later, is remembered only when it comes twice in a row: a passing failure
#: costs one job more, and a file that always fails so holds its caller's conversion gate twice, never more.
FILE_DECIDED: Final = frozenset(
    f"unreadable_file: {text}"
    for text in (
        "the file is damaged or truncated, or is not what its extension says",
        "the file is too large to read within the memory limit",
        "the PDF is password-protected (encrypted)",
        "the file is password-protected (encrypted) or in an old binary format: "
        "remove the password and save it as a current .docx, .xlsx or .pptx",
        "LibreOffice could not open the file: it is damaged, or not what its extension says",
        "the file holds more or larger pictures than can be drawn within the memory limit",
        "the picture is larger than 120 megapixels",
        "the PDF has no pages",
        "the picture cannot be made small enough to show",
        "the file is password-protected (encrypted)",
        "the file holds a picture larger than 120 megapixels",
        "the converted file is larger than 32 MB",
        "nothing in the clip's artifacts could be read",
    )
)

#: The status of a refusal answered from memory: both codes of :data:`REMEMBERED` are ``422``.
_REFUSED_STATUS: Final = 422

#: What a render shows: a picture, a page of a PDF or document, or a slide of a deck.
Noun = Literal["image", "page", "slide"]

#: The note the queue adds to a job's message for the outputs it could not keep, their reasons in the order they first
#: came (:meth:`~workbench.api.jobs.JobQueue._keep`). A prepare draws whole pages only, so the whole render of page 1
#: is the first of its outputs, which are in name order: when it was lost, its reason is the first.
_LOST: Final = re.compile(r"\d+ outputs? could not be kept \(([a-z ,]+)\)\Z")

#: What a page or a window of pages may be written as, by the unit of the file: ``page 3``, ``3``, ``pages 2-5`` or
#: ``2-5`` (for a deck ``slide 3``, ``3``, ``slides 2-5`` or ``2-5``), in any case, with the singular and the plural
#: taken alike. Nine digits at most, so no number is too long to read.
_PART: Final[Mapping[PagedUnit, re.Pattern[str]]] = {
    "page": re.compile(r"(?:pages?\s*)?(\d{1,9})(?:\s*-\s*(\d{1,9}))?", re.IGNORECASE),
    "slide": re.compile(r"(?:slides?\s*)?(\d{1,9})(?:\s*-\s*(\d{1,9}))?", re.IGNORECASE),
}
_GRAMMAR: Final[Mapping[PagedUnit, str]] = {
    "page": "no_such_part: give a page as 'page 3', or pages as 'pages 2-5'",
    "slide": "no_such_part: give a slide as 'slide 3', or slides as 'slides 2-5'",
}


class Rendition(BaseModel):
    """One whole render of a file that exists now: the picture, or a page or slide.

    Attributes:
        n: The page or slide, counted from 1; 1 for a picture.
        width: Its width in pixels.
        height: Its height in pixels.
        mime: Its type.
        bytes: Its size in bytes.
    """

    n: int
    width: int
    height: int
    mime: Literal["image/jpeg", "image/png"]
    bytes: int


class Manifest(BaseModel):
    """What a file is to the tools that show it, and which of its renders exist; never its content.

    Attributes:
        path: The file's path.
        kind: ``image``, ``pdf``, ``office`` for a text document or a deck drawn from the PDF it was converted to,
            ``media`` for a clip, or ``none`` for a file that is not shown as pictures.
        unit: What ``total`` and the pages count: ``slide`` for a deck, ``frame`` for a clip, ``page`` for every other
            file, ``none`` too.
        total: Pages of a PDF or a document, slides of a deck, frames of a clip (none for audio, and for a clip with no
            pictures); 1 for a picture, 0 for ``none``.
        pages_without_text: The pages or slides with no text of their own (scans, pictures); empty for a clip.
        renditions: The whole renders that exist now, by page, slide or frame.
        frames: The frame count of a picture with several, of which only the first is shown; else ``None``, a clip's
            too (its frames are ``total``).
        hint: A short note, such as which pages were not checked for text; ``None`` for none.
        duration_s: The length of a clip in seconds, as far as its artifacts say; ``None`` for any other file.
        transcript: Whether a clip has a transcript to read; ``False`` for any other file.
    """

    path: str
    kind: Literal["image", "pdf", "office", "media", "none"]
    unit: Unit = "page"
    total: int
    pages_without_text: list[int]
    renditions: list[Rendition]
    frames: int | None
    hint: str | None
    duration_s: float | None = None
    transcript: bool = False


@dataclasses.dataclass(frozen=True)
class WithBlocks:
    """A tool's answer and the image blocks that go with it, in the order its ``shown`` names them.

    Attributes:
        answer: The answer.
        blocks: The MCP image blocks (:func:`image_block`).
    """

    answer: BaseModel
    blocks: list[dict[str, Any]]


class RenderJobs(Protocol):
    """The queue as a call that renders sees it, within that call's limits and on its gate.

    The MCP door's :class:`~workbench.api.tools_mcp.CallJobs` is one, on the user's own turn; :class:`RouteJobs` is the
    file routes'. The turn and both submits are on the same gate.
    """

    async def submit_renders(
        self,
        p: Principal,
        source: FileRecord,
        args: dict[str, Any],
        keeper: Keeper,
        *,
        turn: Turn | None = None,
        converted: DerivedRecord | None = None,
        artifacts: Sequence[DerivedRecord] = (),
    ) -> tuple[JobRecord, JobResult, list[DerivedRecord]]:
        """Render a picture, pages of a PDF or a clip's frames, as :meth:`~workbench.api.jobs.JobQueue.submit_renders`.

        Args:
            p: The caller, user and chat the job runs for: the source's own.
            source: The file to render.
            args: The runner's other arguments.
            keeper: Records the job before any render becomes visible, and picks the renders to keep.
            turn: The turn of the gate the caller holds (:meth:`turn`), in which the job runs; ``None`` waits for one.
            converted: The PDF an Office source was converted to, which the job reads; ``None`` reads the source.
            artifacts: A clip's raw artifacts, which the job reads in place of the clip.

        Returns:
            The job record, the executor's answer and the renders kept.
        """
        ...

    async def submit_conversion(
        self, p: Principal, source: FileRecord, args: dict[str, Any], keeper: Keeper, *, turn: Turn | None = None
    ) -> tuple[JobRecord, JobResult, list[DerivedRecord]]:
        """Convert an Office file to a PDF, as :meth:`~workbench.api.jobs.JobQueue.submit_conversion` does.

        Args:
            p: The caller, user and chat the job runs for: the source's own.
            source: The Office file.
            args: The runner's other arguments.
            keeper: Records the job before the PDF becomes visible, and picks the outputs to keep.
            turn: The turn of the gate the caller holds (:meth:`turn`), in which the job runs; ``None`` waits for one.

        Returns:
            The job record, the executor's answer and the PDF kept, if it was.
        """
        ...

    def turn(self, p: Principal, *, converting: bool = False) -> AbstractAsyncContextManager[Turn]:
        """Hold one turn of the gate for the jobs of one call, as :meth:`~workbench.api.jobs.JobQueue.turn` does.

        Args:
            p: The caller and user.
            converting: Whether the turn converts an Office file: it is then of the caller's conversion gate, whatever
                the call's own gate is, waited for up to ``SOURCE_LOCK_WAIT_S`` if the call's own wait is shorter (it
                waits out another file's conversion, which ends within one job wall), and the jobs sent in it run on
                that gate.

        Returns:
            The turn, held while the context runs; entering it raises ``Busy`` as a job's turn does.
        """
        ...

    def source_lock(self, source_id: str) -> AbstractAsyncContextManager[None]:
        """Hold the lock of one source's renders, as :meth:`~workbench.api.jobs.JobQueue.source_lock` does.

        Args:
            source_id: The source's id.

        Returns:
            The lock, held while the context runs; entering it raises ``Busy("wait_timeout")`` after
            ``SOURCE_LOCK_WAIT_S``.
        """
        ...


def gate_for(turn: Turn | None, gate: Gate) -> Gate:
    """The gate a job of a call runs on: its conversion's turn's, inside one, or else the call's own.

    Args:
        turn: The turn the job is sent in, or ``None``.
        gate: The call's own gate.

    Returns:
        ``conversion`` for a job sent in a turn of the conversion gate; ``gate`` otherwise, which the queue checks
        against any other turn.
    """
    return "conversion" if turn is not None and turn.gate == "conversion" else gate


@dataclasses.dataclass(frozen=True)
class RouteJobs:
    """The queue as a file route sees it: its call's limits, and the gate its jobs wait for.

    Attributes:
        queue: The app's job queue.
        call: The call's wall, wait and caller slots.
        gate: ``prepare`` for the prepare route, ``user`` for a view on the user's own turn.
    """

    queue: JobQueue
    call: CallLimits
    gate: Gate

    async def submit_renders(
        self,
        p: Principal,
        source: FileRecord,
        args: dict[str, Any],
        keeper: Keeper,
        *,
        turn: Turn | None = None,
        converted: DerivedRecord | None = None,
        artifacts: Sequence[DerivedRecord] = (),
    ) -> tuple[JobRecord, JobResult, list[DerivedRecord]]:
        """Render within the call's limits, on the route's gate.

        Args:
            p: The caller, user and chat the job runs for: the source's own.
            source: The file to render.
            args: The runner's other arguments.
            keeper: Records the job before any render becomes visible, and picks the renders to keep.
            turn: The turn of the route's gate the call holds (:meth:`turn`), or ``None`` to wait for one.
            converted: The PDF an Office source was converted to, which the job reads; ``None`` reads the source.
            artifacts: A clip's raw artifacts, which the job reads in place of the clip.

        Returns:
            What :meth:`~workbench.api.jobs.JobQueue.submit_renders` returns.
        """
        return await self.queue.submit_renders(
            p,
            source,
            args,
            keeper,
            call=self.call,
            gate=gate_for(turn, self.gate),
            turn=turn,
            converted=converted,
            artifacts=artifacts,
        )

    async def submit_conversion(
        self, p: Principal, source: FileRecord, args: dict[str, Any], keeper: Keeper, *, turn: Turn | None = None
    ) -> tuple[JobRecord, JobResult, list[DerivedRecord]]:
        """Convert an Office file to a PDF within the call's limits, on the route's gate.

        Args:
            p: The caller, user and chat the job runs for: the source's own.
            source: The Office file.
            args: The runner's other arguments.
            keeper: Records the job before the PDF becomes visible, and picks the outputs to keep.
            turn: The turn of the route's gate the call holds (:meth:`turn`), or ``None`` to wait for one.

        Returns:
            What :meth:`~workbench.api.jobs.JobQueue.submit_conversion` returns.
        """
        gate = gate_for(turn, self.gate)
        return await self.queue.submit_conversion(p, source, args, keeper, call=self.call, gate=gate, turn=turn)

    def turn(self, p: Principal, *, converting: bool = False) -> AbstractAsyncContextManager[Turn]:
        """Hold one turn of the route's gate, or of the caller's conversion gate, within the call's limits.

        Args:
            p: The caller and user.
            converting: Whether the turn converts an Office file. It then waits for the conversion gate up to
                ``SOURCE_LOCK_WAIT_S``, if the route's own wait is shorter, as for a source's lock.

        Returns:
            The turn, held while the context runs.
        """
        if not converting:
            return self.queue.turn(p, call=self.call, gate=self.gate)
        call = dataclasses.replace(self.call, wait_s=max(self.call.wait_s, SOURCE_LOCK_WAIT_S))
        return self.queue.turn(p, call=call, gate="conversion")

    def source_lock(self, source_id: str) -> AbstractAsyncContextManager[None]:
        """Hold the lock of one source's renders, waiting at most ``SOURCE_LOCK_WAIT_S``, whatever the route's wait.

        Args:
            source_id: The source's id.

        Returns:
            The lock, held while the context runs.
        """
        return self.queue.source_lock(source_id, SOURCE_LOCK_WAIT_S)


def _is_render(rec: DerivedRecord) -> bool:
    """Tell whether a derived file is a render: a picture or a page, of a type a render has, with its size.

    Args:
        rec: The derived file.

    Returns:
        Whether it is.
    """
    return rec.role in _RENDER_ROLES and rec.mime in SIGNATURES and rec.width is not None and rec.height is not None


def _facts(head: DerivedRecord) -> RenderFacts | None:
    """Read the facts the whole render of page 1 keeps, leniently: a fact this version does not know is left out.

    A later version may keep more facts (and gives each it adds a default, so that it reads this version's); a head
    it prepared is still read here, after a rollback, rather than taken for one with no facts, which no job could mend.

    Args:
        head: The whole render of page 1.

    Returns:
        The facts, or ``None`` when it keeps none that can be read: a render made by a job that did not look for them.
    """
    stored = head.meta.get("facts")
    if isinstance(stored, Mapping):
        stored = {name: value for name, value in stored.items() if name in RenderFacts.model_fields}
    try:
        return RenderFacts.model_validate(stored)
    except ValidationError:
        return None


def manifest_of(source: FileRecord, rows: Sequence[DerivedRecord]) -> Manifest | None:
    """Build a file's manifest from its rows alone.

    Args:
        source: The file.
        rows: Its derived files, as :meth:`~workbench.api.store.Store.derived_of` lists them.

    Returns:
        The manifest: the facts of the whole render of page 1 and every whole render of its role, by page. ``None``
        when there is no whole render of page 1 that carries its facts: the file is not prepared yet. The unit is the
        facts'; a head kept before there were units has none, and reads as ``page``.
    """
    whole = [rec for rec in rows if _is_render(rec) and rec.strips == 1]
    head = next((rec for rec in whole if rec.ordinal == 1), None)
    facts = None if head is None else _facts(head)
    if head is None or facts is None:
        return None
    renditions = [
        Rendition.model_validate(
            {"n": rec.ordinal, "width": rec.width, "height": rec.height, "mime": rec.mime, "bytes": rec.size}
        )
        for rec in sorted(whole, key=lambda rec: rec.ordinal)
        if rec.role == head.role
    ]
    return Manifest(
        path=source.virtual_path,
        kind=facts.kind,
        unit=facts.unit,
        total=facts.total,
        pages_without_text=facts.pages_without_text,
        renditions=renditions,
        frames=facts.frames,
        hint=facts.hint,
    )


def _is_frame(rec: DerivedRecord) -> bool:
    """Tell whether a derived file is a frame of a clip: a JPEG of its role with its size.

    Args:
        rec: The derived file.

    Returns:
        Whether it is.
    """
    return rec.role == _FRAME_ROLE and rec.mime in SIGNATURES and rec.width is not None and rec.height is not None


def media_manifest_of(source: FileRecord, rows: Sequence[DerivedRecord]) -> Manifest | None:
    """Build a clip's manifest from its rows alone.

    The facts ride on the transcript, else on frame 1 (:meth:`~workbench.api.jobs.JobQueue._render_place`), and are read
    leniently as a picture's are (:func:`_facts`).

    Args:
        source: The clip.
        rows: Its derived files, as :meth:`~workbench.api.store.Store.derived_of` lists them.

    Returns:
        The manifest: the frames that exist, by their number, and whether a transcript does. ``None`` when the clip has
        neither the transcript nor frame 1 with facts of a clip that can be read: it is not prepared yet, or what it
        was prepared as is gone.
    """
    transcript = next((rec for rec in rows if rec.role == _TRANSCRIPT_ROLE), None)
    frames = sorted((rec for rec in rows if _is_frame(rec)), key=lambda rec: rec.ordinal)
    head = transcript or next((rec for rec in frames if rec.ordinal == 1), None)
    facts = None if head is None else _facts(head)
    if facts is None or facts.kind != "media":
        return None
    renditions = [
        Rendition.model_validate(
            {"n": rec.ordinal, "width": rec.width, "height": rec.height, "mime": rec.mime, "bytes": rec.size}
        )
        for rec in frames
    ]
    return Manifest(
        path=source.virtual_path,
        kind="media",
        unit="frame",
        total=facts.total,
        pages_without_text=[],
        renditions=renditions,
        frames=None,
        hint=facts.hint,
        duration_s=facts.duration_s,
        transcript=transcript is not None,
    )


def converted_of(rows: Sequence[DerivedRecord]) -> DerivedRecord | None:
    """Find the PDF an Office file was converted to among its derived files.

    Args:
        rows: The file's derived files, as :meth:`~workbench.api.store.Store.derived_of` lists them.

    Returns:
        The row of role ``pdf`` (a source has one at most), or ``None`` when it was not converted, or its PDF went.
    """
    return next((rec for rec in rows if rec.role == _CONVERTED_ROLE), None)


def _office(source: FileRecord, pdf: DerivedRecord) -> dict[str, object]:
    """The ``office`` argument of a render job over an Office file's PDF: what the PDF stands for.

    Args:
        source: The Office file; its suffix gives the unit.
        pdf: The PDF it was converted to; its row keeps the slides the reader counted in a deck.

    Returns:
        An :class:`~workbench.renders.OfficeRequest` as JSON. A slide count the row does not keep as a whole number of
        at least 1 is left out (``None``), so a row of another version never fails the job: the executor then only
        cannot say that the deck's slides are not its pages.
    """
    slides = pdf.meta.get("slides")
    counted = slides if isinstance(slides, int) and not isinstance(slides, bool) and slides >= 1 else None
    return {"unit": unit_of(source.name), "slides": counted}


def _budget(settings: Settings) -> int:
    """The seconds a render job's runner has, 3/4 of the job's wall, so it can say it timed out.

    Args:
        settings: The api's settings; ``job_wall_s`` is the wall.

    Returns:
        At least 1.
    """
    return max(1, settings.job_wall_s * 3 // 4)


def _decider(
    *, strips: int, on_demand: bool
) -> Callable[[JobResult, Sequence[str]], tuple[list[str], str, dict[str, object]]]:
    """How a render job is judged: an ``ok`` answer that reads as a render answer keeps every output the job staged.

    Args:
        strips: Into how many strips each page is cut.
        on_demand: Whether the job makes pages a call asked for after the prepare.

    Returns:
        The keeper's ``decide``: given the answer and the ids planned for its outputs, the ids to keep (all or none),
        the outcome and the criteria of the event.
    """

    def decide(result: JobResult, planned: Sequence[str]) -> tuple[list[str], str, dict[str, object]]:
        usable = result.status == "ok" and _readable(RenderAnswer, result.result)
        keep: list[str] = list(planned) if usable else []
        outcome, more = judged(result, usable)
        criteria: dict[str, object] = {
            "renders": len(planned),
            "strips": strips,
            "on_demand": on_demand,
            "outputs": len(keep),
        }
        return keep, outcome, {**criteria, **more}

    return decide


def _keeper(audit: AuditLog, p: Principal, source: FileRecord, *, strips: int, on_demand: bool) -> OutputKeeper:
    """The keeper of a render job: it records the job by the source and the renders it keeps, before any is visible.

    An ``ok`` answer that reads as a render answer keeps every render the job staged; any other keeps none. The queue
    keeps each only as the render the answer names (:meth:`~workbench.api.jobs.JobQueue.submit_renders`).

    Args:
        audit: The audit log.
        p: The caller, user and chat of the source.
        source: The file rendered.
        strips: Into how many strips each page is cut.
        on_demand: Whether the job makes pages a call asked for after the prepare.

    Returns:
        The keeper.
    """
    return OutputKeeper(audit, p, "prepare", [source.id], _decider(strips=strips, on_demand=on_demand))


@dataclasses.dataclass(frozen=True)
class _RemakingKeeper(OutputKeeper):
    """The keeper of a clip's prepare that makes again what an earlier prepare left without its facts.

    A transcript or a frame an earlier prepare could not finish (its head was lost to the quota, say) stays beside the
    clip's artifacts with no manifest. The job makes them all again, so before it keeps anything the keeper replaces
    what is there, as an artifact posted again does (:meth:`~workbench.api.store.Store.place_derived`): the one event
    of the prepare names the clip, the new ids and then the ids replaced, with their count as ``replaced``, and only
    then are the old rows and files deleted. A job that kept nothing replaces nothing.

    Attributes:
        store: The store, which deletes the rows replaced.
        stale: The clip's transcript and frame rows found, under its lock, when the job was sent.
    """

    store: Store
    stale: Sequence[DerivedRecord]

    @override
    def record(self, result: JobResult, planned: Sequence[str]) -> Sequence[str]:
        """Write the job's event, naming what it keeps and what that replaces, then delete what is replaced.

        Args:
            result: The executor's answer.
            planned: The ids the job's outputs will have.

        Returns:
            The ids to keep.

        Raises:
            Exception: What the event's write raised, with nothing deleted; or what a deletion raised, after the event,
                which is recorded again as an ``error`` by the same ids and raised, with nothing kept. A row that was
                not deleted is replaced by the next prepare.
        """
        keep, outcome, criteria = self.decide(result, planned)
        replacing: list[DerivedRecord] = list(self.stale) if keep else []
        more: dict[str, object] = {"replaced": len(replacing)} if replacing else {}
        ids = [*keep, *(rec.id for rec in replacing)]
        self.audit.record(
            self.tool, self.p, file_ids=[*self.used, *ids], outcome=outcome, criteria={**criteria, **more}
        )
        try:
            for rec in replacing:
                self.store.remove_derived(rec)
        except BaseException:
            try:
                self.again(ids)
            except Exception as exc:  # the deletion's error is the answer; this one is only logged
                _log.warning(
                    "could not record a prepare that failed to replace what it remade (%s)", type(exc).__name__
                )
            raise
        return keep


def _lost(message: str) -> Exception:
    """Name why the whole render of page 1, or a converted PDF, was not kept, from the note the queue adds to a message.

    Args:
        message: The job's message.

    Returns:
        The store's error of that loss: a 507 for the user's quota, the source's cap of renders or a full volume, and
        ``NotFound`` (404) for a source that went while the job ran, with its chat or by retention. Any other loss,
        such as a render that exists already (or a reserved folder that is not one) or one the store did not foresee,
        or none, is a 500. The first reason names the loss, except a ``duplicate`` that comes before another: a job of
        strips draws again the strips of a page that exist, which the store refuses as duplicates before it refuses
        the one that was lost.
    """
    found = _LOST.search(message)
    reasons: list[str] = found.group(1).split(", ") if found is not None else []
    reason = next((why for why in reasons if why != "duplicate"), reasons[0] if reasons else None)
    if reason == "gone":
        return NotFound("file not found")
    if reason == "quota":
        return QuotaExceeded("workspace quota exceeded")
    if reason == "full":
        return RendersFull("the renders of this file are at their limit")
    if reason == "storage full":
        return StorageFull("storage is full")
    return HTTPException(status_code=500, detail=NOT_KEPT)


def _settled(store: Store, source: FileRecord, result: JobResult) -> dict[str, Any]:
    """Take the runner's answer out of a prepare's or a conversion's job, remembering or counting a refusal of it.

    Args:
        store: The store, which remembers the refusal.
        source: The file the job was of.
        result: The executor's answer.

    Returns:
        The runner's answer, of a job that ran to the end.

    Raises:
        HTTPException: What :func:`~workbench.api.tools._payload` raises; a refusal of :data:`REMEMBERED` is remembered
            for ``source`` first, in the words it is raised with: at once when the file decides it, else counted
            (:data:`FILE_DECIDED`).
    """
    try:
        payload = _payload(result, "file")
    except HTTPException as refused:
        detail = str(refused.detail)
        code = refusal(result)
        if code in REMEMBERED:
            at_once = code == "unsupported_format" or detail in FILE_DECIDED
            store.remember_refusal(source, detail, at_once=at_once)
        raise
    store.forget_refusal(source)  # a job that went well: a count of refusals that may pass starts again
    return payload


def _record_remembered(
    audit: AuditLog, p: Principal, source: FileRecord, detail: str, *, strips: int, on_demand: bool
) -> None:
    """Record a refusal answered from memory as its job's was: an ``error`` of ``prepare`` naming the file, by its code.

    Args:
        audit: The audit log, bound to the call.
        p: The caller, user and chat of the source.
        source: The file refused.
        detail: The refusal remembered; its code alone is recorded.
        strips: Into how many strips the call would have cut each page.
        on_demand: Whether the call asked for pages after the prepare.

    Raises:
        AuditWriteError: The event could not be written; the refusal must not go out.
    """
    criteria: dict[str, object] = {
        "renders": 0,
        "strips": strips,
        "on_demand": on_demand,
        "outputs": 0,
        "refused": detail.partition(": ")[0],
        "remembered": True,
    }
    audit.record("prepare", p, file_ids=[source.id], outcome="error", criteria=criteria)


async def _refuse_remembered(
    p: Principal, source: FileRecord, *, store: Store, audit: AuditLog, strips: int, on_demand: bool
) -> None:
    """Answer a file refused before with the same refusal, with no job: recorded first, then raised.

    The caller holds the source's lock and has checked the call's purpose for ``modify``, as before a job.

    Args:
        p: The caller, user and chat of the source.
        source: The file.
        store: The store, which remembers the refusal.
        audit: The audit log, bound to the call.
        strips: Into how many strips the call would have cut each page.
        on_demand: Whether the call asked for pages after the prepare.

    Raises:
        HTTPException: ``422``, the refusal remembered, word for word.
        AuditWriteError: Its event could not be written; the refusal does not go out.
    """
    detail = store.refusal_of(source)
    if detail is None:
        return
    await run_in_threadpool(_record_remembered, audit, p, source, detail, strips=strips, on_demand=on_demand)
    raise HTTPException(status_code=_REFUSED_STATUS, detail=detail)


async def _convert(
    p: Principal,
    source: FileRecord,
    *,
    settings: Settings,
    store: Store,
    audit: AuditLog,
    jobs: RenderJobs,
    turn: Turn,
    on_demand: bool,
) -> DerivedRecord:
    """Convert an Office file to a PDF in a turn the caller holds, and answer the PDF's row.

    The caller holds the source's lock and a turn of the conversion gate, and has checked the call's purpose and the
    refusals remembered. The job is recorded before the PDF is visible (``modify`` / ``file.prepare``, by the source's
    id and then the PDF's), with ``converted`` among its criteria and no render. A refusal of the file itself is
    remembered (:func:`_settled`).

    Args:
        p: The caller, user and chat of the source.
        source: The Office file.
        settings: The api's settings; ``job_wall_s`` gives the runner's time.
        store: The store.
        audit: The audit log, bound to the call.
        jobs: The queue, within the call's limits and on its gate.
        turn: The turn of the conversion gate the caller holds; the job runs in it.
        on_demand: Whether the conversion is for pages a call asked for after the prepare (its PDF went).

    Returns:
        The PDF's row, read again after the job.

    Raises:
        ValueError: ``turn`` is not of the conversion gate, so the caller could convert beside another conversion of
            its own; that is a programming error, raised before any job or event.
        AuditWriteError: The job's event could not be written; no PDF is kept.
        Busy: The executor is busy.
        HTTPException: The runner refused the file (its code's status: 422 ``unreadable_file`` for a damaged,
            password-protected or old binary file, one that could not be converted or whose PDF would pass 32 MB, one
            holding a picture over 120 megapixels or more pictures than can be drawn within the memory limit; 422
            ``unsupported_format`` for a deck with no slides, a macro-enabled one or a slide show), which is remembered;
            the job ran out of time (422), or failed or answered what cannot be read (500); or the PDF was not kept for
            a reason that is not one of the three below (500).
        QuotaExceeded: The PDF would take the user over their quota (507).
        RendersFull: It would take the source over its cap of derived files (507).
        StorageFull: The volume is full (507).
        NotFound: The file went while the job ran, with its chat (404).
    """

    def decide(result: JobResult, planned: Sequence[str]) -> tuple[list[str], str, dict[str, object]]:
        usable = result.status == "ok" and _readable(ConvertAnswer, result.result)
        keep: list[str] = list(planned) if usable else []
        outcome, more = judged(result, usable)
        criteria: dict[str, object] = {
            "renders": 0,
            "strips": 1,
            "on_demand": on_demand,
            "converted": True,
            "outputs": len(keep),
        }
        return keep, outcome, {**criteria, **more}

    if turn.gate != "conversion":
        raise ValueError("a conversion runs in a turn of the conversion gate")
    keeper = OutputKeeper(audit, p, "prepare", [source.id], decide)
    _, result, _ = await jobs.submit_conversion(p, source, {"budget_s": _budget(settings)}, keeper, turn=turn)
    _parse(ConvertAnswer, _settled(store, source, result))
    pdf = converted_of(await run_in_threadpool(store.derived_of, source))
    if pdf is None:
        raise _lost(result.message)
    return pdf


def not_shown(source: FileRecord) -> Manifest:
    """The manifest of a file that is not shown as pictures: ``kind: none``, nothing drawn, and the hint to read it.

    Args:
        source: The file.

    Returns:
        The manifest, counted by page.
    """
    return Manifest(
        path=source.virtual_path,
        kind="none",
        unit="page",
        total=0,
        pages_without_text=[],
        renditions=[],
        frames=None,
        hint=NOT_SHOWN,
    )


def nothing_found(source: FileRecord) -> Manifest:
    """The manifest of a clip that has neither artifact: no frame, no transcript, and the hint that nothing was found.

    Args:
        source: The clip.

    Returns:
        The manifest, counted by frame.
    """
    return Manifest(
        path=source.virtual_path,
        kind="media",
        unit="frame",
        total=0,
        pages_without_text=[],
        renditions=[],
        frames=None,
        hint=NOTHING_FOUND,
    )


async def _prepared_clip(
    p: Principal, source: FileRecord, *, settings: Settings, store: Store, audit: AuditLog, jobs: RenderJobs
) -> Manifest:
    """Answer a clip's manifest, making its transcript and frames from its artifacts the first time.

    The clip itself is no input of any job: the one job reads the artifacts Nextext made of it, which the store keeps as
    its derived files, and runs on the caller's prepare gate, as a picture's does, never the conversion gate. A
    transcript or frames an earlier prepare left without their facts (a loss at its keep) are made again by this job and
    replace what is there, recorded with it (:class:`_RemakingKeeper`), so a retry ends with one set of rows.

    Args:
        p: The caller, user and chat of the clip.
        source: The clip.
        settings: The api's settings; ``job_wall_s`` gives the runner's time.
        store: The store.
        audit: The audit log, bound to the call.
        jobs: The queue, within the call's limits and on its gate.

    Returns:
        The manifest.

    Raises:
        PurposeRequired: The call had to state a purpose for ``modify`` and did not; no job runs.
        AuditWriteError: The job's event, or that of a refusal remembered, could not be written; nothing is kept, and
            no refusal goes out.
        Busy: The lock or the queue did not come free in time, or the executor is busy.
        HTTPException: The runner refused the artifacts (422 ``unreadable_file``: neither gave anything usable, which
            is remembered), or an earlier prepare did; the job ran out of time (422), or failed or answered what cannot
            be read (500); or neither the transcript nor frame 1, which holds the facts, was kept for a reason that is
            not one of the three below (500).
        QuotaExceeded: The transcript or a frame would take the user over their quota (507).
        RendersFull: It would take the clip over its cap of derived files (507).
        StorageFull: The volume is full (507).
        NotFound: The clip went while the call waited for the lock or the job ran, with its chat (404). A clip whose
            artifacts went and which is still there is answered at once as one with nothing found.
    """
    rows = await run_in_threadpool(store.derived_of, source)
    manifest = media_manifest_of(source, rows)
    if manifest is not None:
        return manifest
    if not any(rec.role in ARTIFACT_ROLES for rec in rows):
        return nothing_found(source)
    async with jobs.source_lock(source.id):
        rows = await run_in_threadpool(store.derived_of, source)
        manifest = media_manifest_of(source, rows)
        if manifest is not None:
            return manifest
        held = [rec for rec in rows if rec.role in ARTIFACT_ROLES]
        if not held:  # while the call waited for the lock, the clip went, or its artifacts did
            current = await run_in_threadpool(store.get, p, source.virtual_path)  # NotFound: the clip is gone
            if current.id != source.id:
                raise NotFound("file not found")  # another file has its path now
            return nothing_found(source)
        await run_in_threadpool(audit.check, "prepare", p)
        await _refuse_remembered(p, source, store=store, audit=audit, strips=1, on_demand=False)
        args: dict[str, Any] = {"budget_s": _budget(settings), "facts": True}
        stale = [rec for rec in rows if rec.role in (_TRANSCRIPT_ROLE, _FRAME_ROLE)]  # an earlier prepare's leftovers
        keeper = _RemakingKeeper(
            audit, p, "prepare", [source.id], _decider(strips=1, on_demand=False), store=store, stale=stale
        )
        _, result, _ = await jobs.submit_renders(p, source, args, keeper, artifacts=held)
        _parse(RenderAnswer, _settled(store, source, result))
        manifest = media_manifest_of(source, await run_in_threadpool(store.derived_of, source))
        if manifest is None:
            raise _lost(result.message)
    return manifest


async def prepared(
    p: Principal, source: FileRecord, *, settings: Settings, store: Store, audit: AuditLog, jobs: RenderJobs
) -> Manifest:
    """Answer a file's manifest, rendering its first pages the first time.

    A file that is neither a picture, a PDF, an Office file nor a clip is answered at once, ``kind: none``, with no job.
    A clip is answered by :func:`_prepared_clip`: from its artifacts, in one job on the prepare gate, the first time,
    and at once, with no job and no event, when it has none. A file
    whose rows hold the manifest is answered from them, with no job, no event and no wait for the source's lock, which a
    job of later pages may hold. One that does not takes the lock, reads the rows again inside it, and has one job,
    which draws the first pages (but those whole pages it has already), finds the facts and is recorded before any
    render is visible. An Office file takes one turn inside the lock and has two jobs in it: the conversion to a PDF
    (:func:`_convert`), then the drawing of that PDF, both on the caller's conversion gate; one whose PDF is kept
    already is only drawn, on the call's own gate. A second call for the same file waits for the lock (up to
    ``SOURCE_LOCK_WAIT_S``) and then finds the rows. A file the runner refused for what it is, here or in a
    conversion, is answered with that refusal at once, with no job (:data:`REMEMBERED`), and so is every later call.

    Args:
        p: The caller, user and chat of the source.
        source: The file.
        settings: The api's settings; ``job_wall_s`` gives the runner's time.
        store: The store.
        audit: The audit log, bound to the call.
        jobs: The queue, within the call's limits and on its gate.

    Returns:
        The manifest.

    Raises:
        PurposeRequired: The call had to state a purpose for ``modify`` and did not; no job runs.
        AuditWriteError: The job's event, or that of a refusal remembered, could not be written; no render is kept, and
            no refusal goes out.
        Busy: The lock or the queue did not come free in time, or the executor is busy.
        HTTPException: The runner refused the file, now or before (its code's status, such as 422
            ``unreadable_file``), the job ran out of time (422), or failed or answered what cannot be read (500); or the
            whole render of page 1, which holds the facts, or an Office file's PDF, was not kept for a reason that is
            not one of the three below (500).
        QuotaExceeded: That render, or the PDF, would take the user over their quota (507).
        RendersFull: It would take the source over its cap of renders (507).
        StorageFull: The volume is full (507).
        NotFound: The file went while the job ran, with its chat (404).
    """
    kind = kind_of(source.name)
    if kind == "none":
        return not_shown(source)
    if kind == "media":
        return await _prepared_clip(p, source, settings=settings, store=store, audit=audit, jobs=jobs)
    manifest = manifest_of(source, await run_in_threadpool(store.derived_of, source))
    if manifest is not None:
        return manifest
    async with jobs.source_lock(source.id):
        rows = await run_in_threadpool(store.derived_of, source)
        manifest = manifest_of(source, rows)
        if manifest is not None:
            return manifest
        await run_in_threadpool(audit.check, "prepare", p)
        await _refuse_remembered(p, source, store=store, audit=audit, strips=1, on_demand=False)
        have = sorted({rec.ordinal for rec in rows if _is_render(rec) and rec.strips == 1})
        args: dict[str, Any] = {"pages": None, "strips": 1, "have": have, "budget_s": _budget(settings), "facts": True}
        keeper = _keeper(audit, p, source, strips=1, on_demand=False)
        if kind == "office":
            kept = converted_of(rows)
            async with jobs.turn(p, converting=kept is None) as turn:
                pdf = kept or await _convert(
                    p, source, settings=settings, store=store, audit=audit, jobs=jobs, turn=turn, on_demand=False
                )
                args["office"] = _office(source, pdf)
                _, result, _ = await jobs.submit_renders(p, source, args, keeper, turn=turn, converted=pdf)
        else:
            _, result, _ = await jobs.submit_renders(p, source, args, keeper)
        _parse(RenderAnswer, _settled(store, source, result))
        manifest = manifest_of(source, await run_in_threadpool(store.derived_of, source))
        if manifest is None:
            raise _lost(result.message)
    return manifest


def _no_room(rows: Sequence[DerivedRecord], strips: int) -> bool:
    """Tell whether a source's renders leave no room for one more at a strip count, so that a job would keep none.

    The store refuses a render that would take its source past ``DERIVED_MAX_BYTES``. The renders of one file at one
    strip count are of like size, so when the room left is less than the smallest of them the source holds, the job is
    not run: it would be thrown away, with its event, on every view of a page past the cap. With none held at that
    strip count, the job runs and the store decides.

    Args:
        rows: The source's derived files, every one counted.
        strips: Into how many strips each page would be cut.

    Returns:
        Whether there is no room.
    """
    alike = [rec.size for rec in rows if _is_render(rec) and rec.strips == strips]
    return bool(alike) and DERIVED_MAX_BYTES - sum(rec.size for rec in rows) < min(alike)


def _complete(rows: Sequence[DerivedRecord], role: str, strips: int) -> dict[int, list[DerivedRecord]]:
    """Find the pages whose renders at a strip count are all there.

    Args:
        rows: The source's derived files.
        role: ``image`` or ``page``.
        strips: Into how many strips each page is cut.

    Returns:
        Each page that has every one of its strips, to those strips in order; a page that lacks one is not in it.
    """
    cut: dict[int, dict[int, DerivedRecord]] = {}
    for rec in rows:
        if _is_render(rec) and rec.role == role and rec.strips == strips:
            cut.setdefault(rec.ordinal, {})[rec.strip] = rec
    every = range(1, strips + 1)
    return {n: [held[i] for i in every] for n, held in cut.items() if all(i in held for i in every)}


async def renders_for(
    p: Principal,
    source: FileRecord,
    manifest: Manifest,
    pages: Sequence[int],
    strips: int,
    *,
    settings: Settings,
    store: Store,
    audit: AuditLog,
    jobs: RenderJobs,
) -> list[DerivedRecord]:
    """Answer the renders of some pages at a strip count, making those that are missing.

    The rows are read first; when a page lacks a render, the missing pages are made under the source's lock, the rows
    read again inside it, by one job per at most ``RENDERS_PER_JOB // strips`` pages, all in one turn of the gate taken
    inside the lock. The pages of an Office file are drawn from its PDF; when the PDF went, it is converted again first,
    in the same turn, then of the caller's conversion gate (:func:`_convert`), unless the file was refused before: that
    refusal is answered at once. Each job is recorded before any render it made is visible, and does not look
    for the facts again: the manifest holds them (ruling R4). No job runs
    when the source's renders leave no room for one more (:func:`_no_room`): the missing pages are left out as the cap
    would leave them. A page the job did not make, or whose renders the store could not keep (the user's quota, the
    source's cap, a full volume), is left out: the answer is only rows that exist. When none of the pages asked for
    exists once the jobs are through, and a job reported that renders could not be kept, the first such loss is raised,
    as :func:`prepared` raises the loss of the head (:func:`_lost`), or the cap when no job ran for want of room; a page
    left out beside others that exist is not an error.

    Args:
        p: The caller, user and chat of the source.
        source: The file.
        manifest: Its manifest, from :func:`prepared`.
        pages: The pages, counted from 1, in any order; a page outside the file is left out.
        strips: Into how many strips each page is cut, 1 to ``MAX_STRIPS``.
        settings: The api's settings; ``job_wall_s`` gives the runner's time.
        store: The store.
        audit: The audit log, bound to the call.
        jobs: The queue, within the call's limits and on its gate.

    Returns:
        The renders, by page and then by strip; every strip of each page, or none of it.

    Raises:
        ValueError: ``strips`` is not 1 to ``MAX_STRIPS``; the caller checks it first, so this is a programming error.
        PurposeRequired: The call had to state a purpose for ``modify`` and did not; no job runs.
        AuditWriteError: A job's event could not be written; none of its renders is kept.
        Busy: The lock or the queue did not come free in time, or the executor is busy.
        HTTPException: The runner refused the file (its code's status), or refused its conversion now or before (422),
            the job ran out of time (422), or failed or answered what cannot be read (500); an Office file's PDF,
            converted again, was not kept for a reason that is not one of the three below (500); or no page asked for
            exists and the loss a job reported is not one of the three below (500).
        QuotaExceeded: No page asked for exists, and the first loss was the user's quota (507).
        RendersFull: No page asked for exists, and the first loss was the source's cap of renders, or the renders left
            no room for one more (507).
        StorageFull: No page asked for exists, and the first loss was a full volume (507).
        NotFound: No page asked for exists, and the file went while a job ran, with its chat (404).
    """
    if not 1 <= strips <= MAX_STRIPS:
        raise ValueError(f"strips is 1 to {MAX_STRIPS}")
    if manifest.kind in ("none", "media"):  # a clip's frames are made with its transcript, never one by one
        return []
    role = "image" if manifest.kind == "image" else "page"
    wanted = sorted({n for n in pages if 1 <= n <= manifest.total})
    complete = _complete(await run_in_threadpool(store.derived_of, source), role, strips)
    losses: list[str] = []
    full = False
    if any(n not in complete for n in wanted):
        async with jobs.source_lock(source.id):
            rows = await run_in_threadpool(store.derived_of, source)
            complete = _complete(rows, role, strips)
            missing = [n for n in wanted if n not in complete]
            full = bool(missing) and _no_room(rows, strips)
            if missing and not full:
                await run_in_threadpool(audit.check, "prepare", p)
                per_job = RENDERS_PER_JOB // strips
                kept = converted_of(rows) if manifest.kind == "office" else None
                converting = manifest.kind == "office" and kept is None
                if converting:
                    await _refuse_remembered(p, source, store=store, audit=audit, strips=strips, on_demand=True)
                async with jobs.turn(p, converting=converting) as turn:
                    pdf = kept
                    if converting:
                        pdf = await _convert(
                            p, source, settings=settings, store=store, audit=audit, jobs=jobs, turn=turn, on_demand=True
                        )
                    for start in range(0, len(missing), per_job):
                        args: dict[str, Any] = {
                            "pages": missing[start : start + per_job],
                            "strips": strips,
                            "budget_s": _budget(settings),
                            "facts": False,
                        }
                        if pdf is not None:
                            args["office"] = _office(source, pdf)
                        keeper = _keeper(audit, p, source, strips=strips, on_demand=True)
                        _, result, _ = await jobs.submit_renders(p, source, args, keeper, turn=turn, converted=pdf)
                        _parse(RenderAnswer, _payload(result, "file"))
                        if _LOST.search(result.message) is not None:
                            losses.append(result.message)
                complete = _complete(await run_in_threadpool(store.derived_of, source), role, strips)
    found = [rec for n in wanted if n in complete for rec in complete[n]]
    if wanted and not found and full:
        raise RendersFull("the renders of this file are at their limit")
    if wanted and not found and losses:
        raise _lost(losses[0])
    return found


def read_render(store: Store, rec: DerivedRecord) -> bytes | None:
    """Read a render back, whole, if it is what its row says it is.

    Args:
        store: The store.
        rec: The render, as :func:`renders_for` answered it.

    Returns:
        Its bytes, when its row is still there, its file is a regular one under ``RENDER_MAX_BYTES`` and it begins with
        the signature of its type; ``None`` otherwise, logged by the error's type or the check that failed alone,
        never by a path, a name or a byte.
    """
    try:
        with store.open_derived(rec) as src:
            data = src.read(RENDER_MAX_BYTES)
    except (StoreError, PathError, OSError) as exc:
        _log.warning("a render could not be read (%s)", type(exc).__name__)
        return None
    if len(data) >= RENDER_MAX_BYTES:
        _log.warning("a render is not what its row says (size)")
        return None
    if not signature_ok(data, rec.mime):
        _log.warning("a render is not what its row says (signature)")
        return None
    return data


def image_block(mime: str, data: bytes) -> dict[str, Any]:
    """Build the MCP image block of a render, for the model alone.

    Args:
        mime: Its type.
        data: Its bytes.

    Returns:
        The block: its bytes as base64, its type, and the assistant as its only audience.
    """
    return {
        "type": "image",
        "data": base64.b64encode(data).decode("ascii"),
        "mimeType": mime,
        "annotations": {"audience": ["assistant"]},
    }


def noun_of(manifest: Manifest) -> Noun:
    """Say what a file's renders show, by its manifest.

    Args:
        manifest: The file's manifest.

    Returns:
        ``image`` for a picture, ``page`` for a PDF, the unit of an Office file (``page`` or ``slide``); ``page`` for a
        file that is not shown, which has no renders, and for a clip, whose frames are labelled by
        :func:`~workbench.renders.frame_label`.
    """
    if manifest.kind == "image":
        return "image"
    if manifest.kind == "office" and manifest.unit == "slide":
        return "slide"
    return "page"


def label(noun: Noun, n: int, strip: int, strips: int) -> str:
    """Say what a render shows.

    Args:
        noun: ``image``, ``page`` or ``slide`` (:func:`noun_of`).
        n: The page or slide, counted from 1.
        strip: Which strip, counted from 1.
        strips: Into how many strips the page is cut.

    Returns:
        ``image``, ``image, strip 1 of 2``, ``page 3``, ``page 3, strip 2 of 3``, ``slide 3`` or
        ``slide 3, strip 2 of 3``.
    """
    whole = "image" if noun == "image" else f"{noun} {n}"
    return whole if strips == 1 else f"{whole}, strip {strip} of {strips}"


def shown_image(noun: Noun, rec: DerivedRecord) -> ShownImage:
    """Describe a render that comes with an answer.

    Args:
        noun: ``image``, ``page`` or ``slide``, what the file's renders show (:func:`noun_of`).
        rec: The render.

    Returns:
        Its label, page or slide, size and type.
    """
    return ShownImage.model_validate(
        {
            "label": label(noun, rec.ordinal, rec.strip, rec.strips),
            "n": rec.ordinal,
            "width": rec.width,
            "height": rec.height,
            "mime": rec.mime,
        }
    )


def page_window(part: str | None, total: int, *, default_last: int, unit: PagedUnit = "page") -> tuple[int, int]:
    """Read which pages, or slides of a deck, a call asks to see.

    Args:
        part: ``page N`` or ``N`` for one page; ``pages N-M`` or ``N-M`` for N to M; ``None`` for the first pages.
            For ``unit`` ``slide``, ``slide`` and ``slides`` (alike) in place of ``page`` and ``pages``.
        total: How many pages or slides the file has.
        default_last: The last page of the window when ``part`` is ``None``.
        unit: What the file counts, ``page`` or ``slide``; only its own word is taken.

    Returns:
        The first and the last page: ``(1, min(default_last, total))`` for ``None``; otherwise ``(N, N)``, or
        ``(N, min(M, total))``.

    Raises:
        HTTPException: 422 ``no_such_part: …``: ``part`` is not of that unit's grammar, its end comes before its
            start, or N is not a page (or slide) of the file.
    """
    if part is None:
        return 1, min(default_last, total)
    found = _PART[unit].fullmatch(part.strip())
    if found is None:
        raise HTTPException(status_code=422, detail=_GRAMMAR[unit])
    first = int(found.group(1))
    last = first if found.group(2) is None else int(found.group(2))
    if last < first:
        raise HTTPException(status_code=422, detail=_GRAMMAR[unit])
    if not 1 <= first <= total:
        noun = unit if total == 1 else f"{unit}s"
        raise HTTPException(status_code=422, detail=f"no_such_part: this document has {total} {noun}")
    return first, min(last, total)
