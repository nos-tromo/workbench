"""Environment-driven settings for the api and executor roles."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

UID_POOL_SIZE = 16
MIN_KEY_LENGTH = 32

_KEY_VAR = "OPEN_WEBUI_WORKBENCH_KEY"


class ConfigError(ValueError):
    """The environment holds a value the service refuses to start with."""


@dataclass(frozen=True)
class Settings:
    """Every setting the api and the executor read, resolved and validated.

    Attributes:
        data_dir: Root of the workbench-data volume.
        open_webui_key: Bearer key Open WebUI presents; ``None`` for the executor role.
        retention_days: Days a file or job record is kept after it was created.
        retention_interval_s: Seconds between retention sweeps.
        executor_slots: Jobs the executor runs at once; at most the uid pool.
        queue_max: Jobs that may wait for a free slot.
        queue_wait_s: Seconds a job may wait for a free slot.
        jobs_per_user: Jobs one user may have running at once; their further calls wait.
        job_wall_s: Wall-clock seconds a job may run.
        job_memory_mb: Memory a job may use, in megabytes.
        result_max_kb: Largest result returned inline, in kilobytes.
        upload_max_mb: Largest accepted upload, in megabytes.
        workspace_max_mb: Largest total workspace per user, in megabytes.
        executor_socket: Unix socket the api reaches the executor on.
        slot_uid_base: Uid of the first job slot; slot ``n`` runs as ``slot_uid_base + n``.
        audit_output: Audit sink from ``AUDIT_OUTPUT``, or ``None`` when unset.
    """

    data_dir: Path
    open_webui_key: str | None
    retention_days: int
    retention_interval_s: int
    executor_slots: int
    queue_max: int
    queue_wait_s: int
    jobs_per_user: int
    job_wall_s: int
    job_memory_mb: int
    result_max_kb: int
    upload_max_mb: int
    workspace_max_mb: int
    executor_socket: Path
    slot_uid_base: int
    audit_output: str | None


def _positive_int(env: Mapping[str, str], name: str, default: int) -> int:
    """Read ``name`` as a whole number above zero.

    Args:
        env: The environment to read.
        name: Variable name.
        default: Value used when the variable is unset, empty or only whitespace.

    Returns:
        The parsed value, or ``default`` when ``name`` is unset or blank.

    Raises:
        ConfigError: The value is not plain digits, or is zero.
    """
    raw = env.get(name, "").strip()
    if not raw:
        return default
    if not (raw.isascii() and raw.isdigit()) or int(raw) < 1:
        raise ConfigError(f"{name} must be a whole number above zero")
    return int(raw)


def _text(env: Mapping[str, str], name: str) -> str | None:
    """Read ``name`` without surrounding whitespace, treating a blank value as not set.

    Args:
        env: The environment to read.
        name: Variable name.

    Returns:
        The stripped value, or ``None`` when it is unset, empty or only whitespace.
    """
    return env.get(name, "").strip() or None


def _api_key(env: Mapping[str, str]) -> str:
    """Read the caller key the api requires.

    Args:
        env: The environment to read.

    Returns:
        The key, without surrounding whitespace.

    Raises:
        ConfigError: The key is missing or, once stripped, shorter than ``MIN_KEY_LENGTH``.
            The message never holds the value.
    """
    key = env.get(_KEY_VAR, "").strip()
    if len(key) < MIN_KEY_LENGTH:
        raise ConfigError(f"{_KEY_VAR} must be set to at least {MIN_KEY_LENGTH} characters")
    return key


def load_settings(env: Mapping[str, str], *, role: Literal["api", "executor"]) -> Settings:
    """Resolve and validate the settings for one role.

    Args:
        env: The environment to read, normally ``os.environ``.
        role: ``"api"`` requires the caller key; ``"executor"`` never reads it.

    Returns:
        The validated settings.

    Raises:
        ConfigError: A value is missing, malformed or out of range. The message names the variable.
    """
    slots = _positive_int(env, "WORKBENCH_EXECUTOR_SLOTS", 4)
    if slots > UID_POOL_SIZE:
        raise ConfigError(f"WORKBENCH_EXECUTOR_SLOTS must be at most {UID_POOL_SIZE}, the size of the uid pool")
    return Settings(
        data_dir=Path(_text(env, "WORKBENCH_DATA_DIR") or "/data"),
        open_webui_key=_api_key(env) if role == "api" else None,
        retention_days=_positive_int(env, "WORKBENCH_RETENTION_DAYS", 7),
        retention_interval_s=_positive_int(env, "WORKBENCH_RETENTION_INTERVAL_S", 300),
        executor_slots=slots,
        queue_max=_positive_int(env, "WORKBENCH_QUEUE_MAX", 16),
        queue_wait_s=_positive_int(env, "WORKBENCH_QUEUE_WAIT_S", 60),
        jobs_per_user=_positive_int(env, "WORKBENCH_JOBS_PER_USER", 1),
        job_wall_s=_positive_int(env, "WORKBENCH_JOB_WALL_S", 120),
        job_memory_mb=_positive_int(env, "WORKBENCH_JOB_MEMORY_MB", 1536),
        result_max_kb=_positive_int(env, "WORKBENCH_RESULT_MAX_KB", 16),
        upload_max_mb=_positive_int(env, "WORKBENCH_UPLOAD_MAX_MB", 50),
        workspace_max_mb=_positive_int(env, "WORKBENCH_WORKSPACE_MAX_MB", 500),
        executor_socket=Path(_text(env, "WORKBENCH_EXECUTOR_SOCKET") or "/run/workbench/executor.sock"),
        slot_uid_base=_positive_int(env, "WORKBENCH_SLOT_UID_BASE", 20001),
        audit_output=_text(env, "AUDIT_OUTPUT"),
    )
