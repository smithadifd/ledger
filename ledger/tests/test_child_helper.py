"""The race tests' child helper: a ready line read with a deadline, and no live child after."""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time

import pytest

from ledger.tests._child import read_ready_line, running

# Every fixture child ends with this comment, so a process list names any
# child left behind.
MARK = "  # child-helper fixture"
# A child that never prints its ready line.
NEVER_READY = "import time; time.sleep(600)"
# A child that prints the ready line and then outlives any test.
READY_THEN_SLEEP = "print('held', flush=True); import time; time.sleep(600)"
DEADLINE = 2.0
# Well above DEADLINE: only a read that has lost its deadline gets this far.
HANG_GUARD = 20
HUNG = f"the ready-line read did not return within {HANG_GUARD}s: the deadline is gone"
# An outer timer a test runner might arm for the whole run.
OUTER = 30.0
# What an outer timer that came due during the guard is re-armed with, so it
# still fires (a zero would disarm it).
MIN_REARM = 0.001


def _start(code: str):
    return running([sys.executable, "-c", code + MARK], stdin=subprocess.DEVNULL,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def _assert_reaped(proc: subprocess.Popen) -> None:
    try:
        assert proc.poll() is not None, "the child is still running after its test"
        assert _gone(proc.pid), "the child's pid is still live"
    finally:
        # Leave nothing behind even when the assertion fails.
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)


def test_the_ready_line_is_returned_when_the_child_prints_it():
    with _start(READY_THEN_SLEEP) as proc:
        assert read_ready_line(proc, "held", timeout=DEADLINE) == "held"
    _assert_reaped(proc)


def _rearm(handler, timer: tuple[float, float], started: float) -> None:
    """Give back a SIGALRM handler and real-time timer as they were at ``started``.

    ``timer`` is what ``setitimer`` returned when the guard took the timer
    over: the outer timer's remaining time and its interval. The outer timer
    gets its remaining time less what has passed since; one that came due
    meanwhile still fires, at once.
    """
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, handler)
    remaining, interval = timer
    if remaining > 0:
        left = remaining - (time.monotonic() - started)
        signal.setitimer(signal.ITIMER_REAL, max(left, MIN_REARM), interval)


@contextlib.contextmanager
def _hang_guard(seconds: float, message: str):
    """Fail the block by ``message`` if it runs past ``seconds``.

    It borrows SIGALRM and the real-time timer and gives both back as it found
    them, so a timer the test runner armed for the whole run is not lost.
    """
    hung: list[int] = []

    def on_alarm(signum, _frame):
        hung.append(signum)
        pytest.fail(message)

    started = time.monotonic()
    outer = signal.setitimer(signal.ITIMER_REAL, seconds)
    previous = signal.signal(signal.SIGALRM, on_alarm)
    try:
        yield
    finally:
        _rearm(previous, outer, started)
        if hung:
            # The alarm's failure lands inside any pytest.raises in the block,
            # which reports it as a regex mismatch; report it by its own name.
            pytest.fail(message)


def _never_prints_fails_within_the_deadline_and_is_reaped() -> None:
    # If the helper ever blocks without a deadline, the guard fails this test
    # by name instead of letting it hang the run.
    with _hang_guard(HANG_GUARD, HUNG):
        started = time.monotonic()
        with pytest.raises(pytest.fail.Exception, match=r"no 'held' line within 2s"):
            with _start(NEVER_READY) as proc:
                read_ready_line(proc, "held", timeout=DEADLINE)
        elapsed = time.monotonic() - started
    _assert_reaped(proc)
    assert DEADLINE <= elapsed < DEADLINE + 10, elapsed


def test_a_child_that_never_prints_fails_within_the_deadline_and_is_reaped():
    _never_prints_fails_within_the_deadline_and_is_reaped()


def _outer_timer(fired: list[int], seconds: float):
    """Stand in for a runner's timer: arm ``seconds`` with a recording handler.

    Returns the handler and what it replaced, so the test can give the real
    runner's handler and timer back with ``_rearm``.
    """

    def outer_handler(signum, _frame):
        fired.append(signum)

    started = time.monotonic()
    saved = signal.setitimer(signal.ITIMER_REAL, seconds)
    saved_handler = signal.signal(signal.SIGALRM, outer_handler)
    return outer_handler, saved_handler, saved, started


def test_the_hang_guard_leaves_an_outer_timer_armed_with_its_remaining_time():
    fired: list[int] = []
    outer_handler, saved_handler, saved, started = _outer_timer(fired, OUTER)
    try:
        _never_prints_fails_within_the_deadline_and_is_reaped()
        remaining, _interval = signal.getitimer(signal.ITIMER_REAL)
        elapsed = time.monotonic() - started
        assert signal.getsignal(signal.SIGALRM) is outer_handler, \
            "the guard did not give back the outer SIGALRM handler"
        assert remaining > 0, "the guard disarmed the outer timer"
        assert abs(remaining - (OUTER - elapsed)) < 1.0, (remaining, elapsed)
        assert not fired, "the outer timer fired early"
    finally:
        # Give the runner back whatever timer it had before this test.
        _rearm(saved_handler, saved, started)


def test_an_outer_timer_that_comes_due_during_the_guard_still_fires():
    fired: list[int] = []
    _handler, saved_handler, saved, started = _outer_timer(fired, 0.2)
    try:
        with _hang_guard(HANG_GUARD, HUNG):
            time.sleep(0.5)
            assert not fired, "the outer timer fired inside the guard"
        waited = time.monotonic() + 2
        while not fired and time.monotonic() < waited:
            time.sleep(0.01)
        assert fired == [signal.SIGALRM], "an outer timer that came due during the guard never fired"
    finally:
        _rearm(saved_handler, saved, started)


def test_a_child_that_exits_before_printing_fails_naming_the_line_and_is_reaped():
    with pytest.raises(pytest.fail.Exception,
                       match=r"closed its output before printing 'opened'"):
        with _start("pass") as proc:
            read_ready_line(proc, "opened", timeout=DEADLINE)
    _assert_reaped(proc)


def test_an_unexpected_ready_line_fails_and_the_child_is_still_killed():
    with pytest.raises(pytest.fail.Exception,
                       match=r"printed 'busy', expected 'held'"):
        with _start("print('busy', flush=True); import time; time.sleep(600)") as proc:
            read_ready_line(proc, "held", timeout=DEADLINE)
    _assert_reaped(proc)


def test_a_failure_later_in_the_block_still_kills_the_child():
    with pytest.raises(AssertionError, match="later failure"):
        with _start(READY_THEN_SLEEP) as proc:
            read_ready_line(proc, "held", timeout=DEADLINE)
            raise AssertionError("later failure")
    _assert_reaped(proc)
