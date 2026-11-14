"""The job process's one timer: nested time limits that raise in the code that is running.

A job is a single-threaded process that reads one file at a time. Some libraries it calls (openpyxl loading a
workbook, pypdf walking a page tree) run long in pure Python with no look at a clock, so the readers cannot
stop them by checking a deadline themselves. A :func:`limit_until` block makes ``SIGALRM`` raise an error in
whatever code is running once its deadline has passed, at the next bytecode; a call into C (one lxml parse of
one part) ends first, and is bounded by the size of the part and the zip guard.

A :func:`limit_spending` block does the same for a :class:`Meter`: a budget that is spent only while the meter
runs, such as the time a search's regular expression spends matching, counted around each match and not around
the reading in between. Starting and stopping a meter reads the clock and makes no system call, so it can wrap
every one of many short calls; the limit is entered once, around all of them.

Limits nest and share the one real-time interval timer of the process:

- The timer is armed for the earliest moment a limit in force that has not fired can be due: its deadline, or
  for a meter, when it runs out if it is running now or if it were started now (when that moment comes and the
  meter has not run all the time, the timer is armed again for the new earliest moment). A deadline is never
  pushed back: the timer is left alone when it is already armed for that moment or sooner, and a limit whose
  deadline has passed raises at once, in the code that enters or leaves a block, instead of being armed again
  (a timer re-armed from "now" for a deadline that is a few microseconds away would never go off in a loop of
  short blocks).
- The earliest deadline that has passed decides which error is raised, and each limit raises at most once. When
  another limit is due as well, the timer is armed to go off at once after the first error, so that the second
  is raised even if the code that got the first swallows it. A signal that comes while a ``__del__`` is running
  is put off for a moment: an exception raised in a finalizer is printed and dropped, and the code that was to be
  stopped would run on.
- Every meter that is running is stopped, charged up to that moment, before any limit's error is raised: the
  error ends the work the meter times, and it can land in the very code that would stop the meter.
- A block's bookkeeping runs with ``SIGALRM`` blocked, so that a signal never arrives between the limit
  being listed and the timer being armed (it is delivered when the signal is unblocked, inside the block's own
  ``try``). When the last limit leaves, the timer is disarmed.
- The handler of this module stays installed once it has been, so that a ``SIGALRM`` that is still in flight when
  the last limit leaves (the kernel's delivery of a signal can lag behind the code that disarmed the timer) finds
  nothing due and does nothing, instead of reaching a handler that was put back or killing the process by default.
  It is looked for, and installed over another one, when the first limit is entered.

The errors are :class:`BaseException` subclasses, like ``KeyboardInterrupt``: a library that catches
``Exception`` around its own work does not swallow a timeout that lands in it.
"""

import math
import signal
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from types import FrameType

__all__ = ["Meter", "TimeLimit", "limit_spending", "limit_until", "shielded"]

#: A signal that arrives this much before a deadline counts as arriving at it.
_EARLY = 1e-3

#: The smallest interval armed; ``setitimer`` with zero would disarm.
_SOONEST = 1e-6

#: How long to wait before trying again to raise an error that was due inside a finalizer.
_RETRY = 1e-3

#: While a meter is stopped, the timer looks at it again after the time it has left, but not sooner than this; a
#: meter with less left than this that is started just after a look can run out up to this much late.
_IDLE = 1e-2

_ALARM = {signal.SIGALRM}


class TimeLimit(BaseException):
    """The base of the errors a time limit raises."""


class Meter:
    """Seconds that are spent only while the meter runs, between :meth:`start` and :meth:`stop`.

    The code it times must let a :class:`TimeLimit` through: when a limit's error is raised, every meter that is
    running is stopped, since that code is taken to end there.

    Attributes:
        left: The seconds left when it was last stopped; zero or less once they are spent.
        until: While it runs, the :func:`time.monotonic` time at which it runs out; ``None`` while stopped.
    """

    __slots__ = ("left", "until")

    left: float
    until: float | None

    def __init__(self, seconds: float) -> None:
        """Start with a budget, stopped.

        Args:
            seconds: The budget.
        """
        self.left = seconds
        self.until = None

    @property
    def spent(self) -> bool:
        """Whether the budget is used up."""
        return self.left <= 0

    def start(self) -> None:
        """Start spending.

        A limit's error that lands once ``until`` is stored, before the caller's ``try`` that stops the meter, stops
        it on the way out (:func:`_spend`).
        """
        self.until = time.monotonic() + self.left

    def stop(self) -> None:
        """Stop spending, and keep what is left; a stopped meter stays as it is.

        A limit's error can land anywhere in here, as it begins or just after the clock read among other places,
        and the rest of it is then not run. ``left`` is stored before ``until`` is cleared, so at every such point
        the meter is either still running, and the handler stops and charges it before it raises (:func:`_spend`),
        or stopped with its time charged.
        """
        until = self.until
        if until is not None:
            self.left = until - time.monotonic()
            self.until = None


@dataclass(eq=False)
class _Limit:
    """One limit in force.

    Attributes:
        error: The error to raise when it is due.
        deadline: A :func:`time.monotonic` time, for a limit with no meter.
        meter: The budget, for a limit that is due when its meter has run out.
        fired: Whether the error was raised already, or the limit is being left; it raises once.
    """

    error: type[TimeLimit]
    deadline: float = math.inf
    meter: Meter | None = None
    fired: bool = False

    def soonest(self, now: float) -> float:
        """Tell the earliest moment the limit can be due, as far as can be told now.

        Args:
            now: The time.

        Returns:
            The deadline; for a meter, when it runs out if it runs from now on, or infinity when it is spent and
            stopped (it cannot run again: the code that starts it looks at :attr:`Meter.spent` first).
        """
        meter = self.meter
        if meter is None:
            return self.deadline
        if meter.until is not None:
            return meter.until
        return now + max(meter.left, _IDLE) if meter.left > 0 else math.inf

    def passed(self, now: float) -> float | None:
        """Tell whether the limit is due, a signal that comes a moment early counting as on time.

        Args:
            now: The time.

        Returns:
            The moment it became due, or ``None`` if it is not due; a meter is due only while it runs.
        """
        at = self.deadline if self.meter is None else self.meter.until
        return at if at is not None and at <= now + _EARLY else None


_limits: list[_Limit] = []

#: Whether :func:`_fire` is running: a signal that comes meanwhile waits until it has raised its error.
_firing = False


def _block() -> set[int]:
    """Block ``SIGALRM`` in this thread and say what the mask was.

    ``pthread_sigmask`` runs the Python handlers of signals that came in meanwhile *after* it has changed the mask,
    and a handler of ours that raises makes the call fail with the signal already blocked, and the old mask lost.
    So the old mask is asked for first, in a call that changes nothing, and put back if blocking fails. (It costs a
    system call, but a block is entered once per file or per search, not around each piece of text.)

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
    """Arm the timer for the earliest moment a limit that has not fired can be due, or disarm it.

    Returns:
        The limit that is due already and has not fired, if there is one; the timer is not armed for it, the
        caller raises its error. ``None`` otherwise.
    """
    now = time.monotonic()
    earliest: _Limit | None = None
    soonest = math.inf
    for limit in _limits:
        if not limit.fired:
            at = limit.soonest(now)
            if at < soonest:
                earliest, soonest = limit, at
    if earliest is None:
        signal.setitimer(signal.ITIMER_REAL, 0)
        return None
    remaining = soonest - now
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


def _spend(limit: _Limit) -> type[TimeLimit]:
    """Mark a limit as fired and its meter as run out, and stop every meter that is running, charged up to now.

    The error is about to be raised in the code that is running, and a meter runs only around work that the error
    goes through and ends. The code that would stop the meter can be where the error lands (:meth:`Meter.stop` as
    it begins, or just after its clock read), and a meter left running would go on being charged while it is not
    used, until its limit fired in whatever ran next: the reading of the next file.

    Args:
        limit: A limit that is due.

    Returns:
        Its error, to raise.
    """
    for each in _limits:
        running = each.meter
        if running is not None:
            running.stop()
    limit.fired = True
    meter = limit.meter
    if meter is not None:
        meter.left = 0.0
        meter.until = None
    return limit.error


def _rearm() -> None:
    """Arm the timer for what remains; if another limit is due already, arm it to go off at once."""
    if _arm() is not None:
        signal.setitimer(signal.ITIMER_REAL, _SOONEST)


def _fire(_signum: int, _frame: FrameType | None) -> None:
    """Raise the error of the earliest limit that is due.

    Args:
        _signum: The signal number.
        _frame: The interrupted frame.

    Raises:
        TimeLimit: The error of that limit.
    """
    global _firing
    if _firing:
        # A signal handled inside this handler (Python runs handlers between any two bytecodes, this one's too):
        # the one that is running raises first, this one comes back in a moment.
        signal.setitimer(signal.ITIMER_REAL, _RETRY)
        return
    _firing = True
    try:
        now = time.monotonic()
        due = [(at, limit) for limit in _limits if not limit.fired and (at := limit.passed(now)) is not None]
        if not due:
            _rearm()
            return
        if _in_finalizer(_frame):
            signal.setitimer(signal.ITIMER_REAL, _RETRY)  # try again in a moment, outside it
            return
        error = _spend(min(due, key=lambda pair: pair[0])[1])
        _rearm()
        raise error
    finally:
        _firing = False


def _enter(limit: _Limit) -> None:
    """List a limit and arm the timer for it, with ``SIGALRM`` blocked, installing the handler if it is not.

    A signal that came while blocked is delivered when the mask is restored at the end, and raises in the caller,
    which is inside the ``try`` that removes the limit.

    Args:
        limit: The limit to bring into force.

    Raises:
        TimeLimit: The error of a limit, this one or another, that is due.
    """
    before = _block()
    try:
        if not _limits and signal.getsignal(signal.SIGALRM) is not _fire:
            signal.signal(signal.SIGALRM, _fire)
        _limits.append(limit)
        due = _arm()
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, before)
    if due is not None:
        raise _spend(due)


def _leave(limit: _Limit) -> None:
    """Take a limit out and arm the timer for the rest, or disarm it when there are none.

    It may be run again after a :class:`TimeLimit` that landed in it before it had blocked the signal; running it
    again does what is left to do.

    Args:
        limit: The limit that ends.

    Raises:
        TimeLimit: The error of a limit that is due and has not fired.
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
        raise _spend(due)


@contextmanager
def _held(limit: _Limit) -> Iterator[None]:
    """Keep a limit in force inside the block.

    Outside the main thread, where signals cannot be used, the block runs with no limit.

    Args:
        limit: The limit.

    Yields:
        Once the limit is in force.

    Raises:
        TimeLimit: Its error, at once if it is due already (a deadline that has passed, a meter that is spent),
            else when it is due.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    meter = limit.meter
    due = meter.spent if meter is not None else limit.deadline <= time.monotonic()
    if due:
        raise limit.error
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


def limit_until(deadline: float, error: type[TimeLimit]) -> AbstractContextManager[None]:
    """Raise ``error`` in the code running inside the block once the deadline has passed.

    Outside the main thread, where signals cannot be used, the block runs with no limit.

    Args:
        deadline: A :func:`time.monotonic` time.
        error: The error to raise; at once, on entering, if the deadline has already passed.

    Returns:
        The block.
    """
    return _held(_Limit(error, deadline=deadline))


def limit_spending(meter: Meter, error: type[TimeLimit]) -> AbstractContextManager[None]:
    """Raise ``error`` in the code running inside the block once ``meter`` has run for its budget.

    Only the time the meter runs counts; the meter is spent (``left`` zero) when the error is raised. The block
    is meant to be entered once around many short runs of the meter; each run is two clock reads.

    Args:
        meter: The budget, stopped.
        error: The error to raise; at once, on entering, if the meter is spent already.

    Returns:
        The block.
    """
    return _held(_Limit(error, meter=meter))


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
