"""Online backup of a ledger SQLite database.

Uses sqlite3's backup API (or, as a documented alternative, VACUUM INTO).
Never copies the live database file. The copy is written as ``*.partial``,
checked with ``PRAGMA integrity_check``, and renamed only when the check
returns ``ok``.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .paths import resolve_db_path
from .store import BUSY_TIMEOUT_MS, LedgerError
from .store import (
    _recorded_versions,
    _refuse_newer_schema_before_connect,
    _refuse_unknown_versions,
    load_migrations,
)

BACKUP_PAGES_PER_STEP = 64


@dataclass(frozen=True)
class BackupResult:
    path: Path
    integrity: str


def integrity_check(path: Path) -> str:
    """Run ``PRAGMA integrity_check`` on ``path``; return the joined verdict."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    return "\n".join(str(row[0]) for row in rows)


def _refuse_newer_schema_in_copy(path: Path) -> None:
    """Refuse the copy itself when it records a version this reader does not package.

    The pre-check reads the source on its own connection, which closes before
    the copy starts, so a newer migration committed in between reaches the
    copy. Reading the copy checks exactly what would be published.
    """
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        versions = _recorded_versions(conn)
    finally:
        conn.close()
    _refuse_unknown_versions(versions, load_migrations())


def backup_db(db_path: Path | str, dest: Path | str) -> BackupResult:
    """Snapshot ``db_path`` into ``dest`` via the online backup API."""
    source_path = resolve_db_path(db_path)
    if not source_path.is_file():
        raise LedgerError(f"database does not exist: {source_path}")
    dest_path = resolve_db_path(dest)
    live_files = (
        source_path,
        source_path.with_name(source_path.name + "-wal"),
        source_path.with_name(source_path.name + "-shm"),
    )
    if dest_path in live_files:
        raise LedgerError(f"backup target is the live database: {dest_path}")
    # A backup of a newer schema would later be read by this same older code.
    _refuse_newer_schema_before_connect(source_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    partial = dest_path.with_name(dest_path.name + ".partial")
    partial.unlink(missing_ok=True)
    try:
        source = sqlite3.connect(
            str(source_path),
            timeout=BUSY_TIMEOUT_MS / 1000,
        )
        try:
            source.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            target = sqlite3.connect(str(partial), timeout=BUSY_TIMEOUT_MS / 1000)
            try:
                target.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
                source.backup(target, pages=BACKUP_PAGES_PER_STEP)
                target.execute("PRAGMA journal_mode = DELETE")
            finally:
                target.close()
        finally:
            source.close()
        verdict = integrity_check(partial)
        if verdict != "ok":
            lines = verdict.splitlines() or [verdict]
            more = f" (+{len(lines) - 1} more)" if len(lines) > 1 else ""
            raise LedgerError(f"backup failed integrity_check: {lines[0]}{more}")
        _refuse_newer_schema_in_copy(partial)
        partial.replace(dest_path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return BackupResult(path=dest_path, integrity="ok")
