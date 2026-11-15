"""Environment-driven settings for the api and executor roles."""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from workbench.utils.audit_event import ACTIONS
from workbench.utils.audit_runtime import parse_required_actions

UID_POOL_SIZE = 16
MIN_KEY_LENGTH = 32

#: The largest result limit, in kilobytes: cutting an answer to its limit grows with the square of the limit.
MAX_RESULT_KB = 256

_OPEN_WEBUI_KEY: Final = "OPEN_WEBUI_WORKBENCH_KEY"
_DOCINT_KEY: Final = "DOCINT_WORKBENCH_KEY"

#: Why the api does not start without a caller: each key is optional, but one of them is needed.
NO_CALLER_KEY: Final = f"{_OPEN_WEBUI_KEY} or {_DOCINT_KEY} must be set to at least {MIN_KEY_LENGTH} characters"

#: docint's tools when ``DOCINT_WORKBENCH_TOOLS`` lists none, and its limits when they are not set.
DOCINT_DEFAULT_TOOLS: Final = frozenset({"run_python"})
DOCINT_DEFAULT_WALL_S: Final = 60
DOCINT_DEFAULT_SLOTS: Final = 2

#: The least wall time docint's jobs may be given: the inline schema's lowest ``time_limit_s``.
MIN_DOCINT_WALL_S: Final = 5


class ConfigError(ValueError):
    """The environment holds a value the service refuses to start with."""


@dataclass(frozen=True)
class Settings:
    """Every setting the api and the executor read, resolved and validated.

    Attributes:
        data_dir: Root of the workbench-data volume.
        open_webui_key: Bearer key Open WebUI presents; ``None`` when unset, and always for the executor role.
        retention_days: Days a file or job record is kept after it was created.
        retention_interval_s: Seconds between retention sweeps.
        executor_slots: Jobs the executor runs at once; at most the uid pool.
        queue_max: Jobs that may wait for a free slot.
        queue_wait_s: Seconds a job may wait for a free slot.
        jobs_per_user: Jobs one user may have running at once; their further calls wait.
        job_wall_s: Wall-clock seconds a job may run.
        job_memory_mb: Memory a job may use, in megabytes.
        result_max_kb: Largest result returned inline, in kilobytes; at most ``MAX_RESULT_KB``.
        upload_max_mb: Largest accepted upload, in megabytes.
        workspace_max_mb: Largest total workspace per user, in megabytes.
        executor_socket: Unix socket the api reaches the executor on.
        slot_uid_base: Uid of the first job slot; slot ``n`` runs as ``slot_uid_base + n``.
        audit_output: Audit sink from ``AUDIT_OUTPUT``, or ``None`` when unset.
        docint_key: Bearer key docint presents; ``None`` when unset, and always for the executor role.
        docint_tools: The tools docint may use.
        docint_wall_s: Wall-clock seconds one of docint's jobs may run; at most ``job_wall_s``.
        docint_slots: Jobs of docint's that may run at once; fewer than ``executor_slots``.
        require_purpose: The audit actions a call through the MCP door must state a purpose for.
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
    docint_key: str | None
    docint_tools: frozenset[str]
    docint_wall_s: int
    docint_slots: int
    require_purpose: frozenset[str]


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


def _caller_key(env: Mapping[str, str], name: str) -> str | None:
    """Read one caller's key, which may be left unset.

    Args:
        env: The environment to read.
        name: Variable name.

    Returns:
        The key, without surrounding whitespace, or ``None`` when it is unset, empty or only whitespace.

    Raises:
        ConfigError: The key is set but, once stripped, shorter than ``MIN_KEY_LENGTH``. The message names the
            variable and never holds the value.
    """
    key = _text(env, name)
    if key is not None and len(key) < MIN_KEY_LENGTH:
        raise ConfigError(f"{name} must be set to at least {MIN_KEY_LENGTH} characters")
    return key


def _docint_tools(env: Mapping[str, str]) -> frozenset[str]:
    """Read ``DOCINT_WORKBENCH_TOOLS``, a comma-separated list of tools.

    Args:
        env: The environment to read.

    Returns:
        The listed tools, each stripped, blank entries dropped; ``DOCINT_DEFAULT_TOOLS`` when none is listed.
    """
    listed = frozenset(part.strip() for part in env.get("DOCINT_WORKBENCH_TOOLS", "").split(",")) - {""}
    return listed or DOCINT_DEFAULT_TOOLS


def _required_purpose(env: Mapping[str, str]) -> frozenset[str]:
    """Read ``AUDIT_REQUIRE_PURPOSE``: the actions a call must state a purpose for.

    Args:
        env: The environment to read.

    Returns:
        The listed actions, lower-cased; none when the variable is unset or blank.

    Raises:
        ConfigError: An entry is not an action a caller may list. The message names the variable.
    """
    try:
        return parse_required_actions(_text(env, "AUDIT_REQUIRE_PURPOSE"), known=ACTIONS - {"lifecycle"})
    except ValueError as exc:
        raise ConfigError(str(exc)) from None


def load_settings(env: Mapping[str, str], *, role: Literal["api", "executor"]) -> Settings:
    """Resolve and validate the settings for one role.

    Args:
        env: The environment to read, normally ``os.environ``.
        role: ``"api"`` requires a caller key, Open WebUI's or docint's, and reads docint's limits and
            ``AUDIT_REQUIRE_PURPOSE``; ``"executor"`` reads none of these and keeps their defaults.

    Returns:
        The validated settings.

    Raises:
        ConfigError: A value is missing, malformed or out of range. The message names the variable.
    """
    slots = _positive_int(env, "WORKBENCH_EXECUTOR_SLOTS", 4)
    if slots > UID_POOL_SIZE:
        raise ConfigError(f"WORKBENCH_EXECUTOR_SLOTS must be at most {UID_POOL_SIZE}, the size of the uid pool")
    result_max_kb = _positive_int(env, "WORKBENCH_RESULT_MAX_KB", 16)
    if result_max_kb > MAX_RESULT_KB:
        raise ConfigError(f"WORKBENCH_RESULT_MAX_KB must be at most {MAX_RESULT_KB}")
    job_wall_s = _positive_int(env, "WORKBENCH_JOB_WALL_S", 120)
    open_webui_key: str | None = None
    docint_key: str | None = None
    docint_tools, docint_wall_s, docint_slots = DOCINT_DEFAULT_TOOLS, DOCINT_DEFAULT_WALL_S, DOCINT_DEFAULT_SLOTS
    require_purpose: frozenset[str] = frozenset()
    if role == "api":
        open_webui_key = _caller_key(env, _OPEN_WEBUI_KEY)
        docint_key = _caller_key(env, _DOCINT_KEY)
        if open_webui_key is None and docint_key is None:
            raise ConfigError(NO_CALLER_KEY)
        docint_tools = _docint_tools(env)
        docint_wall_s = _positive_int(env, "DOCINT_WORKBENCH_WALL_S", DOCINT_DEFAULT_WALL_S)
        docint_slots = _positive_int(env, "DOCINT_WORKBENCH_SLOTS", DOCINT_DEFAULT_SLOTS)
        require_purpose = _required_purpose(env)
        if docint_key is not None and not MIN_DOCINT_WALL_S <= docint_wall_s <= job_wall_s:
            raise ConfigError(
                f"DOCINT_WORKBENCH_WALL_S must be from {MIN_DOCINT_WALL_S} to WORKBENCH_JOB_WALL_S ({job_wall_s})"
            )
        if docint_key is not None and docint_slots >= slots:
            raise ConfigError(f"DOCINT_WORKBENCH_SLOTS must be less than WORKBENCH_EXECUTOR_SLOTS ({slots})")
    return Settings(
        data_dir=Path(_text(env, "WORKBENCH_DATA_DIR") or "/data"),
        open_webui_key=open_webui_key,
        retention_days=_positive_int(env, "WORKBENCH_RETENTION_DAYS", 7),
        retention_interval_s=_positive_int(env, "WORKBENCH_RETENTION_INTERVAL_S", 300),
        executor_slots=slots,
        queue_max=_positive_int(env, "WORKBENCH_QUEUE_MAX", 16),
        queue_wait_s=_positive_int(env, "WORKBENCH_QUEUE_WAIT_S", 60),
        jobs_per_user=_positive_int(env, "WORKBENCH_JOBS_PER_USER", 1),
        job_wall_s=job_wall_s,
        job_memory_mb=_positive_int(env, "WORKBENCH_JOB_MEMORY_MB", 1536),
        result_max_kb=result_max_kb,
        upload_max_mb=_positive_int(env, "WORKBENCH_UPLOAD_MAX_MB", 50),
        workspace_max_mb=_positive_int(env, "WORKBENCH_WORKSPACE_MAX_MB", 500),
        executor_socket=Path(_text(env, "WORKBENCH_EXECUTOR_SOCKET") or "/run/workbench/executor.sock"),
        slot_uid_base=_positive_int(env, "WORKBENCH_SLOT_UID_BASE", 20001),
        audit_output=_text(env, "AUDIT_OUTPUT"),
        docint_key=docint_key,
        docint_tools=docint_tools,
        docint_wall_s=docint_wall_s,
        docint_slots=docint_slots,
        require_purpose=require_purpose,
    )
