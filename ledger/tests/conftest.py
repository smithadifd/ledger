"""Put ``skills/_lib`` on ``sys.path`` so ``import ledger`` works under pytest."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LIB = Path(__file__).resolve().parents[2]
_LIB_STR = str(_LIB)
if _LIB_STR not in sys.path:
    sys.path.insert(0, _LIB_STR)

_ACTOR_ENV = (
    "LEDGER_OPERATOR",
    "LEDGER_AGENT",
    "LEDGER_MODEL",
    "LEDGER_SESSION",
    "LEDGER_HARNESS",
)


@pytest.fixture(autouse=True)
def clear_inherited_actor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with no inherited actor environment.

    A runner whose shell exports LEDGER_OPERATOR (or the other actor
    variables) must not change what the suite stamps. A test that needs
    one of these variables sets it itself through monkeypatch.
    """
    for name in _ACTOR_ENV:
        monkeypatch.delenv(name, raising=False)
