"""The job's one timer: nested limits on a single real-time timer, which never clobber each other."""

import signal
import threading
import time
from types import FrameType

import pytest

from workbench.executor.timer import TimeLimit, limit_until


class Inner(TimeLimit):
    """The error of the limit nested inside."""


class Outer(TimeLimit):
    """The error of the limit around."""


def spin(seconds: float = 5.0) -> None:
    """Run pure Python, with no look at a clock and an ``except Exception`` in the way, for up to ``seconds``."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            sum(range(1000))
        except Exception:
            pass


def test_a_limit_raises_its_error_in_pure_python_code_at_its_deadline() -> None:
    """A loop that never looks at a clock is interrupted, and a swallowing ``except Exception`` does not stop it."""
    began = time.monotonic()
    with pytest.raises(Inner):
        with limit_until(began + 0.3, Inner):
            spin()
    assert 0.25 < time.monotonic() - began < 1.0


def test_a_deadline_that_has_passed_raises_at_once_and_arms_nothing() -> None:
    """The block is not entered."""
    entered = False
    with pytest.raises(Inner):
        with limit_until(time.monotonic() - 1, Inner):
            entered = True
    assert not entered
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_the_earlier_limit_fires_first_and_the_outer_one_is_still_armed_after_it() -> None:
    """Inner 0.3 s inside outer 1 s: the inner error ends the inner block, then the outer fires at 1 s."""
    began = time.monotonic()
    with pytest.raises(Outer):
        with limit_until(began + 1.0, Outer):
            with pytest.raises(Inner):
                with limit_until(began + 0.3, Inner):
                    spin()
            assert 0.25 < time.monotonic() - began < 0.9
            spin()
    assert 0.95 < time.monotonic() - began < 1.6


def test_an_inner_limit_that_ends_in_time_gives_the_timer_back_to_the_outer_one() -> None:
    """Leaving the inner block arms the timer for the outer deadline again."""
    began = time.monotonic()
    with pytest.raises(Outer):
        with limit_until(began + 0.8, Outer):
            with limit_until(began + 5.0, Inner):
                time.sleep(0.05)
            remaining = signal.getitimer(signal.ITIMER_REAL)[0]
            assert 0.5 < remaining < 0.8
            spin()
    assert 0.75 < time.monotonic() - began < 1.5


def test_a_longer_inner_limit_does_not_postpone_the_outer_one() -> None:
    """The outer deadline is the earlier one: its error is raised, from inside the inner block."""
    began = time.monotonic()
    with pytest.raises(Outer):
        with limit_until(began + 0.3, Outer):
            with limit_until(began + 5.0, Inner):
                spin()
    assert 0.25 < time.monotonic() - began < 1.0


def test_each_limit_raises_once() -> None:
    """After its error was caught inside its block, the same limit does not fire again."""
    began = time.monotonic()
    with limit_until(began + 5.0, Outer):
        with limit_until(began + 0.2, Inner):
            try:
                spin()
            except Inner:
                pass
            spin(0.5)
    assert time.monotonic() - began < 1.5


def test_the_handler_and_the_timer_are_put_back() -> None:
    """The process's own ``SIGALRM`` handler is back when the last limit ends, with no timer running."""

    def mine(_signum: int, _frame: FrameType | None) -> None:
        raise AssertionError("not meant to fire")

    before = signal.signal(signal.SIGALRM, mine)
    try:
        with limit_until(time.monotonic() + 5, Inner):
            assert signal.getsignal(signal.SIGALRM) is not mine
            with limit_until(time.monotonic() + 6, Outer):
                pass
            assert signal.getsignal(signal.SIGALRM) is not mine
        assert signal.getsignal(signal.SIGALRM) is mine
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    finally:
        signal.signal(signal.SIGALRM, before)


def test_a_limit_that_is_not_reached_leaves_nothing_behind() -> None:
    """A block that ends in time leaves no timer, and no later signal raises anything."""
    with limit_until(time.monotonic() + 0.2, Inner):
        pass
    time.sleep(0.4)
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_outside_the_main_thread_a_block_runs_without_a_limit() -> None:
    """Signals reach only the main thread; elsewhere the block is just run."""
    done: list[str] = []

    def work() -> None:
        with limit_until(time.monotonic() + 0.05, Inner):
            time.sleep(0.2)
            done.append("finished")

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    assert done == ["finished"]
