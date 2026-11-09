"""Append-only hash-chained SQLite ledger store.

BEFORE UPDATE and BEFORE DELETE triggers refuse writes on the events table.
This is an accident guard, not a security boundary: anyone with filesystem
access can drop the triggers or replace the file. Events are validated at
ingestion; ``verify`` re-checks the hash chain and payloads.

The library is importable and has no CLI. Callers open a path, append
validated events, query current-state views, and verify the chain.
"""

from .dbfile import UnsafeDatabaseFile
from .paths import (
    SharedDatabaseError,
    SyncPathError,
    SyncPathWarning,
    resolve_db_path,
    resolve_system_paths,
)
from .schema import ValidationError, known_kinds
from .store import (
    ChainBroken,
    Event,
    IdempotencyConflict,
    Ledger,
    LedgerError,
    claim_database,
    open_ledger,
)

__all__ = [
    "ChainBroken",
    "Event",
    "IdempotencyConflict",
    "Ledger",
    "LedgerError",
    "SharedDatabaseError",
    "SyncPathError",
    "SyncPathWarning",
    "UnsafeDatabaseFile",
    "ValidationError",
    "claim_database",
    "known_kinds",
    "open_ledger",
    "resolve_db_path",
    "resolve_system_paths",
]
