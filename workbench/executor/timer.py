"""The job process's one timer: nested time limits that raise in the code that is running.

A job is a single-threaded process that reads one file at a time. Some libraries it calls (openpyxl loading a
workbook, pypdf walking a page tree) run long in pure Python with no look at a clock, so the readers cannot
stop them by checking a deadline themselves. A :func:`limit_until` block makes ``SIGALRM`` raise an error in
whatever code is running once its deadline has passed, at the next bytecode; a call into C (one lxml parse of
one part) ends first, and is bounded by the size of the part and the zip guard.

Limits nest and share the one real-time interval timer of the process:

- The timer is armed for the earliest deadline of the limits in force that have not fired. A deadline is never
  pushed back: the timer is left alone when it is already armed for that moment or sooner, and a limit whose
  deadline has passed raises at once, in the code that enters or leaves a block, instead of being armed again
  (a timer re-armed from "now" for a deadline that is a few microseconds away would never go off in a loop of
  short blocks).
- The earliest deadline that has passed decides which error is raised, and each limit raises at most once. A
  signal that comes while a ``__del__`` is running is put off for a moment: an exception raised in a finalizer is
  printed and dropped, and the code that was to be stopped would run on.
- A block's bookkeeping runs with ``SIGALRM`` blocked, so that a signal never arrives between the limit
  being listed and the timer being armed (it is delivered when the signal is unblocked, inside the block's own
  ``try``). When the last limit leaves, the timer is disarmed.
- The handler of this module stays installed once it has been, so that a ``SIGALRM`` that is still in flight when
  the last limit leaves (the kernel's delivery of a signal can lag behind the code that disarmed the timer) finds
  nothing due and does nothing, instead of reaching a handler that was put back or killing the process by default.

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

__all__ = ["TimeLimit", "limit_until", "shielded"]

#: A signal that arrives this much before a deadline counts as arriving at it.
_EARLY = 1e-3

#: The smallest interval armed; ``setitimer`` with zero would disarm.
_SOONEST = 1e-6

#: How long to wait before trying again to raise an error that was due inside a finalizer.
_RETRY = 1e-3

_ALARM = {signal.SIGALRM}


class TimeLimit(BaseException):
    """The base of the errors a time limit raises."""


@dataclass(eq=False)
class _Limit:
    """One limit in force.

    Attributes:
        deadline: A :func:`time.monotonic` time.
        error: The error to raise when it has passed.
        fired: Whether the error was raised already, or the limit is being left; it raises once.
    """

    deadline: float
    error: type[TimeLimit]
    fired: bool = False


_limits: list[_Limit] = []


def _block() -> set[int]:
    """Block ``SIGALRM`` in this thread and say what the mask was.

    ``pthread_sigmask`` runs the Python handlers of signals that came in meanwhile *after* it has changed the mask,
    and a handler of ours that raises makes the call fail with the signal already blocked, and the old mask lost.
    So the old mask is asked for first, in a call that changes nothing, and put back if blocking fails.

    Returns:
        The mask as it was, to be put back with ``SIG_SETMASK``.

    Raises:
        TimeLimit: A limit's error, raised by a handler that ran during the call; the mask is as it was.
    """
    before = signal.pthread_sigmask(signal.SIG_BLOCK, ())
    try:
        signal.pthread_sigmask(signal.SIG_BLOCK, _ALARM)
    except BaseException:
        signal.pthread_sigmask(signal.SIG_SETMASK, before)
        raise
    return before


def _arm() -> _Limit | None:
    """Arm the timer for the earliest deadline of the limits that have not fired, or disarm it.

    Returns:
        The limit whose deadline has already passed and that has not fired, if there is one; the timer is not
        armed for it, the caller raises its error. ``None`` otherwise.
    """
    waiting = [limit for limit in _limits if not limit.fired]
    if not waiting:
        signal.setitimer(signal.ITIMER_REAL, 0)
        return None
    earliest = min(waiting, key=lambda limit: limit.deadline)
    remaining = earliest.deadline - time.monotonic()
    if remaining <= 0:
        return earliest
    armed = signal.getitimer(signal.ITIMER_REAL)[0]
    if not 0 < armed <= remaining:
        signal.setitimer(signal.ITIMER_REAL, max(remaining, _SOONEST))
    return None


def _in_finalizer(frame: FrameType | None) -> bool:
    """Tell whether a frame is running inside a ``__del__``, where an exception cannot go anywhere.

    Python prints and drops an exception raised in a finalizer, so a timeout raised there would be lost and the
    code that ought to have been stopped would run on.

    Args:
        frame: The frame the signal interrupted.

    Returns:
        ``True`` if it, or a frame that called it, is a ``__del__``.
    """
    while frame is not None:
        if frame.f_code.co_name == "__del__":
            return True
        frame = frame.f_back
    return False


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
    if _in_finalizer(_frame):
        signal.setitimer(signal.ITIMER_REAL, _RETRY)  # try again in a moment, outside it
        return
    first = min(due, key=lambda limit: limit.deadline)
    first.fired = True
    _arm()
    raise first.error


def _raise(limit: _Limit) -> None:
    """Raise a limit's error now, once.

    Args:
        limit: A limit whose deadline has passed.

    Raises:
        TimeLimit: Its error.
    """
    limit.fired = True
    raise limit.error


def _enter(limit: _Limit) -> None:
    """List a limit and arm the timer for it, with ``SIGALRM`` blocked, installing the handler if it is not.

    A signal that came while blocked is delivered when the mask is restored at the end, and raises in the caller,
    which is inside the ``try`` that removes the limit.

    Args:
        limit: The limit to bring into force.

    Raises:
        TimeLimit: The error of a limit, this one or another, whose deadline has passed.
    """
    before = _block()
    try:
        if signal.getsignal(signal.SIGALRM) is not _fire:
            signal.signal(signal.SIGALRM, _fire)
        _limits.append(limit)
        due = _arm()
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, before)
    if due is not None:
        _raise(due)


def _leave(limit: _Limit) -> None:
    """Take a limit out and arm the timer for the rest, or disarm it when there are none.

    It may be run again after a :class:`TimeLimit` that landed in it before it had blocked the signal; running it
    again does what is left to do.

    Args:
        limit: The limit that ends.

    Raises:
        TimeLimit: The error of a limit whose deadline has passed and that has not fired.
    """
    limit.fired = True
    before = _block()
    try:
        if limit in _limits:
            _limits.remove(limit)
        due = _arm()
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, before)
    if due is not None:
        _raise(due)


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
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    if deadline <= time.monotonic():
        raise error
    limit = _Limit(deadline, error)
    try:
        _enter(limit)
        yield
    finally:
        # A limit that fires in the few bytecodes before _leave has blocked the signal aborts it: leave again,
        # then let that error go on its way.
        landed: TimeLimit | None = None
        while True:
            try:
                _leave(limit)
                break
            except TimeLimit as late:
                landed = landed or late
        if landed is not None:
            raise landed


@contextmanager
def shielded() -> Iterator[None]:
    """Hold back ``SIGALRM`` inside the block; a signal that comes meanwhile is delivered when the block ends.

    For closing something that is left suspended inside a time limit (a generator abandoned by the code that
    consumed it): a deadline that passes while its ``finally`` clauses run would otherwise raise in the middle
    of them.

    Yields:
        With the signal blocked.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    before = _block()
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, before)
