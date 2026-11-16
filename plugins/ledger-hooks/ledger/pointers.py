"""The conversation-pointer sidecar for resume cards.

A sidecar is any executable that searches past conversations. It is
installed by naming it in ``LEDGER_POINTER_SIDECAR``; the card runs it with
the item id as its only argument (no shell) and reads a JSON array of
``{"ref": ..., "note": ...}`` objects from stdout. With no sidecar named,
the card has no conversation-pointers section. A sidecar that fails or
prints something else gives one pointer saying so, never a failed card.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Iterable, Mapping

from .card import PointerSource

ENV_NAME = "LEDGER_POINTER_SIDECAR"
TIMEOUT_SECONDS = 10


def _unavailable(reason: str) -> list[dict[str, Any]]:
    return [{"ref": "(conversation sidecar unavailable)", "note": reason}]


def run_sidecar(command: str, item_id: str) -> list[dict[str, Any]]:
    try:
        result = subprocess.run(
            [command, item_id],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _unavailable(type(exc).__name__)
    if result.returncode != 0:
        return _unavailable(f"exit {result.returncode}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return _unavailable("output is not JSON")
    if type(data) is not list or any(type(entry) is not dict for entry in data):
        return _unavailable("output is not a list of objects")
    return data


def sidecar_from_env(env: Mapping[str, str] | None = None) -> PointerSource | None:
    """The installed sidecar as a pointer source, or ``None``."""
    command = (os.environ if env is None else env).get(ENV_NAME) or ""
    if not command:
        return None

    def source(item_id: str) -> Iterable[Mapping[str, Any]]:
        return run_sidecar(command, item_id)

    return source


__all__ = ["ENV_NAME", "run_sidecar", "sidecar_from_env"]
