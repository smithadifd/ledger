"""Bind SQLite opens to safe database files and side files."""
from __future__ import annotations

import errno
import os
import sqlite3
import stat
from pathlib import Path
from typing import Callable

Identity = tuple[int, int]
Snapshot = dict[int, Identity]
_SIDE_SUFFIXES = ("-wal", "-shm", "-journal")
_FD_ROOTS = (Path("/proc/self/fd"), Path("/dev/fd"))
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


def precheck_files(path: Path) -> bool:
    """Check names without opening them; return whether the main file exists."""
    main = _check_main(path, absent_ok=True)
    for suffix in _SIDE_SUFFIXES:
        _check_sibling(Path(f"{path}{suffix}"))
    return main is not None


def precheck_siblings(path: Path) -> None:
    for suffix in _SIDE_SUFFIXES:
        _check_sibling(Path(f"{path}{suffix}"))


def snapshot(path: Path) -> Snapshot:
    """Return the live process descriptor table, keyed by descriptor number."""
    root = next((candidate for candidate in _FD_ROOTS if candidate.is_dir()), None)
    if root is None:
        _unsafe("db_fd_listing_unavailable", "no descriptor table is available", path)
    try:
        names = os.listdir(root)
    except OSError as exc:
        _unsafe("db_fd_listing_unavailable", str(exc), path, exc)
    result: Snapshot = {}
    for name in names:
        if not name.isdecimal():
            continue
        fd = int(name)
        try:
            found = os.fstat(fd)
        except OSError as exc:
            if exc.errno == errno.EBADF:
                continue
            _unsafe("db_stat_failed", f"fstat({fd}): {exc}", path, exc)
        result[fd] = _identity(found)
    return result


def _changed(before: Snapshot, after: Snapshot) -> Snapshot:
    return {fd: identity for fd, identity in after.items() if before.get(fd) != identity}


def _seam(stage: str, path: Path) -> None:
    if _TEST_SEAM is not None:
        _TEST_SEAM(stage, path)


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


def _verify_main(path: Path, before: Snapshot) -> tuple[int, Identity]:
    after = snapshot(path)
    new = _changed(before, after)
    named: os.stat_result | None = None
    if len(new) == 1:
        fd, identity = next(iter(new.items()))
    elif not new:
        named = _main_after_connect(path)
        matches = [
            (fd, identity)
            for fd, identity in before.items()
            if identity == _identity(named)
        ]
        if not matches:
            _unsafe("db_fd_unknown", "connect opened 0 descriptors, expected 1", path)
        fd, identity = matches[0]
    else:
        _unsafe("db_fd_unknown", f"connect opened {len(new)} descriptors, expected 1", path)
    try:
        opened = os.fstat(fd)
    except OSError as exc:
        _unsafe("db_stat_failed", f"fstat({fd}): {exc}", path, exc)
    if not stat.S_ISREG(opened.st_mode):
        _unsafe("db_not_regular", "opened descriptor is not a regular file", path)
    if named is None:
        named = _main_after_connect(path)
    if _identity(opened) != _identity(named) or identity != _identity(opened):
        _unsafe("db_swapped", "opened descriptor is not the file at the database path", path)
    if opened.st_nlink != 1:
        _unsafe("db_hardlinked", f"opened database has {opened.st_nlink} links", path)
    return fd, identity


def _verify_siblings(
    path: Path,
    before: Snapshot,
    main: tuple[int, Identity],
    mode: str,
    *,
    immutable: bool,
) -> None:
    after = snapshot(path)
    main_fd, main_identity = main
    if after.get(main_fd) != main_identity:
        _unsafe("db_fd_unknown", "main descriptor changed during first read", path)
    new = _changed(before, after)
    new.pop(main_fd, None)
    if immutable or mode != "wal":
        if new:
            _unsafe("db_fd_unknown", f"first read opened {len(new)} unexpected descriptors", path)
        return

    expected: dict[str, Identity] = {}
    for suffix in ("-wal", "-shm"):
        sibling = Path(f"{path}{suffix}")
        found = _check_sibling(sibling)
        if found is None:
            _unsafe("db_sibling_unsafe", f"{suffix} is absent after WAL read", sibling)
        expected[suffix] = _identity(found)

    counts = {suffix: 0 for suffix in expected}
    for identity in new.values():
        matches = [suffix for suffix, wanted in expected.items() if identity == wanted]
        if len(matches) != 1:
            _unsafe("db_fd_unknown", "new descriptor does not uniquely match -wal or -shm", path)
        counts[matches[0]] += 1
        if counts[matches[0]] > 1:
            _unsafe("db_fd_unknown", f"multiple descriptors match {matches[0]}", path)

    already = set(before.values())
    for suffix, identity in expected.items():
        if counts[suffix] == 1:
            continue
        if identity not in already:
            _unsafe(
                "db_sibling_unsafe",
                f"{suffix} is neither newly opened nor already open in this process",
                Path(f"{path}{suffix}"),
            )


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
        precheck_files(path)
        before = snapshot(path)
        _seam("before_connect", path)
        try:
            conn = opener()
        except sqlite3.Error:
            raise
        _seam("after_connect", path)
        main = _verify_main(path, before)
        _seam("before_first_read", path)
        try:
            row = conn.execute("PRAGMA journal_mode").fetchone()
        except sqlite3.Error:
            raise
        _seam("after_first_read", path)
        mode = str(row[0]).lower()
        _verify_siblings(path, before, main, mode, immutable=immutable)

        if set_wal and mode != "wal":
            _seam("before_wal_switch", path)
            precheck_siblings(path)
            try:
                row = conn.execute("PRAGMA journal_mode = WAL").fetchone()
                mode = str(row[0]).lower()
                conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            except sqlite3.Error:
                raise
            _verify_siblings(path, before, main, mode, immutable=immutable)
        return conn, mode
    except BaseException as exc:
        if conn is not None:
            conn.close()
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, (UnsafeDatabaseFile, sqlite3.Error)):
            raise
        if isinstance(exc, OSError):
            _unsafe("db_stat_failed", str(exc), path, exc)
        _unsafe("db_stat_failed", f"{type(exc).__name__}: {exc}", path, exc)
