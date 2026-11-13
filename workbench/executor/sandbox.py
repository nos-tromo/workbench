"""Run one job as a fresh process under a slot uid, with limits, a wall clock and a clean environment."""

import asyncio
import contextlib
import os
import resource
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from workbench.protocol import Limits

MB = 1024 * 1024

CLEAN_ENV: Mapping[str, str] = MappingProxyType(
    {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "MPLBACKEND": "Agg",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
    }
)

_KILLED_BY = frozenset({signal.SIGKILL, signal.SIGXCPU})


@dataclass(frozen=True)
class Slot:
    """One of the executor's job slots; each has an id pair no other slot and no other service uses.

    Attributes:
        index: Position of the slot, from 0.
        uid: User id jobs in this slot run as.
        gid: Group id jobs in this slot run as.
    """

    index: int
    uid: int
    gid: int


def clean_env(cwd: Path) -> dict[str, str]:
    """Build the environment of a job: ``CLEAN_ENV`` and ``HOME`` set to the job directory.

    Args:
        cwd: The job directory.

    Returns:
        The only variables the job sees; nothing of the supervisor's own environment.
    """
    return {**CLEAN_ENV, "HOME": str(cwd)}


def preexec(slot: Slot | None, limits: Limits) -> Callable[[], None]:
    """Build the function that runs in the child between ``fork`` and ``exec``.

    The child leaves the supervisor's session, drops its groups and switches to the slot's
    ids, then lowers its limits. Failing at any step aborts the spawn, so a job never runs
    with more rights than intended.

    Args:
        slot: The slot whose ids the job runs as, or ``None`` to keep the current ids (tests, dev).
        limits: The limits to set in the child.

    Returns:
        The function to pass as ``preexec_fn``.
    """
    ceilings = {
        resource.RLIMIT_CPU: limits.cpu_s,
        resource.RLIMIT_FSIZE: limits.file_mb * MB,
        resource.RLIMIT_NPROC: limits.nproc,
        resource.RLIMIT_NOFILE: limits.nofile,
        resource.RLIMIT_CORE: 0,
    }
    if sys.platform == "linux":
        ceilings[resource.RLIMIT_AS] = limits.memory_mb * MB  # other kernels refuse or ignore it

    def child() -> None:
        """Isolate the freshly forked child."""
        os.setsid()
        if slot is not None:
            os.setgroups([])
            os.setgid(slot.gid)
            os.setuid(slot.uid)
        for which, ceiling in ceilings.items():
            resource.setrlimit(which, (ceiling, ceiling))

    return child


def _kill_group(pgid: int) -> None:
    """Kill every process of the job's process group; one that is already gone is fine.

    Args:
        pgid: The group id, which is the job's own pid since the job leads its session.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


def _outcome(returncode: int) -> Literal["ok", "error", "killed"]:
    """Classify how a job that ended by itself ended.

    Args:
        returncode: The exit code, negative for death by a signal.

    Returns:
        ``ok`` for exit 0, ``killed`` when the kernel stopped it with ``SIGKILL`` or ``SIGXCPU``,
        else ``error``.
    """
    if returncode == 0:
        return "ok"
    if returncode < 0 and -returncode in _KILLED_BY:
        return "killed"
    return "error"


async def run_in_slot(
    argv: list[str], cwd: Path, slot: Slot | None, limits: Limits
) -> tuple[Literal["ok", "error", "timeout", "killed"], int]:
    """Run ``argv`` as a fresh process in ``cwd`` under the slot's ids and the limits.

    The process leads its own session. When it ends, by itself, on the wall clock or because
    this task is cancelled, everything left in its process group is killed with it.

    Args:
        argv: The command line; ``argv[0]`` should be an absolute path since ``PATH`` is fixed.
        cwd: The job directory; the process starts there and has it as ``HOME``.
        slot: The slot to run as, or ``None`` to keep the current uid (tests and dev).
        limits: Wall time and the kernel limits.

    Returns:
        The outcome and the exit code (negative for death by a signal). ``timeout`` means the
        wall time ran out and the group was killed.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=clean_env(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=preexec(slot, limits),
        start_new_session=False,
    )
    returncode: int | None
    try:
        returncode = await asyncio.wait_for(process.wait(), timeout=limits.wall_s)
    except TimeoutError:
        returncode = None
    finally:
        _kill_group(process.pid)
    if returncode is None:
        return "timeout", await process.wait()
    return _outcome(returncode), returncode
