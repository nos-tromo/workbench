"""The job's one timer: nested limits on a single real-time timer, which never clobber each other."""

import os
import random
import signal
import threading
import time
from types import FrameType

import pytest

from workbench.executor import timer
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


def test_the_timer_is_disarmed_and_a_late_signal_does_nothing() -> None:
    """After the last limit the timer is off; a signal that comes late finds nothing due, and does not kill the job."""
    with limit_until(time.monotonic() + 5, Inner):
        with limit_until(time.monotonic() + 6, Outer):
            pass
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 0
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert timer._limits == []
    for _ in range(5):
        os.kill(os.getpid(), signal.SIGALRM)
        time.sleep(0.01)
    assert signal.getsignal(signal.SIGALRM) is timer._fire


def test_the_handler_of_the_module_is_installed_over_another_one() -> None:
    """Whatever handler was there is replaced while a limit is in force (and the module's own stays after it)."""

    def mine(_signum: int, _frame: FrameType | None) -> None:
        raise AssertionError("not meant to fire")

    signal.signal(signal.SIGALRM, mine)
    with limit_until(time.monotonic() + 5, Inner):
        assert signal.getsignal(signal.SIGALRM) is timer._fire
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


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


@pytest.mark.parametrize("spin_us", [5, 50, 150, 400])
def test_rapid_nesting_does_not_postpone_the_outer_deadline(spin_us: int) -> None:
    """Inner blocks entered and left faster than every 100 us: the outer limit still fires at its deadline.

    The timer used to be armed again from "now" for at least 100 us each time, so that a deadline a few
    microseconds away was pushed back by every enter and every exit: a 5 us body never fired within two seconds.
    """
    began = time.monotonic()
    with pytest.raises(Outer):
        with limit_until(began + 0.05, Outer):
            while time.monotonic() < began + 2.0:
                with limit_until(time.monotonic() + 10, Inner):
                    end = time.perf_counter() + spin_us / 1e6
                    while time.perf_counter() < end:
                        pass
    assert 0.05 <= time.monotonic() - began < 0.08
    assert timer._limits == []
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_a_passed_deadline_raises_when_a_block_is_entered_or_left_and_not_later() -> None:
    """An outer limit that has passed raises at once from the next enter, instead of being armed again."""
    began = time.monotonic()
    woke: list[float] = []
    with pytest.raises(Outer):
        with limit_until(began + 0.05, Outer):
            signal.setitimer(signal.ITIMER_REAL, 0)  # as if the signal had been lost
            time.sleep(0.07)
            woke.append(time.monotonic())
            with limit_until(time.monotonic() + 10, Inner):
                pytest.fail("the block was entered after its outer limit had passed")
    assert time.monotonic() - woke[0] < 0.05
    assert timer._limits == []


def test_an_error_raised_while_a_limit_is_being_listed_leaves_nothing_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    """A signal handled inside the arming of an inner limit raises the outer error; the inner limit is removed."""
    real = timer._arm
    failed: list[bool] = []

    def arm_and_fail() -> object:
        if len(timer._limits) == 2 and not failed:
            failed.append(True)
            timer._limits[0].fired = True
            raise Outer
        return real()

    with pytest.raises(Outer):
        with limit_until(time.monotonic() + 5, Outer):
            monkeypatch.setattr(timer, "_arm", arm_and_fail)
            with limit_until(time.monotonic() + 5, Inner):
                pytest.fail("not entered")
    monkeypatch.undo()
    assert failed
    assert timer._limits == []
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_an_error_that_lands_before_a_limit_can_block_the_signal_does_not_strand_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal in the few bytecodes before ``_leave`` blocks aborts it: it runs again, and the error goes on."""
    real = signal.pthread_sigmask
    state = {"failed": False}

    def mask(how: int, signals: set[int]) -> set[int]:
        if how == signal.SIG_BLOCK and not state["failed"] and len(timer._limits) == 2:
            state["failed"] = True
            raise Outer
        return real(how, signals)

    with pytest.raises(Outer):
        with limit_until(time.monotonic() + 5, Outer):
            with limit_until(time.monotonic() + 5, Inner):
                monkeypatch.setattr(signal, "pthread_sigmask", mask)
    monkeypatch.undo()
    assert state["failed"]
    assert timer._limits == []
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_limits_are_never_left_behind_by_a_signal_at_any_point() -> None:
    """The outer limit fires at a random point of a loop of inner blocks; none stays listed, and nothing raises later.

    The bodies are long enough for the timer to expire in them; the reviewer's probe found four limits left behind
    in 3 000 trials when the bookkeeping could be interrupted.
    """
    rng = random.Random(3)
    for _ in range(400):
        began = time.monotonic()
        with pytest.raises(Outer):
            with limit_until(began + 0.003 + rng.random() * 0.002, Outer):
                while time.monotonic() < began + 1.0:
                    with limit_until(time.monotonic() + 5, Inner):
                        end = time.perf_counter() + 30e-6
                        while time.perf_counter() < end:
                            pass
        assert timer._limits == []
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_a_signal_during_a_shielded_block_is_delivered_when_it_ends() -> None:
    """Inside the shield nothing is raised; the deadline that passed meanwhile fires when the signal is let through."""
    began = time.monotonic()
    inside = False
    with pytest.raises(Inner):
        with limit_until(began + 0.1, Inner):
            with timer.shielded():
                time.sleep(0.2)
                inside = True
    assert inside
    assert 0.19 < time.monotonic() - began < 0.5
    assert timer._limits == []


def test_a_signal_handled_inside_the_call_that_blocks_it_does_not_leave_it_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler that raises while ``pthread_sigmask`` is blocking must not leave the signal blocked.

    ``pthread_sigmask`` runs the handlers of signals that came meanwhile after it has set the mask, so a raising
    handler made the call fail with the signal blocked and the old mask lost. In the container run this left
    ``SIGALRM`` blocked for the rest of the process, after which no limit ever fired.
    """
    real = signal.pthread_sigmask
    pending = {"failures": 0}

    def mask(how: int, signals: set[int]) -> set[int]:
        result = real(how, signals)
        if how == signal.SIG_BLOCK and signal.SIGALRM in set(signals) and pending["failures"]:
            pending["failures"] -= 1
            raise Outer
        return result

    monkeypatch.setattr(signal, "pthread_sigmask", mask)
    pending["failures"] = 1
    with pytest.raises(Outer):
        with limit_until(time.monotonic() + 5, Inner):
            pytest.fail("not entered")
    assert signal.SIGALRM not in real(signal.SIG_BLOCK, [])
    pending["failures"] = 1
    with pytest.raises(Outer):
        with timer.shielded():
            pytest.fail("not entered")
    assert signal.SIGALRM not in real(signal.SIG_BLOCK, [])
    monkeypatch.undo()
    assert timer._limits == []
    began = time.monotonic()
    with pytest.raises(Inner):
        with limit_until(began + 0.1, Inner):
            spin()
    assert time.monotonic() - began < 0.5


def test_a_limit_is_not_raised_inside_a_finalizer_where_it_would_be_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deadline that passes while a ``__del__`` runs is raised just after it, in the code that was running.

    An exception raised in a finalizer is printed and dropped, so the timeout would have been lost, and a test run
    with warnings as errors failed on it now and then.
    """
    dropped: list[object] = []
    monkeypatch.setattr("sys.unraisablehook", lambda unraisable: dropped.append(unraisable.exc_value))

    class Slow:
        def __del__(self) -> None:
            spin(0.3)

    began = time.monotonic()
    with pytest.raises(Inner):
        with limit_until(began + 0.1, Inner):
            doomed = Slow()
            del doomed
            spin()
    monkeypatch.undo()
    assert dropped == []
    assert 0.29 < time.monotonic() - began < 1.0
    assert timer._limits == []
