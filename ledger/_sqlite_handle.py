"""Ask SQLite which descriptors one connection holds.

``dbfile.checked_connect`` binds a new connection to the files at its names by
asking SQLite, never by reading the process's descriptor table. A process-wide
``sqlite3_auto_extension`` hook records each new ``sqlite3*`` handle in a list
armed on the connecting thread only, so another thread's open is never counted
as this connection's. The main, WAL and journal file objects come from
``sqlite3_file_control``. The descriptor inside each, and the ``-shm``
descriptor of the shared-memory node the main file is attached to, are read
from SQLite's ``os_unix.c`` structs, which are not public API. Every such read
is anchored by a pointer equality before its descriptor is used, and a failed
anchor raises ``Unattributable`` instead of reading on:

- main and WAL (and an open rollback journal) ``unixFile.h``: the object's
  ``zPath`` pointer must equal the pointer SQLite's public API returns for that
  file's name (SQLite stores the pager's name pointer, not a copy);
- ``unixShmNode.hShm``: the node's ``pInode`` must equal the main
  ``unixFile.pInode``. The node's stored name is never read: the node is shared
  by every connection on the inode in this process, so its name is the spelling
  of whichever connection opened it first.

The ``-shm`` read follows two stored pointers before its anchor runs, so a
wrong struct layout there can crash the process instead of refusing; it runs
only after the main anchor has confirmed the ``unixFile`` layout.
"""
from __future__ import annotations

import atexit
import ctypes
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

SQLITE_OK = 0
FCNTL_FILE_POINTER = 7
FCNTL_VFS_POINTER = 27
FCNTL_JOURNAL_POINTER = 28
LIMIT_VARIABLE_NUMBER = 9


# The leading fields of os_unix.c's unixFile, unixShm and unixShmNode, with the
# C types os_unix.c declares, so each offset follows the platform's ABI: on a
# 32-bit ABI eFileLock, ctrlFlags and lastErrno pack into 8 bytes after h, and
# zPath is at 32, not at seven pointers. Pointer fields are c_void_p so a read
# yields the raw address. Only the offsets are used; nothing is read through
# these classes.
class _UnixFile(ctypes.Structure):
    _fields_ = [
        ("pMethod", ctypes.c_void_p),
        ("pVfs", ctypes.c_void_p),
        ("pInode", ctypes.c_void_p),
        ("h", ctypes.c_int),
        ("eFileLock", ctypes.c_ubyte),
        ("ctrlFlags", ctypes.c_ushort),
        ("lastErrno", ctypes.c_int),
        ("lockingContext", ctypes.c_void_p),
        ("pPreallocatedUnused", ctypes.c_void_p),
        ("zPath", ctypes.c_void_p),
        ("pShm", ctypes.c_void_p),
    ]


class _UnixShm(ctypes.Structure):
    _fields_ = [("pShmNode", ctypes.c_void_p)]


class _UnixShmNode(ctypes.Structure):
    _fields_ = [
        ("pInode", ctypes.c_void_p),
        ("pShmMutex", ctypes.c_void_p),
        ("zFilename", ctypes.c_void_p),
        ("hShm", ctypes.c_int),
    ]


_OFF_PINODE = _UnixFile.pInode.offset
_OFF_H = _UnixFile.h.offset
_OFF_ZPATH = _UnixFile.zPath.offset
_OFF_PSHM = _UnixFile.pShm.offset
_SHM_NODE = _UnixShm.pShmNode.offset
_NODE_PINODE = _UnixShmNode.pInode.offset
_NODE_HSHM = _UnixShmNode.hShm.offset
# A file object smaller than the unixFile prefix cannot hold the fields read.
_MIN_OS_FILE = ctypes.sizeof(_UnixFile)


class Unattributable(Exception):
    """The connection's handle or one of its descriptors cannot be attributed."""


class _VfsHead(ctypes.Structure):
    """The leading public fields of ``sqlite3_vfs``."""

    _fields_ = [
        ("iVersion", ctypes.c_int),
        ("szOsFile", ctypes.c_int),
        ("mxPathname", ctypes.c_int),
        ("pNext", ctypes.c_void_p),
        ("zName", ctypes.c_char_p),
    ]


_SIGNATURES = {
    "sqlite3_libversion": ([], ctypes.c_char_p),
    "sqlite3_auto_extension": ([ctypes.c_void_p], ctypes.c_int),
    "sqlite3_cancel_auto_extension": ([ctypes.c_void_p], ctypes.c_int),
    "sqlite3_file_control": (
        [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p],
        ctypes.c_int,
    ),
    "sqlite3_limit": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_int),
    "sqlite3_db_filename": ([ctypes.c_void_p, ctypes.c_char_p], ctypes.c_void_p),
    "sqlite3_filename_wal": ([ctypes.c_void_p], ctypes.c_void_p),
    "sqlite3_filename_journal": ([ctypes.c_void_p], ctypes.c_void_p),
}

_ENTRY = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_local = threading.local()


def _entry(db, _errmsg, _api) -> int:
    """Record a new handle on the armed thread. Never calls SQLite, never raises."""
    try:
        captured = getattr(_local, "captured", None)
        if captured is not None:
            captured.append(db)
    except BaseException:  # a lost handle refuses the connect; never unwind into SQLite
        pass
    return SQLITE_OK


# A module global, so the callback SQLite holds is never collected.
_CALLBACK = _ENTRY(_entry)


@dataclass(frozen=True)
class _Hook:
    lib: ctypes.CDLL | None
    error: str | None


_install_lock = threading.Lock()
_hook: _Hook | None = None


# The usual cause of a failed load, and the remedy, appended to the refusal text.
# No final full stop: the refusal appends ": <path>" after this detail.
_NOT_FOUND_HINT = (
    "The usual cause is a Python build from python-build-standalone (used by "
    "uv-managed Python and some other installer Pythons), which links SQLite "
    "statically and hides its C functions. Use a Python whose sqlite3 module "
    "links a shared libsqlite3, such as a distro build"
)


def _library_names() -> Iterator[str | None]:
    import _sqlite3

    name = getattr(_sqlite3, "__file__", None)
    if name:
        yield name
    yield None


def _load_library() -> ctypes.CDLL:
    """Resolve SQLite's C API: the ``_sqlite3`` module's library, else the process."""
    errors: list[str] = []
    for name in _library_names():
        try:
            lib = ctypes.CDLL(name)
            for symbol, (argtypes, restype) in _SIGNATURES.items():
                function = getattr(lib, symbol)
                function.argtypes = argtypes
                function.restype = restype
        except (OSError, AttributeError) as exc:
            errors.append(f"{name or 'process'}: {exc}")
            continue
        return lib
    raise OSError("SQLite C API not found (" + "; ".join(errors) + "). " + _NOT_FOUND_HINT)


def _install() -> _Hook:
    try:
        lib = _load_library()
    except OSError as exc:
        return _Hook(None, f"SQLite library unavailable: {exc}")
    version = (lib.sqlite3_libversion() or b"").decode("ascii", "replace")
    if version != sqlite3.sqlite_version:
        return _Hook(
            None,
            f"resolved SQLite {version}, but the sqlite3 module uses {sqlite3.sqlite_version}",
        )
    pointer = ctypes.cast(_CALLBACK, ctypes.c_void_p)
    rc = lib.sqlite3_auto_extension(pointer)
    if rc != SQLITE_OK:
        return _Hook(None, f"sqlite3_auto_extension returned {rc}")
    # Cancelled at exit, before interpreter teardown frees the callback or the
    # interpreter that owns it (a subinterpreter runs this at its destruction):
    # SQLite must never call a freed callback. Exit handlers run last-registered
    # first, so one registered before this install opens no ledger: it finds no
    # capture and is refused.
    atexit.register(lib.sqlite3_cancel_auto_extension, pointer)
    failure: str | None
    try:
        with capturing() as captured:
            probe = sqlite3.connect(":memory:")
        probe.close()
    except Exception as exc:
        failure = f"capture self-test failed: {type(exc).__name__}: {exc}"
    else:
        failure = None
        if len(captured) != 1:
            failure = f"capture self-test saw {len(captured)} connections, expected 1"
    if failure is not None:
        lib.sqlite3_cancel_auto_extension(pointer)
        return _Hook(None, failure)
    return _Hook(lib, None)


def ensure_hook() -> ctypes.CDLL:
    """Install the capture hook once per process; raise ``Unattributable`` if it is unavailable.

    The first call installs and self-tests the hook under a lock; its result,
    including a failure, is kept for the life of the process.
    """
    global _hook
    with _install_lock:
        if _hook is None:
            _hook = _install()
        hook = _hook
    if hook.error is not None or hook.lib is None:
        raise Unattributable(f"connection capture unavailable: {hook.error}")
    return hook.lib


@contextmanager
def capturing() -> Iterator[list[int]]:
    """Arm this thread's capture list for the duration of the block."""
    if getattr(_local, "captured", None) is not None:
        raise Unattributable("a connection capture is already armed on this thread")
    captured: list[int] = []
    _local.captured = captured
    try:
        yield captured
    finally:
        _local.captured = None


def _ptr(address: int) -> int | None:
    return ctypes.c_void_p.from_address(address).value


def _int(address: int) -> int:
    return ctypes.c_int.from_address(address).value


def limit(handle: int, category: int) -> int:
    """Return ``sqlite3_limit(handle, category, -1)``: the handle's current limit."""
    return ensure_hook().sqlite3_limit(handle, category, -1)


def _file_control_pointer(handle: int, op: int) -> int:
    out = ctypes.c_void_p()
    rc = ensure_hook().sqlite3_file_control(handle, b"main", op, ctypes.byref(out))
    if rc != SQLITE_OK or not out.value:
        raise Unattributable(f"sqlite3_file_control({op}) returned {rc}")
    return out.value


def vfs_name(handle: int) -> str:
    """Return the name of the VFS the connection's main database uses."""
    head = _VfsHead.from_address(_file_control_pointer(handle, FCNTL_VFS_POINTER))
    if head.szOsFile < _MIN_OS_FILE:
        raise Unattributable(
            f"VFS file object is {head.szOsFile} bytes, expected at least {_MIN_OS_FILE}"
        )
    return (head.zName or b"").decode("utf-8", "replace")


def _db_filename(handle: int) -> int | None:
    return ensure_hook().sqlite3_db_filename(handle, b"main")


def _wal_filename(main_name: int) -> int | None:
    return ensure_hook().sqlite3_filename_wal(main_name)


def _journal_filename(main_name: int) -> int | None:
    return ensure_hook().sqlite3_filename_journal(main_name)


def _main_file(handle: int) -> int:
    """The main ``unixFile``, after its ``zPath`` anchor holds."""
    file = _file_control_pointer(handle, FCNTL_FILE_POINTER)
    if not _ptr(file):
        raise Unattributable("main file object is not open")
    anchor = _db_filename(handle)
    if not anchor or _ptr(file + _OFF_ZPATH) != anchor:
        raise Unattributable("main file object does not hold the connection's database name")
    return file


def main_fd(handle: int) -> int:
    """The descriptor of the connection's main database file."""
    return _int(_main_file(handle) + _OFF_H)


def _journal_object_fd(handle: int, anchor_of, label: str) -> int | None:
    file = _file_control_pointer(handle, FCNTL_JOURNAL_POINTER)
    if not _ptr(file):
        return None
    main_name = _db_filename(handle)
    anchor = anchor_of(main_name) if main_name else None
    if not anchor or _ptr(file + _OFF_ZPATH) != anchor:
        raise Unattributable(f"{label} file object does not hold the connection's {label} name")
    return _int(file + _OFF_H)


def wal_fd(handle: int) -> int | None:
    """The descriptor of the connection's ``-wal`` file, or ``None`` when it is not open."""
    return _journal_object_fd(handle, _wal_filename, "-wal")


def journal_fd(handle: int) -> int | None:
    """The descriptor of the connection's rollback journal, or ``None`` when it is not open."""
    return _journal_object_fd(handle, _journal_filename, "-journal")


def shm_fd(handle: int) -> int | None:
    """The ``-shm`` descriptor of the node the main file is attached to, or ``None``."""
    file = _main_file(handle)
    shm = _ptr(file + _OFF_PSHM)
    if not shm:
        return None
    node = _ptr(shm + _SHM_NODE)
    if not node:
        raise Unattributable("-shm node is missing")
    if _ptr(node + _NODE_PINODE) != _ptr(file + _OFF_PINODE):
        raise Unattributable("-shm node does not belong to the connection's database file")
    return _int(node + _NODE_HSHM)
