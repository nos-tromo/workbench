"""The job's one timer: nested limits on a single real-time timer, which never clobber each other."""

import os
import random
import signal
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from types import CodeType, FrameType
from typing import Any

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
    assert 0.25 < time.monotonic() - began < 3.0


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
            assert 0.25 < time.monotonic() - began
            spin()
    assert 0.95 < time.monotonic() - began < 4.0


def test_an_inner_limit_that_ends_in_time_gives_the_timer_back_to_the_outer_one() -> None:
    """Leaving the inner block arms the timer for the outer deadline again."""
    began = time.monotonic()
    with pytest.raises(Outer):
        with limit_until(began + 2.0, Outer):
            with limit_until(began + 5.0, Inner):
                time.sleep(0.01)
            now = time.monotonic()
            remaining = signal.getitimer(signal.ITIMER_REAL)[0]
            assert 0 < remaining <= began + 2.0 - now + 0.01
            spin()
    assert 1.95 < time.monotonic() - began < 4.5


def test_a_longer_inner_limit_does_not_postpone_the_outer_one() -> None:
    """The outer deadline is the earlier one: its error is raised, from inside the inner block."""
    began = time.monotonic()
    with pytest.raises(Outer):
        with limit_until(began + 0.3, Outer):
            with limit_until(began + 5.0, Inner):
                spin()
    assert 0.25 < time.monotonic() - began < 3.0


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
    assert time.monotonic() - began < 4.0


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
    microseconds away was pushed back by every enter and every exit: a 5 us body never fired within two seconds,
    a 400 us one only after 1.84 s. The bound is wide enough for a loaded machine and well short of either.
    """
    began = time.monotonic()
    with pytest.raises(Outer):
        with limit_until(began + 0.05, Outer):
            while time.monotonic() < began + 2.0:
                with limit_until(time.monotonic() + 10, Inner):
                    end = time.perf_counter() + spin_us / 1e6
                    while time.perf_counter() < end:
                        pass
    assert 0.05 <= time.monotonic() - began < 0.5
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
    assert time.monotonic() - woke[0] < 0.5
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
    assert 0.19 < time.monotonic() - began < 2.0
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
    assert time.monotonic() - began < 3.0


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
    assert 0.29 < time.monotonic() - began < 3.0
    assert timer._limits == []


def test_a_second_limit_that_is_due_too_fires_even_if_the_first_error_is_swallowed() -> None:
    """Both deadlines pass while the signal cannot be handled (one long call into C, here a shield).

    The earlier one's error is raised first and a library swallows it; the other one used to be left with the
    timer disarmed, so it never fired inside the block (the reviewer's ``swallow.py``). It fires at once now.
    """
    began = time.monotonic()
    reached: list[str] = []
    with pytest.raises(Outer):
        with limit_until(began + 0.05, Outer):
            with limit_until(began + 0.03, Inner):
                try:
                    with timer.shielded():
                        time.sleep(0.1)
                except Inner:
                    reached.append("inner swallowed")
                spin(1.5)
                reached.append("spun to the end")
    assert reached == ["inner swallowed"]
    assert time.monotonic() - began < 1.0
    assert timer._limits == []
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_a_signal_handled_inside_the_handler_waits_until_it_has_raised() -> None:
    """Python runs handlers between any two bytecodes, the handler's own too: a nested one must not raise first.

    It would replace the error the running handler is about to raise, so that one limit's error is never seen.
    """
    returned: list[bool] = []
    with pytest.raises(Outer):
        with limit_until(time.monotonic() + 5, Outer):
            timer._firing = True  # as if the handler were running
            try:
                timer._limits[0].deadline = time.monotonic() - 1
                timer._fire(signal.SIGALRM, None)
                returned.append(True)  # it raised nothing: the running handler raises first
                assert signal.getitimer(signal.ITIMER_REAL)[0] <= 0.001  # and it comes back in a moment
            finally:
                timer._firing = False
            spin()
    assert returned == [True]
    assert timer._limits == []


# --- a meter: a budget spent only while it runs -------------------------------------------------------------------


class Spent(TimeLimit):
    """The error of a meter's limit."""


def run(meter: timer.Meter, seconds: float) -> None:
    """Run the meter for about ``seconds`` of pure Python, the way a search matches a piece of text."""
    if meter.spent:
        raise Spent
    meter.start()
    try:
        spin(seconds)
    finally:
        meter.stop()


def test_a_meter_is_spent_only_while_it_runs() -> None:
    """Half a second of waiting with the meter stopped costs nothing; running it for its 0.2 s raises, once."""
    meter = timer.Meter(0.2)
    began = time.monotonic()
    with pytest.raises(Spent):
        with timer.limit_spending(meter, Spent):
            for _ in range(10):
                time.sleep(0.05)
                run(meter, 0.001)
            assert 0.1 < meter.left < 0.2
            run(meter, 5)
    assert 0.65 < time.monotonic() - began < 3.0
    assert meter.spent
    assert meter.until is None
    assert timer._limits == []
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_a_meter_run_in_many_short_pieces_runs_out_at_its_total() -> None:
    """Thousands of short runs with gaps between: the budget is their sum, and the gaps are not charged."""
    meter = timer.Meter(0.1)
    ran = 0.0
    began = time.monotonic()
    with pytest.raises(Spent):
        with timer.limit_spending(meter, Spent):
            while time.monotonic() < began + 5:
                start = time.monotonic()
                try:
                    run(meter, 50e-6)
                finally:
                    ran += time.monotonic() - start
                spin(50e-6)
    assert 0.095 <= ran < 0.2
    assert time.monotonic() - began > 0.15
    assert meter.spent


def test_running_a_meter_makes_no_system_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Starting and stopping it reads the clock only: the timer and the mask are not touched for each run."""
    meter = timer.Meter(5.0)
    calls: list[str] = []

    def counted(name: str) -> Callable[..., Any]:
        original = getattr(signal, name)

        def call(*args: Any) -> Any:
            calls.append(name)
            return original(*args)

        return call

    with timer.limit_spending(meter, Spent):
        for name in ("setitimer", "getitimer", "pthread_sigmask", "getsignal", "signal"):
            monkeypatch.setattr(signal, name, counted(name))
        for _ in range(20_000):
            meter.start()
            meter.stop()
        monkeypatch.undo()
    assert calls == []
    assert 4.9 < meter.left < 5.0


def test_a_meter_and_deadlines_nest_in_either_order() -> None:
    """A deadline inside a meter's limit fires while the meter is stopped; the meter fires later, at its total."""
    meter = timer.Meter(0.3)
    began = time.monotonic()
    with pytest.raises(Spent):
        with timer.limit_spending(meter, Spent):
            with pytest.raises(Inner):
                with limit_until(began + 0.1, Inner):
                    spin()
            assert meter.left == 0.3
            with limit_until(began + 30, Outer):
                run(meter, 5)
    assert 0.35 < time.monotonic() - began < 3.0
    assert timer._limits == []
    meter = timer.Meter(5.0)
    with pytest.raises(Outer):
        with limit_until(time.monotonic() + 0.2, Outer):
            with timer.limit_spending(meter, Spent):
                run(meter, 5)
    assert 4.5 < meter.left < 5.0
    assert timer._limits == []
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_a_spent_meter_raises_on_entering() -> None:
    """Nothing runs on a budget that is gone."""
    meter = timer.Meter(0.0)
    with pytest.raises(Spent):
        with timer.limit_spending(meter, Spent):
            pytest.fail("not entered")
    assert timer._limits == []


@contextmanager
def handled_at(code: CodeType, at: int, steps: list[int]) -> Iterator[None]:
    """Handle ``SIGALRM`` in ``code`` before its step number ``at``, as CPython does when the signal comes then.

    The steps are the function's start (0) and each instruction it runs, in order; their offsets are appended to
    ``steps``. CPython itself looks for signals at fewer places (a function's start, after a call, a jump back),
    all of them among these. With ``at`` not among the steps run, nothing is handled.
    """
    monitoring = sys.monitoring
    tool = next((t for t in (3, 4) if monitoring.get_tool(t) is None), None)
    if tool is None:
        pytest.skip("no free sys.monitoring tool id")

    def step(_code: CodeType, offset: int) -> None:
        steps.append(offset)
        if len(steps) == at + 1:
            timer._fire(signal.SIGALRM, sys._getframe(1))

    events = monitoring.events.PY_START | monitoring.events.INSTRUCTION
    monitoring.use_tool_id(tool, "test_timer")
    try:
        monitoring.register_callback(tool, monitoring.events.PY_START, step)
        monitoring.register_callback(tool, monitoring.events.INSTRUCTION, step)
        monitoring.set_local_events(tool, code, events)
        yield
    finally:
        monitoring.set_local_events(tool, code, 0)
        monitoring.register_callback(tool, monitoring.events.PY_START, None)
        monitoring.register_callback(tool, monitoring.events.INSTRUCTION, None)
        monitoring.free_tool_id(tool)


@pytest.mark.parametrize("method", ["start", "stop"])
def test_a_deadline_that_lands_anywhere_in_starting_or_stopping_a_meter_leaves_it_stopped_and_charged(
    method: str,
) -> None:
    """A deadline's error lands as the method begins, then before each of its instructions in turn.

    Among them are the two places a per-file deadline was found to land during a short match: as ``stop()``
    begins, and just after its clock read. Each time the meter is stopped afterwards and charged for what it ran,
    and reading that goes on for three times its budget, with the meter stopped, is not cut off by its limit.
    It used to be left running with its end in the past or near; the next file's reading then ran it out.

    The shield is entered before the deadline's limit, so a busy machine cannot deliver the signal before the
    sweep's own landing; the bounds on what is left hold however long a loaded machine takes to run the meter.
    """
    code = timer.Meter.start.__code__ if method == "start" else timer.Meter.stop.__code__
    path: list[int] = []
    with handled_at(code, -1, path):
        run(timer.Meter(1.0), 0.0)
    assert len(path) >= 8
    for at in range(len(path)):
        meter = timer.Meter(0.015)
        steps: list[int] = []
        with timer.limit_spending(meter, Spent):
            with pytest.raises(Inner):
                with timer.shielded(), limit_until(time.monotonic() + 0.01, Inner):
                    time.sleep(0.011)  # the deadline passes; its signal waits for the end of the shield
                    with handled_at(code, at, steps):
                        run(meter, 0.003)
            assert steps == path[: at + 1]
            assert meter.until is None
            # stop: the 3 ms run is charged; start: the error lands before the run, so at most the start is
            assert meter.left <= (0.012 if method == "stop" else 0.015)
            left = meter.left
            meter.stop()  # what the search does at the end of every file: a stopped meter stays as it is
            spin(0.05)
            assert meter.left == left
        assert timer._limits == []
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_deadlines_that_land_among_short_runs_of_a_meter_never_leave_it_running() -> None:
    """A deadline lands wherever CPython handles its signal in a loop of runs a few microseconds long, 300 times.

    The meter is stopped after every one. Before, about one landing in five or six left it running.
    """
    rng = random.Random(5)
    meter = timer.Meter(60.0)
    running = 0
    with timer.limit_spending(meter, Spent):
        for _ in range(300):
            began = time.monotonic()
            with suppress(Inner), limit_until(began + 0.0005 + rng.random() * 0.002, Inner):
                while time.monotonic() < began + 1.0:
                    run(meter, 0.0)
                pytest.fail("the deadline did not land")
            if meter.until is not None:
                running += 1
                meter.stop()
    assert running == 0
    assert 59 < meter.left < 60
    assert timer._limits == []
