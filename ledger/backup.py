"""Online backup of a ledger SQLite database.

Uses sqlite3's backup API (or, as a documented alternative, VACUUM INTO).
Never copies the live database file. The copy is written at a unique
``.<name>.*.partial`` name beside the destination, checked with ``PRAGMA
integrity_check``, and renamed only when the check returns ``ok`` and the
source path still names the file the copy was read from.
"""
from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .dbfile import checked_connect, precheck_files, verify_bound
from .paths import resolve_db_path
from .store import BUSY_TIMEOUT_MS, LedgerError, _connect_readonly
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
    resolved = resolve_db_path(path)
    conn = _connect_readonly(resolved)
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
    resolved = resolve_db_path(path)
    conn = _connect_readonly(resolved)
    try:
        versions = _recorded_versions(conn)
    finally:
        conn.close()
    _refuse_unknown_versions(versions, load_migrations())


_SIDE_FILE_SUFFIXES = ("-wal", "-shm", "-journal")


def file_identity(path: Path) -> tuple[int, int] | None:
    """The ``(device, inode)`` of the file at ``path``, or ``None`` when no file has that name.

    Read with ``lstat``, so a symbolic link is a file of its own and never
    redirects the check. Any error other than a missing name propagates: a
    caller refuses a destination it cannot prove distinct.
    """
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        return None
    return found.st_dev, found.st_ino


def _swap_ascii_case(name: str) -> str:
    return "".join(
        char.swapcase() if char.isascii() and char.isalpha() else char
        for char in name
    )


def _tells_case_apart(dest_path: Path) -> bool:
    """True only when the folder that would hold ``dest_path`` is shown to tell case apart.

    The folder is the nearest existing ancestor of ``dest_path`` (a backup
    creates the missing ones inside it). One of its entries is looked up
    again with the case of its ASCII letters swapped: when no file has the
    swapped name, or another file has it, the folder tells case apart. A
    folder with no entry to try, or any error, shows nothing, and the answer
    is False.
    """
    folder = dest_path.parent
    try:
        while True:
            try:
                found = os.lstat(folder)
                break
            except FileNotFoundError:
                if folder.parent == folder:
                    return False
                folder = folder.parent
        if not stat.S_ISDIR(found.st_mode):
            return False
        with os.scandir(folder) as entries:
            for entry in entries:
                swapped = _swap_ascii_case(entry.name)
                if swapped == entry.name:
                    continue
                original = file_identity(folder / entry.name)
                if original is None or file_identity(folder / swapped) == original:
                    return False
                # The swapped name is missing or another file. Read the entry
                # again: had it been removed meanwhile, the swapped name would
                # be missing on a folder that folds case too.
                return file_identity(folder / entry.name) == original
    except OSError:
        return False
    return False


def refuse_side_file_name(dest_path: Path, verb: str) -> None:
    """Refuse a destination named like a database side file, as its volume reads the name.

    SQLite removes a file on a side-file name when the last connection to
    that database closes, so the name is refused whether or not a file has
    it yet. The suffix compare ignores case unless the destination's folder
    is shown to tell case apart: where the volume folds case, ``-WAL`` names
    the ``-wal`` file, and where it does not, ``copy.db-WAL`` is an ordinary
    name. A folder that cannot be shown either way is treated as folding.
    """
    name = dest_path.name
    if name.endswith(_SIDE_FILE_SUFFIXES) or (
        name.casefold().endswith(_SIDE_FILE_SUFFIXES)
        and not _tells_case_apart(dest_path)
    ):
        raise LedgerError(
            f"{verb} target is named like a database side file "
            f"(-wal, -shm or -journal): {dest_path}"
        )


def _refuse_live_destination(source_path: Path, dest_path: Path) -> None:
    """Refuse a backup destination that is the live database or one of its side files.

    Decided by file identity, never by comparing spellings: a volume can
    read two spellings as one name (case, Unicode normalization), and a hard
    link is another name for the same file. A destination no file has yet
    cannot be the live file; the exact spelling of a live name still refuses
    here, and any other side-file name is left to ``refuse_side_file_name``.
    """
    live_files = (source_path,) + tuple(
        source_path.with_name(source_path.name + suffix)
        for suffix in _SIDE_FILE_SUFFIXES
    )
    if dest_path in live_files:
        raise LedgerError(f"backup target is the live database: {dest_path}")
    try:
        dest_identity = file_identity(dest_path)
        if dest_identity is None:
            return
        for live in live_files:
            if file_identity(live) == dest_identity:
                raise LedgerError(f"backup target is the live database: {dest_path}")
    except OSError as exc:
        raise LedgerError(
            f"backup target cannot be told apart from the live database: "
            f"{dest_path} ({exc})"
        ) from exc


def backup_db(db_path: Path | str, dest: Path | str) -> BackupResult:
    """Snapshot ``db_path`` into ``dest`` via the online backup API."""
    source_path = resolve_db_path(db_path)
    dest_path = resolve_db_path(dest)
    # Before the source precheck, which refuses a hard-linked live file for
    # its own reasons: the destination guard stands on its own.
    _refuse_live_destination(source_path, dest_path)
    if not precheck_files(source_path):
        raise LedgerError(f"database does not exist: {source_path}")
    # A file published on a name SQLite treats as a side file is removed by
    # a later last closer.
    refuse_side_file_name(dest_path, "backup")
    # A backup of a newer schema would later be read by this same older code.
    _refuse_newer_schema_before_connect(source_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    # A unique partial name, so two backups to one destination never unlink
    # or write each other's copy.
    handle, partial_name = tempfile.mkstemp(
        prefix=f".{dest_path.name}.", suffix=".partial", dir=dest_path.parent
    )
    partial = Path(partial_name)
    try:
        os.close(handle)
        source, _source_mode, source_proof = checked_connect(
            source_path,
            lambda: sqlite3.connect(
                str(source_path),
                timeout=BUSY_TIMEOUT_MS / 1000,
            ),
        )
        try:
            source.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            target, _target_mode, _target_proof = checked_connect(
                partial,
                lambda: sqlite3.connect(
                    str(partial), timeout=BUSY_TIMEOUT_MS / 1000
                ),
            )
            try:
                target.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
                source.backup(target, pages=BACKUP_PAGES_PER_STEP)
                target.execute("PRAGMA journal_mode = DELETE")
            finally:
                target.close()
            # The copy was read from the proved source file: publish it only
            # while the source path still names that file.
            verify_bound(source_proof)
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
