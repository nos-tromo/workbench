"""The job process's one timer: nested time limits that raise in the code that is running.

A job is a single-threaded process that reads one file at a time. Some libraries it calls (openpyxl loading a
workbook, pypdf walking a page tree) run long in pure Python with no look at a clock, so the readers cannot
stop them by checking a deadline themselves. A :func:`limit_until` block makes ``SIGALRM`` raise an error in
whatever code is running once its deadline has passed, at the next bytecode; a call into C (one lxml parse of
one part) ends first, and is bounded by the size of the part and the zip guard.

Limits nest and share the one real-time interval timer of the process. The timer is always armed for the
earliest deadline of the limits in force; the earliest deadline that has passed decides which error is raised,
and each limit raises at most once; leaving a block arms the timer for the limits that are left, or disarms it
and puts the process's earlier ``SIGALRM`` handler back when there are none. So the regular expression budget
of a search and the deadline of a reader (which is in force while the search's matching runs) do not clobber
each other.

The errors are :class:`BaseException` subclasses, like ``KeyboardInterrupt``: a library that catches
``Exception`` around its own work does not swallow a timeout that lands in it.
"""

import signal
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import FrameType
from typing import Any

__all__ = ["TimeLimit", "limit_until"]

#: Smallest interval armed: ``setitimer`` with zero would disarm, and a deadline that has passed must fire.
_SOONEST = 1e-4

#: A signal that arrives this much before a deadline counts as arriving at it.
_EARLY = 1e-3


class TimeLimit(BaseException):
    """The base of the errors a time limit raises."""


@dataclass(eq=False)
class _Limit:
    """One limit in force.

    Attributes:
        deadline: A :func:`time.monotonic` time.
        error: The error to raise when it has passed.
        fired: Whether the error was raised already; a limit raises once.
    """

    deadline: float
    error: type[TimeLimit]
    fired: bool = False


_limits: list[_Limit] = []
_previous: Any = None


def _arm() -> None:
    """Arm the timer for the earliest deadline of the limits that have not fired, or disarm it."""
    waiting = [limit.deadline for limit in _limits if not limit.fired]
    if waiting:
        signal.setitimer(signal.ITIMER_REAL, max(min(waiting) - time.monotonic(), _SOONEST))
    else:
        signal.setitimer(signal.ITIMER_REAL, 0)


def _fire(_signum: int, _frame: FrameType | None) -> None:
    """Raise the error of the earliest limit whose deadline has passed.

    Args:
        _signum: The signal number.
        _frame: The interrupted frame.

    Raises:
        TimeLimit: The error of that limit.
    """
    now = time.monotonic()
    due = [limit for limit in _limits if not limit.fired and limit.deadline <= now + _EARLY]
    if not due:
        _arm()
        return
    first = min(due, key=lambda limit: limit.deadline)
    first.fired = True
    _arm()
    raise first.error


@contextmanager
def limit_until(deadline: float, error: type[TimeLimit]) -> Iterator[None]:
    """Raise ``error`` in the code running inside the block once the deadline has passed.

    Outside the main thread, where signals cannot be used, the block runs with no limit.

    Args:
        deadline: A :func:`time.monotonic` time.
        error: The error to raise.

    Yields:
        Once the limit is in force.

    Raises:
        TimeLimit: ``error``, at once if the deadline has already passed, else when it does.
    """
    global _previous
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    if deadline <= time.monotonic():
        raise error
    limit = _Limit(deadline, error)
    if not _limits:
        _previous = signal.signal(signal.SIGALRM, _fire)
    _limits.append(limit)
    _arm()
    try:
        yield
    finally:
        limit.fired = True
        _limits.remove(limit)
        if _limits:
            _arm()
        else:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, _previous)
