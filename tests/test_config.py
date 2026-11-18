"""Settings are read from the environment, with the spec's defaults and hard limits."""

import dataclasses
from pathlib import Path
from typing import Literal

import pytest

from tests.callers import GHOSTWRITER_TOOLS
from workbench.config import GHOSTWRITER_DEFAULT_TOOLS, NO_CALLER_KEY, ConfigError, Settings, load_settings

KEY = "k" * 32
D = "d" * 32
A = "a" * 32


def test_defaults_match_the_spec() -> None:
    """With only a caller's key set, every limit takes its documented default."""
    s = load_settings({"GHOSTWRITER_WORKBENCH_KEY": A}, role="api")
    assert (s.retention_days, s.retention_interval_s) == (7, 300)
    assert (s.executor_slots, s.queue_max) == (4, 16)
    assert (s.jobs_per_user, s.job_wall_s, s.job_memory_mb) == (1, 120, 1536)
    assert (s.result_max_kb, s.upload_max_mb, s.workspace_max_mb) == (16, 50, 500)
    assert s.data_dir == Path("/data")
    assert s.executor_socket == Path("/run/workbench/executor.sock")
    assert s.slot_uid_base == 20001
    assert s.audit_output is None
    assert s.docint_key is None
    assert (s.docint_tools, s.docint_wall_s, s.docint_slots) == (frozenset({"run_python"}), 60, 2)
    assert s.require_purpose == frozenset()
    assert s.ghostwriter_key == A
    assert (s.ghostwriter_tools, s.ghostwriter_wall_s, s.ghostwriter_slots) == (GHOSTWRITER_TOOLS, 120, 2)


def test_every_variable_is_read_by_its_exact_name() -> None:
    """Each documented variable overrides exactly its own field."""
    s = load_settings(
        {
            "WORKBENCH_DATA_DIR": "/srv/state",
            "WORKBENCH_RETENTION_DAYS": "3",
            "WORKBENCH_RETENTION_INTERVAL_S": "30",
            "WORKBENCH_EXECUTOR_SLOTS": "2",
            "WORKBENCH_QUEUE_MAX": "5",
            "WORKBENCH_JOBS_PER_USER": "2",
            "WORKBENCH_JOB_WALL_S": "11",
            "WORKBENCH_JOB_MEMORY_MB": "256",
            "WORKBENCH_RESULT_MAX_KB": "8",
            "WORKBENCH_UPLOAD_MAX_MB": "9",
            "WORKBENCH_WORKSPACE_MAX_MB": "10",
            "WORKBENCH_EXECUTOR_SOCKET": "/run/other/exec.sock",
            "WORKBENCH_SLOT_UID_BASE": "30001",
            "AUDIT_OUTPUT": "container",
            "DOCINT_WORKBENCH_KEY": D,
            "DOCINT_WORKBENCH_TOOLS": "run_python,search_files",
            "DOCINT_WORKBENCH_WALL_S": "9",
            "DOCINT_WORKBENCH_SLOTS": "1",
            "AUDIT_REQUIRE_PURPOSE": "query",
            "GHOSTWRITER_WORKBENCH_KEY": A,
            "GHOSTWRITER_WORKBENCH_TOOLS": "list_files,write_table",
            "GHOSTWRITER_WORKBENCH_WALL_S": "10",
            "GHOSTWRITER_WORKBENCH_SLOTS": "1",
        },
        role="api",
    )
    assert s == Settings(
        data_dir=Path("/srv/state"),
        retention_days=3,
        retention_interval_s=30,
        executor_slots=2,
        queue_max=5,
        jobs_per_user=2,
        job_wall_s=11,
        job_memory_mb=256,
        result_max_kb=8,
        upload_max_mb=9,
        workspace_max_mb=10,
        executor_socket=Path("/run/other/exec.sock"),
        slot_uid_base=30001,
        audit_output="container",
        docint_key=D,
        docint_tools=frozenset({"run_python", "search_files"}),
        docint_wall_s=9,
        docint_slots=1,
        require_purpose=frozenset({"query"}),
        ghostwriter_key=A,
        ghostwriter_tools=frozenset({"list_files", "write_table"}),
        ghostwriter_wall_s=10,
        ghostwriter_slots=1,
    )


def test_unset_string_values_fall_back_to_the_default() -> None:
    """Unset path and audit variables mean their defaults."""
    s = load_settings({"GHOSTWRITER_WORKBENCH_KEY": A}, role="api")
    assert s.data_dir == Path("/data")
    assert s.executor_socket == Path("/run/workbench/executor.sock")
    assert s.audit_output is None


@pytest.mark.parametrize("value", ["", "  ", "\n"])
def test_blank_string_values_fall_back_to_the_default(value: str) -> None:
    """An empty or whitespace-only path or audit variable means its default, not a blank path."""
    s = load_settings(
        {
            "GHOSTWRITER_WORKBENCH_KEY": A,
            "WORKBENCH_DATA_DIR": value,
            "WORKBENCH_EXECUTOR_SOCKET": value,
            "AUDIT_OUTPUT": value,
        },
        role="api",
    )
    assert s.data_dir == Path("/data")
    assert s.executor_socket == Path("/run/workbench/executor.sock")
    assert s.audit_output is None


def test_surrounding_whitespace_on_string_values_is_stripped() -> None:
    """A real path or audit value keeps its content but not the whitespace around it."""
    s = load_settings(
        {
            "GHOSTWRITER_WORKBENCH_KEY": A,
            "WORKBENCH_DATA_DIR": " /srv/state\n",
            "WORKBENCH_EXECUTOR_SOCKET": "\t/run/other/exec.sock ",
            "AUDIT_OUTPUT": "container\n",
        },
        role="api",
    )
    assert s.data_dir == Path("/srv/state")
    assert s.executor_socket == Path("/run/other/exec.sock")
    assert s.audit_output == "container"


def test_settings_are_frozen() -> None:
    """A loaded Settings cannot be changed in place."""
    s = load_settings({}, role="executor")
    with pytest.raises(AttributeError):
        s.executor_slots = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("DOCINT_WORKBENCH_KEY", "docint_key"),
        ("GHOSTWRITER_WORKBENCH_KEY", "ghostwriter_key"),
    ],
)
def test_api_requires_a_long_key(name: str, field: str) -> None:
    """Whichever caller's it is, a key that is set has at least 32 characters, and the error names its variable."""
    with pytest.raises(ConfigError, match=f"^{name} must"):
        load_settings({name: "k" * 31}, role="api")
    with pytest.raises(ConfigError, match=f"^{name} must"):
        load_settings({name: "k" * 31 + "\n"}, role="api")
    assert getattr(load_settings({name: KEY + "\n"}, role="api"), field) == KEY


def test_the_key_is_stripped_before_the_length_check() -> None:
    """Surrounding whitespace such as a trailing newline neither counts toward the length nor is stored."""
    with pytest.raises(ConfigError, match="GHOSTWRITER_WORKBENCH_KEY"):
        load_settings({"GHOSTWRITER_WORKBENCH_KEY": "a" * 31 + "\n"}, role="api")
    s = load_settings({"GHOSTWRITER_WORKBENCH_KEY": A + "\n"}, role="api")
    assert s.ghostwriter_key == A


def test_the_error_never_carries_the_key() -> None:
    """A refused short key is never echoed back in the error message."""
    short = "s3cret-" + "x" * 10
    with pytest.raises(ConfigError) as info:
        load_settings({"GHOSTWRITER_WORKBENCH_KEY": short}, role="api")
    assert short not in str(info.value)


def test_no_key_is_in_the_settings_repr() -> None:
    """Settings shown in a log line or a traceback name no caller's key."""
    shown = repr(load_settings({"DOCINT_WORKBENCH_KEY": D, "GHOSTWRITER_WORKBENCH_KEY": A}, role="api"))
    assert D not in shown
    assert A not in shown


def test_executor_needs_no_key() -> None:
    """The executor role has no caller key, even when one is in its environment."""
    assert load_settings({}, role="executor").ghostwriter_key is None
    both = load_settings({"DOCINT_WORKBENCH_KEY": D, "GHOSTWRITER_WORKBENCH_KEY": A}, role="executor")
    assert (both.docint_key, both.ghostwriter_key) == (None, None)


def test_the_docint_key_alone_starts_the_api() -> None:
    """The api starts with docint's key alone; Ghostwriter's is then ``None``."""
    s = load_settings({"DOCINT_WORKBENCH_KEY": D}, role="api")
    assert (s.docint_key, s.ghostwriter_key) == (D, None)


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"GHOSTWRITER_WORKBENCH_KEY": "  "},
        {"DOCINT_WORKBENCH_KEY": "d" * 31},
        {"GHOSTWRITER_WORKBENCH_KEY": "a" * 31, "DOCINT_WORKBENCH_KEY": D},
    ],
)
def test_no_usable_key_stops_the_api(env: dict[str, str]) -> None:
    """No key, a blank one or a short one beside a good one: the api does not start, and no key is in the error."""
    with pytest.raises(ConfigError) as refused:
        load_settings(env, role="api")
    assert [value for value in env.values() if value.strip() and value.strip() in str(refused.value)] == []


def test_the_docint_defaults() -> None:
    """With only docint's key set, it may run Python, for 60 seconds, two jobs at once; no action needs a purpose."""
    s = load_settings({"DOCINT_WORKBENCH_KEY": D}, role="api")
    assert (s.docint_tools, s.docint_wall_s, s.docint_slots, s.require_purpose) == (
        frozenset({"run_python"}),
        60,
        2,
        frozenset(),
    )


@pytest.mark.parametrize(
    ("name", "value", "named"),
    [
        ("DOCINT_WORKBENCH_WALL_S", "121", "DOCINT_WORKBENCH_WALL_S"),
        ("DOCINT_WORKBENCH_WALL_S", "4", "DOCINT_WORKBENCH_WALL_S"),
        ("DOCINT_WORKBENCH_SLOTS", "4", "DOCINT_WORKBENCH_SLOTS"),
        ("WORKBENCH_EXECUTOR_SLOTS", "2", "DOCINT_WORKBENCH_SLOTS"),
    ],
)
def test_the_docint_rules_stop_the_api_never_the_executor(name: str, value: str, named: str) -> None:
    """The wall cap of docint's jobs is from 5 to the job wall, and docint leaves a slot free; not for the executor."""
    env = {"DOCINT_WORKBENCH_KEY": D, name: value}
    with pytest.raises(ConfigError, match=named):
        load_settings(env, role="api")
    assert load_settings(env, role="executor").docint_key is None


def test_the_docint_rules_wait_for_its_key() -> None:
    """Without docint's key, a host with two slots or a short job wall starts its api, Ghostwriter's limits fitting."""
    two_slots = {"GHOSTWRITER_WORKBENCH_KEY": A, "WORKBENCH_EXECUTOR_SLOTS": "2", "GHOSTWRITER_WORKBENCH_SLOTS": "1"}
    assert load_settings(two_slots, role="api").docint_key is None
    short = {"GHOSTWRITER_WORKBENCH_KEY": A, "WORKBENCH_JOB_WALL_S": "30", "GHOSTWRITER_WORKBENCH_WALL_S": "30"}
    assert load_settings(short, role="api").job_wall_s == 30


@pytest.mark.parametrize("name", ["DOCINT_WORKBENCH_WALL_S", "DOCINT_WORKBENCH_SLOTS"])
def test_docint_limits_are_not_parsed_without_its_key(name: str) -> None:
    """A malformed limit of docint's does not stop an api that has only Ghostwriter's key; with docint's key it does."""
    env = {"GHOSTWRITER_WORKBENCH_KEY": A, name: "abc"}
    assert load_settings(env, role="api").docint_key is None
    with pytest.raises(ConfigError, match=name):
        load_settings({**env, "DOCINT_WORKBENCH_KEY": D}, role="api")


def test_the_docint_limits_take_their_bounds() -> None:
    """The bounds themselves are allowed: a wall cap of 5 or of the job wall, and one slot fewer than the executor's."""
    env = {"DOCINT_WORKBENCH_KEY": D, "WORKBENCH_EXECUTOR_SLOTS": "3"}
    assert load_settings({**env, "DOCINT_WORKBENCH_WALL_S": "5"}, role="api").docint_wall_s == 5
    assert load_settings({**env, "DOCINT_WORKBENCH_WALL_S": "120"}, role="api").docint_wall_s == 120
    assert load_settings(env, role="api").docint_slots == 2


def test_the_tool_list_is_a_comma_list() -> None:
    """Entries are stripped and blank ones dropped; a blank list means ``run_python``."""
    env = {"DOCINT_WORKBENCH_KEY": D}
    assert load_settings({**env, "DOCINT_WORKBENCH_TOOLS": " run_python , ,"}, role="api").docint_tools == frozenset(
        {"run_python"}
    )
    assert load_settings({**env, "DOCINT_WORKBENCH_TOOLS": ""}, role="api").docint_tools == frozenset({"run_python"})


def test_the_ghostwriter_key_alone_starts_the_api() -> None:
    """The api starts with Ghostwriter's key alone; docint's is then ``None``."""
    s = load_settings({"GHOSTWRITER_WORKBENCH_KEY": A}, role="api")
    assert (s.docint_key, s.ghostwriter_key) == (None, A)


def test_the_ghostwriter_defaults() -> None:
    """With only Ghostwriter's key set, it may use the nine tools, for 120 seconds, two jobs at once."""
    s = load_settings({"GHOSTWRITER_WORKBENCH_KEY": A}, role="api")
    assert (s.ghostwriter_tools, s.ghostwriter_wall_s, s.ghostwriter_slots) == (GHOSTWRITER_TOOLS, 120, 2)
    assert GHOSTWRITER_DEFAULT_TOOLS == GHOSTWRITER_TOOLS


@pytest.mark.parametrize("env", [{}, {"GHOSTWRITER_WORKBENCH_KEY": "  "}, {"GHOSTWRITER_WORKBENCH_KEY": "a" * 31}])
def test_no_usable_ghostwriter_key_stops_the_api(env: dict[str, str]) -> None:
    """No key, a blank one or a short one: the api does not start; with none, the error names both variables."""
    with pytest.raises(ConfigError) as refused:
        load_settings(env, role="api")
    message = str(refused.value)
    if not env:
        assert message == NO_CALLER_KEY
        assert all(name in message for name in ("DOCINT_WORKBENCH_KEY", "GHOSTWRITER_WORKBENCH_KEY"))
    assert [value for value in env.values() if value.strip() and value.strip() in message] == []


def test_the_old_key_alone_starts_nothing() -> None:
    """With only OPEN_WEBUI_WORKBENCH_KEY set, the api refuses, naming the two keys that count."""
    with pytest.raises(ConfigError) as e:
        load_settings({"OPEN_WEBUI_WORKBENCH_KEY": "x" * 43}, role="api")
    assert str(e.value) == NO_CALLER_KEY
    assert NO_CALLER_KEY.startswith("DOCINT_WORKBENCH_KEY or GHOSTWRITER_WORKBENCH_KEY must be set")
    assert "OPEN_WEBUI" not in NO_CALLER_KEY


def test_the_queue_wait_is_no_setting() -> None:
    """A leftover WORKBENCH_QUEUE_WAIT_S changes nothing, even one that would not parse: each call has its own wait."""
    env = {"GHOSTWRITER_WORKBENCH_KEY": A}
    assert load_settings({**env, "WORKBENCH_QUEUE_WAIT_S": "abc"}, role="api") == load_settings(env, role="api")
    assert "queue_wait_s" not in {f.name for f in dataclasses.fields(Settings)}


@pytest.mark.parametrize(
    ("name", "value", "named"),
    [
        ("GHOSTWRITER_WORKBENCH_WALL_S", "121", "GHOSTWRITER_WORKBENCH_WALL_S"),
        ("GHOSTWRITER_WORKBENCH_WALL_S", "9", "GHOSTWRITER_WORKBENCH_WALL_S"),
        ("GHOSTWRITER_WORKBENCH_SLOTS", "4", "GHOSTWRITER_WORKBENCH_SLOTS"),
        ("WORKBENCH_EXECUTOR_SLOTS", "2", "GHOSTWRITER_WORKBENCH_SLOTS"),
    ],
)
def test_the_ghostwriter_rules_stop_the_api_never_the_executor(name: str, value: str, named: str) -> None:
    """Ghostwriter's wall cap is from 10 to the job wall, and it leaves a slot free; not for the executor."""
    env = {"GHOSTWRITER_WORKBENCH_KEY": A, name: value}
    with pytest.raises(ConfigError, match=named):
        load_settings(env, role="api")
    assert load_settings(env, role="executor").ghostwriter_key is None


def test_the_ghostwriter_rules_wait_for_its_key() -> None:
    """Without Ghostwriter's key, a wall cap it would refuse does not stop the api."""
    env = {"DOCINT_WORKBENCH_KEY": D, "GHOSTWRITER_WORKBENCH_WALL_S": "9"}
    assert load_settings(env, role="api").ghostwriter_key is None


def test_the_ghostwriter_tool_list_is_a_comma_list() -> None:
    """Entries are stripped and blank ones dropped; a blank list means the nine tools."""
    env = {"GHOSTWRITER_WORKBENCH_KEY": A}
    assert load_settings({**env, "GHOSTWRITER_WORKBENCH_TOOLS": " list_files , ,"}, role="api").ghostwriter_tools == (
        frozenset({"list_files"})
    )
    assert load_settings({**env, "GHOSTWRITER_WORKBENCH_TOOLS": ""}, role="api").ghostwriter_tools == GHOSTWRITER_TOOLS


def test_require_purpose_is_parsed_and_checked() -> None:
    """The listed actions, lower-cased; a misspelt one stops the api, and the executor reads none."""
    env = {"GHOSTWRITER_WORKBENCH_KEY": A}
    s = load_settings({**env, "AUDIT_REQUIRE_PURPOSE": "query, Disclose,combine"}, role="api")
    assert s.require_purpose == frozenset({"query", "disclose", "combine"})
    with pytest.raises(ConfigError, match="AUDIT_REQUIRE_PURPOSE"):
        load_settings({**env, "AUDIT_REQUIRE_PURPOSE": "quiery"}, role="api")
    with pytest.raises(ConfigError, match="AUDIT_REQUIRE_PURPOSE"):
        load_settings({**env, "AUDIT_REQUIRE_PURPOSE": "lifecycle"}, role="api")
    assert load_settings({**env, "AUDIT_REQUIRE_PURPOSE": "quiery"}, role="executor").require_purpose == frozenset()
    assert load_settings({**env, "AUDIT_REQUIRE_PURPOSE": ""}, role="api").require_purpose == frozenset()


@pytest.mark.parametrize("name", ["WORKBENCH_EXECUTOR_SLOTS", "WORKBENCH_RETENTION_DAYS", "WORKBENCH_JOB_WALL_S"])
@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_non_positive_or_non_numeric_values_are_refused(name: str, value: str) -> None:
    """A WORKBENCH_* number that is not a whole number above zero names the variable in the error."""
    with pytest.raises(ConfigError, match=name):
        load_settings({"GHOSTWRITER_WORKBENCH_KEY": A, name: value}, role="api")


@pytest.mark.parametrize(
    ("name", "field", "default"),
    [
        ("WORKBENCH_EXECUTOR_SLOTS", "executor_slots", 4),
        ("WORKBENCH_RETENTION_DAYS", "retention_days", 7),
        ("WORKBENCH_JOB_WALL_S", "job_wall_s", 120),
    ],
)
@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_empty_values_mean_the_default(name: str, field: str, default: int, value: str) -> None:
    """An empty or whitespace-only number is treated as not set and takes its default."""
    s = load_settings({"GHOSTWRITER_WORKBENCH_KEY": A, name: value}, role="api")
    assert getattr(s, field) == default


@pytest.mark.parametrize("value", ["1.5", "1_0", "+4", "\u0664"])
def test_only_plain_ascii_digits_are_numbers(value: str) -> None:
    """Forms that Python's int() would accept but a config value should not are refused."""
    with pytest.raises(ConfigError, match="WORKBENCH_QUEUE_MAX"):
        load_settings({"GHOSTWRITER_WORKBENCH_KEY": A, "WORKBENCH_QUEUE_MAX": value}, role="api")


@pytest.mark.parametrize("role", ["api", "executor"])
def test_every_number_is_checked_in_both_roles(role: Literal["api", "executor"]) -> None:
    """The executor validates the same limits as the api."""
    env = {"GHOSTWRITER_WORKBENCH_KEY": A, "WORKBENCH_JOB_MEMORY_MB": "0"}
    with pytest.raises(ConfigError, match="WORKBENCH_JOB_MEMORY_MB"):
        load_settings(env, role=role)


def test_slots_are_capped_at_the_uid_pool() -> None:
    """The uid pool has 16 slots: 16 is accepted and 17 is refused."""
    assert load_settings({"WORKBENCH_EXECUTOR_SLOTS": "16"}, role="executor").executor_slots == 16
    with pytest.raises(ConfigError, match="WORKBENCH_EXECUTOR_SLOTS"):
        load_settings({"WORKBENCH_EXECUTOR_SLOTS": "17"}, role="executor")


@pytest.mark.parametrize("role", ["api", "executor"])
def test_the_result_limit_is_at_most_256_kb(role: Literal["api", "executor"]) -> None:
    """Cutting a result to its limit grows with the square of the limit: 256 KB is accepted, 257 is refused."""
    env = {"GHOSTWRITER_WORKBENCH_KEY": A}
    assert load_settings({**env, "WORKBENCH_RESULT_MAX_KB": "1"}, role=role).result_max_kb == 1
    assert load_settings({**env, "WORKBENCH_RESULT_MAX_KB": "256"}, role=role).result_max_kb == 256
    with pytest.raises(ConfigError, match="WORKBENCH_RESULT_MAX_KB") as refused:
        load_settings({**env, "WORKBENCH_RESULT_MAX_KB": "257"}, role=role)
    assert "256" in str(refused.value)
