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
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, override

import pytest
from fastapi import HTTPException

from tests.api.conftest import CALL, PG, AuditLines, put, unrecorded
from tests.api.test_tools_templates import Local
from tests.conftest import scaled
from tests.fixtures import make_fixtures as mk
from tests.fixtures import make_images as mi
from workbench.api import renders as api_renders
from workbench.api.audit import AuditLog, PurposeRequired
from workbench.api.errors import describe
from workbench.api.jobs import Busy, Gate, JobQueue
from workbench.api.metrics import Metrics
from workbench.api.renders import (
    MAX_READ_IMAGES,
    MAX_SHOWN_BYTES,
    MAX_VIEW_IMAGES,
    Manifest,
    RenderJobs,
    RouteJobs,
    WithBlocks,
    image_block,
    label,
    manifest_of,
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
from workbench.config import Settings
from workbench.models import DerivedRecord, DerivedSpec, FileRecord
from workbench.protocol import JobRequest, JobResult
from workbench.renders import PAGE_LONG_SIDE, RENDER_MAX_BYTES, RENDERS_PER_JOB
from workbench.utils.audit_event import AuditWriteError, verify_chain

pytestmark = pytest.mark.anyio

#: The hint of a file that is neither a picture nor a PDF.
NOT_SHOWN = "This kind of file is not shown as pictures; read it with read_document."


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


async def test_a_word_file_is_kind_none_without_a_job(world: World) -> None:
    """Neither a picture nor a PDF: kind ``none``, nothing to show, no job and no event."""
    built = mk.make_docx(world.built / "harbour-notes.docx")
    source = put(world.store, "harbour-notes.docx", built.read_bytes(), p=PG)
    events = len(world.lines())
    manifest = await world.prepare(source)

    assert manifest.model_dump(mode="json") == {
        "path": source.virtual_path,
        "kind": "none",
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
    assert [label("image", 1, 1, 1), label("image", 1, 1, 2), label("pdf", 3, 1, 1), label("pdf", 3, 2, 3)] == [
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
