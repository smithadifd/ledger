"""Put ``skills/_lib`` on ``sys.path`` so ``import ledger`` works under pytest."""
from __future__ import annotations

import sys
from pathlib import Path

_LIB = Path(__file__).resolve().parents[2]
_LIB_STR = str(_LIB)
if _LIB_STR not in sys.path:
    sys.path.insert(0, _LIB_STR)
