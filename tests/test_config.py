"""Settings are read from the environment, with the spec's defaults and hard limits."""

from pathlib import Path
from typing import Literal

import pytest

from workbench.config import ConfigError, Settings, load_settings

KEY = "k" * 32


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
    )


def test_unset_and_empty_string_values_fall_back_to_the_default() -> None:
    """An unset or empty path or audit variable means its default, not an error."""
    s = load_settings(
        {
            "OPEN_WEBUI_WORKBENCH_KEY": KEY,
            "WORKBENCH_DATA_DIR": "",
            "WORKBENCH_EXECUTOR_SOCKET": "",
            "AUDIT_OUTPUT": "",
        },
        role="api",
    )
    assert s.data_dir == Path("/data")
    assert s.executor_socket == Path("/run/workbench/executor.sock")
    assert s.audit_output is None


def test_settings_are_frozen() -> None:
    """A loaded Settings cannot be changed in place."""
    s = load_settings({}, role="executor")
    with pytest.raises(AttributeError):
        s.executor_slots = 1  # type: ignore[misc]


def test_api_requires_a_long_key() -> None:
    """The api role refuses a missing key, an empty one and one of 31 characters."""
    with pytest.raises(ConfigError, match="OPEN_WEBUI_WORKBENCH_KEY"):
        load_settings({}, role="api")
    with pytest.raises(ConfigError, match="OPEN_WEBUI_WORKBENCH_KEY"):
        load_settings({"OPEN_WEBUI_WORKBENCH_KEY": ""}, role="api")
    with pytest.raises(ConfigError, match="OPEN_WEBUI_WORKBENCH_KEY"):
        load_settings({"OPEN_WEBUI_WORKBENCH_KEY": "k" * 31}, role="api")


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
