"""The race tests' child helper: a ready line read with a deadline, and no live child after."""
from __future__ import annotations

import os
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


def test_a_child_that_never_prints_fails_within_the_deadline_and_is_reaped():
    started = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match=r"no 'held' line within 2s"):
        with _start(NEVER_READY) as proc:
            read_ready_line(proc, "held", timeout=DEADLINE)
    elapsed = time.monotonic() - started
    _assert_reaped(proc)
    assert DEADLINE <= elapsed < DEADLINE + 10, elapsed


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
