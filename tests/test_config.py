"""Settings are read from the environment, with the spec's defaults and hard limits."""

from pathlib import Path
from typing import Literal

import pytest

from workbench.config import ConfigError, Settings, load_settings

KEY = "k" * 32
D = "d" * 32


def test_defaults_match_the_spec() -> None:
    """With only the caller key set, every limit takes its documented default."""
    s = load_settings({"OPEN_WEBUI_WORKBENCH_KEY": KEY}, role="api")
    assert (s.retention_days, s.retention_interval_s) == (7, 300)
    assert (s.executor_slots, s.queue_max, s.queue_wait_s) == (4, 16, 60)
    assert (s.jobs_per_user, s.job_wall_s, s.job_memory_mb) == (1, 120, 1536)
    assert (s.result_max_kb, s.upload_max_mb, s.workspace_max_mb) == (16, 50, 500)
    assert s.data_dir == Path("/data")
    assert s.executor_socket == Path("/run/workbench/executor.sock")
    assert s.slot_uid_base == 20001
    assert s.open_webui_key == KEY
    assert s.audit_output is None
    assert s.docint_key is None
    assert (s.docint_tools, s.docint_wall_s, s.docint_slots) == (frozenset({"run_python"}), 60, 2)
    assert s.require_purpose == frozenset()


def test_every_variable_is_read_by_its_exact_name() -> None:
    """Each documented variable overrides exactly its own field."""
    s = load_settings(
        {
            "OPEN_WEBUI_WORKBENCH_KEY": KEY,
            "WORKBENCH_DATA_DIR": "/srv/state",
            "WORKBENCH_RETENTION_DAYS": "3",
            "WORKBENCH_RETENTION_INTERVAL_S": "30",
            "WORKBENCH_EXECUTOR_SLOTS": "2",
            "WORKBENCH_QUEUE_MAX": "5",
            "WORKBENCH_QUEUE_WAIT_S": "7",
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
        },
        role="api",
    )
    assert s == Settings(
        data_dir=Path("/srv/state"),
        open_webui_key=KEY,
        retention_days=3,
        retention_interval_s=30,
        executor_slots=2,
        queue_max=5,
        queue_wait_s=7,
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
    )


def test_unset_string_values_fall_back_to_the_default() -> None:
    """Unset path and audit variables mean their defaults."""
    s = load_settings({"OPEN_WEBUI_WORKBENCH_KEY": KEY}, role="api")
    assert s.data_dir == Path("/data")
    assert s.executor_socket == Path("/run/workbench/executor.sock")
    assert s.audit_output is None


@pytest.mark.parametrize("value", ["", "  ", "\n"])
def test_blank_string_values_fall_back_to_the_default(value: str) -> None:
    """An empty or whitespace-only path or audit variable means its default, not a blank path."""
    s = load_settings(
        {
            "OPEN_WEBUI_WORKBENCH_KEY": KEY,
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
            "OPEN_WEBUI_WORKBENCH_KEY": KEY,
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
    ("name", "field"), [("OPEN_WEBUI_WORKBENCH_KEY", "open_webui_key"), ("DOCINT_WORKBENCH_KEY", "docint_key")]
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
    with pytest.raises(ConfigError, match="OPEN_WEBUI_WORKBENCH_KEY"):
        load_settings({"OPEN_WEBUI_WORKBENCH_KEY": "k" * 31 + "\n"}, role="api")
    s = load_settings({"OPEN_WEBUI_WORKBENCH_KEY": KEY + "\n"}, role="api")
    assert s.open_webui_key == KEY


def test_the_error_never_carries_the_key() -> None:
    """A refused short key is never echoed back in the error message."""
    short = "s3cret-" + "x" * 10
    with pytest.raises(ConfigError) as info:
        load_settings({"OPEN_WEBUI_WORKBENCH_KEY": short}, role="api")
    assert short not in str(info.value)


def test_executor_needs_no_key() -> None:
    """The executor role has no caller key, even when one is in its environment."""
    assert load_settings({}, role="executor").open_webui_key is None
    assert load_settings({"OPEN_WEBUI_WORKBENCH_KEY": KEY}, role="executor").open_webui_key is None


def test_either_key_alone_starts_the_api() -> None:
    """Open WebUI's key is optional now: the api starts with either caller's key, and the other is ``None``."""
    assert load_settings({"OPEN_WEBUI_WORKBENCH_KEY": KEY}, role="api").docint_key is None
    assert load_settings({"DOCINT_WORKBENCH_KEY": D}, role="api").open_webui_key is None


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"OPEN_WEBUI_WORKBENCH_KEY": "  "},
        {"DOCINT_WORKBENCH_KEY": "d" * 31},
        {"OPEN_WEBUI_WORKBENCH_KEY": "k" * 31, "DOCINT_WORKBENCH_KEY": D},
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
    """Without docint's key, a host with two slots or a short job wall still starts its api."""
    assert (
        load_settings({"OPEN_WEBUI_WORKBENCH_KEY": KEY, "WORKBENCH_EXECUTOR_SLOTS": "2"}, role="api").docint_key is None
    )
    assert load_settings({"OPEN_WEBUI_WORKBENCH_KEY": KEY, "WORKBENCH_JOB_WALL_S": "30"}, role="api").job_wall_s == 30


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


def test_require_purpose_is_parsed_and_checked() -> None:
    """The listed actions, lower-cased; a misspelt one stops the api, and the executor reads none."""
    env = {"OPEN_WEBUI_WORKBENCH_KEY": KEY}
    s = load_settings({**env, "AUDIT_REQUIRE_PURPOSE": "query, Disclose,combine"}, role="api")
    assert s.require_purpose == frozenset({"query", "disclose", "combine"})
    with pytest.raises(ConfigError, match="AUDIT_REQUIRE_PURPOSE"):
        load_settings({**env, "AUDIT_REQUIRE_PURPOSE": "quiery"}, role="api")
    with pytest.raises(ConfigError, match="AUDIT_REQUIRE_PURPOSE"):
        load_settings({**env, "AUDIT_REQUIRE_PURPOSE": "lifecycle"}, role="api")
    assert load_settings({**env, "AUDIT_REQUIRE_PURPOSE": "quiery"}, role="executor").require_purpose == frozenset()


@pytest.mark.parametrize("name", ["WORKBENCH_EXECUTOR_SLOTS", "WORKBENCH_RETENTION_DAYS", "WORKBENCH_JOB_WALL_S"])
@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_non_positive_or_non_numeric_values_are_refused(name: str, value: str) -> None:
    """A WORKBENCH_* number that is not a whole number above zero names the variable in the error."""
    with pytest.raises(ConfigError, match=name):
        load_settings({"OPEN_WEBUI_WORKBENCH_KEY": KEY, name: value}, role="api")


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
    s = load_settings({"OPEN_WEBUI_WORKBENCH_KEY": KEY, name: value}, role="api")
    assert getattr(s, field) == default


@pytest.mark.parametrize("value", ["1.5", "1_0", "+4", "\u0664"])
def test_only_plain_ascii_digits_are_numbers(value: str) -> None:
    """Forms that Python's int() would accept but a config value should not are refused."""
    with pytest.raises(ConfigError, match="WORKBENCH_QUEUE_MAX"):
        load_settings({"OPEN_WEBUI_WORKBENCH_KEY": KEY, "WORKBENCH_QUEUE_MAX": value}, role="api")


@pytest.mark.parametrize("role", ["api", "executor"])
def test_every_number_is_checked_in_both_roles(role: Literal["api", "executor"]) -> None:
    """The executor validates the same limits as the api."""
    env = {"OPEN_WEBUI_WORKBENCH_KEY": KEY, "WORKBENCH_JOB_MEMORY_MB": "0"}
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
    env = {"OPEN_WEBUI_WORKBENCH_KEY": KEY}
    assert load_settings({**env, "WORKBENCH_RESULT_MAX_KB": "1"}, role=role).result_max_kb == 1
    assert load_settings({**env, "WORKBENCH_RESULT_MAX_KB": "256"}, role=role).result_max_kb == 256
    with pytest.raises(ConfigError, match="WORKBENCH_RESULT_MAX_KB") as refused:
        load_settings({**env, "WORKBENCH_RESULT_MAX_KB": "257"}, role=role)
    assert "256" in str(refused.value)
