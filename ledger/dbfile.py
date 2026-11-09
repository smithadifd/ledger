"""Bind SQLite opens to safe database files and side files.

The check asks SQLite which descriptors the new connection holds (see
``_sqlite_handle``) and compares each one's identity with the file at its
name. It never reads or counts the process's descriptor table, so what other
threads open, and SQLite's reuse of a descriptor it already holds, do not
change the verdict.
"""
from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path
from typing import Callable

from . import _sqlite_handle

Identity = tuple[int, int]
_SIDE_SUFFIXES = ("-wal", "-shm", "-journal")
_TEST_SEAM: Callable[[str, Path], None] | None = None


class UnsafeDatabaseFile(ValueError):
    """A database name or opened descriptor does not satisfy the file invariant."""

    def __init__(self, code: str, detail: str, path: Path):
        self.code = code
        self.path = path
        super().__init__(f"{code}: {detail}: {path}")


def _identity(value: os.stat_result) -> Identity:
    return value.st_dev, value.st_ino


def _unsafe(code: str, detail: str, path: Path, cause: BaseException | None = None):
    error = UnsafeDatabaseFile(code, detail, path)
    if cause is None:
        raise error
    raise error from cause


def _lstat(path: Path, *, absent_ok: bool) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError as exc:
        if absent_ok:
            return None
        _unsafe("db_stat_failed", "file disappeared during descriptor check", path, exc)
    except OSError as exc:
        _unsafe("db_stat_failed", str(exc), path, exc)


def _fstat(fd: int, path: Path) -> os.stat_result:
    try:
        return os.fstat(fd)
    except OSError as exc:
        _unsafe("db_stat_failed", f"fstat({fd}): {exc}", path, exc)


def _check_main(path: Path, *, absent_ok: bool) -> os.stat_result | None:
    found = _lstat(path, absent_ok=absent_ok)
    if found is None:
        return None
    if stat.S_ISLNK(found.st_mode):
        _unsafe("db_symlink", "database path is a symbolic link", path)
    if not stat.S_ISREG(found.st_mode):
        _unsafe("db_not_regular", "database path is not a regular file", path)
    if found.st_nlink != 1:
        _unsafe("db_hardlinked", f"database file has {found.st_nlink} links", path)
    return found


def _check_sibling(path: Path) -> os.stat_result | None:
    found = _lstat(path, absent_ok=True)
    if found is None:
        return None
    suffix = path.name
    if stat.S_ISLNK(found.st_mode):
        detail = f"{suffix} is a symbolic link"
    elif not stat.S_ISREG(found.st_mode):
        detail = f"{suffix} is not a regular file"
    elif found.st_nlink > 1:
        detail = f"{suffix} has {found.st_nlink} links"
    elif found.st_nlink == 0:
        return None
    else:
        return found
    _unsafe("db_sibling_unsafe", detail, path)


def _precheck(path: Path) -> os.stat_result | None:
    """Check names without opening them; return the main file's ``lstat``, if any."""
    main = _check_main(path, absent_ok=True)
    for suffix in _SIDE_SUFFIXES:
        _check_sibling(Path(f"{path}{suffix}"))
    return main


def precheck_files(path: Path) -> bool:
    """Check names without opening them; return whether the main file exists."""
    return _precheck(path) is not None


def precheck_siblings(path: Path) -> None:
    for suffix in _SIDE_SUFFIXES:
        _check_sibling(Path(f"{path}{suffix}"))


def _seam(stage: str, path: Path) -> None:
    if _TEST_SEAM is not None:
        _TEST_SEAM(stage, path)


def _bind(path: Path, conn: sqlite3.Connection, captured: list[int]) -> int:
    """Return the one handle opened on this thread, once it is proven to be ``conn``'s."""
    if len(captured) != 1:
        _unsafe(
            "db_fd_unknown", f"connect opened {len(captured)} connections, expected 1", path
        )
    handle = captured[0]
    category = _sqlite_handle.LIMIT_VARIABLE_NUMBER
    original = conn.getlimit(category)
    probe = original - 1 if original > 1 else original + 1
    conn.setlimit(category, probe)
    try:
        applied = conn.getlimit(category)
        seen = _sqlite_handle.limit(handle, category)
    finally:
        conn.setlimit(category, original)
    if applied != probe or seen != probe:
        _unsafe(
            "db_fd_unattributable",
            "the connection opened on this thread is not the one returned",
            path,
        )
    return handle


def _main_after_connect(path: Path) -> os.stat_result:
    named = _lstat(path, absent_ok=True)
    if named is None:
        _unsafe("db_swapped", "database path disappeared after connect", path)
    assert named is not None
    if stat.S_ISLNK(named.st_mode):
        _unsafe("db_symlink", "database path became a symbolic link", path)
    if not stat.S_ISREG(named.st_mode):
        _unsafe("db_not_regular", "database path is not a regular file", path)
    if named.st_nlink != 1:
        _unsafe("db_hardlinked", f"database file has {named.st_nlink} links", path)
    return named


def _verify_main(
    path: Path, handle: int, pre_id: Identity | None
) -> tuple[int, Identity]:
    """The connection's main descriptor is the file at ``path`` now, and at the precheck."""
    vfs = _sqlite_handle.vfs_name(handle)
    if not vfs.startswith("unix"):
        _unsafe("db_fd_unattributable", f"connection uses the {vfs!r} VFS, not unix", path)
    fd = _sqlite_handle.main_fd(handle)
    opened = _fstat(fd, path)
    named = _main_after_connect(path)
    if _identity(opened) != _identity(named):
        _unsafe("db_swapped", "opened descriptor is not the file at the database path", path)
    if pre_id is not None and _identity(opened) != pre_id:
        _unsafe(
            "db_swapped",
            "opened descriptor is not the file the precheck found at the database path",
            path,
        )
    if not stat.S_ISREG(opened.st_mode):
        _unsafe("db_not_regular", "opened descriptor is not a regular file", path)
    if opened.st_nlink != 1:
        _unsafe("db_hardlinked", f"opened database has {opened.st_nlink} links", path)
    return fd, _identity(opened)


def _verify_side_descriptor(path: Path, suffix: str, fd: int) -> None:
    """The connection's own ``suffix`` descriptor is the regular file at that name."""
    sibling = Path(f"{path}{suffix}")
    opened = _fstat(fd, sibling)
    named = _lstat(sibling, absent_ok=True)
    if named is None:
        detail = f"{suffix} is absent after the first read"
    elif stat.S_ISLNK(named.st_mode):
        detail = f"{suffix} is a symbolic link"
    elif not stat.S_ISREG(named.st_mode):
        detail = f"{suffix} is not a regular file"
    elif named.st_nlink != 1:
        detail = f"{suffix} has {named.st_nlink} links"
    elif _identity(opened) != _identity(named):
        detail = f"open {suffix} descriptor is not the file at {sibling.name}"
    elif not stat.S_ISREG(opened.st_mode):
        detail = f"open {suffix} is not a regular file"
    elif opened.st_nlink != 1:
        detail = f"open {suffix} has {opened.st_nlink} links"
    else:
        return
    _unsafe("db_sibling_unsafe", detail, sibling)


def _verify_siblings(
    path: Path,
    handle: int,
    main: tuple[int, Identity],
    mode: str,
    *,
    immutable: bool,
) -> None:
    main_fd, main_identity = main
    again = _sqlite_handle.main_fd(handle)
    if again != main_fd or _identity(_fstat(again, path)) != main_identity:
        _unsafe("db_fd_unknown", "main descriptor changed during first read", path)
    if immutable or mode != "wal":
        journal = _sqlite_handle.journal_fd(handle)
        if journal is not None:
            _verify_side_descriptor(path, "-journal", journal)
        return
    wal = _sqlite_handle.wal_fd(handle)
    if wal is None:
        _unsafe("db_sibling_unsafe", "-wal is not open after WAL read", Path(f"{path}-wal"))
    _verify_side_descriptor(path, "-wal", wal)
    shm = _sqlite_handle.shm_fd(handle)
    if shm is None:
        _unsafe(
            "db_sibling_unsafe", "-shm is not attached after WAL read", Path(f"{path}-shm")
        )
    _verify_side_descriptor(path, "-shm", shm)


def checked_connect(
    path: Path,
    opener: Callable[[], sqlite3.Connection],
    *,
    immutable: bool = False,
    set_wal: bool = False,
) -> tuple[sqlite3.Connection, str]:
    """Open and bind one SQLite connection before returning it to its caller."""
    conn: sqlite3.Connection | None = None
    try:
        found = _precheck(path)
        pre_id = None if found is None else _identity(found)
        _sqlite_handle.ensure_hook()
        _seam("before_connect", path)
        with _sqlite_handle.capturing() as captured:
            conn = opener()
        _seam("after_connect", path)
        handle = _bind(path, conn, captured)
        main = _verify_main(path, handle, pre_id)
        _seam("before_first_read", path)
        row = conn.execute("PRAGMA journal_mode").fetchone()
        _seam("after_first_read", path)
        mode = str(row[0]).lower()
        _verify_siblings(path, handle, main, mode, immutable=immutable)

        if set_wal and mode != "wal":
            _seam("before_wal_switch", path)
            precheck_siblings(path)
            row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
            mode = str(row[0]).lower()
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            _verify_siblings(path, handle, main, mode, immutable=immutable)
        return conn, mode
    except BaseException as exc:
        if conn is not None:
            conn.close()
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, (UnsafeDatabaseFile, sqlite3.Error)):
            raise
        if isinstance(exc, _sqlite_handle.Unattributable):
            _unsafe("db_fd_unattributable", str(exc), path, exc)
        if isinstance(exc, OSError):
            _unsafe("db_stat_failed", str(exc), path, exc)
        _unsafe("db_stat_failed", f"{type(exc).__name__}: {exc}", path, exc)
