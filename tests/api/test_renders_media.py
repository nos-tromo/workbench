"""A clip is prepared from the two artifacts Nextext made of it, and never enters a job itself.

The jobs run the real ``prepare_file`` runner in this process (:class:`~tests.api.test_tools_templates.Local`) over
artifacts built at test time (``tests/fixtures/make_media.py``); every name, sentence and id is invented. Here the api's
part: which job runs and with what, what the manifest says, what is recorded before a frame or the transcript is
visible, what a replaced artifact makes of the rows made from the old one, and the caps of a clip's derived files.
"""

import asyncio
import dataclasses
import io
import json
import random
import zipfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any, Literal

import pytest
from fastapi import HTTPException
from fastapi.concurrency import run_in_threadpool
from PIL import Image

from tests.api.conftest import PG, AuditLines, put
from tests.api.test_renders import Held, World, held_world
from tests.api.test_tools_templates import Local
from tests.conftest import FakeOffice, scaled
from tests.fixtures import make_media
from workbench.api import files as files_module
from workbench.api import store as store_module
from workbench.api.audit import AuditLog
from workbench.api.jobs import SOURCE_LOCK_WAIT_S, JobQueue
from workbench.api.metrics import Metrics
from workbench.api.renders import Manifest, media_manifest_of
from workbench.api.store import NotFound, QuotaExceeded, Store
from workbench.config import Settings
from workbench.models import DerivedRecord, DerivedSpec, FileRecord
from workbench.protocol import JobRequest, JobResult
from workbench.renders import (
    ARTIFACT_ROLES,
    ARTIFACTS,
    DERIVED_MAX_BYTES,
    JSONL_MAX_BYTES,
    NO_SPEECH,
    NOTHING_FOUND,
    TRANSCRIPT_MAX_BYTES,
    ZIP_MAX_BYTES,
)
from workbench.utils.audit_event import verify_chain

pytestmark = pytest.mark.anyio

MIB = 1024 * 1024
ROLE_TYPES = {"nextext_jsonl": "application/x-ndjson", "nextext_zip": "application/zip"}
ROLE_MAX = {"nextext_jsonl": JSONL_MAX_BYTES, "nextext_zip": ZIP_MAX_BYTES}
NOTHING_USABLE = "unreadable_file: nothing in the clip's artifacts could be read"

#: Two sentences of an invented tour, the last ending at 91 s.
SPOKEN = [
    make_media.jsonl_record(0, 5, "The quay opens at nine.", speaker="Speaker 1", index=0),
    make_media.jsonl_record(60, 91, "The tour ends at the lighthouse.", speaker="Speaker 2", index=1),
]


@pytest.fixture
def anyio_backend() -> str:
    """Run the async tests on asyncio, the loop the api runs on."""
    return "asyncio"


@pytest.fixture
def world(
    settings: Settings, store: Store, audit: AuditLog, audit_lines: AuditLines, tmp_path: Path
) -> Iterator[World]:
    """The world of ``test_renders.py``, its audit chain open; the chain is closed and verified afterwards."""
    built = tmp_path / "built"
    built.mkdir()
    executor = Local(settings.data_dir, tmp_path / "jobs")
    audit.start({"retention_days": 7})
    queue = JobQueue(settings, executor, store, Metrics(settings.data_dir))
    yield World(settings, store, audit, executor, queue, audit_lines, built)
    audit.close()
    report = verify_chain(audit_lines())
    assert report.ok, report.problems


def clip(world: World, name: str = "harbour-tour.mp4") -> FileRecord:
    """An invented clip of Ghostwriter's chat: bytes that no job ever reads."""
    return put(world.store, name, b"synthetic clip bytes", p=PG)


def keep(world: World, source: FileRecord, data: bytes, role: Literal["nextext_jsonl", "nextext_zip"]) -> DerivedRecord:
    """Keep an artifact as ``POST /files/derived`` does, with the route's bounds and roomy quota."""
    spec = DerivedSpec(role, 0, ROLE_TYPES[role], None, None, {})
    staged = world.store.stage_derived(
        source,
        io.BytesIO(data),
        spec,
        max_bytes=ROLE_MAX[role],
        quota_bytes=1000 * MIB,
        check=lambda _: None,
    )
    made, _ = world.store.place_derived(staged, quota_bytes=1000 * MIB, record=lambda *_: None)
    return made


def add_jsonl(world: World, source: FileRecord, records: Sequence[dict[str, Any]] = SPOKEN) -> DerivedRecord:
    """Keep a JSON Lines of ``records`` as the clip's transcript artifact."""
    path = make_media.make_jsonl(world.built / "speech.jsonl", records)
    return keep(world, source, path.read_bytes(), "nextext_jsonl")


def add_zip(world: World, source: FileRecord, frames: int = 3, **more: Any) -> DerivedRecord:
    """Keep a keyframes zip of ``frames`` frames as the clip's frames artifact."""
    path = make_media.make_keyframes_zip(world.built / "keyframes.zip", frames, **more)
    return keep(world, source, path.read_bytes(), "nextext_zip")


def rows_of(world: World, source: FileRecord, role: str) -> list[DerivedRecord]:
    """The source's derived files of one role, in their order."""
    return [rec for rec in world.store.derived_of(source) if rec.role == role]


def facts_of(rec: DerivedRecord) -> dict[str, Any]:
    """The facts a row keeps."""
    facts = rec.meta["facts"]
    assert isinstance(facts, dict)
    return facts


async def refused_with(world: World, source: FileRecord) -> tuple[int, str]:
    """Prepare ``source`` and give the status and the detail of the refusal it ends in."""
    with pytest.raises(HTTPException) as refused:
        await world.prepare(source)
    return refused.value.status_code, str(refused.value.detail)


# --- the first prepare --------------------------------------------------------------------------------------------


async def test_a_clip_with_its_artifacts_is_prepared_once(world: World) -> None:
    """The manifest counts the frames; one event names the clip, the transcript and the frames; then no job runs."""
    source = clip(world)
    add_jsonl(world, source)
    add_zip(world, source)
    manifest = await world.prepare(source)
    frames = rows_of(world, source, "frame")
    (transcript,) = rows_of(world, source, "transcript")

    assert manifest.model_dump(mode="json", exclude={"renditions"}) == {
        "path": source.virtual_path,
        "kind": "media",
        "unit": "frame",
        "total": 3,
        "pages_without_text": [],
        "frames": None,
        "hint": None,
        "duration_s": 91.0,
        "transcript": True,
    }
    assert [r.n for r in manifest.renditions] == [row.ordinal for row in frames] == [1, 2, 3]
    assert [(r.width, r.height, r.mime, r.bytes) for r in manifest.renditions] == [
        (64, 36, "image/jpeg", world.file_of(row).stat().st_size) for row in frames
    ]
    (event,) = world.events()
    assert (event["action"], event["outcome"]) == ("modify", "ok")
    assert event["target"]["ids"][0] == source.id
    assert sorted(event["target"]["ids"][1:]) == sorted([transcript.id, *(row.id for row in frames)])
    assert event["criteria"] == {"caller": "ghostwriter", "renders": 4, "strips": 1, "on_demand": False, "outputs": 4}
    assert world.store.files_in_chat(PG) == [source]  # neither a frame nor the transcript is a file of the chat

    requests, events = len(world.executor.requests), len(world.lines())
    assert await world.prepare(source) == manifest
    assert (len(world.executor.requests), len(world.lines())) == (requests, events) == (1, events)


async def test_the_clip_never_enters_the_job(world: World) -> None:
    """The job's input files are the two artifacts' derived paths, and the clip's name is not among them."""
    source = clip(world)
    jsonl, zipped = add_jsonl(world, source), add_zip(world, source)
    await world.prepare(source)

    (req,) = world.executor.requests
    assert (req.tool, req.input_files) == ("prepare_file", [jsonl.relative_path, zipped.relative_path])
    assert source.name not in req.input_files
    assert req.args["path"] == source.name
    assert req.args["media"] == {
        "nextext_jsonl": jsonl.relative_path,
        "nextext_zip": zipped.relative_path,
        "video": True,
    }
    assert (req.args["facts"], req.args["budget_s"]) == (True, max(1, world.settings.job_wall_s * 3 // 4))
    assert sorted(set(req.args) - {"media"}) == ["budget_s", "facts", "path"]


async def test_audio_with_a_transcript_has_no_frames(world: World) -> None:
    """A recording has no zip: no frame, a transcript, and the length of its speech."""
    source = clip(world, "voice-memo.m4a")
    add_jsonl(world, source)
    manifest = await world.prepare(source)
    (transcript,) = rows_of(world, source, "transcript")

    assert manifest.model_dump(mode="json") == {
        "path": source.virtual_path,
        "kind": "media",
        "unit": "frame",
        "total": 0,
        "pages_without_text": [],
        "renditions": [],
        "frames": None,
        "hint": None,
        "duration_s": 91.0,
        "transcript": True,
    }
    assert rows_of(world, source, "frame") == []
    (req,) = world.executor.requests
    assert req.args["media"]["video"] is False
    (event,) = world.events()
    assert event["target"]["ids"] == [source.id, transcript.id]


async def test_a_silent_video_has_frames_and_no_transcript(world: World) -> None:
    """A video with no speech has no JSON Lines: the hint says so, and the facts ride on frame 1."""
    source = clip(world)
    add_zip(world, source)
    manifest = await world.prepare(source)
    frames = rows_of(world, source, "frame")

    assert (manifest.kind, manifest.total, manifest.transcript, manifest.hint) == ("media", 3, False, NO_SPEECH)
    assert manifest.duration_s == 8.0  # no speech: the last frame's time
    assert rows_of(world, source, "transcript") == []
    assert facts_of(frames[0])["kind"] == "media"
    assert all("facts" not in row.meta for row in frames[1:])
    assert [r.n for r in manifest.renditions] == [1, 2, 3]


async def test_a_clip_without_artifacts_answers_at_once(world: World) -> None:
    """Never transcribed, or no speech and no pictures: no job, no event, and the hint says nothing was found."""
    events = len(world.lines())
    for name in ("harbour-tour.mp4", "voice-memo.m4a"):
        source = clip(world, name)
        manifest = await world.prepare(source)
        assert manifest.model_dump(mode="json") == {
            "path": source.virtual_path,
            "kind": "media",
            "unit": "frame",
            "total": 0,
            "pages_without_text": [],
            "renditions": [],
            "frames": None,
            "hint": NOTHING_FOUND,
            "duration_s": None,
            "transcript": False,
        }
    assert (world.executor.requests, len(world.lines())) == ([], events)
    assert world.store.derived_of(source) == []


async def test_the_times_of_speech_and_pictures_are_both_in_the_duration(world: World) -> None:
    """Ruling W5: the clip lasts as long as its last frame says, when that is past the speech's end."""
    source = clip(world)
    add_jsonl(world, source, [make_media.jsonl_record(0, 18, "A short sentence.", speaker="Speaker 1")])
    add_zip(world, source, times=(0.0, 9.0, 27.0))
    manifest = await world.prepare(source)
    assert manifest.duration_s == 27.0


# --- what the executor cannot read, and what is remembered of it -------------------------------------------------


async def test_unusable_artifacts_are_422(world: World) -> None:
    """A zip with no keyframe and a JSON Lines that is none: 422, recorded as an error, and no row is made."""
    source = clip(world)
    path = make_media.make_keyframes_zip(world.built / "notes.zip", 0, times=None, extra={"notes.txt": b"not frames"})
    keep(world, source, path.read_bytes(), "nextext_zip")
    keep(world, source, b"not json\n", "nextext_jsonl")

    assert await refused_with(world, source) == (422, NOTHING_USABLE)
    assert rows_of(world, source, "transcript") == rows_of(world, source, "frame") == []
    assert sorted(rec.role for rec in world.store.derived_of(source)) == ["nextext_jsonl", "nextext_zip"]
    (event,) = world.events()
    assert (event["action"], event["outcome"]) == ("modify", "error")
    assert event["criteria"]["refused"] == "unreadable_file"
    assert event["target"]["ids"] == [source.id]


async def test_unusable_artifacts_are_refused_again_at_once(world: World) -> None:
    """The refusal is decided by the artifacts alone, so it is remembered at once: the next prepare runs no job.

    It is recorded as the first was, as remembered. A new artifact forgets it: the route's part, tested with the routes.
    """
    source = clip(world)
    keep(world, source, b"not json\n", "nextext_jsonl")

    first = await refused_with(world, source)
    assert world.store.refusal_of(source) == NOTHING_USABLE
    second = await refused_with(world, source)

    assert first == second == (422, NOTHING_USABLE)
    assert len(world.executor.requests) == 1
    first_event, again = world.events()
    assert (first_event["outcome"], again["outcome"]) == ("error", "error")
    assert "remembered" not in first_event["criteria"]
    assert again["criteria"]["remembered"] is True


async def test_one_unreadable_part_leaves_the_other(world: World) -> None:
    """Frames that cannot be read leave the transcript, with the hint, and the prepare is a 200."""
    source = clip(world)
    add_jsonl(world, source)
    keep(world, source, b"PK\x03\x04" + bytes(40), "nextext_zip")
    manifest = await world.prepare(source)

    assert (manifest.total, manifest.transcript, manifest.renditions) == (0, True, [])
    assert manifest.hint == "The frames could not be read."
    assert [rec.role for rec in world.store.derived_of(source)].count("transcript") == 1


# --- a replaced artifact ------------------------------------------------------------------------------------------


async def test_a_replaced_artifact_is_prepared_again(world: World) -> None:
    """A new JSON Lines takes the old one's transcript and frames with it; the next prepare makes them again."""
    source = clip(world)
    old = add_jsonl(world, source)
    add_zip(world, source)
    first = await world.prepare(source)
    made = [rec for rec in world.store.derived_of(source) if rec.role in ("transcript", "frame")]

    add_jsonl(world, source, [make_media.jsonl_record(0, 120, "A longer tour.", speaker="Speaker 1")])
    assert [rec.role for rec in world.store.derived_of(source)] == ["nextext_jsonl", "nextext_zip"]
    assert all(not world.file_of(rec).exists() for rec in made)
    second = await world.prepare(source)

    assert (first.duration_s, second.duration_s) == (91.0, 120.0)
    assert (second.total, second.transcript) == (3, True)
    assert len(world.executor.requests) == 2
    assert {rec.id for rec in made}.isdisjoint(rec.id for rec in world.store.derived_of(source))
    assert old.id not in {rec.id for rec in world.store.derived_of(source)}
    assert [event["outcome"] for event in world.events()] == ["ok", "ok"]
    assert world.events()[1]["target"]["ids"][0] == source.id


async def test_an_artifact_posted_again_during_a_prepare_waits_for_it(world: World) -> None:
    """Review Focus 3: it waits for the clip's lock, and then replaces what the prepare made, which is made again."""
    held = world.over(Held(world.settings.data_dir, world.built.parent / "held-jobs"))
    executor = held.executor
    assert isinstance(executor, Held)
    source = clip(held)
    add_jsonl(held, source)
    add_zip(held, source)
    preparing = asyncio.create_task(held.prepare(source))
    await asyncio.wait_for(executor.through.wait(), 5)

    async def post_again() -> None:
        async with held.queue.source_lock(source.id, SOURCE_LOCK_WAIT_S):
            await run_in_threadpool(
                add_jsonl, held, source, [make_media.jsonl_record(0, 200, "A new tour.", speaker="Speaker 1")]
            )

    posting = asyncio.create_task(post_again())
    await asyncio.sleep(0.2)
    assert not posting.done()  # it waits for the prepare, which holds the lock
    executor.release.set()
    first = await preparing
    await posting

    assert first.duration_s == 91.0  # the prepare answered what it had read
    assert [rec.role for rec in held.store.derived_of(source)] == ["nextext_jsonl", "nextext_zip"]
    again = await held.prepare(source)
    assert (again.duration_s, again.total, len(executor.requests)) == (200.0, 3, 2)


# --- a retry after a lost head ------------------------------------------------------------------------------------


async def leave_frames_without_a_head(
    world: World, source: FileRecord, monkeypatch: pytest.MonkeyPatch
) -> list[DerivedRecord]:
    """Prepare a clip whose transcript the quota refuses: 507, and its frames stay, with no head to carry the facts.

    The store is left refusing nothing again, as when the user frees room.
    """
    real = world.store.register_derived

    def refuse_the_transcript(owner: FileRecord, staged: Path, spec: DerivedSpec, **more: Any) -> DerivedRecord:
        if spec.role == "transcript":
            raise QuotaExceeded("workspace quota exceeded")
        return real(owner, staged, spec, **more)

    monkeypatch.setattr(world.store, "register_derived", refuse_the_transcript)
    with pytest.raises(QuotaExceeded):
        await world.prepare(source)
    monkeypatch.setattr(world.store, "register_derived", real)
    left = rows_of(world, source, "frame")
    assert (len(left), rows_of(world, source, "transcript")) == (3, [])
    assert [event["outcome"] for event in world.events()] == ["ok", "error"]  # the job, and the transcript named again
    return left


async def test_a_retry_after_a_lost_head_replaces_what_was_left_and_records_no_error(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The retry makes the frames again: it replaces the old ones, in its own event, and ends with one set of rows."""
    source = clip(world)
    add_jsonl(world, source)
    add_zip(world, source)
    old = await leave_frames_without_a_head(world, source, monkeypatch)

    manifest = await world.prepare(source)
    rows = world.store.derived_of(source)
    new = [rec for rec in rows if rec.role in ("transcript", "frame")]

    assert (manifest.total, manifest.transcript, [r.n for r in manifest.renditions]) == (3, True, [1, 2, 3])
    assert sorted(rec.role for rec in rows) == ["frame"] * 3 + ["nextext_jsonl", "nextext_zip", "transcript"]
    assert {rec.id for rec in old}.isdisjoint(rec.id for rec in rows)
    assert all(not world.file_of(rec).exists() for rec in old)
    assert all(world.file_of(rec).is_file() for rec in new)
    assert len(world.executor.requests) == 2
    outcomes = [event["outcome"] for event in world.events()]
    assert outcomes == ["ok", "error", "ok"]  # no error event for a frame that was made again
    retry = world.events()[2]
    assert retry["target"]["ids"][0] == source.id
    assert sorted(retry["target"]["ids"][1:5]) == sorted(rec.id for rec in new)
    assert retry["target"]["ids"][5:] == [rec.id for rec in old]
    assert retry["criteria"] == {
        "caller": "ghostwriter",
        "renders": 4,
        "strips": 1,
        "on_demand": False,
        "outputs": 4,
        "replaced": 3,
    }
    assert await world.prepare(source) == manifest  # and the next is answered from the rows
    assert len(world.executor.requests) == 2


async def test_a_retry_that_cannot_delete_what_it_replaces_keeps_nothing_and_says_so(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The event is written first; a deletion that fails is recorded as an error by the same ids and nothing is kept.

    The old rows stay, so the next prepare replaces them as this one would have.
    """
    source = clip(world)
    add_jsonl(world, source)
    add_zip(world, source)
    old = await leave_frames_without_a_head(world, source, monkeypatch)
    real = world.store.remove_derived

    def cannot(rec: DerivedRecord) -> None:
        raise OSError("synthetic: read-only file system")

    monkeypatch.setattr(world.store, "remove_derived", cannot)
    with pytest.raises(OSError):
        await world.prepare(source)

    assert [rec.id for rec in rows_of(world, source, "frame")] == [rec.id for rec in old]
    assert rows_of(world, source, "transcript") == []
    *_, said, failed = world.events()
    assert (said["outcome"], failed["outcome"]) == ("ok", "error")
    assert failed["target"]["ids"] == said["target"]["ids"][1:]  # the new ids and the old ones, not the clip

    monkeypatch.setattr(world.store, "remove_derived", real)
    manifest = await world.prepare(source)
    assert (manifest.total, manifest.transcript) == (3, True)
    assert sorted(rec.role for rec in world.store.derived_of(source)) == (
        ["frame"] * 3 + ["nextext_jsonl", "nextext_zip", "transcript"]
    )


async def test_a_job_that_keeps_nothing_replaces_nothing(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry whose job fails leaves the rows an earlier prepare left: nothing is deleted for it."""
    source = clip(world)
    add_jsonl(world, source)
    add_zip(world, source)
    old = await leave_frames_without_a_head(world, source, monkeypatch)
    world.executor.status = "error"
    with pytest.raises(HTTPException):
        await world.prepare(source)

    assert [rec.id for rec in rows_of(world, source, "frame")] == [rec.id for rec in old]
    assert "replaced" not in world.events()[-1]["criteria"]


# --- a clip gone while its prepare waits --------------------------------------------------------------------------


async def test_a_clip_taken_back_while_its_prepare_waits_for_the_lock_is_not_found(world: World) -> None:
    """The prepare waits for the clip's lock; the clip is taken back meanwhile: 404, like any file that is gone."""
    source = clip(world)
    add_jsonl(world, source)
    add_zip(world, source)
    async with world.queue.source_lock(source.id, SOURCE_LOCK_WAIT_S):
        waiting = asyncio.create_task(world.prepare(source))
        await asyncio.sleep(0.2)
        assert not waiting.done()
        world.store.remove_upload(source, lambda ids, outcome: None)

    with pytest.raises(NotFound):
        await waiting
    assert world.executor.requests == []


async def test_a_clip_whose_artifacts_went_is_answered_as_one_with_nothing_found(world: World) -> None:
    """The same wait, but only the artifacts go (the clip stays): the answer is the manifest of nothing found."""
    source = clip(world)
    held = [add_jsonl(world, source), add_zip(world, source)]
    async with world.queue.source_lock(source.id, SOURCE_LOCK_WAIT_S):
        waiting = asyncio.create_task(world.prepare(source))
        await asyncio.sleep(0.2)
        for rec in held:
            world.store.remove_derived(rec)

    manifest = await waiting
    assert (manifest.total, manifest.hint, manifest.transcript) == (0, NOTHING_FOUND, False)
    assert world.executor.requests == []


async def test_a_clip_replaced_under_its_name_while_its_prepare_waits_is_not_found(world: World) -> None:
    """Another file has the clip's path when the lock comes free: the clip asked for is gone, so 404."""
    source = clip(world)
    add_jsonl(world, source)
    async with world.queue.source_lock(source.id, SOURCE_LOCK_WAIT_S):
        waiting = asyncio.create_task(world.prepare(source))
        await asyncio.sleep(0.2)
        world.store.remove_upload(source, lambda ids, outcome: None)
        assert clip(world).id != source.id

    with pytest.raises(NotFound):
        await waiting


# --- one definition of the artifacts' roles ------------------------------------------------------------------------


def test_the_artifact_roles_have_one_definition() -> None:
    """The store, the route and the queue take each role's type and extension from ``workbench.renders``."""
    assert ARTIFACT_ROLES == frozenset(ARTIFACTS) == {"nextext_jsonl", "nextext_zip"}
    route_types = {str(role): mime for role, (mime, _) in files_module.ROLE_TYPES.items()}
    for role, (mime, extension) in ARTIFACTS.items():
        assert store_module._ROLE_TYPES[role] == frozenset({mime})
        assert store_module._DERIVED_EXTENSIONS[mime] == extension
        assert store_module._RENDER_NAME.fullmatch(f"{'a' * 32}.{extension}")
        assert route_types[role] == mime
    assert store_module._RENDER_NAME.fullmatch(f"{'a' * 32}.exe") is None


# --- the rows, read back ------------------------------------------------------------------------------------------


async def test_stored_media_facts_are_read_leniently(world: World) -> None:
    """A head whose facts hold a key this version does not know still gives the manifest; unreadable ones give none."""
    source = clip(world)
    add_jsonl(world, source)
    add_zip(world, source)
    prepared_manifest = await world.prepare(source)
    rows = world.store.derived_of(source)
    index = next(i for i, rec in enumerate(rows) if rec.role == "transcript")
    facts = facts_of(rows[index])

    def with_facts(stored: object) -> list[DerivedRecord]:
        return [
            *rows[:index],
            dataclasses.replace(rows[index], meta={**rows[index].meta, "facts": stored}),
            *rows[index + 1 :],
        ]

    assert media_manifest_of(source, with_facts({**facts, "a_later_field": [1, 2]})) == prepared_manifest
    assert media_manifest_of(source, with_facts({"kind": "media"})) is None  # not facts: the job is made again
    assert media_manifest_of(source, with_facts({**facts, "kind": "pdf"})) is None  # not a clip's
    assert media_manifest_of(source, [rec for rec in rows if rec.role not in ("transcript", "frame")]) is None


async def test_a_clips_frames_are_the_rows_by_their_number(world: World) -> None:
    """The renditions are the frame rows in time order, each with its own size; a lost frame is no rendition."""
    source = clip(world)
    add_zip(world, source, times=(0.0, 4.0, 8.0))
    await world.prepare(source)
    rows = world.store.derived_of(source)
    lost = next(rec for rec in rows if rec.role == "frame" and rec.ordinal == 2)
    manifest = media_manifest_of(source, [rec for rec in rows if rec.id != lost.id])

    assert isinstance(manifest, Manifest)
    assert (manifest.total, [r.n for r in manifest.renditions]) == (3, [1, 3])


async def test_a_clips_frames_are_never_made_one_by_one(world: World) -> None:
    """A page that is missing is made by a job of its own; a clip's frames come with its transcript, so none runs."""
    source = clip(world)
    add_zip(world, source)
    manifest = await world.prepare(source)
    requests, events = len(world.executor.requests), len(world.lines())

    assert await world.renders(source, manifest, [1, 2, 3]) == []
    assert (len(world.executor.requests), len(world.lines())) == (requests, events)


# --- the gate ------------------------------------------------------------------------------------------------------


def gates_while_running(world: World, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, frozenset[tuple[str, str]]]]:
    """Watch which shared gates the caller holds while each job runs: the tool and the gates' keys, in order.

    A gate exists only while a turn of it is held, so the keys are read from inside the executor, when a job runs.
    """
    seen: list[tuple[str, frozenset[tuple[str, str]]]] = []
    real = world.executor.run

    async def watch(req: JobRequest, timeout_s: float) -> JobResult:
        seen.append((req.tool, frozenset(world.queue._shared_gates)))
        return await real(req, timeout_s)

    monkeypatch.setattr(world.executor, "run", watch)
    return seen


async def test_a_clip_is_prepared_on_the_prepare_gate_and_never_the_conversion_gate(
    world: World, fake_office: FakeOffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ruling W3: while a clip's job runs the caller holds its prepare gate and not its conversion gate.

    The same watch on a deck sees the conversion gate held by both of its jobs, so the observable is a real one.
    """
    source = clip(world)
    add_jsonl(world, source)
    add_zip(world, source)
    seen = gates_while_running(world, monkeypatch)
    sent: list[dict[str, Any]] = []
    real = world.queue.submit_renders

    async def watch(*args: Any, **more: Any) -> Any:
        sent.append(more)
        return await real(*args, **more)

    monkeypatch.setattr(world.queue, "submit_renders", watch)
    manifest = await world.prepare(source)

    (call,) = sent
    assert (manifest.total, call["gate"], call["turn"], call.get("converted")) == (3, "prepare", None, None)
    assert [rec.role for rec in call["artifacts"]] == ["nextext_jsonl", "nextext_zip"]
    assert seen == [("prepare_file", frozenset({("prepare", "ghostwriter")}))]

    seen.clear()
    await world.prepare(world.deck(3))
    assert [(tool, ("conversion", "ghostwriter") in gates) for tool, gates in seen] == [
        ("convert_office", True),
        ("prepare_file", True),
    ]


async def test_a_clips_prepare_does_not_wait_for_a_decks_conversion(world: World, fake_office: FakeOffice) -> None:
    """The same caller's conversion gate is held by a deck's conversion; the clip's prepare is answered meanwhile."""
    held, executor = held_world(world)
    source = held.deck(3)
    taken = clip(held)
    add_jsonl(held, taken)
    add_zip(held, taken)
    converting = asyncio.create_task(held.prepare(source))
    await asyncio.wait_for(executor.converting.wait(), 5)

    manifest = await asyncio.wait_for(held.prepare(taken), scaled(10))
    assert (manifest.total, manifest.transcript) == (3, True)
    assert not converting.done()
    executor.release.set()
    assert (await converting).total == 3
    assert fake_office.calls == 1


# --- a 4K clip -----------------------------------------------------------------------------------------------------


def noise_zip(path: Path, frames: int, size: tuple[int, int]) -> Path:
    """Write a keyframes zip of noise frames, their JPEG quality lowered until the zip is under ``ZIP_MAX_BYTES``."""
    noise = Image.frombytes("RGB", size, random.Random(7).randbytes(size[0] * size[1] * 3))
    for quality in range(90, 5, -10):
        buffer = io.BytesIO()
        noise.save(buffer, "JPEG", quality=quality)
        if frames * buffer.tell() < ZIP_MAX_BYTES - MIB:
            break
    manifest = {"frames": [{"file": f"frame_{i:03d}.jpg", "index": i, "time_sec": i * 7.0} for i in range(frames)]}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        for index in range(frames):
            archive.writestr(f"frame_{index:03d}.jpg", buffer.getvalue())
        archive.writestr("manifest.json", json.dumps(manifest))
    assert path.stat().st_size < ZIP_MAX_BYTES
    return path


async def test_a_4k_clip_fits_its_cap(world: World) -> None:
    """Review Focus 5: 19 noise frames of 3840 x 2160 and 7 MiB of long sentences: no ``RendersFull``, under 64 MiB.

    Each frame is 1280 x 720 under its cap, the transcript is cut at its own (4 MiB, with the hint that says so), and
    the whole set of the clip's derived files, both artifacts included, is within the source's cap.
    """
    source = clip(world)
    lines: list[str] = []
    size = 0
    while size < 7 * MIB:
        index = len(lines)
        words = " ".join(f"quay{index}-{n}" for n in range(60))  # about 600 characters a sentence
        record = make_media.jsonl_record(index * 2.0, index * 2.0 + 1.5, words, speaker="Speaker 1")
        lines.append(json.dumps(record) + "\n")
        size += len(lines[-1])
    keep(world, source, "".join(lines).encode(), "nextext_jsonl")
    keep(world, source, noise_zip(world.built / "noise.zip", 19, (3840, 2160)).read_bytes(), "nextext_zip")
    manifest = await world.prepare(source)
    rows = world.store.derived_of(source)

    assert (manifest.total, manifest.transcript, len(manifest.renditions)) == (19, True, 19)
    assert {(r.width, r.height) for r in manifest.renditions} == {(1280, 720)}
    (transcript,) = (rec for rec in rows if rec.role == "transcript")
    assert TRANSCRIPT_MAX_BYTES - 2048 < transcript.size <= TRANSCRIPT_MAX_BYTES  # cut at the last whole line
    assert manifest.hint is not None and "The transcript is cut at" in manifest.hint
    assert sum(rec.size for rec in rows) < DERIVED_MAX_BYTES
    assert [rec.role for rec in rows].count("frame") == 19
    assert [event["outcome"] for event in world.events()] == ["ok"]
    assert [rec.role for rec in rows if rec.role.startswith("nextext")] == ["nextext_jsonl", "nextext_zip"]
