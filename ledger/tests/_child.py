"""Child processes for race tests: a bounded ready-line read, and no live child after the test.

A race test starts its racing writer in a child process and waits for the
child's ready line before it lets the race run. A bare ``readline()`` blocks
until the child prints, so a child that stalls first hangs the whole run.
``read_ready_line`` waits on the pipe with a deadline and fails the test,
naming the line it expected, when that line does not come in time.
``running`` starts the child and, on every way out of its block (a pass, a
failed assertion, a timeout), kills the child if it is still alive and waits
for it, so no child outlives its test.
"""
from __future__ import annotations

import contextlib
import os
import selectors
import subprocess
import time
from collections.abc import Iterator
from typing import Any

import pytest

READY_TIMEOUT = 30.0
REAP_TIMEOUT = 30.0


def read_ready_line(proc: subprocess.Popen, expected: str,
                    timeout: float = READY_TIMEOUT) -> str:
    """Return the child's first output line, stripped, when it equals ``expected``.

    Fails the test when no full line arrives within ``timeout`` seconds, when
    the child closes its output first, or when the line is anything else. It
    reads one byte at a time, so nothing the child prints after the ready line
    is taken from the pipe.
    """
    stream = proc.stdout
    assert stream is not None, "start the child with stdout=subprocess.PIPE"
    fd = stream.fileno()
    deadline = time.monotonic() + timeout
    data = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while not data.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                pytest.fail(f"the child printed no {expected!r} line within {timeout:g}s"
                            f" (read so far: {bytes(data)!r})")
            chunk = os.read(fd, 1)
            if not chunk:
                pytest.fail(f"the child closed its output before printing {expected!r}"
                            f" (read so far: {bytes(data)!r})")
            data += chunk
    line = data.decode(getattr(stream, "encoding", None) or "utf-8", "replace").strip()
    if line != expected:
        pytest.fail(f"the child printed {line!r}, expected {expected!r}")
    return line


def reap(proc: subprocess.Popen, timeout: float = REAP_TIMEOUT) -> None:
    """Kill the child if it is still running, wait for it, and close its pipes."""
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=timeout)
    finally:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                # Closing stdin flushes it, which fails once the child is gone.
                with contextlib.suppress(OSError):
                    stream.close()


@contextlib.contextmanager
def running(args: Any, **popen_kwargs: Any) -> Iterator[subprocess.Popen]:
    """Start ``subprocess.Popen(args, **popen_kwargs)`` and reap the child on any exit."""
    proc = subprocess.Popen(args, **popen_kwargs)
    try:
        yield proc
    finally:
        reap(proc)
