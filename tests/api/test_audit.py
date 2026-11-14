"""The api's audit chain names files by id and never by name, and a failed write is never swallowed."""

import dataclasses
import io
import json
from collections.abc import Iterator
from typing import Any, override

import pytest

from workbench.api.audit import OPERATIONS, SERVICE, AuditLog
from workbench.config import load_settings
from workbench.models import Principal
from workbench.utils.audit_event import ACTIONS, AuditEmitter, AuditWriteError, Purpose, verify_chain

KEY = "zz-synthetic-caller-key-0123456789abcdef"

EXPECTED_OPERATIONS = {
    "upload": ("collect", "file.upload"),
    "view": ("disclose", "file.view"),
    "read": ("disclose", "file.read"),
    "display": ("disclose", "file.display"),
    "delete": ("delete", "file.delete"),
    "read_document": ("query", "tool.read_document"),
    "search_files": ("query", "tool.search_files"),
    "run_python": ("combine", "tool.run_python"),
    "inspect_template": ("query", "tool.inspect_template"),
    "fill_template": ("modify", "tool.fill_template"),
    "write_table": ("collect", "tool.write_table"),
    "write_document": ("collect", "tool.write_document"),
}


class Stream(io.StringIO):
    """An in-memory audit stream the test can break.

    Attributes:
        failing: While set, every write fails as a broken output would.
    """

    failing: bool = False

    @override
    def write(self, s: str, /) -> int:
        """Keep ``s``, or fail like a broken output while ``failing`` is set."""
        if self.failing:
            raise OSError("synthetic audit stream failure")
        return super().write(s)

    @property
    def raw(self) -> str:
        """Everything written so far."""
        return self.getvalue()

    def events(self) -> list[dict[str, Any]]:
        """Every event written so far, parsed, in order."""
        return [json.loads(line) for line in self.raw.splitlines()]


@pytest.fixture
def stream() -> Stream:
    """An empty in-memory audit stream."""
    return Stream()


@pytest.fixture
def log(stream: Stream) -> Iterator[AuditLog]:
    """A started log writing to ``stream``, without a heartbeat thread.

    Afterwards the chain is closed and verified, so every test that records
    ends with an intact chain.
    """
    audit = AuditLog(AuditEmitter(SERVICE, "0.0.1", stream=stream, heartbeat_interval_s=0))
    audit.start({"retention_days": 7})
    yield audit
    stream.failing = False
    audit.close()
    report = verify_chain(stream.events())
    assert report.ok, report.problems


@pytest.fixture
def alice() -> Principal:
    """A synthetic user."""
    return Principal("open-webui", "user-a", "chat-1")


def test_record_writes_a_verifiable_chain(log: AuditLog, stream: Stream, alice: Principal) -> None:
    """An upload is one event on a chain that verifies, naming the file by id and the chat as its container."""
    event_id = log.record("upload", alice, file_ids=["f1"])
    log.close()

    lines = stream.events()
    assert verify_chain(lines).ok
    last = lines[-2]
    assert last["event_id"] == event_id
    assert (last["action"], last["operation"], last["outcome"]) == ("collect", "file.upload", "ok")
    assert last["service"] == SERVICE
    assert last["actor"] == {"user": "user-a", "via": "service"}
    assert last["target"] == {"kind": "file", "ids": ["f1"], "count": 1, "container": "chat:chat-1"}
    assert last["criteria"] == {"caller": "open-webui"}
    assert "parent_event_id" not in last
    assert "purpose" not in last


def test_the_container_is_the_chat_of_the_principal_given(log: AuditLog, stream: Stream, alice: Principal) -> None:
    """The caller says which chat folder the event is about, by the principal it passes; a header is not consulted."""
    log.record("view", dataclasses.replace(alice, chat_id="chat-2"), file_ids=["f1"])

    assert stream.events()[-1]["target"]["container"] == "chat:chat-2"


def test_chain_starts_with_the_start_event_and_the_config(log: AuditLog, stream: Stream) -> None:
    """The chain opens with ``lifecycle`` / ``start`` and a system ``admin`` / ``config.audit`` carrying the config."""
    first, config = stream.events()

    assert (first["action"], first["operation"]) == ("lifecycle", "start")
    assert (config["action"], config["operation"], config["outcome"]) == ("admin", "config.audit", "ok")
    assert config["actor"] == {"user": None, "via": "system"}
    assert config["criteria"] == {"retention_days": 7}


def test_config_event_carries_no_secret(stream: Stream) -> None:
    """The caller key never reaches the stream: the config holds limits and periods only."""
    settings = load_settings({"OPEN_WEBUI_WORKBENCH_KEY": KEY}, role="api")
    log = AuditLog(AuditEmitter(SERVICE, "0.0.1", stream=stream, heartbeat_interval_s=0))

    log.start(
        {
            "retention_days": settings.retention_days,
            "retention_interval_s": settings.retention_interval_s,
            "upload_max_mb": settings.upload_max_mb,
            "workspace_max_mb": settings.workspace_max_mb,
        }
    )
    log.record("read", Principal("open-webui", "user-a", "chat-1"), file_ids=["f1"])
    log.close()

    assert KEY not in stream.raw
    assert settings.open_webui_key == KEY
    assert verify_chain(stream.events()).ok


def test_every_tool_key_maps_to_an_audit_action() -> None:
    """Each recorded key has its action and operation, and every action is a known audit action."""
    assert set(OPERATIONS) == set(EXPECTED_OPERATIONS)
    assert OPERATIONS == EXPECTED_OPERATIONS
    assert {action for action, _ in OPERATIONS.values()} <= ACTIONS


@pytest.mark.parametrize("key", sorted(EXPECTED_OPERATIONS))
def test_each_key_is_written_under_its_action_and_operation(
    log: AuditLog, stream: Stream, alice: Principal, key: str
) -> None:
    """Recording a key writes exactly its action and operation."""
    log.record(key, alice, file_ids=["f1", "f2"])

    event = stream.events()[-1]
    assert (event["action"], event["operation"]) == EXPECTED_OPERATIONS[key]
    assert event["target"]["ids"] == ["f1", "f2"]
    assert event["target"]["count"] == 2


def test_write_failure_propagates(log: AuditLog, stream: Stream, alice: Principal) -> None:
    """A broken output raises ``AuditWriteError``, so the caller can refuse to answer."""
    stream.failing = True

    with pytest.raises(AuditWriteError):
        log.record("read", alice, file_ids=["f1"])
    with pytest.raises(AuditWriteError):
        log.record_system("delete", "retention.sweep", file_ids=["f1"])


def test_caller_is_always_in_the_criteria_and_cannot_be_overridden(
    log: AuditLog, stream: Stream, alice: Principal
) -> None:
    """Extra criteria are kept beside ``caller``, which always names the authenticated caller."""
    log.record("read_document", alice, file_ids=["f1"], criteria={"pages": 3, "caller": "someone-else"})

    assert stream.events()[-1]["criteria"] == {"caller": "open-webui", "pages": 3}


def test_caller_survives_the_criteria_limit(log: AuditLog, stream: Stream, alice: Principal) -> None:
    """With more criteria than the emitter keeps, ``caller`` is still among them."""
    many = {f"k{n:02d}": n for n in range(60)}

    log.record("search_files", alice, criteria=many)

    criteria = stream.events()[-1]["criteria"]
    assert criteria["caller"] == "open-webui"
    assert len(criteria) == 32


def test_outcome_and_purpose_are_recorded(log: AuditLog, stream: Stream, alice: Principal) -> None:
    """A denied or failed operation and a stated purpose are written as given."""
    log.record("view", alice, file_ids=["f1"], outcome="denied", purpose=Purpose(case_ref="case-0001"))

    event = stream.events()[-1]
    assert event["outcome"] == "denied"
    assert event["purpose"] == {"case_ref": "case-0001"}


def test_a_call_without_files_records_an_empty_target(log: AuditLog, stream: Stream, alice: Principal) -> None:
    """A tool call that touched no file still names its chat and counts zero files."""
    log.record("search_files", alice, criteria={"matches": 0})

    assert stream.events()[-1]["target"] == {"kind": "file", "count": 0, "container": "chat:chat-1"}


def test_unknown_key_is_refused_and_writes_nothing(log: AuditLog, stream: Stream, alice: Principal) -> None:
    """A key outside ``OPERATIONS`` is a programming error, not an event."""
    before = len(stream.events())

    with pytest.raises(KeyError):
        log.record("rename", alice, file_ids=["f1"])

    assert len(stream.events()) == before


def test_record_system_acts_as_the_system_and_names_files_by_id(log: AuditLog, stream: Stream) -> None:
    """A retention sweep is a system event over file ids, without an account and without a container."""
    event_id = log.record_system(
        "delete", "retention.sweep", file_ids=["f1", "f2"], criteria={"period_days": 7}, outcome="ok"
    )

    event = stream.events()[-1]
    assert event["event_id"] == event_id
    assert (event["action"], event["operation"], event["outcome"]) == ("delete", "retention.sweep", "ok")
    assert event["actor"] == {"user": None, "via": "system"}
    assert event["target"] == {"kind": "file", "ids": ["f1", "f2"], "count": 2}
    assert event["criteria"] == {"period_days": 7}


def test_record_system_without_files_has_no_target(log: AuditLog, stream: Stream) -> None:
    """A system event that touched no file carries no target at all."""
    log.record_system("admin", "retention.refused", criteria={"reason": "state"}, outcome="error")

    event = stream.events()[-1]
    assert "target" not in event
    assert event["outcome"] == "error"


def test_no_file_name_reaches_the_stream(log: AuditLog, stream: Stream, alice: Principal) -> None:
    """Only ids go in: nothing here takes a name, so a name cannot appear."""
    log.record("upload", alice, file_ids=["f1"])
    log.record_system("delete", "retention.sweep", file_ids=["f1"])

    assert "quarterly-figures" not in stream.raw


def test_open_writes_to_the_selected_stream_under_the_service_name(
    capsys: pytest.CaptureFixture[str], alice: Principal
) -> None:
    """Without ``AUDIT_OUTPUT`` the chain goes to stdout, as the service ``workbench`` at the given version."""
    settings = load_settings({"OPEN_WEBUI_WORKBENCH_KEY": KEY}, role="api")
    log = AuditLog.open(settings, "9.9.9")

    log.start({"retention_days": settings.retention_days})
    log.record("upload", alice, file_ids=["f1"])
    log.close()

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(e["action"], e["operation"]) for e in lines] == [
        ("lifecycle", "start"),
        ("admin", "config.audit"),
        ("collect", "file.upload"),
        ("lifecycle", "shutdown"),
    ]
    assert {(e["service"], e["service_version"]) for e in lines} == {("workbench", "9.9.9")}
    assert verify_chain(lines).ok


def test_open_refuses_an_unknown_output() -> None:
    """An ``AUDIT_OUTPUT`` that names no mode stops startup instead of writing nowhere."""
    settings = dataclasses.replace(
        load_settings({"OPEN_WEBUI_WORKBENCH_KEY": KEY}, role="api"), audit_output="zz-nowhere"
    )

    with pytest.raises(ValueError, match="unknown AUDIT_OUTPUT"):
        AuditLog.open(settings, "0.0.1")


# --- long lists of files --------------------------------------------------------------------------------------


def file_ids(n: int) -> list[str]:
    """``n`` synthetic file ids, in order."""
    return [f"{i:032x}" for i in range(n)]


def test_201_ids_are_one_event_and_one_continuation(log: AuditLog, stream: Stream, alice: Principal) -> None:
    """The event names the first 200 ids and counts all 201; the 201st goes on a continuation, cut nowhere."""
    ids = file_ids(201)
    event_id = log.record("search_files", alice, file_ids=ids, criteria={"regex": False, "max_hits": 50})

    primary, more = stream.events()[-2:]
    assert primary["event_id"] == event_id
    assert (primary["action"], primary["operation"]) == ("query", "tool.search_files")
    assert primary["target"] == {"kind": "file", "ids": ids[:200], "count": 201, "container": "chat:chat-1"}
    assert primary["criteria"] == {"caller": "open-webui", "regex": False, "max_hits": 50}
    assert (more["action"], more["operation"], more["outcome"]) == ("query", "tool.search_files.ids", "ok")
    assert more["parent_event_id"] == event_id
    assert more["actor"] == primary["actor"]
    assert more["target"] == {"kind": "file", "ids": ids[200:], "count": 1, "container": "chat:chat-1"}
    assert "criteria" not in more
    assert "ids_truncated" not in stream.raw


def test_400_ids_are_exactly_two_events(log: AuditLog, stream: Stream, alice: Principal) -> None:
    """Two full chunks of 200: the event and one continuation, and nothing after them."""
    before = len(stream.events())
    log.record("run_python", alice, file_ids=file_ids(400))

    written = stream.events()[before:]
    assert [(e["operation"], len(e["target"]["ids"]), e["target"]["count"]) for e in written] == [
        ("tool.run_python", 200, 400),
        ("tool.run_python.ids", 200, 200),
    ]
    assert [i for e in written for i in e["target"]["ids"]] == file_ids(400)


def test_a_continuation_keeps_the_outcome_and_the_purpose(log: AuditLog, stream: Stream, alice: Principal) -> None:
    """A failed run's continuation is a failure too, for the same stated purpose."""
    purpose = Purpose(case_ref="case-0001")
    log.record("run_python", alice, file_ids=file_ids(250), outcome="error", purpose=purpose)

    primary, more = stream.events()[-2:]
    assert (primary["outcome"], more["outcome"]) == ("error", "error")
    assert primary["purpose"] == more["purpose"] == {"case_ref": "case-0001"}
    assert len(more["target"]["ids"]) == more["target"]["count"] == 50


def test_a_system_event_over_201_ids_is_split_too(log: AuditLog, stream: Stream) -> None:
    """The service's own events follow the same rule, its container and count kept."""
    ids = file_ids(201)
    event_id = log.record_system(
        "delete", "retention.sweep", file_ids=ids, criteria={"rule": "orphan"}, container="chat:chat-1"
    )

    primary, more = stream.events()[-2:]
    assert primary["target"] == {"kind": "file", "ids": ids[:200], "count": 201, "container": "chat:chat-1"}
    assert (more["operation"], more["parent_event_id"], more["actor"]) == (
        "retention.sweep.ids",
        event_id,
        {"user": None, "via": "system"},
    )
    assert more["target"] == {"kind": "file", "ids": ids[200:], "count": 1, "container": "chat:chat-1"}
    assert "criteria" not in more


def test_a_failed_continuation_raises(
    log: AuditLog, stream: Stream, alice: Principal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A continuation that cannot be written fails the record, as the first event's would."""
    write = stream.write

    def no_continuation(text: str, /) -> int:
        if '.ids"' in text:
            raise OSError("synthetic audit stream failure")
        return write(text)

    monkeypatch.setattr(stream, "write", no_continuation)
    with pytest.raises(AuditWriteError):
        log.record("run_python", alice, file_ids=file_ids(201))
    with pytest.raises(AuditWriteError):
        log.record_system("delete", "retention.sweep", file_ids=file_ids(201))
