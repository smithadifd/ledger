"""Resolve a ledger DB path and refuse synced roots.

The database, WAL and SHM files must not sit under Syncthing, iCloud,
Dropbox, or OneDrive / File Provider sync roots. Callers pass an explicit
path; this module does not pick a default.

Each system (for example a work ledger and a home ledger) keeps its own
database and never shares data with another: ``resolve_system_paths``
refuses two systems that resolve to the same file, and the claim stored in
the database itself (``store.claim_database``) refuses to install a system on
a database another system already claimed.
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path
from typing import Mapping

SUPPORTED_LOCATION = "~/.local/share/ledger"


class SyncPathError(ValueError):
    """The resolved database path sits under a synced root."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class SyncPathWarning(UserWarning):
    """The resolved database path sits under a potentially synced location."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _is_same(path1: Path, path2: Path) -> bool:
    if path1 == path2:
        return True
    try:
        return os.path.samefile(path1, path2)
    except OSError:
        return False


def _cloud_storage_provider(path: Path) -> str | None:
    try:
        cloud_root = (Path.home() / "Library" / "CloudStorage").resolve()
    except Exception:
        return None

    for ancestor in (path, *path.parents):
        if _is_same(ancestor, cloud_root):
            rel = path.relative_to(ancestor)
            if rel.parts:
                return rel.parts[0]
            return "CloudStorage"
    return None


def _synced_reason(path: Path) -> str | None:
    provider = _cloud_storage_provider(path)
    if provider is not None:
        return f"File Provider ({provider}); use supported location {SUPPORTED_LOCATION}"
    for part in path.parts:
        if part == "Mobile Documents":
            return "iCloud (Mobile Documents)"
        if part.lower() == "dropbox":
            return "Dropbox"
    for ancestor in (path, *path.parents):
        if (ancestor / ".stfolder").exists():
            return "Syncthing (.stfolder)"
        if (ancestor / ".dropbox").exists():
            return "Dropbox"
    return None


def _synced_warning(path: Path) -> str | None:
    try:
        home = Path.home().resolve()
    except Exception:
        return None

    desktop = home / "Desktop"
    documents = home / "Documents"

    for ancestor in (path, *path.parents):
        if _is_same(ancestor, desktop):
            return (
                "path sits under ~/Desktop which may be synced by iCloud Desktop & Documents; "
                f"risk of database corruption or sync conflicts; use supported location {SUPPORTED_LOCATION}"
            )
        if _is_same(ancestor, documents):
            return (
                "path sits under ~/Documents which may be synced by iCloud Desktop & Documents; "
                f"risk of database corruption or sync conflicts; use supported location {SUPPORTED_LOCATION}"
            )
    return None


def resolve_db_path(path: Path | str) -> Path:
    """Expand ``~``, make absolute, and refuse a synced root."""
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = Path.cwd() / resolved
    resolved = resolved.resolve()
    reason = _synced_reason(resolved)
    if reason is not None:
        raise SyncPathError(f"refusing synced root ({reason}): {resolved}")
    warning_reason = _synced_warning(resolved)
    if warning_reason is not None:
        warnings.warn(
            f"potential synced path ({warning_reason}): {resolved}",
            SyncPathWarning,
            stacklevel=2,
        )
    return resolved


class SharedDatabaseError(ValueError):
    """Two systems resolve to one ledger database."""


def resolve_system_paths(systems: Mapping[str, Path | str]) -> dict[str, Path]:
    """Resolve each system's database and refuse any two that share a file.

    Paths are compared after ``~`` expansion and symlink resolution, so a
    link or a relative spelling of another system's database is refused too.
    """
    resolved: dict[str, Path] = {}
    owners: dict[Path, str] = {}
    for system, path in systems.items():
        db = resolve_db_path(path)
        if db in owners:
            raise SharedDatabaseError(
                f"systems {owners[db]!r} and {system!r} share one database: {db}"
            )
        owners[db] = system
        resolved[system] = db
    return resolved
