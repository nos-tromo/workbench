"""The api's renders: a file is prepared once, later pages and strips are made on demand, and renders are read back.

The jobs run the real ``prepare_file`` runner in this process (:class:`~tests.api.test_tools_templates.Local`) over
files built at test time; every text, name and id is invented. Here the api's part: which jobs run and with what, what
the manifest says, what is recorded before a render is visible, the lock that lets two prepares of one file run one job,
the cap of a source's renders, and the checks a render passes before its bytes go anywhere.
"""

import asyncio
import base64
import contextlib
import dataclasses
import functools
import io
import json
import logging
import subprocess
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, override

import pytest
from fastapi import HTTPException

from tests.api.conftest import CALL, PG, AuditLines, put, unrecorded
from tests.api.test_tools_templates import Local
from tests.conftest import FakeOffice, scaled
from tests.fixtures import make_fixtures as mk
from tests.fixtures import make_images as mi
from tests.fixtures import make_templates as mt
from workbench.api import renders as api_renders
from workbench.api import tools_mcp
from workbench.api import tools_views as views
from workbench.api.audit import AuditLog, PurposeRequired
from workbench.api.errors import describe
from workbench.api.jobs import MCP_QUEUE_WAIT_S, Busy, CallLimits, Gate, JobQueue, Turn
from workbench.api.metrics import Metrics
from workbench.api.renders import (
    MAX_READ_IMAGES,
    MAX_SHOWN_BYTES,
    MAX_VIEW_IMAGES,
    Manifest,
    RenderJobs,
    RouteJobs,
    WithBlocks,
    converted_of,
    image_block,
    label,
    manifest_of,
    noun_of,
    page_window,
    prepared,
    read_render,
    renders_for,
    shown_image,
)
from workbench.api.store import (
    Conflict,
    NotFound,
    QuotaExceeded,
    RendersFull,
    StorageFull,
    Store,
    StoreError,
    new_file_id,
)
from workbench.api.tools import ShownImage
from workbench.api.tools_mcp import CallJobs
from workbench.api.tools_views import ViewedFile, ViewFileRequest
from workbench.config import Settings
from workbench.models import DerivedRecord, DerivedSpec, FileRecord, Principal
from workbench.protocol import JobRequest, JobResult
from workbench.renders import PAGE_LONG_SIDE, RENDER_MAX_BYTES, RENDERS_PER_JOB
from workbench.utils.audit_event import AuditWriteError, verify_chain

pytestmark = pytest.mark.anyio

#: The hint of a file that is neither a picture nor a PDF.
NOT_SHOWN = "This kind of file, such as a spreadsheet, is not shown as pictures; read it with read_document."


@pytest.fixture
def anyio_backend() -> str:
    """Run the async tests on asyncio, the loop the api runs on."""
    return "asyncio"


class Slow(Local):
    """The in-process executor, each job of which waits a moment before it runs, so that calls overlap."""

    @override
    async def run(self, req: JobRequest, timeout_s: float) -> JobResult:
        """Wait, then run the runner."""
        await asyncio.sleep(0.2)
        return await super().run(req, timeout_s)


class Held(Local):
    """The in-process executor, each job of which, its runner through, waits for the test to let it answer."""

    def __init__(self, data_dir: Path, jobs: Path) -> None:
        """Run the real runners; answer when :attr:`release` is set."""
        super().__init__(data_dir, jobs)
        self.through = asyncio.Event()
        self.release = asyncio.Event()

    @override
    async def run(self, req: JobRequest, timeout_s: float) -> JobResult:
        """Run the runner, say so, then wait to answer."""
        result = await super().run(req, timeout_s)
        self.through.set()
        await self.release.wait()
        return result


@dataclasses.dataclass(frozen=True)
class World:
    """The api's collaborators over the in-process executor, and the helpers the tests share.

    Attributes:
        settings: The api's settings.
        store: The store.
        audit: The audit log, its chain open.
        executor: The in-process executor.
        queue: The job queue over it.
        lines: The audit events written so far.
        built: Where files are built.
    """

    settings: Settings
    store: Store
    audit: AuditLog
    executor: Local
    queue: JobQueue
    lines: AuditLines
    built: Path

    def over(self, executor: Local) -> "World":
        """The same world over another executor, with a queue of its own."""
        queue = JobQueue(self.settings, executor, self.store, Metrics(self.settings.data_dir))
        return dataclasses.replace(self, executor=executor, queue=queue)

    def jobs(self, gate: Gate = "user") -> RouteJobs:
        """The queue as a file route sees it."""
        return RouteJobs(self.queue, CALL, gate)

    async def prepare(self, source: FileRecord, *, audit: AuditLog | None = None) -> Manifest:
        """Prepare a file as the prepare route does: on the prepare gate."""
        return await prepared(
            PG, source, settings=self.settings, store=self.store, audit=audit or self.audit, jobs=self.jobs("prepare")
        )

    async def renders(
        self,
        source: FileRecord,
        manifest: Manifest,
        pages: Sequence[int],
        strips: int = 1,
        *,
        audit: AuditLog | None = None,
    ) -> list[DerivedRecord]:
        """The renders of some pages, made on demand on the user's own turn."""
        return await renders_for(
            PG,
            source,
            manifest,
            pages,
            strips,
            settings=self.settings,
            store=self.store,
            audit=audit or self.audit,
            jobs=self.jobs(),
        )

    def events(self) -> list[dict[str, Any]]:
        """The ``file.prepare`` events written so far."""
        return [event for event in self.lines() if event["operation"] == "file.prepare"]

    def pdf(self, name: str, pages: Sequence[str | None]) -> FileRecord:
        """A PDF of those pages, uploaded to Ghostwriter's chat; ``None`` is a page with a picture and no text."""
        return put(self.store, name, mk.make_pdf(self.built / name, pages).read_bytes(), p=PG)

    def photo(self, name: str = "quay-photo.jpg") -> FileRecord:
        """A small photo, uploaded to Ghostwriter's chat."""
        return put(self.store, name, mi.make_photo(self.built / name).read_bytes(), p=PG)

    def deck(self, slides: int, name: str = "harbour-deck.pptx", **more: Any) -> FileRecord:
        """A deck of ``slides`` invented slides (``make_deck``'s other arguments in ``more``), uploaded to the chat."""
        return put(self.store, name, mk.make_deck(self.built / name, slides, **more).read_bytes(), p=PG)

    def converted(self, source: FileRecord) -> DerivedRecord:
        """The PDF an Office file was converted to, which must be there."""
        pdf = converted_of(self.store.derived_of(source))
        assert pdf is not None
        return pdf

    def file_of(self, rec: DerivedRecord) -> Path:
        """Where a render lies on the volume."""
        return self.settings.data_dir / "workspaces" / rec.caller / rec.user_id / rec.chat_id / rec.relative_path


@pytest.fixture
def world(
    settings: Settings, store: Store, audit: AuditLog, audit_lines: AuditLines, tmp_path: Path
) -> Iterator[World]:
    """The world, its audit chain open; the chain is closed and verified afterwards."""
    built = tmp_path / "built"
    built.mkdir()
    executor = Local(settings.data_dir, tmp_path / "jobs")
    audit.start({"retention_days": 7})
    queue = JobQueue(settings, executor, store, Metrics(settings.data_dir))
    yield World(settings, store, audit, executor, queue, audit_lines, built)
    audit.close()
    report = verify_chain(audit_lines())
    assert report.ok, report.problems


def quay_pages(count: int) -> list[str | None]:
    """The text of each page of an invented quay list."""
    return [f"Quay list, page {n}" for n in range(1, count + 1)]


def capped(store: Store, monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    """Lower the source's cap of renders (``DERIVED_MAX_BYTES``, the store's default) to ``cap`` bytes."""
    real = functools.partial(Store.register_derived, store)  # the store's own, however often this is applied

    def register(*args: Any, **more: Any) -> DerivedRecord:
        return real(*args, **{**more, "cap_bytes": cap})

    monkeypatch.setattr(store, "register_derived", register)


# --- prepared ----------------------------------------------------------------------------------------------------


async def test_prepare_runs_once_and_then_answers_from_the_rows(world: World) -> None:
    """The first prepare runs one job; the second answers the same manifest from the rows, with no job and no event."""
    source = world.photo()
    first = await world.prepare(source)
    requests, events = len(world.executor.requests), len(world.lines())
    second = await world.prepare(source)

    assert (first.kind, first.total, first.frames, [r.n for r in first.renditions]) == ("image", 1, None, [1])
    assert second == first
    assert (requests, len(world.executor.requests), len(world.lines())) == (1, 1, events)


async def test_a_manifest_names_pages_without_text_and_renditions(world: World) -> None:
    """A scan's page without text is named; each rendition is a row, its size its file's; one event names them all."""
    source = world.pdf("harbour-report.pdf", ["Harbour figures 2026", None, "Quay list"])
    manifest = await world.prepare(source)
    rows = world.store.derived_of(source)

    assert manifest.model_dump(exclude={"renditions"}) == {
        "path": source.virtual_path,
        "kind": "pdf",
        "unit": "page",
        "total": 3,
        "pages_without_text": [2],
        "frames": None,
        "hint": None,
    }
    assert [r.n for r in manifest.renditions] == [row.ordinal for row in rows] == [1, 2, 3]
    assert [r.bytes for r in manifest.renditions] == [world.file_of(row).stat().st_size for row in rows]
    assert [(r.width, r.height, r.mime) for r in manifest.renditions] == [(o.width, o.height, o.mime) for o in rows]
    assert all(max(r.width, r.height) == PAGE_LONG_SIDE for r in manifest.renditions)
    (event,) = world.events()
    assert (event["action"], event["outcome"]) == ("modify", "ok")
    assert event["target"]["ids"] == [source.id, *(row.id for row in rows)]
    assert event["criteria"] == {"caller": "ghostwriter", "renders": 3, "strips": 1, "on_demand": False, "outputs": 3}
    assert world.store.files_in_chat(PG) == [source]  # renders are never files of the chat


async def test_a_spreadsheet_is_kind_none_without_a_job(world: World) -> None:
    """Neither a picture, a PDF nor a document or deck: kind ``none``, nothing to show, no job and no event."""
    built = mk.make_xlsx(world.built / "quarterly-figures.xlsx")
    source = put(world.store, "quarterly-figures.xlsx", built.read_bytes(), p=PG)
    events = len(world.lines())
    manifest = await world.prepare(source)

    assert manifest.model_dump(mode="json") == {
        "path": source.virtual_path,
        "kind": "none",
        "unit": "page",
        "total": 0,
        "pages_without_text": [],
        "renditions": [],
        "frames": None,
        "hint": NOT_SHOWN,
    }
    assert (world.executor.requests, len(world.lines())) == ([], events)
    assert await world.renders(source, manifest, [1]) == []


async def test_a_prepare_looks_for_text_and_an_on_demand_job_does_not(world: World) -> None:
    """Ruling R4: the prepare's job asks for the facts, the job of a later page does not (the api holds them)."""
    source = world.pdf("quay-list.pdf", quay_pages(21))
    manifest = await world.prepare(source)
    await world.renders(source, manifest, [21])

    prepare, later = (req.args for req in world.executor.requests)
    budget = max(1, world.settings.job_wall_s * 3 // 4)
    assert prepare == {"pages": None, "strips": 1, "have": [], "budget_s": budget, "facts": True, "path": source.name}
    assert later == {"pages": [21], "strips": 1, "budget_s": budget, "facts": False, "path": source.name}


async def test_a_prepare_does_not_render_again_the_pages_it_has(world: World) -> None:
    """Whole pages already rendered go to the job as ``have``, so only the others are drawn."""
    source = world.pdf("quay-list.pdf", quay_pages(3))
    staged = world.built / "p0002.jpg"
    mi.make_photo(staged)
    spec = DerivedSpec("page", 2, "image/jpeg", 64, 48, {})
    held = world.store.register_derived(source, staged, spec, file_id=new_file_id(), quota_bytes=10**9)
    manifest = await world.prepare(source)

    (req,) = world.executor.requests
    assert req.args["have"] == [2]
    assert [r.n for r in manifest.renditions] == [1, 2, 3]
    assert next(row for row in world.store.derived_of(source) if row.ordinal == 2) == held


async def test_two_prepares_at_once_run_one_job(world: World) -> None:
    """Review Focus 4: two prepares of one file at once run one job, keep one set of renders and record no error."""
    slow = world.over(Slow(world.settings.data_dir, world.built.parent / "slow-jobs"))
    source = slow.pdf("harbour-report.pdf", ["Harbour figures 2026", None, "Quay list"])
    first, second = await asyncio.gather(slow.prepare(source), slow.prepare(source))

    assert first == second
    assert len(slow.executor.requests) == 1
    assert [row.ordinal for row in slow.store.derived_of(source)] == [1, 2, 3]
    assert [event["outcome"] for event in slow.lines() if event["operation"].startswith("file.")] == ["ok"]


async def test_a_prepared_file_is_answered_while_its_lock_is_held(world: World) -> None:
    """Final review M1: the rows answer a prepared file, and the pages it has, without waiting for the source's lock.

    A job of later pages may hold the lock for seconds; a view of what is there does not wait behind it.
    """
    source = world.pdf("quay-list.pdf", quay_pages(3))
    manifest = await world.prepare(source)
    requests = len(world.executor.requests)
    held, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        async with world.queue.source_lock(source.id, 60):
            held.set()
            await release.wait()

    holder = asyncio.create_task(hold())
    await held.wait()
    try:
        again = await asyncio.wait_for(world.prepare(source), scaled(2))
        pages = await asyncio.wait_for(world.renders(source, manifest, [1, 2]), scaled(2))
    finally:
        release.set()
        await holder
    assert again == manifest
    assert [r.ordinal for r in pages] == [1, 2]
    assert len(world.executor.requests) == requests


async def test_a_chat_deleted_while_its_prepare_runs_is_not_found(world: World) -> None:
    """Final review M4: the file went with its chat, so the prepare is a 404, not a render that could not be kept."""
    executor = Held(world.settings.data_dir, world.built.parent / "held-jobs")
    held = world.over(executor)
    source = held.pdf("harbour-report.pdf", ["Harbour figures 2026", None])
    preparing = asyncio.create_task(held.prepare(source))
    await executor.through.wait()
    held.store.delete_chat(PG, unrecorded)
    executor.release.set()

    with pytest.raises(NotFound) as gone:
        await preparing
    assert describe(gone.value) == (404, "not found")
    assert held.store.derived_of(source) == []
    assert [event["outcome"] for event in held.events()] == ["ok", "error"]


async def test_a_refusal_answers_its_code_and_keeps_nothing(world: World) -> None:
    """A text file named ``.png`` is refused as unreadable (422); the refusal is recorded by its code alone."""
    source = put(world.store, "quay-photo.png", b"Quay list, not a picture", p=PG)
    with pytest.raises(HTTPException) as refused:
        await world.prepare(source)

    assert refused.value.status_code == 422
    assert str(refused.value.detail).startswith("unreadable_file:")
    assert world.store.derived_of(source) == []
    (event,) = world.events()
    assert (event["outcome"], event["target"]["ids"]) == ("error", [source.id])
    assert event["criteria"] == {
        "caller": "ghostwriter",
        "renders": 0,
        "strips": 1,
        "on_demand": False,
        "outputs": 0,
        "refused": "unreadable_file",
    }


async def test_an_answer_that_is_no_render_answer_is_an_error_and_keeps_nothing(world: World) -> None:
    """A job that answers ``ok`` with what is no render answer is a 500, recorded as ``error`` with nothing kept."""
    source = world.photo()
    world.executor.payload = {"answer": 42}
    world.executor.files = {"p0001.jpg": b"\xff\xd8\xff"}
    with pytest.raises(HTTPException) as failed:
        await world.prepare(source)

    assert failed.value.status_code == 500
    assert world.store.derived_of(source) == []
    (event,) = world.events()
    assert (event["outcome"], event["target"]["ids"]) == ("error", [source.id])
    assert (event["criteria"]["renders"], event["criteria"]["outputs"]) == (1, 0)


async def test_a_render_job_that_must_state_a_purpose_runs_none(world: World) -> None:
    """A call bound to a log that needs a purpose for ``modify``, and states none, is refused before any job.

    A prepare is refused so; so is a later page, while the pages already made are still answered without a job.
    """
    source = world.photo()
    bound = world.audit.bound(None, "req-1", required=frozenset({"modify"}))
    with pytest.raises(PurposeRequired):
        await world.prepare(source, audit=bound)
    assert world.executor.requests == []
    (event,) = world.events()
    assert (event["outcome"], event["criteria"]["refused"]) == ("denied", "purpose_required")

    scan = world.pdf("quay-list.pdf", quay_pages(21))
    manifest = await world.prepare(scan)
    assert [r.ordinal for r in await world.renders(scan, manifest, [1], audit=bound)] == [1]
    with pytest.raises(PurposeRequired):
        await world.renders(scan, manifest, [21], audit=bound)
    assert len(world.executor.requests) == 1
    assert [event["outcome"] for event in world.events()] == ["denied", "ok", "denied"]


async def test_a_prepare_that_cannot_be_recorded_keeps_no_render(
    world: World, audit_stream: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: the job's event cannot be written, so no render is kept and the error goes up."""
    source = world.photo()

    def refuse(_text: str, /) -> int:
        raise OSError("synthetic audit stream failure")

    with monkeypatch.context() as broken:
        broken.setattr(audit_stream, "write", refuse)
        with pytest.raises(AuditWriteError):
            await world.prepare(source)

    assert world.store.derived_of(source) == []


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        (QuotaExceeded("workspace quota exceeded"), QuotaExceeded),
        (RendersFull("the renders of this file are at their limit"), RendersFull),
        (StorageFull("storage is full"), StorageFull),
        (Conflict("render exists"), HTTPException),
        (NotFound("file not found"), HTTPException),
    ],
    ids=["quota", "full", "storage-full", "duplicate", "failed"],
)
async def test_a_head_that_cannot_be_kept_raises_its_loss(
    world: World, monkeypatch: pytest.MonkeyPatch, error: StoreError, raised: type[Exception]
) -> None:
    """The whole render of page 1 holds the facts; one not kept is the store's loss (507), or else a 500."""
    source = world.photo()

    def refuse(*_: Any, **__: Any) -> DerivedRecord:
        raise error

    monkeypatch.setattr(world.store, "register_derived", refuse)
    with pytest.raises(raised) as lost:
        await world.prepare(source)

    if isinstance(lost.value, HTTPException):
        assert lost.value.status_code == 500
    assert world.store.derived_of(source) == []
    assert [event["outcome"] for event in world.events()] == ["ok", "error"]  # the render, named again


# --- renders_for -------------------------------------------------------------------------------------------------


async def test_missing_pages_and_strips_are_made_on_demand(world: World) -> None:
    """Page 21 of 22 is one job; page 2 in two strips is another; asking again makes none; the manifest grows."""
    source = world.pdf("quay-list.pdf", quay_pages(22))
    manifest = await world.prepare(source)
    assert [r.n for r in manifest.renditions] == list(range(1, 21))

    later = await world.renders(source, manifest, [21])
    assert [(r.ordinal, r.strip, r.strips) for r in later] == [(21, 1, 1)]
    assert len(world.executor.requests) == 2
    halves = await world.renders(source, manifest, [2], 2)
    assert [(r.ordinal, r.strip, r.strips) for r in halves] == [(2, 1, 2), (2, 2, 2)]
    assert len(world.executor.requests) == 3

    assert await world.renders(source, manifest, [21]) == later
    assert await world.renders(source, manifest, [2], 2) == halves
    assert len(world.executor.requests) == 3
    assert [r.n for r in (await world.prepare(source)).renditions] == list(range(1, 22))
    on_demand = [event["criteria"] for event in world.events()][1:]
    assert on_demand == [
        {"caller": "ghostwriter", "renders": 1, "strips": 1, "on_demand": True, "outputs": 1},
        {"caller": "ghostwriter", "renders": 2, "strips": 2, "on_demand": True, "outputs": 2},
    ]


async def test_renders_come_by_page_then_strip_and_jobs_hold_at_most_their_cap(world: World) -> None:
    """Pages asked in any order come back in page order; one job makes at most ``RENDERS_PER_JOB`` renders."""
    source = world.pdf("quay-list.pdf", quay_pages(8))
    manifest = await world.prepare(source)
    made = await world.renders(source, manifest, [8, 1, 3, 2, 4, 5, 6, 7, 3], 3)

    assert [(r.ordinal, r.strip) for r in made] == [(n, i) for n in range(1, 9) for i in (1, 2, 3)]
    chunks = [req.args["pages"] for req in world.executor.requests[1:]]
    assert chunks == [[1, 2, 3, 4, 5, 6], [7, 8]]
    assert all(len(pages) * 3 <= RENDERS_PER_JOB for pages in chunks)


async def test_pages_outside_the_document_are_left_out(world: World) -> None:
    """A page past the last, or before the first, is no render and costs no job; a strip count past 3 is a bug."""
    source = world.pdf("quay-list.pdf", quay_pages(2))
    manifest = await world.prepare(source)

    assert [r.ordinal for r in await world.renders(source, manifest, [0, 2, 3])] == [2]
    assert len(world.executor.requests) == 1
    for strips in (0, 4):
        with pytest.raises(ValueError, match="strips is 1 to 3"):
            await world.renders(source, manifest, [1], strips)


async def test_a_calls_jobs_render_on_demand_too(world: World) -> None:
    """The MCP door's queue is render jobs as the routes' is: a strip of a picture through it."""
    source = world.photo()
    jobs: RenderJobs = CallJobs(world.queue, CALL)
    manifest = await prepared(PG, source, settings=world.settings, store=world.store, audit=world.audit, jobs=jobs)
    strips = await renders_for(
        PG, source, manifest, [1], 2, settings=world.settings, store=world.store, audit=world.audit, jobs=jobs
    )

    assert [(r.role, r.ordinal, r.strip, r.strips) for r in strips] == [("image", 1, 1, 2), ("image", 1, 2, 2)]


async def test_a_source_at_its_render_cap_says_so(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review Focus 5: a page past the cap is 507 when it is all that was asked; pages rendered are still served.

    A call for a page that was lost and one that exists answers the one that exists. A head past the cap is 507 too.
    """
    source = world.pdf("quay-list.pdf", quay_pages(22))
    manifest = await world.prepare(source)
    capped(world.store, monkeypatch, sum(row.size for row in world.store.derived_of(source)))

    with pytest.raises(RendersFull):
        await world.renders(source, manifest, [21])
    assert [r.ordinal for r in await world.renders(source, manifest, [1, 21])] == [1]
    assert [event["outcome"] for event in world.events()][1:] == ["ok", "error", "ok", "error"]

    fresh = world.pdf("harbour-report.pdf", ["Harbour figures 2026", None])
    capped(world.store, monkeypatch, 1)
    with pytest.raises(RendersFull):
        await world.prepare(fresh)
    assert world.store.derived_of(fresh) == []


async def test_a_page_past_the_cap_runs_no_job_when_no_render_has_room(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Final review M7: when the room left is less than the smallest page held, a page past the cap makes no job.

    The limit is raised at once: no request reaches the executor and no event is written. With room for a page like
    the smallest, the job runs, and the store decides.
    """
    source = world.pdf("quay-list.pdf", quay_pages(22))
    manifest = await world.prepare(source)
    rows = world.store.derived_of(source)
    held, smallest = sum(row.size for row in rows), min(row.size for row in rows)
    monkeypatch.setattr(api_renders, "DERIVED_MAX_BYTES", held + smallest - 1)
    capped(world.store, monkeypatch, held + smallest - 1)
    requests, events = len(world.executor.requests), len(world.events())

    with pytest.raises(RendersFull):
        await world.renders(source, manifest, [21])
    assert [r.ordinal for r in await world.renders(source, manifest, [1, 21])] == [1]
    assert (len(world.executor.requests), len(world.events())) == (requests, events)

    monkeypatch.setattr(api_renders, "DERIVED_MAX_BYTES", held + smallest)
    capped(world.store, monkeypatch, held + smallest)
    with contextlib.suppress(RendersFull):  # the job ran; page 21 may still be larger than the smallest page
        await world.renders(source, manifest, [21])
    assert len(world.executor.requests) == requests + 1


async def test_two_views_of_one_later_page_at_once_run_one_job(world: World) -> None:
    """The rows are read again under the lock: the second call finds the page the first made, and makes none."""
    source = world.pdf("quay-list.pdf", quay_pages(21))
    manifest = await world.prepare(source)
    slow = world.over(Slow(world.settings.data_dir, world.built.parent / "slow-jobs"))
    first, second = await asyncio.gather(slow.renders(source, manifest, [21]), slow.renders(source, manifest, [21]))

    assert first == second
    assert [r.ordinal for r in first] == [21]
    assert len(slow.executor.requests) == 1
    assert [event["outcome"] for event in world.events()] == ["ok", "ok"]


async def test_a_page_that_lost_a_strip_is_left_out_whole(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """A page whose second strip the store could not keep is not answered as half a page, now or when asked again.

    With no page of the call left whole, the loss itself is raised; with one left, that one is answered.
    """
    source = world.pdf("quay-list.pdf", quay_pages(2))
    manifest = await world.prepare(source)
    real = world.store.register_derived

    def refuse_second(source: FileRecord, staged: Path, spec: DerivedSpec, **more: Any) -> DerivedRecord:
        if spec.meta.get("strip") == 2:
            raise RendersFull("the renders of this file are at their limit")
        return real(source, staged, spec, **more)

    monkeypatch.setattr(world.store, "register_derived", refuse_second)

    with pytest.raises(RendersFull):
        await world.renders(source, manifest, [1, 2], 2)
    assert [(row.ordinal, row.strip) for row in world.store.derived_of(source) if row.strips == 2] == [(1, 1), (2, 1)]
    with pytest.raises(RendersFull):
        await world.renders(source, manifest, [2], 2)
    assert len(world.executor.requests) == 3  # asked again, the page is made again


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        (QuotaExceeded("workspace quota exceeded"), QuotaExceeded),
        (RendersFull("the renders of this file are at their limit"), RendersFull),
        (StorageFull("storage is full"), StorageFull),
        (Conflict("render exists"), HTTPException),
        (NotFound("file not found"), HTTPException),
    ],
    ids=["quota", "full", "storage-full", "duplicate", "failed"],
)
async def test_pages_none_of_which_came_back_raise_their_loss(
    world: World, monkeypatch: pytest.MonkeyPatch, error: StoreError, raised: type[Exception]
) -> None:
    """Ruling R6: when no page asked for exists once the jobs are through, the first loss they reported is raised.

    It is the store's own error as :func:`prepared` raises it for the head: the user's quota, the source's cap, a full
    volume (each 507), and a 500 of the route's own text for any other loss. The pages that exist stay served.
    """
    source = world.pdf("quay-list.pdf", quay_pages(22))
    manifest = await world.prepare(source)

    def refuse(*_: Any, **__: Any) -> DerivedRecord:
        raise error

    monkeypatch.setattr(world.store, "register_derived", refuse)
    with pytest.raises(raised) as lost:
        await world.renders(source, manifest, [21])

    if isinstance(lost.value, HTTPException):
        assert (lost.value.status_code, lost.value.detail) == (500, "the renders of this file could not be kept")
    else:
        assert str(lost.value) == str(error)
    assert [r.ordinal for r in await world.renders(source, manifest, [1])] == [1]
    assert [r.ordinal for r in await world.renders(source, manifest, [1, 21])] == [1]


async def test_a_page_the_job_did_not_make_is_left_out_without_a_loss(world: World) -> None:
    """A job that answers ``ok`` without the page, and reports no loss, leaves it out: nothing to raise."""
    source = world.pdf("quay-list.pdf", quay_pages(22))
    manifest = await world.prepare(source)
    world.executor.payload = {
        "kind": "pdf",
        "total": 22,
        "pages_without_text": [],
        "frames": None,
        "hint": None,
        "renders": [],
    }

    assert await world.renders(source, manifest, [21]) == []
    assert len(world.executor.requests) == 2


async def test_a_busy_queue_propagates(world: World) -> None:
    """A page that cannot be made now is the queue's refusal, for the caller to answer."""
    source = world.pdf("quay-list.pdf", quay_pages(21))
    manifest = await world.prepare(source)
    world.executor.status = "busy"
    with pytest.raises(Busy):
        await world.renders(source, manifest, [21])


# --- Office files ------------------------------------------------------------------------------------------------


def turns_of(world: World, monkeypatch: pytest.MonkeyPatch) -> tuple[list[Turn], list[tuple[str, Turn | None]]]:
    """Watch the turns the queue hands out, and the turn each conversion and render job is sent with, in order."""
    taken: list[Turn] = []
    sent: list[tuple[str, Turn | None]] = []
    turn, renders, conversion = world.queue.turn, world.queue.submit_renders, world.queue.submit_conversion

    @contextlib.asynccontextmanager
    async def counted(*args: Any, **more: Any) -> AsyncIterator[Turn]:
        async with turn(*args, **more) as held:
            taken.append(held)
            yield held

    async def render(*args: Any, **more: Any) -> Any:
        sent.append(("prepare_file", more.get("turn")))
        return await renders(*args, **more)

    async def convert(*args: Any, **more: Any) -> Any:
        sent.append(("convert_office", more.get("turn")))
        return await conversion(*args, **more)

    monkeypatch.setattr(world.queue, "turn", counted)
    monkeypatch.setattr(world.queue, "submit_renders", render)
    monkeypatch.setattr(world.queue, "submit_conversion", convert)
    return taken, sent


async def test_a_deck_is_converted_once_and_drawn(
    world: World, fake_office: FakeOffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deck is converted to one PDF, kept beside it, and its slides drawn from that PDF in the same turn.

    Each job is recorded before what it made is visible: the conversion by the deck and its PDF, with ``converted``;
    the drawing by the deck and each slide, as a PDF's prepare is.
    """
    taken, sent = turns_of(world, monkeypatch)
    source = world.deck(3, picture_only=[2])
    manifest = await world.prepare(source)
    rows = world.store.derived_of(source)
    pdf = world.converted(source)
    pages = [row for row in rows if row.role == "page"]

    assert (manifest.kind, manifest.unit, manifest.total, manifest.pages_without_text) == ("office", "slide", 3, [2])
    assert (manifest.frames, manifest.hint) == (None, None)
    assert [r.n for r in manifest.renditions] == [row.ordinal for row in pages] == [1, 2, 3]
    assert sorted(row.role for row in rows) == ["page", "page", "page", "pdf"]
    assert (pdf.mime, pdf.width, pdf.height, pdf.meta["slides"]) == ("application/pdf", None, None, 3)
    convert, render = world.executor.requests
    assert (convert.tool, convert.input_files) == ("convert_office", [source.name])
    assert convert.args == {"budget_s": max(1, world.settings.job_wall_s * 3 // 4), "path": source.name}
    assert (render.tool, render.input_files, render.args["path"]) == (
        "prepare_file",
        [pdf.relative_path],
        pdf.relative_path,
    )
    assert render.args["office"] == {"unit": "slide", "slides": 3}
    (turn,) = taken
    assert [(tool, held is turn) for tool, held in sent] == [("convert_office", True), ("prepare_file", True)]
    assert turn.gate == "conversion"  # an Office file's prepare waits for no picture's, and no picture's for it
    assert fake_office.calls == 1
    converted, drawn = world.events()
    assert (converted["action"], converted["outcome"]) == ("modify", "ok")
    assert converted["target"]["ids"] == [source.id, pdf.id]
    assert converted["criteria"] == {
        "caller": "ghostwriter",
        "renders": 0,
        "strips": 1,
        "on_demand": False,
        "converted": True,
        "outputs": 1,
    }
    assert (drawn["action"], drawn["outcome"]) == ("modify", "ok")
    assert drawn["target"]["ids"] == [source.id, *(row.id for row in pages)]
    assert drawn["criteria"] == {"caller": "ghostwriter", "renders": 3, "strips": 1, "on_demand": False, "outputs": 3}
    assert world.store.files_in_chat(PG) == [source]  # neither the PDF nor a slide is a file of the chat


async def test_a_word_file_is_drawn_as_pages(world: World, fake_office: FakeOffice) -> None:
    """A Word file is converted and drawn as a deck is, and counted by page."""
    source = put(world.store, "harbour-notes.docx", mk.make_docx(world.built / "harbour-notes.docx").read_bytes(), p=PG)
    manifest = await world.prepare(source)

    assert (manifest.kind, manifest.unit, manifest.total, manifest.pages_without_text) == ("office", "page", 3, [])
    assert [r.n for r in manifest.renditions] == [1, 2, 3]
    _, render = world.executor.requests
    assert render.args["office"] == {"unit": "page", "slides": None}
    assert fake_office.decks == [False]


async def test_a_prepared_deck_answers_from_the_rows(world: World, fake_office: FakeOffice) -> None:
    """A deck prepared once is answered from its rows: no request, no event, no conversion."""
    source = world.deck(3)
    first = await world.prepare(source)
    requests, events = len(world.executor.requests), len(world.lines())

    assert await world.prepare(source) == first
    assert (len(world.executor.requests), len(world.lines()), fake_office.calls) == (requests, events, 1)


async def test_a_deck_converted_before_is_not_converted_again(world: World, fake_office: FakeOffice) -> None:
    """A deck with its PDF and no head (its slides' renders went): the prepare only draws, from that PDF."""
    source = world.deck(3)
    staged = mk.make_pdf(world.built / "converted.pdf", ["Invented slide 1", "Invented slide 2", "Invented slide 3"])
    spec = DerivedSpec("pdf", 0, "application/pdf", None, None, {"pages": 3, "slides": 3})
    pdf = world.store.register_derived(source, staged, spec, file_id=new_file_id(), quota_bytes=10**9)
    manifest = await world.prepare(source)

    (req,) = world.executor.requests
    assert (req.tool, req.input_files, req.args["office"]) == (
        "prepare_file",
        [pdf.relative_path],
        {"unit": "slide", "slides": 3},
    )
    assert fake_office.calls == 0
    assert (manifest.kind, manifest.unit, manifest.total) == ("office", "slide", 3)
    (event,) = world.events()
    assert event["target"]["ids"][0] == source.id
    assert "converted" not in event["criteria"]


@pytest.mark.parametrize(
    ("meta", "slides"),
    [({"pages": 3, "slides": 3}, 3), ({"pages": 3, "slides": "3"}, None), ({"slides": True}, None), ({}, None)],
    ids=["counted", "text", "bool", "missing"],
)
async def test_a_slide_count_the_pdf_row_does_not_keep_is_left_out(
    world: World, fake_office: FakeOffice, meta: dict[str, object], slides: int | None
) -> None:
    """The slide count goes to the job only as the whole number a conversion kept; any other is left out, not failed."""
    source = world.deck(3)
    staged = mk.make_pdf(world.built / "converted.pdf", ["Invented slide 1", "Invented slide 2", "Invented slide 3"])
    spec = DerivedSpec("pdf", 0, "application/pdf", None, None, meta)
    world.store.register_derived(source, staged, spec, file_id=new_file_id(), quota_bytes=10**9)
    manifest = await world.prepare(source)

    (req,) = world.executor.requests
    assert req.args["office"] == {"unit": "slide", "slides": slides}
    assert (manifest.kind, manifest.total) == ("office", 3)


async def test_later_slides_reuse_the_pdf(world: World, fake_office: FakeOffice) -> None:
    """Slide 21 of 22 is one job over the PDF kept at the prepare: no conversion again."""
    source = world.deck(22, chart_on=None)
    manifest = await world.prepare(source)
    pdf = world.converted(source)
    made = await world.renders(source, manifest, [21])

    assert [r.ordinal for r in made] == [21]
    (req,) = world.executor.requests[2:]
    assert (req.tool, req.input_files) == ("prepare_file", [pdf.relative_path])
    assert req.args == {
        "pages": [21],
        "strips": 1,
        "budget_s": max(1, world.settings.job_wall_s * 3 // 4),
        "facts": False,
        "office": {"unit": "slide", "slides": 22},
        "path": pdf.relative_path,
    }
    assert fake_office.calls == 1
    assert world.events()[-1]["criteria"] == {
        "caller": "ghostwriter",
        "renders": 1,
        "strips": 1,
        "on_demand": True,
        "outputs": 1,
    }


async def test_a_lost_pdf_is_converted_again(
    world: World, fake_office: FakeOffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deck whose PDF went is converted again for a later slide, then drawn, both in one turn under its lock."""
    source = world.deck(22, chart_on=None)
    manifest = await world.prepare(source)
    lost = world.converted(source)
    world.store.remove_derived(lost)
    taken, sent = turns_of(world, monkeypatch)
    made = await world.renders(source, manifest, [21])

    again = world.converted(source)
    assert again.id != lost.id
    assert [r.ordinal for r in made] == [21]
    convert, render = world.executor.requests[2:]
    assert (convert.tool, render.tool, render.input_files) == ("convert_office", "prepare_file", [again.relative_path])
    (turn,) = taken
    assert [(tool, held is turn) for tool, held in sent] == [("convert_office", True), ("prepare_file", True)]
    assert turn.gate == "conversion"  # a conversion on demand waits in the same gate as a prepare's
    assert fake_office.calls == 2
    converted, drawn = world.events()[-2:]
    assert converted["target"]["ids"] == [source.id, again.id]
    assert converted["criteria"] == {
        "caller": "ghostwriter",
        "renders": 0,
        "strips": 1,
        "on_demand": True,
        "converted": True,
        "outputs": 1,
    }
    assert drawn["criteria"]["on_demand"] is True and "converted" not in drawn["criteria"]


async def test_two_prepares_of_a_deck_convert_once(world: World, fake_office: FakeOffice) -> None:
    """Two prepares of one deck at once: one conversion, one render job, one set of rows, no error."""
    slow = world.over(Slow(world.settings.data_dir, world.built.parent / "slow-jobs"))
    source = slow.deck(3)
    first, second = await asyncio.gather(slow.prepare(source), slow.prepare(source))

    assert first == second
    assert [req.tool for req in slow.executor.requests] == ["convert_office", "prepare_file"]
    assert fake_office.calls == 1
    assert sorted(row.role for row in slow.store.derived_of(source)) == ["page", "page", "page", "pdf"]
    assert [event["outcome"] for event in slow.events()] == ["ok", "ok"]


async def test_the_source_lock_is_taken_before_the_turn(
    world: World, fake_office: FakeOffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every path takes the source's lock first and a turn of its gate second: the reverse order can deadlock.

    When the prepare of a deck and the on-demand render of its 21st slide each take their turn, the source's lock is
    already held (one source tracked), and both are let go afterwards.
    """
    held_at_turn: list[int] = []
    real_turn = world.queue.turn

    def turn(p: Principal, **limits: Any) -> contextlib.AbstractAsyncContextManager[Turn]:
        held_at_turn.append(world.queue.tracked_sources)
        return real_turn(p, **limits)

    monkeypatch.setattr(world.queue, "turn", turn)
    source = world.deck(22)

    manifest = await world.prepare(source)
    drawn = await world.renders(source, manifest, [21])

    assert [rec.ordinal for rec in drawn] == [21]
    assert held_at_turn == [1, 1]
    assert world.queue.tracked_sources == 0


async def test_a_conversion_runs_only_in_a_turn_of_the_conversion_gate(world: World, fake_office: FakeOffice) -> None:
    """Review M7: a conversion sent in a turn of the user's or the prepare gate is a programming error.

    It is refused before any job or event, so no future caller converts beside another conversion of its caller.
    """
    source = world.deck(3)
    for gate in ("user", "prepare"):
        jobs = world.jobs(gate)
        async with jobs.turn(PG) as turn:
            with pytest.raises(ValueError, match="conversion gate"):
                await api_renders._convert(
                    PG,
                    source,
                    settings=world.settings,
                    store=world.store,
                    audit=world.audit,
                    jobs=jobs,
                    turn=turn,
                    on_demand=False,
                )
    assert (world.executor.requests, world.events(), fake_office.calls) == ([], [], 0)


async def test_a_turn_is_let_go_after_a_refused_conversion(world: World, fake_office: FakeOffice) -> None:
    """A prepare whose conversion is refused lets its turn and its lock go: the next prepare of a deck runs at once.

    One deck is refused for its pictures (every page came to the ceiling), a slide master for having no slides; a
    turn that stayed held would keep the caller's prepare gate, and the third prepare would wait for it.
    """
    fake_office.ceiling = 0
    crowded = world.deck(2, name="harbour-crowded.pptx")
    master = mt.as_potx(mt.make_master(world.built / "harbour-master.pptx", examples=0), world.built / "master.potx")
    empty = put(world.store, "harbour-master.potx", master.read_bytes(), p=PG)

    refusals: list[str] = []
    for source in (crowded, empty):
        with pytest.raises(HTTPException) as refused:
            await world.prepare(source)
        assert refused.value.status_code == 422
        refusals.append(str(refused.value.detail))
    fake_office.ceiling = None

    assert (
        refusals[0]
        == "unreadable_file: the file holds more or larger pictures than can be drawn within the memory limit"
    )
    assert refusals[1].startswith("unsupported_format: the deck has no slides")
    assert world.queue.tracked_sources == 0
    manifest = await asyncio.wait_for(world.prepare(world.deck(3)), scaled(5))
    assert (manifest.kind, manifest.total) == ("office", 3)


async def test_an_encrypted_deck_is_422_with_nothing_kept(world: World, fake_office: FakeOffice) -> None:
    """Review Focus 1: a password-protected deck (an OLE container) is refused before it is converted; nothing is kept.

    The one job, the conversion, is recorded as an ``error`` by its code; no slide is drawn.
    """
    source = put(world.store, "harbour-deck.pptx", mk.make_ole(world.built / "harbour-deck.pptx").read_bytes(), p=PG)
    with pytest.raises(HTTPException) as refused:
        await world.prepare(source)

    assert refused.value.status_code == 422
    assert str(refused.value.detail).startswith("unreadable_file:")
    assert "password-protected" in str(refused.value.detail)
    assert world.store.derived_of(source) == []
    assert [req.tool for req in world.executor.requests] == ["convert_office"]
    assert fake_office.calls == 0
    (event,) = world.events()
    assert (event["action"], event["outcome"], event["target"]["ids"]) == ("modify", "error", [source.id])
    assert event["criteria"] == {
        "caller": "ghostwriter",
        "renders": 0,
        "strips": 1,
        "on_demand": False,
        "converted": True,
        "outputs": 0,
        "refused": "unreadable_file",
    }
    # The turn taken inside the lock is let go with it: the prepare gate, one job at a time, serves the next at once.
    assert world.queue.tracked_sources == 0
    assert (await asyncio.wait_for(world.prepare(world.photo()), scaled(5))).kind == "image"


async def test_a_pdf_past_the_quota_is_507(
    world: World, fake_office: FakeOffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PDF counts in the user's quota: one the store's quota refuses is 507, and no slide is drawn or kept."""
    source = world.deck(3)
    real = functools.partial(Store.register_derived, world.store)

    def register(source: FileRecord, staged: Path, spec: DerivedSpec, **more: Any) -> DerivedRecord:
        return real(source, staged, spec, **({**more, "quota_bytes": 1} if spec.role == "pdf" else more))

    monkeypatch.setattr(world.store, "register_derived", register)
    with pytest.raises(QuotaExceeded) as refused:
        await world.prepare(source)

    assert describe(refused.value) == (507, "workspace quota exceeded")
    assert world.store.derived_of(source) == []
    assert [req.tool for req in world.executor.requests] == ["convert_office"]
    assert [event["outcome"] for event in world.events()] == ["ok", "error"]  # the PDF, named again


# --- the conversion gate and the source's lock -------------------------------------------------------------------

#: Ghostwriter's user B, as its calls name them, and as the store's own calls in a test do.
PB = Principal("ghostwriter", "user-b", "chat-1", confined=True)
PB_SETUP = Principal("ghostwriter", "user-b", "chat-1")


class HeldConversion(Local):
    """The in-process executor, whose conversions wait for the test to let them run; every other job runs at once.

    Attributes:
        converting: Set once a conversion has come and waits.
        release: Set by the test to let the conversions run.
        running: Conversions in hand now.
        peak: The most conversions in hand at once.
    """

    def __init__(self, data_dir: Path, jobs: Path) -> None:
        """Run the real runners; hold each conversion until :attr:`release` is set."""
        super().__init__(data_dir, jobs)
        self.converting = asyncio.Event()
        self.release = asyncio.Event()
        self.running = 0
        self.peak = 0

    @override
    async def run(self, req: JobRequest, timeout_s: float) -> JobResult:
        """Hold a conversion until released, then run the runner; run any other job at once."""
        if req.tool != "convert_office":
            return await super().run(req, timeout_s)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            self.converting.set()
            await self.release.wait()
            return await super().run(req, timeout_s)
        finally:
            self.running -= 1


def held_world(world: World, **settings: Any) -> tuple[World, HeldConversion]:
    """The world over an executor that holds conversions, with a queue of its own over ``settings`` changed."""
    executor = HeldConversion(world.settings.data_dir, world.built.parent / "held-conversions")
    changed = dataclasses.replace(world.settings, **settings)
    queue = JobQueue(changed, executor, world.store, Metrics(changed.data_dir))
    return dataclasses.replace(world, executor=executor, queue=queue), executor


def queue_length(world: World) -> float:
    """How many calls wait in the world's queue, its gates' lines included."""
    return world.queue.metrics.registry.get_sample_value("workbench_queue_length", {}) or 0.0


async def until(condition: Callable[[], bool], seconds: float = 2.0) -> None:
    """Let the loop run until ``condition()`` holds."""
    async with asyncio.timeout(scaled(seconds)):
        while not condition():
            await asyncio.sleep(0.005)


def a_call(world: World, wait_s: int = MCP_QUEUE_WAIT_S) -> CallJobs:
    """The queue as one MCP call of Ghostwriter's sees it: its wall, its wait for its turn and its slots."""
    call = CallLimits(world.settings.ghostwriter_wall_s, wait_s, world.settings.ghostwriter_slots)
    return CallJobs(world.queue, call)


async def view(world: World, source: FileRecord, jobs: CallJobs, p: Principal = PG) -> ViewedFile | WithBlocks:
    """Call ``view_file`` as the MCP door does, through ``jobs``."""
    body = ViewFileRequest(path=source.virtual_path)
    return await views.view_file(body, p, world.settings, world.store, world.audit, jobs)


def shown_labels(answer: ViewedFile | WithBlocks) -> list[str]:
    """The labels of the images a view answered with, one block for each."""
    assert isinstance(answer, WithBlocks)
    viewed = answer.answer
    assert isinstance(viewed, ViewedFile)
    assert len(answer.blocks) == len(viewed.shown)
    return [shown.label for shown in viewed.shown]


async def test_a_pictures_prepare_runs_while_a_deck_converts(world: World, fake_office: FakeOffice) -> None:
    """Final review: an Office file's prepare takes the conversion gate, so user B's photo is prepared meanwhile."""
    held, executor = held_world(world)
    source = held.deck(3)
    photo = put(held.store, "quay-photo.jpg", mi.make_photo(held.built / "quay-photo.jpg").read_bytes(), p=PB_SETUP)
    converting = asyncio.create_task(held.prepare(source))
    await executor.converting.wait()

    manifest = await asyncio.wait_for(
        prepared(PB, photo, settings=held.settings, store=held.store, audit=held.audit, jobs=held.jobs("prepare")),
        scaled(2),
    )
    assert (manifest.kind, [r.n for r in manifest.renditions]) == ("image", [1])
    assert not converting.done()
    executor.release.set()
    assert (await converting).total == 3
    assert fake_office.calls == 1


async def test_an_on_demand_conversion_waits_for_a_prepares_in_the_conversion_gate(
    world: World, fake_office: FakeOffice
) -> None:
    """A deck whose PDF went is converted again for a later slide: that waits for user B's conversion, never beside it.

    It waits in the conversion gate's own line, and runs once the prepare's conversion and drawing are through.
    """
    held, executor = held_world(world)
    mine = held.deck(22, chart_on=None)
    executor.release.set()
    manifest = await held.prepare(mine)
    held.store.remove_derived(held.converted(mine))
    executor.release.clear()
    executor.converting.clear()
    theirs = put(held.store, "quay-deck.pptx", mk.make_deck(held.built / "quay-deck.pptx", 3).read_bytes(), p=PB_SETUP)
    preparing = asyncio.create_task(
        prepared(PB, theirs, settings=held.settings, store=held.store, audit=held.audit, jobs=held.jobs("prepare"))
    )
    await executor.converting.wait()
    later = asyncio.create_task(held.renders(mine, manifest, [21]))
    await until(lambda: queue_length(held) == 1)
    assert executor.running == 1

    executor.release.set()
    await preparing
    assert [rec.ordinal for rec in await later] == [21]
    assert executor.peak == 1
    assert [(req.user_id, req.tool) for req in executor.requests][2:] == [
        ("user-b", "convert_office"),
        ("user-b", "prepare_file"),
        ("user-a", "convert_office"),
        ("user-a", "prepare_file"),
    ]


async def test_a_call_waits_for_the_sources_lock_while_its_prepare_converts(
    world: World, fake_office: FakeOffice
) -> None:
    """Final review: a view of a deck whose prepare converts it waits for that prepare, past its own turn's wait.

    Then it answers the slides from the rows, and no second conversion runs. While it waits it holds no slot, no turn
    of its user and no place in a line: with two slots, one the conversion's, the same user's photo is viewed at once.
    """
    held, executor = held_world(world, executor_slots=2)
    source = held.deck(3)
    photo = held.photo()
    preparing = asyncio.create_task(held.prepare(source))
    await executor.converting.wait()
    viewing = asyncio.create_task(view(held, source, a_call(held, wait_s=0)))
    await asyncio.sleep(0.1)

    assert not viewing.done()
    assert (held.queue.tracked_users, queue_length(held)) == (0, 0)
    pictured = await asyncio.wait_for(view(held, photo, a_call(held, wait_s=0)), scaled(2))
    assert shown_labels(pictured) == ["image"]
    executor.release.set()
    await preparing
    assert shown_labels(await viewing) == ["slide 1", "slide 2", "slide 3"]
    assert fake_office.calls == 1
    assert [req.tool for req in held.executor.requests].count("convert_office") == 1


async def test_a_lock_held_past_its_bound_is_wait_timeout(
    world: World, fake_office: FakeOffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A call waits for a source's lock ``SOURCE_LOCK_WAIT_S`` at the most (lowered here); then it is refused busy."""
    monkeypatch.setattr(tools_mcp, "SOURCE_LOCK_WAIT_S", 0.2)
    held, executor = held_world(world)
    source = held.deck(3)
    preparing = asyncio.create_task(held.prepare(source))
    await executor.converting.wait()
    async with asyncio.timeout(scaled(2)):
        with pytest.raises(Busy) as refused:
            await view(held, source, a_call(held))
    assert refused.value.reason == "wait_timeout"
    executor.release.set()
    await preparing


def their_deck(world: World, name: str = "quay-deck.pptx") -> FileRecord:
    """A deck of three invented slides of user B's chat."""
    return put(world.store, name, mk.make_deck(world.built / name, 3).read_bytes(), p=PB_SETUP)


async def test_a_view_that_converts_waits_out_another_conversion(world: World, fake_office: FakeOffice) -> None:
    """Ruling R19: a view of a deck no one prepared converts it on demand, and waits out user B's conversion to do so.

    It waits for the conversion gate past its own turn's wait (none here), up to ``SOURCE_LOCK_WAIT_S``, in the gate's
    own line and not the queue's; then it converts and answers the slides, never ``wait_timeout``.
    """
    held, executor = held_world(world)
    theirs, mine = their_deck(held), held.deck(3)
    preparing = asyncio.create_task(
        prepared(PB, theirs, settings=held.settings, store=held.store, audit=held.audit, jobs=held.jobs("prepare"))
    )
    await executor.converting.wait()
    viewing = asyncio.create_task(view(held, mine, a_call(held, wait_s=0)))
    await until(lambda: queue_length(held) == 1)
    await asyncio.sleep(0.1)

    assert not viewing.done()
    line = held.queue._shared_gates["conversion", "ghostwriter"]  # the conversion gate's own count of who waits
    assert (line.waiting, held.queue._waiting, held.queue.tracked_users) == (1, 0, 0)
    executor.release.set()
    await preparing
    assert shown_labels(await viewing) == ["slide 1", "slide 2", "slide 3"]
    assert executor.peak == 1
    assert [(req.user_id, req.tool) for req in held.executor.requests] == [
        ("user-b", "convert_office"),
        ("user-b", "prepare_file"),
        ("user-a", "convert_office"),
        ("user-a", "prepare_file"),
    ]
    assert queue_length(held) == 0


async def test_a_view_that_converts_waits_for_the_gate_no_longer_than_its_bound(
    world: World, fake_office: FakeOffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Past ``SOURCE_LOCK_WAIT_S`` (lowered here) for the conversion gate, a view that would convert is refused busy."""
    monkeypatch.setattr(tools_mcp, "SOURCE_LOCK_WAIT_S", 0.2)
    held, executor = held_world(world)
    theirs, mine = their_deck(held), held.deck(3)
    preparing = asyncio.create_task(
        prepared(PB, theirs, settings=held.settings, store=held.store, audit=held.audit, jobs=held.jobs("prepare"))
    )
    await executor.converting.wait()
    async with asyncio.timeout(scaled(2)):
        with pytest.raises(Busy) as refused:
            await view(held, mine, a_call(held, wait_s=0))
    assert refused.value.reason == "wait_timeout"
    assert queue_length(held) == 0
    executor.release.set()
    await preparing
    assert conversions(held) == 1


# --- refusals remembered -----------------------------------------------------------------------------------------

#: The refusal of a deck whose pictures cannot all be drawn within the memory limit, as the runner words it.
TOO_MANY_PICTURES = "unreadable_file: the file holds more or larger pictures than can be drawn within the memory limit"


def conversions(world: World) -> int:
    """How many conversion jobs reached the executor."""
    return [req.tool for req in world.executor.requests].count("convert_office")


async def refused_with(call: Awaitable[object]) -> tuple[int, str]:
    """The status and the text a call is refused with."""
    with pytest.raises(HTTPException) as refused:
        await call
    return refused.value.status_code, str(refused.value.detail)


async def test_a_refused_deck_is_answered_from_memory(world: World, fake_office: FakeOffice) -> None:
    """Final review: a deck refused once is refused again at once, by a prepare or a view, with no conversion.

    Each answer from memory is recorded as the refusal was, the file alone named, and says it was remembered.
    """
    fake_office.ceiling = 0
    source = world.deck(2)
    first = await refused_with(world.prepare(source))
    calls = fake_office.calls
    again = await refused_with(world.prepare(source))
    viewed = await refused_with(view(world, source, a_call(world)))

    assert first == again == viewed == (422, TOO_MANY_PICTURES)
    assert (conversions(world), fake_office.calls) == (1, calls)
    assert world.store.derived_of(source) == []
    converting, *remembered = world.events()
    assert (converting["outcome"], converting["criteria"]["refused"], converting["criteria"]["converted"]) == (
        "error",
        "unreadable_file",
        True,
    )
    assert [(event["outcome"], event["target"]["ids"]) for event in remembered] == [("error", [source.id])] * 2
    assert [event["criteria"] for event in remembered] == [
        {
            "caller": "ghostwriter",
            "renders": 0,
            "strips": 1,
            "on_demand": False,
            "outputs": 0,
            "refused": "unreadable_file",
            "remembered": True,
        }
    ] * 2


async def test_a_refused_picture_is_answered_from_memory(world: World) -> None:
    """A text file named ``.png`` is refused once by its prepare's job; later prepares are refused with no job."""
    source = put(world.store, "quay-photo.png", b"Quay list, not a picture", p=PG)
    first = await refused_with(world.prepare(source))
    again = await refused_with(world.prepare(source))

    assert first == again
    assert first[0] == 422 and first[1].startswith("unreadable_file:")
    assert len(world.executor.requests) == 1


@pytest.mark.parametrize(
    ("status", "payload", "raised"),
    [
        ("error", {"error": "read_timeout: drawing the file took longer than 86 s"}, HTTPException),
        ("timeout", {}, HTTPException),
        ("error", {"error": "hidden_memory: the container held shared memory no job's count showed"}, HTTPException),
        ("error", {"error": "an error no code names"}, HTTPException),
        ("busy", {}, Busy),
    ],
    ids=["read-timeout", "wall", "hidden-memory", "failed", "busy"],
)
async def test_a_refusal_that_may_pass_is_not_remembered(
    world: World, status: str, payload: dict[str, Any], raised: type[Exception]
) -> None:
    """A prepare that ran out of time, failed or found the executor busy is tried again by the next, with a job."""
    source = world.photo()
    world.executor.status, world.executor.payload = status, payload
    for _ in range(2):
        with pytest.raises(raised):
            await world.prepare(source)
    assert len(world.executor.requests) == 2
    assert world.store.refusal_of(source) is None


@pytest.mark.parametrize(
    "detail",
    [
        "unreadable_file: the converted file could not be written",
        "unreadable_file: the renders could not be written",
        "unreadable_file: the file could not be converted to pages",
        "unreadable_file: an invented reason no runner gives",
    ],
    ids=["conversion-not-written", "renders-not-written", "not-converted", "unknown"],
)
async def test_a_refusal_that_may_pass_is_remembered_when_it_comes_twice_in_a_row(world: World, detail: str) -> None:
    """Ruling R22: an ``unreadable_file`` the file does not decide alone is not remembered once, and is twice in a row.

    Such are a job that could not write inside its sandbox, a conversion that ended for no reason the file gives, and
    any text the api does not know: the second prepare runs a job again, the third is answered from memory.
    """
    source = world.photo()
    world.executor.status, world.executor.payload = "error", {"error": detail}
    assert await refused_with(world.prepare(source)) == (422, detail)
    assert (len(world.executor.requests), world.store.refusal_of(source)) == (1, None)
    assert await refused_with(world.prepare(source)) == (422, detail)
    assert (len(world.executor.requests), world.store.refusal_of(source)) == (2, detail)
    assert await refused_with(world.prepare(source)) == (422, detail)
    assert len(world.executor.requests) == 2
    assert world.events()[-1]["criteria"]["remembered"] is True


async def test_a_success_between_two_refusals_that_may_pass_starts_the_count_again(world: World) -> None:
    """Ruling R22: a prepare that went well between two such refusals resets their count: two more are needed."""
    source = world.photo()
    passing = {"error": "unreadable_file: the renders could not be written"}
    world.executor.status, world.executor.payload = "error", passing
    await refused_with(world.prepare(source))
    world.executor.status, world.executor.payload = "ok", None
    assert (await world.prepare(source)).kind == "image"
    for row in world.store.derived_of(source):  # its renders went: the next call prepares it again
        world.store.remove_derived(row)
    world.executor.status, world.executor.payload = "error", passing
    await refused_with(world.prepare(source))
    assert world.store.refusal_of(source) is None
    await refused_with(world.prepare(source))
    assert world.store.refusal_of(source) == passing["error"]
    assert len(world.executor.requests) == 4


@pytest.mark.parametrize("detail", sorted(api_renders.FILE_DECIDED))
async def test_a_refusal_the_file_decides_is_remembered_at_once(world: World, detail: str) -> None:
    """Ruling R22: each allow-listed ``unreadable_file`` text, and any ``unsupported_format``, is remembered at once."""
    source = world.photo()
    world.executor.status, world.executor.payload = "error", {"error": detail}
    assert await refused_with(world.prepare(source)) == (422, detail)
    assert await refused_with(world.prepare(source)) == (422, detail)
    assert len(world.executor.requests) == 1
    other = world.photo("quay-photo-2.jpg")
    unsupported = "unsupported_format: an invented reason no runner gives"
    world.executor.payload = {"error": unsupported}
    assert await refused_with(world.prepare(other)) == (422, unsupported)
    assert world.store.refusal_of(other) == unsupported


def test_the_refusals_remembered_at_once_are_the_runners_own_words() -> None:
    """The allow-list is the runners' own texts of refusals the file decides, each tied to the constant that says it."""
    from workbench.executor import readers
    from workbench.executor.render import encode, images, office, pages
    from workbench.executor.runners import convert

    said = {
        readers.DAMAGED,
        readers.TOO_BIG,
        readers.PDF_ENCRYPTED,
        readers._OFFICE_ENCRYPTED,
        office.TOO_MANY_PICTURES,
        office._NOT_OPENED,
        images._TOO_MANY_PIXELS,
        pages._NO_PAGES,
        encode._NO_FIT,
        convert._LOCKED,
        convert._PICTURE_TOO_LARGE,
        convert._TOO_LARGE,
    }
    assert {f"unreadable_file: {text}" for text in said} == api_renders.FILE_DECIDED
    passing = {convert._NOT_WRITTEN, convert._NOT_CONVERTED, office._NOT_CONVERTED, "the renders could not be written"}
    assert not {f"unreadable_file: {text}" for text in passing} & api_renders.FILE_DECIDED


async def test_a_new_upload_under_the_same_name_is_converted_afresh(world: World, fake_office: FakeOffice) -> None:
    """A refusal is its file's: the same name uploaded again, beside it or after it went, is a new file, converted."""
    fake_office.ceiling = 0
    refused = world.deck(2)
    await refused_with(world.prepare(refused))
    fake_office.ceiling = None
    beside = world.deck(2)
    world.store.remove(refused)
    after = world.deck(2)

    assert beside.id != refused.id != after.id and after.name == refused.name
    assert (await world.prepare(beside)).total == 2
    assert (await world.prepare(after)).total == 2
    assert conversions(world) == 3


async def test_a_lost_pdf_converted_again_and_refused_is_remembered(
    world: World, fake_office: FakeOffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deck whose PDF went and whose conversion again is refused: the next slide asked for is refused at once."""
    source = world.deck(22, chart_on=None)
    manifest = await world.prepare(source)
    world.store.remove_derived(world.converted(source))
    fake_office.ceiling = 0
    first = await refused_with(world.renders(source, manifest, [21]))
    again = await refused_with(world.renders(source, manifest, [22]))

    assert first == again == (422, TOO_MANY_PICTURES)
    assert conversions(world) == 2
    assert world.events()[-1]["criteria"] == {
        "caller": "ghostwriter",
        "renders": 0,
        "strips": 1,
        "on_demand": True,
        "outputs": 0,
        "refused": "unreadable_file",
        "remembered": True,
    }


async def test_a_remembered_refusal_is_recorded_before_it_goes_out(
    world: World, audit_stream: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed: an answer from memory whose event cannot be written is the audit's failure, never the refusal.

    A call that must state a purpose and states none is denied first, as for a job.
    """
    source = put(world.store, "quay-photo.png", b"Quay list, not a picture", p=PG)
    await refused_with(world.prepare(source))
    bound = world.audit.bound(None, "req-2", required=frozenset({"modify"}))
    with pytest.raises(PurposeRequired):
        await world.prepare(source, audit=bound)
    assert world.events()[-1]["outcome"] == "denied"

    def refuse(_text: str, /) -> int:
        raise OSError("synthetic audit stream failure")

    with monkeypatch.context() as broken:
        broken.setattr(audit_stream, "write", refuse)
        with pytest.raises(AuditWriteError):
            await world.prepare(source)
    assert len(world.executor.requests) == 1


# --- reading back ------------------------------------------------------------------------------------------------


async def test_a_render_whose_bytes_are_wrong_is_not_read(world: World, caplog: pytest.LogCaptureFixture) -> None:
    """A render overwritten with text, one too large or one gone is no render: ``None``, logged by type alone."""
    source = world.photo()
    await world.prepare(source)
    (row,) = world.store.derived_of(source)
    path = world.file_of(row)
    good = path.read_bytes()

    assert read_render(world.store, row) == good
    with caplog.at_level(logging.WARNING, logger="workbench.api.renders"):
        path.write_bytes(b"Quay list, not a picture")
        assert read_render(world.store, row) is None
        path.write_bytes(good[:3] + b"\0" * RENDER_MAX_BYTES)
        assert read_render(world.store, row) is None
        path.unlink()
        assert read_render(world.store, row) is None
    assert caplog.messages
    assert not any(word in message for message in caplog.messages for word in ("quay", "chat-1", row.id, str(path)))


async def test_shown_images_are_labelled_by_picture_page_and_strip(world: World) -> None:
    """Each render shown says what it is: its label, page, size and type."""
    source = world.photo()
    manifest = await world.prepare(source)
    (whole,) = await world.renders(source, manifest, [1])
    first, _ = await world.renders(source, manifest, [1], 2)

    assert shown_image("image", whole) == ShownImage(
        label="image", n=1, width=whole.width or 0, height=whole.height or 0, mime="image/jpeg"
    )
    assert shown_image("image", first).label == "image, strip 1 of 2"
    assert [label("image", 1, 1, 1), label("image", 1, 1, 2), label("page", 3, 1, 1), label("page", 3, 2, 3)] == [
        "image",
        "image, strip 1 of 2",
        "page 3",
        "page 3, strip 2 of 3",
    ]


def test_a_manifest_needs_a_head_with_its_facts() -> None:
    """Without a whole render of page 1 that carries the facts, there is no manifest yet.

    Final review M2: a fact the head carries that this version does not know (a later version's) is ignored.
    """
    owner = {"caller": "ghostwriter", "user_id": "user-a", "chat_id": "chat-1"}
    source = FileRecord(
        id="f" * 32,
        name="quay-list.pdf",
        size=9,
        mime="application/pdf",
        kind="upload",
        created_at=1.0,
        job_id=None,
        **owner,
    )

    def row(n: int, meta: dict[str, object]) -> DerivedRecord:
        name = f"{n:032x}"
        return DerivedRecord(
            id=name,
            source_id=source.id,
            role="page",
            ordinal=n,
            name=f"{name}.jpg",
            size=5,
            mime="image/jpeg",
            width=9,
            height=9,
            meta=meta,
            created_at=1.0,
            **owner,
        )

    facts: dict[str, object] = {"kind": "pdf", "total": 2, "pages_without_text": [], "frames": None, "hint": None}
    assert manifest_of(source, []) is None
    assert manifest_of(source, [row(2, {})]) is None
    assert manifest_of(source, [row(1, {}), row(2, {})]) is None
    assert manifest_of(source, [row(1, {"facts": {"kind": "pdf"}})]) is None
    manifest = manifest_of(source, [row(1, {"facts": facts}), row(2, {}), row(2, {"strip": 1, "strips": 2})])
    assert manifest is not None
    assert [(r.n, r.bytes) for r in manifest.renditions] == [(1, 5), (2, 5)]
    later = manifest_of(source, [row(1, {"facts": {**facts, "duration": 12.5}}), row(2, {})])
    assert later is not None and (later.kind, later.total) == ("pdf", 2)


def test_a_v07_head_reads_as_unit_page() -> None:
    """Ruling R7: a head kept by v0.7 has no ``unit`` and reads as ``page``; an Office head is a manifest of its own."""
    owner = {"caller": "ghostwriter", "user_id": "user-a", "chat_id": "chat-1"}

    def source(name: str) -> FileRecord:
        return FileRecord(
            id="f" * 32, name=name, size=9, mime="application/pdf", kind="upload", created_at=1.0, job_id=None, **owner
        )

    def head(facts: dict[str, object]) -> DerivedRecord:
        return DerivedRecord(
            id="1" * 32,
            source_id="f" * 32,
            role="page",
            ordinal=1,
            name=f"{'1' * 32}.jpg",
            size=5,
            mime="image/jpeg",
            width=9,
            height=9,
            meta={"facts": facts},
            created_at=1.0,
            **owner,
        )

    v07: dict[str, object] = {"kind": "pdf", "total": 1, "pages_without_text": [], "frames": None, "hint": None}
    old = manifest_of(source("quay-list.pdf"), [head(v07)])
    assert old is not None
    assert (old.kind, old.unit, old.model_dump(mode="json")["unit"]) == ("pdf", "page", "page")
    deck = manifest_of(source("harbour-deck.pptx"), [head({**v07, "kind": "office", "unit": "slide"})])
    assert deck is not None
    assert (deck.kind, deck.unit, deck.total) == ("office", "slide", 1)


def test_a_clip_s_facts_make_no_page_manifest() -> None:
    """Facts of kind media count frames, which a page manifest cannot carry: no manifest, until the media branch."""
    owner = {"caller": "ghostwriter", "user_id": "user-a", "chat_id": "chat-1"}
    source = FileRecord(
        id="f" * 32,
        name="harbour-tour.mp4",
        size=9,
        mime="video/mp4",
        kind="upload",
        created_at=1.0,
        job_id=None,
        **owner,
    )
    facts: dict[str, object] = {
        "kind": "media",
        "unit": "frame",
        "total": 1,
        "pages_without_text": [],
        "frames": None,
        "hint": None,
        "duration_s": 4.0,
    }
    head = DerivedRecord(
        id="1" * 32,
        source_id=source.id,
        role="page",
        ordinal=1,
        name=f"{'1' * 32}.jpg",
        size=5,
        mime="image/jpeg",
        width=9,
        height=9,
        meta={"facts": facts},
        created_at=1.0,
        **owner,
    )
    assert manifest_of(source, [head]) is None


# --- the shared rules --------------------------------------------------------------------------------------------


def test_page_window_grammar() -> None:
    """Pages are ``page N``, ``N``, ``pages N-M`` or ``N-M``, the end cut at the last page; anything else is 422."""
    assert page_window(None, 10, default_last=4) == (1, 4)
    assert page_window(None, 2, default_last=4) == (1, 2)
    assert page_window("page 3", 10, default_last=4) == (3, 3)
    assert page_window("3", 10, default_last=4) == (3, 3)
    assert page_window(" Page 3 ", 10, default_last=4) == (3, 3)
    assert page_window("pages 2-5", 10, default_last=4) == (2, 5)
    assert page_window("2-5", 10, default_last=4) == (2, 5)
    assert page_window("pages 8-20", 10, default_last=4) == (8, 10)
    for part, detail in [
        ("page 11", "no_such_part: this document has 10 pages"),
        ("page 0", "no_such_part: this document has 10 pages"),
        ("pages 11-12", "no_such_part: this document has 10 pages"),
        ("slide x", "no_such_part: "),
        ("pages 5-2", "no_such_part: "),
        ("page " + "9" * 5000, "no_such_part: "),
    ]:
        with pytest.raises(HTTPException) as refused:
            page_window(part, 10, default_last=4)
        assert refused.value.status_code == 422
        assert str(refused.value.detail).startswith(detail)
    with pytest.raises(HTTPException) as one:
        page_window("page 2", 1, default_last=4)
    assert one.value.detail == "no_such_part: this document has 1 page"


def test_slide_window_grammar() -> None:
    """A deck's slides are ``slide N``, ``N``, ``slides N-M`` or ``N-M``, either word alike; a page is refused there.

    A PDF's and a document's pages keep their grammar, which does not take slides.
    """
    assert page_window("slide 3", 10, default_last=4, unit="slide") == (3, 3)
    assert page_window("slides 2-5", 10, default_last=4, unit="slide") == (2, 5)
    assert page_window("Slide 2-5", 10, default_last=4, unit="slide") == (2, 5)
    assert page_window("slides 3", 10, default_last=4, unit="slide") == (3, 3)
    assert page_window("3", 10, default_last=4, unit="slide") == (3, 3)
    assert page_window("2-5", 10, default_last=4, unit="slide") == (2, 5)
    assert page_window("slides 8-20", 10, default_last=4, unit="slide") == (8, 10)
    assert page_window(None, 10, default_last=4, unit="slide") == (1, 4)
    slides = "no_such_part: give a slide as 'slide 3', or slides as 'slides 2-5'"
    for part, detail in [
        ("page 3", slides),
        ("pages 2-5", slides),
        ("slides 5-2", slides),
        ("slide 11", "no_such_part: this document has 10 slides"),
        ("slide 0", "no_such_part: this document has 10 slides"),
    ]:
        with pytest.raises(HTTPException) as refused:
            page_window(part, 10, default_last=4, unit="slide")
        assert (refused.value.status_code, refused.value.detail) == (422, detail)
    with pytest.raises(HTTPException) as one:
        page_window("slide 2", 1, default_last=4, unit="slide")
    assert one.value.detail == "no_such_part: this document has 1 slide"

    assert page_window("page 3", 10, default_last=4, unit="page") == (3, 3)
    with pytest.raises(HTTPException) as paged:
        page_window("slide 3", 10, default_last=4, unit="page")
    assert paged.value.detail == "no_such_part: give a page as 'page 3', or pages as 'pages 2-5'"


def test_labels_name_slides() -> None:
    """A slide's render says so; a picture's and a page's say what they said in v0.7."""
    assert label("slide", 3, 1, 1) == "slide 3"
    assert label("slide", 3, 2, 3) == "slide 3, strip 2 of 3"
    assert [label("image", 1, 1, 1), label("image", 1, 2, 2), label("page", 3, 1, 1), label("page", 3, 2, 3)] == [
        "image",
        "image, strip 2 of 2",
        "page 3",
        "page 3, strip 2 of 3",
    ]


def test_the_noun_of_a_manifest() -> None:
    """A picture is an image, a PDF's renders are pages, and an Office file's are its unit's."""
    base: dict[str, object] = {
        "path": "/workspace/chat-1/x",
        "total": 1,
        "pages_without_text": [],
        "renditions": [],
        "frames": None,
        "hint": None,
    }
    nouns = [
        noun_of(Manifest.model_validate({**base, **more}))
        for more in (
            {"kind": "image"},
            {"kind": "pdf"},
            {"kind": "office", "unit": "page"},
            {"kind": "office", "unit": "slide"},
        )
    ]
    assert nouns == ["image", "page", "page", "slide"]


def test_the_image_block_is_for_the_assistant() -> None:
    """An image goes to the model as an MCP image block, base64, for the assistant alone."""
    data = b"\x89PNG\r\n\x1a\nquay"
    assert image_block("image/png", data) == {
        "type": "image",
        "data": base64.b64encode(data).decode("ascii"),
        "mimeType": "image/png",
        "annotations": {"audience": ["assistant"]},
    }


def test_the_caps_of_what_is_shown() -> None:
    """Eight images to look at, four with a read, 2.5 MiB raw; an answer with blocks keeps both."""
    assert (MAX_VIEW_IMAGES, MAX_READ_IMAGES, MAX_SHOWN_BYTES) == (8, 4, 2_621_440)
    shown = ShownImage(label="page 3", n=3, width=8, height=8, mime="image/jpeg")
    answer = WithBlocks(shown, [image_block("image/jpeg", b"\xff\xd8\xff")])
    assert (answer.answer, len(answer.blocks)) == (shown, 1)
    assert json.dumps(answer.blocks)


def test_the_api_imports_no_image_library() -> None:
    """The api's image has no imaging library: neither its app nor its renders load one."""
    code = (
        "import sys, workbench.api.main, workbench.api.renders; "
        "print(sorted(m for m in ('PIL', 'pypdfium2', 'pi_heif') if m in sys.modules))"
    )
    done = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, check=True, timeout=60)
    assert done.stdout.strip() == "[]"
