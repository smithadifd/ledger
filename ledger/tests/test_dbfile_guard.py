"""Database opens are bound to safe files and to the connection's own descriptors.

The guard asks SQLite which descriptors a new connection holds and compares
each with the file at its name; it never reads the process's descriptor table.
Kinds: C = read-write create, E = read-write existing, R = read-only
(``mode=ro``), I = ``immutable=1``.
"""
from __future__ import annotations

import ctypes
import io
import json
import os
import selectors
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import pytest

import ledger._sqlite_handle as _sqlite_handle
import ledger.backup as backup_mod
import ledger.dbfile as dbfile
import ledger.store as store
import ledger.verbs.doctor as doctor_mod
from ledger import open_ledger, resolve_db_path
from ledger.backup import backup_db
from ledger.cli import main
from ledger.dbfile import UnsafeDatabaseFile
from ledger.store import open_ledger_readonly
from ledger.verbs import EXIT_REFUSED

ROOT = Path(__file__).resolve().parents[2]
READY_DEADLINE = 30.0


def _seed(path: Path):
    lg = open_ledger(path)
    lg.append(
        kind="item.created",
        payload={"type": "ticket", "title": "descriptor fixture"},
        item_id="T-1",
        idempotency_key="seed-T-1",
        harness="pytest",
    )
    return lg


def _json_cli(path: Path, *verb: str) -> tuple[int, dict, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["--json", "--db", str(path), *verb])
    return code, json.loads(out.getvalue()), err.getvalue()


def _set_rollback_mode(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    finally:
        conn.close()


def _make_database(path: Path, value: str) -> None:
    """A rollback-mode database whose one row names which file it is."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("CREATE TABLE identity_value (value TEXT NOT NULL)")
        conn.execute("INSERT INTO identity_value VALUES (?)", (value,))
        conn.commit()
    finally:
        conn.close()


def _value(conn: sqlite3.Connection) -> str:
    return conn.execute("SELECT value FROM identity_value").fetchone()[0]


def _path_id(path: Path) -> tuple[int, int]:
    found = os.lstat(path)
    return found.st_dev, found.st_ino


def _fd_id(fd: int) -> tuple[int, int]:
    found = os.fstat(fd)
    return found.st_dev, found.st_ino


def _opener(db: Path, kind: str) -> Callable[[], sqlite3.Connection]:
    if kind == "C":
        return lambda: sqlite3.connect(str(db), isolation_level=None)
    query = {"E": "mode=rw", "R": "mode=ro", "I": "immutable=1"}[kind]
    return lambda: sqlite3.connect(f"{db.as_uri()}?{query}", uri=True, isolation_level=None)


def _checked(db: Path, kind: str, **kwargs) -> sqlite3.Connection:
    conn, _mode = dbfile.checked_connect(
        db, _opener(db, kind), immutable=kind == "I", **kwargs
    )
    return conn


def _assert_closed(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        conn.execute("SELECT 1")


def _refuse(
    db: Path, opener: Callable[[], sqlite3.Connection], **kwargs
) -> UnsafeDatabaseFile:
    """Run a checked connect that must refuse: never returned, and closed."""
    opened: list[sqlite3.Connection] = []
    returned: list[sqlite3.Connection] = []

    def recording() -> sqlite3.Connection:
        conn = opener()
        opened.append(conn)
        return conn

    try:
        with pytest.raises(UnsafeDatabaseFile) as info:
            conn, _mode = dbfile.checked_connect(db, recording, **kwargs)
            returned.append(conn)
        assert returned == [], "a refused connect must never return its connection"
        for conn in opened:
            _assert_closed(conn)
    finally:
        for conn in returned:
            conn.close()
    return info.value


def _refuse_kind(db: Path, kind: str) -> UnsafeDatabaseFile:
    return _refuse(db, _opener(db, kind), immutable=kind == "I")


def _record_main_fd(monkeypatch) -> list[tuple[int, int]]:
    """Record (thread id, descriptor) for every main descriptor the guard reads."""
    real = _sqlite_handle.main_fd
    seen: list[tuple[int, int]] = []

    def recording(handle: int) -> int:
        fd = real(handle)
        seen.append((threading.get_ident(), fd))
        return fd

    monkeypatch.setattr(_sqlite_handle, "main_fd", recording)
    return seen


def _open_descriptors() -> dict[int, tuple[int, int]]:
    """This process's descriptors and their identities (test-side only)."""
    root = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    found: dict[int, tuple[int, int]] = {}
    for name in os.listdir(root):
        try:
            found[int(name)] = _fd_id(int(name))
        except OSError:
            continue
    return found


def _is_busy(exc: BaseException) -> bool:
    text = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and ("locked" in text or "busy" in text)


def _hammer(seconds: float, workers: list[Callable[[], None]]) -> tuple[int, list[BaseException]]:
    """Run each worker in its own thread in a loop until the deadline.

    Returns the number of completed iterations and every error. SQLite's own
    busy answers under write contention are not guard refusals, so a worker
    that gets one just goes round again; anything else, a refusal above all,
    is an error and stops that worker.
    """
    deadline = time.monotonic() + seconds
    counts = [0] * len(workers)
    errors: list[BaseException] = []
    lock = threading.Lock()

    def run(index: int) -> None:
        try:
            while time.monotonic() < deadline:
                try:
                    workers[index]()
                except sqlite3.OperationalError as exc:
                    if not _is_busy(exc):
                        raise
                    continue
                counts[index] += 1
        except BaseException as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(workers))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(seconds + 60)
        assert not thread.is_alive()
    return sum(counts), errors


def _start_child(program: str, *args: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", program, *args],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            if not selector.select(READY_DEADLINE):
                pytest.fail("child did not print its ready line in time")
        line = proc.stdout.readline()
        assert line.strip() == "ready", line
    except BaseException:
        _stop_child(proc)
        raise
    return proc


def _stop_child(proc: subprocess.Popen) -> str:
    """Close the child's stdin (its stop signal); kill it if it does not exit."""
    try:
        out, err = proc.communicate(timeout=30)  # closes stdin, then waits
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 0, err
    return out


def _store_worker(db: Path, kind: str) -> Callable[[], None]:
    """One connect, one read and (E) one insert through the ledger's own openers."""

    def work() -> None:
        if kind == "E":
            conn = store.connect(db)
            try:
                conn.execute("SELECT count(*) FROM hammer").fetchone()
                conn.execute("INSERT INTO hammer (n) VALUES (1)")
            finally:
                conn.close()
        else:
            conn = store._connect_readonly(db)
            try:
                conn.execute("SELECT count(*) FROM hammer").fetchone()
            finally:
                conn.close()

    return work


def _create_hammer_db(db: Path) -> None:
    """Create a WAL database through the checked read-write open (kind C)."""
    conn = store.connect(db)
    try:
        conn.execute("CREATE TABLE hammer (n INTEGER)")
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


# --- K: the v0.1.14 cases ------------------------------------------------------


def test_valid_wal_database_opens_readwrite_and_readonly(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()

    with open_ledger(db) as writer:
        assert [event.item_id for event in writer.events()] == ["T-1"]
    with open_ledger_readonly(db) as reader:
        assert [event.item_id for event in reader.events()] == ["T-1"]


def test_given_database_symlink_is_refused_before_resolution(tmp_path: Path) -> None:
    target = tmp_path / "target.db"
    _seed(target).close()
    given = tmp_path / "given.db"
    os.symlink(target, given)

    with pytest.raises(UnsafeDatabaseFile, match=r"^db_symlink:"):
        resolve_db_path(given)
    code, body, err = _json_cli(given, "query")
    assert (code, body["error"]["code"], err) == (EXIT_REFUSED, "db_symlink", "")


def test_database_hard_link_is_refused_before_connect(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    linked = tmp_path / "linked.db"
    os.link(db, linked)
    stages = []
    monkeypatch.setattr(dbfile, "_TEST_SEAM", lambda stage, _path: stages.append(stage))

    with pytest.raises(UnsafeDatabaseFile, match=r"^db_hardlinked:"):
        open_ledger(linked)
    assert stages == [], "the hard link must be refused before a connect is attempted"


def test_fifo_is_refused_without_blocking_readonly_open(tmp_path: Path) -> None:
    fifo = tmp_path / "ledger.db"
    os.mkfifo(fifo)
    program = (
        "from ledger.store import open_ledger_readonly; "
        "p=" + repr(str(fifo)) + "; "
        "\ntry: open_ledger_readonly(p)\n"
        "except BaseException as e: print(type(e).__name__ + ':' + str(e))\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("read-only FIFO open blocked instead of refusing")
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("UnsafeDatabaseFile:db_not_regular:"), result.stdout


def test_unlinked_sibling_is_absent_but_hardlinked_sibling_refuses(
    tmp_path: Path, monkeypatch
) -> None:
    sibling = tmp_path / "ledger.db-journal"
    sibling.write_bytes(b"journal")
    actual = os.lstat(sibling)
    link_count = 0

    def lstat_with_link_count(path):
        assert Path(path) == sibling
        return SimpleNamespace(st_mode=actual.st_mode, st_nlink=link_count)

    def outcome() -> str:
        try:
            found = dbfile._check_sibling(sibling)
        except UnsafeDatabaseFile as exc:
            return exc.code
        return "absent" if found is None else "accepted"

    monkeypatch.setattr(os, "lstat", lstat_with_link_count)
    assert outcome() == "absent", "a zero-link side file must be treated as absent"

    link_count = 2
    assert outcome() == "db_sibling_unsafe", "a second hard link must still be refused"


@pytest.mark.parametrize("opener", [open_ledger, open_ledger_readonly])
@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_planted_sibling_symlink_is_refused(tmp_path: Path, suffix: str, opener) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    victim = tmp_path / "victim"
    victim.write_bytes(b"victim")
    os.symlink(victim, Path(f"{db}{suffix}"))

    with pytest.raises(
        UnsafeDatabaseFile,
        match=rf"^db_sibling_unsafe: ledger\.db{suffix} is a symbolic link:",
    ):
        opener(db)
    assert victim.read_bytes() == b"victim"


@pytest.mark.parametrize("opener", [open_ledger, open_ledger_readonly])
def test_planted_shm_hard_link_is_refused_without_touching_victim(
    tmp_path: Path, opener
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    victim = tmp_path / "victim"
    before = b"v" * 6000
    victim.write_bytes(before)
    os.link(victim, Path(f"{db}-shm"))

    with pytest.raises(UnsafeDatabaseFile, match=r"^db_sibling_unsafe:"):
        opener(db)
    assert victim.read_bytes() == before


@pytest.mark.parametrize("kind", ["E", "R"])
def test_main_descriptor_swap_after_connect_is_refused(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    replacement = tmp_path / "replacement.db"
    _seed(replacement).close()
    fired = False

    def swap(stage: str, path: Path) -> None:
        nonlocal fired
        if not fired and stage == "after_connect" and path == db:
            os.replace(replacement, db)
            fired = True

    monkeypatch.setattr(dbfile, "_TEST_SEAM", swap)
    refusal = _refuse_kind(db, kind)
    # The fresh comparison refuses; the old descriptor's link count (now 0) is
    # checked only after both identity comparisons.
    assert str(refusal).startswith(
        "db_swapped: opened descriptor is not the file at the database path:"
    ), refusal
    assert fired


@pytest.mark.parametrize("opener", [open_ledger, open_ledger_readonly])
def test_unrelated_descriptor_opened_during_connect_is_accepted(
    tmp_path: Path, monkeypatch, opener
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("x", encoding="utf-8")
    held = []

    def open_extra(stage: str, path: Path) -> None:
        if stage == "before_connect" and path == db and not held:
            held.append(unrelated.open("rb"))

    monkeypatch.setattr(dbfile, "_TEST_SEAM", open_extra)
    try:
        with opener(db) as lg:
            assert [event.item_id for event in lg.events()] == ["T-1"]
        assert held, "the unrelated open must land inside the connect window"
    finally:
        for handle in held:
            handle.close()


def test_sibling_name_swapped_away_from_already_open_descriptor_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    holder = _seed(db)
    replacement = tmp_path / "other-shm"
    replacement.write_bytes(b"r" * 32768)
    fired = False

    def swap_shm(stage: str, path: Path) -> None:
        nonlocal fired
        if not fired and stage == "before_first_read" and path == db:
            os.replace(replacement, Path(f"{db}-shm"))
            fired = True

    monkeypatch.setattr(dbfile, "_TEST_SEAM", swap_shm)
    try:
        refusal = _refuse_kind(db, "R")
        assert str(refusal).startswith(
            "db_sibling_unsafe: open -shm descriptor is not the file at ledger.db-shm:"
        ), refusal
        assert fired
    finally:
        holder.close()


def test_lstat_failure_refuses_by_name(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    resolved = resolve_db_path(db)
    real = os.lstat

    def denied(path, *args, **kwargs):
        if Path(path) == resolved:
            raise PermissionError("denied by test")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", denied)
    with pytest.raises(UnsafeDatabaseFile, match=r"^db_stat_failed:"):
        dbfile.precheck_files(resolved)


def test_integrity_check_refuses_partial_replaced_by_symlink(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.db"
    _seed(source).close()
    other = tmp_path / "other.db"
    _seed(other).close()
    other_before = other.read_bytes()
    dest = tmp_path / "backup.db"
    real_integrity = backup_mod.integrity_check
    observed = []

    def replace_then_check(partial: Path) -> str:
        partial.unlink()
        os.symlink(other, partial)
        try:
            return real_integrity(partial)
        except BaseException as exc:
            observed.append(exc)
            raise

    monkeypatch.setattr(backup_mod, "integrity_check", replace_then_check)
    monkeypatch.setattr(backup_mod, "_refuse_newer_schema_in_copy", lambda _path: None)

    with pytest.raises(UnsafeDatabaseFile, match=r"^db_symlink:"):
        backup_db(source, dest)
    assert len(observed) == 1 and isinstance(observed[0], UnsafeDatabaseFile)
    assert not dest.exists()
    assert other.read_bytes() == other_before


def test_doctor_expired_claims_does_not_hide_database_refusal(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    victim = tmp_path / "victim"
    victim.write_bytes(b"victim")
    os.symlink(victim, Path(f"{db}-shm"))

    with pytest.raises(UnsafeDatabaseFile, match=r"^db_sibling_unsafe:"):
        doctor_mod._expired_claims(db)


@pytest.mark.parametrize("kind", ["E", "R"])
def test_rename_away_and_back_with_reused_descriptor_refuses(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db = tmp_path / "ledger.db"
    replacement = tmp_path / "replacement.db"
    held = tmp_path / "ledger.held"
    _make_database(db, "database-at-path")
    _make_database(replacement, "replacement-database")
    original = sqlite3.connect(db)
    original.execute("SELECT * FROM identity_value").fetchall()
    first_replacement = sqlite3.connect(replacement)
    first_replacement.execute("SELECT 1").fetchall()
    locked_replacement = sqlite3.connect(replacement, isolation_level=None)
    locked_replacement.execute("BEGIN")
    locked_replacement.execute("SELECT * FROM identity_value").fetchall()
    first_replacement.close()  # parks the replacement's descriptor for reuse
    stages: list[str] = []

    def swap_at_connect(stage: str, path: Path) -> None:
        assert path == db
        stages.append(stage)
        if stage == "before_connect":
            os.rename(db, held)
            os.rename(replacement, db)
        elif stage == "after_connect":
            os.rename(db, replacement)
            os.rename(held, db)

    monkeypatch.setattr(dbfile, "_TEST_SEAM", swap_at_connect)
    opener = (lambda: sqlite3.connect(db)) if kind == "E" else _opener(db, "R")
    try:
        refusal = _refuse(db, opener)
        assert refusal.code == "db_swapped"
        assert stages == ["before_connect", "after_connect"]
    finally:
        locked_replacement.close()
        original.close()


def test_swap_to_already_open_database_with_reused_descriptor_refuses(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    replacement = tmp_path / "replacement.db"
    _seed(db).close()
    _seed(replacement).close()
    held_replacement = sqlite3.connect(replacement)
    held_replacement.execute("SELECT 1 FROM events").fetchone()
    held_original = sqlite3.connect(db)
    held_original.execute("BEGIN")
    held_original.execute("SELECT count(*) FROM events").fetchone()
    recycled = sqlite3.connect(db)
    recycled.execute("SELECT 1").fetchone()
    recycled.close()
    fired = False

    def swap_after_connect(stage: str, path: Path) -> None:
        nonlocal fired
        if stage == "after_connect" and path == db:
            os.replace(replacement, db)
            fired = True

    monkeypatch.setattr(dbfile, "_TEST_SEAM", swap_after_connect)
    try:
        refusal = _refuse(db, _opener(db, "E"))
        assert refusal.code == "db_swapped"
        assert fired
    finally:
        held_original.rollback()
        held_original.close()
        held_replacement.close()


def test_opener_returning_a_preexisting_connection_refuses(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    replacement = tmp_path / "replacement.db"
    _seed(replacement).close()
    parked = sqlite3.connect(db)

    def return_parked_after_swap():
        os.replace(replacement, db)
        return parked

    refusal = _refuse(db, return_parked_after_swap)
    assert str(refusal).startswith(
        "db_fd_unknown: connect opened 0 connections, expected 1:"
    ), refusal


def test_opened_main_descriptor_with_second_link_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    real_fstat = os.fstat
    armed = False
    identity = (os.lstat(db).st_dev, os.lstat(db).st_ino)

    def arm(stage: str, path: Path) -> None:
        nonlocal armed
        if stage == "after_connect" and path == db:
            armed = True

    def linked_fstat(fd: int):
        found = real_fstat(fd)
        if armed and (found.st_dev, found.st_ino) == identity:
            return SimpleNamespace(
                st_dev=found.st_dev,
                st_ino=found.st_ino,
                st_mode=found.st_mode,
                st_nlink=2,
            )
        return found

    monkeypatch.setattr(dbfile, "_TEST_SEAM", arm)
    monkeypatch.setattr(os, "fstat", linked_fstat)
    with pytest.raises(
        UnsafeDatabaseFile, match=r"^db_hardlinked: opened database has 2 links:"
    ):
        open_ledger_readonly(db)


def test_rollback_first_read_accepts_unrelated_new_descriptor(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    _set_rollback_mode(db)
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("unrelated", encoding="utf-8")
    held = []

    def open_extra(stage: str, path: Path) -> None:
        if stage == "before_first_read" and path == db and not held:
            held.append(unrelated.open("rb"))

    monkeypatch.setattr(dbfile, "_TEST_SEAM", open_extra)
    try:
        conn, mode = dbfile.checked_connect(db, lambda: sqlite3.connect(db))
        try:
            assert mode == "delete"
            assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        finally:
            conn.close()
        assert held
    finally:
        for handle in held:
            handle.close()


def test_wal_switch_rechecks_planted_sibling_symlink(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    _set_rollback_mode(db)
    victim = tmp_path / "victim"
    victim.write_bytes(b"victim")
    fired = False

    def plant(stage: str, path: Path) -> None:
        nonlocal fired
        if stage == "before_wal_switch" and path == db and not fired:
            os.symlink(victim, Path(f"{db}-wal"))
            fired = True

    monkeypatch.setattr(dbfile, "_TEST_SEAM", plant)
    with pytest.raises(UnsafeDatabaseFile, match=r"^db_sibling_unsafe:"):
        open_ledger(db)
    assert fired
    assert victim.read_bytes() == b"victim"


def test_descriptor_replaced_at_same_number_is_accepted(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    _set_rollback_mode(db)
    old_file = tmp_path / "old"
    replacement = tmp_path / "replacement"
    old_file.write_text("old", encoding="utf-8")
    replacement.write_text("replacement", encoding="utf-8")

    with old_file.open("rb") as target, replacement.open("rb") as source:
        fired = False

        def replace_fd(stage: str, path: Path) -> None:
            nonlocal fired
            if stage == "before_connect" and path == db and not fired:
                os.dup2(source.fileno(), target.fileno())
                fired = True

        monkeypatch.setattr(dbfile, "_TEST_SEAM", replace_fd)
        conn, _mode = dbfile.checked_connect(db, lambda: sqlite3.connect(db))
        conn.close()
        assert fired


def test_unrelated_descriptor_after_first_wal_read_is_accepted(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    unrelated = tmp_path / "unrelated"
    unrelated.write_text("unrelated", encoding="utf-8")
    held = []

    def open_extra(stage: str, path: Path) -> None:
        if stage == "after_first_read" and path == db and not held:
            held.append(unrelated.open("rb"))

    monkeypatch.setattr(dbfile, "_TEST_SEAM", open_extra)
    try:
        with open_ledger_readonly(db) as reader:
            assert [event.item_id for event in reader.events()] == ["T-1"]
        assert held
    finally:
        for handle in held:
            handle.close()


def test_backup_partial_symlink_is_refused_before_victim_is_touched(
    tmp_path: Path, monkeypatch
) -> None:
    source = tmp_path / "source.db"
    _seed(source).close()
    victim = tmp_path / "victim.db"
    _seed(victim).close()
    victim_before = victim.read_bytes()
    dest = tmp_path / "backup.db"
    partial = Path(f"{dest}.partial")
    fired = False

    def plant(stage: str, path: Path) -> None:
        nonlocal fired
        if stage == "before_connect" and path == partial and not fired:
            os.symlink(victim, partial)
            fired = True

    monkeypatch.setattr(dbfile, "_TEST_SEAM", plant)
    with pytest.raises(UnsafeDatabaseFile, match=r"^db_symlink:"):
        backup_db(source, dest)
    assert fired
    assert victim.read_bytes() == victim_before
    assert not dest.exists()
    assert not partial.exists()


def test_non_exception_during_checked_open_closes_connection_and_propagates(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    opened = []

    def opener():
        conn = sqlite3.connect(db)
        opened.append(conn)
        return conn

    def interrupt(stage: str, path: Path) -> None:
        if stage == "after_connect" and path == db:
            raise KeyboardInterrupt

    monkeypatch.setattr(dbfile, "_TEST_SEAM", interrupt)
    with pytest.raises(KeyboardInterrupt):
        dbfile.checked_connect(db, opener)
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        opened[0].execute("SELECT 1")


def test_main_descriptor_change_during_first_read_is_refused(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    other = tmp_path / "other"
    other.write_bytes(b"other")
    real = _sqlite_handle.main_fd
    calls: list[int] = []

    with other.open("rb") as stand_in:

        def changing(handle: int) -> int:
            fd = real(handle)
            calls.append(fd)
            return fd if len(calls) == 1 else stand_in.fileno()

        monkeypatch.setattr(_sqlite_handle, "main_fd", changing)
        with pytest.raises(
            UnsafeDatabaseFile,
            match=r"^db_fd_unknown: main descriptor changed during first read:",
        ):
            open_ledger_readonly(db)
    assert len(calls) == 2


def test_doctor_summary_does_not_hide_database_refusal(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    victim = tmp_path / "victim"
    victim.write_bytes(b"victim")
    os.symlink(victim, Path(f"{db}-shm"))

    with pytest.raises(UnsafeDatabaseFile, match=r"^db_sibling_unsafe:"):
        doctor_mod._summary(db)


# --- N: concurrency and the rename route ---------------------------------------
#
# N1 and N2 create their databases before the concurrent loops, so they do not
# exercise concurrent first opens of a new ledger; N20 covers a main file that
# another creator writes between the precheck and the connect.


def test_threads_on_one_database_are_never_refused(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _create_hammer_db(db)
    workers = [_store_worker(db, kind) for kind in ("E", "E", "R") * 2]

    total, errors = _hammer(2.0, workers)
    assert errors == []
    assert total >= len(workers)


def test_threads_on_different_databases_are_never_refused(tmp_path: Path) -> None:
    dbs = [tmp_path / f"ledger-{i}.db" for i in range(3)]
    for db in dbs:
        _create_hammer_db(db)
    workers = [
        _store_worker(dbs[i % 3], "E" if i < 3 else "R") for i in range(6)
    ]

    total, errors = _hammer(2.0, workers)
    assert errors == []
    assert total >= len(workers)


def test_unrelated_opens_in_a_loop_never_refuse_a_connect(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _create_hammer_db(db)
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"noise")

    def noise() -> None:
        with unrelated.open("rb") as handle:
            handle.read(1)

    workers = [_store_worker(db, kind) for kind in ("E", "E", "R") * 2] + [noise]
    total, errors = _hammer(2.0, workers)
    assert errors == []
    assert total >= len(workers)


@pytest.mark.parametrize("kind", ["E", "R"])
def test_reopen_reusing_a_parked_descriptor_is_accepted(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db = tmp_path / "ledger.db"
    _make_database(db, "database-at-path")
    holder = sqlite3.connect(db, isolation_level=None)
    holder.execute("BEGIN")
    holder.execute("SELECT * FROM identity_value").fetchall()
    seen = _record_main_fd(monkeypatch)
    try:
        for _round in range(3):
            parked = _opener(db, kind)()
            parked.execute("SELECT 1 FROM identity_value").fetchone()
            parked.close()  # the holder's lock keeps this descriptor parked for reuse
            before = _open_descriptors()
            seen.clear()
            conn = _checked(db, kind)
            try:
                assert _value(conn) == "database-at-path"
            finally:
                conn.close()
            fd = seen[0][1]
            assert before.get(fd) == _path_id(db), "the reopen must reuse a parked descriptor"
    finally:
        holder.rollback()
        holder.close()


@pytest.mark.parametrize("kind", ["E", "R"])
def test_rename_away_and_back_with_fresh_open_refuses(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db = tmp_path / "ledger.db"
    replacement = tmp_path / "replacement.db"
    held = tmp_path / "ledger.held"
    _make_database(db, "database-at-path")
    _make_database(replacement, "replacement-database")
    holder = sqlite3.connect(db)
    holder.execute("SELECT * FROM identity_value").fetchall()

    def swap_at_connect(stage: str, path: Path) -> None:
        if stage == "before_connect":
            os.rename(db, held)
            os.rename(replacement, db)
        elif stage == "after_connect":
            os.rename(db, replacement)
            os.rename(held, db)

    monkeypatch.setattr(dbfile, "_TEST_SEAM", swap_at_connect)
    try:
        assert _refuse_kind(db, kind).code == "db_swapped"
    finally:
        holder.close()


# Each state holds the name for a short pause: with no pause the name changes
# inside nearly every connect, almost every one refuses, and the window the
# precheck comparison closes (R0 saw one file, connect and R3 see the other) is
# rarely reached.
_RENAMER = r"""
import os, select, sys, time
db, held, replacement = sys.argv[1:4]
print("ready", flush=True)
while not select.select([sys.stdin], [], [], 0)[0]:
    os.rename(db, held)
    os.rename(replacement, db)
    time.sleep(0.0002)
    os.rename(db, replacement)
    os.rename(held, db)
    time.sleep(0.0002)
"""


def test_rename_race_never_accepts_a_file_the_precheck_did_not_see(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    replacement = tmp_path / "replacement.db"
    held = tmp_path / "ledger.held"
    _make_database(db, "at-path")
    _make_database(replacement, "replacement")
    file_of = {"at-path": _path_id(db), "replacement": _path_id(replacement)}

    real_lstat = os.lstat
    local = threading.local()

    def recording_lstat(path, *args, **kwargs):
        calls = getattr(local, "calls", None)
        mine = calls is not None and Path(path) == db
        try:
            found = real_lstat(path, *args, **kwargs)
        except FileNotFoundError:
            if mine:
                calls.append(None)
            raise
        if mine:
            calls.append((found.st_dev, found.st_ino))
        return found

    def disarm(stage: str, _path: Path) -> None:
        if stage == "before_first_read":
            local.calls = None

    monkeypatch.setattr(os, "lstat", recording_lstat)
    monkeypatch.setattr(dbfile, "_TEST_SEAM", disarm)
    outcomes: Counter = Counter()
    wrong: list[tuple[str, object]] = []
    child = _start_child(_RENAMER, str(db), str(held), str(replacement))
    try:
        deadline = time.monotonic() + 2.0
        count = 0
        while time.monotonic() < deadline:
            kind = "E" if count % 2 == 0 else "R"
            count += 1
            calls: list = []
            local.calls = calls
            try:
                conn = _checked(db, kind)
            except UnsafeDatabaseFile as exc:
                outcomes[exc.code] += 1
                continue
            except sqlite3.Error:
                outcomes["sqlite_error"] += 1
                continue
            finally:
                local.calls = None
            try:
                value = _value(conn)
            except sqlite3.Error:
                outcomes["sqlite_error"] += 1
                continue
            finally:
                conn.close()
            outcomes["accepted"] += 1
            precheck, checked = calls[0], calls[-1]
            expected = precheck if precheck is not None else checked
            if file_of[value] != expected:
                wrong.append((value, calls))
    finally:
        _stop_child(child)

    assert wrong == [], f"accepted connections read a file the checks did not see: {wrong[:3]}"
    assert outcomes["accepted"] > 0, outcomes
    allowed = {
        "accepted", "db_swapped", "db_fd_unknown", "db_sibling_unsafe",
        "db_stat_failed", "sqlite_error",
    }
    assert set(outcomes) <= allowed, outcomes


@pytest.mark.parametrize("kind", ["E", "R"])
def test_wal_renamed_away_and_back_at_first_read_with_holder_refuses(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    holder = sqlite3.connect(db)
    holder.execute("SELECT count(*) FROM events").fetchone()
    wal = Path(f"{db}-wal")
    parked = tmp_path / "parked-wal"
    victim = tmp_path / "victim"
    victim.write_bytes(b"")

    def swap(stage: str, path: Path) -> None:
        if stage == "before_first_read":
            os.rename(wal, parked)
            os.rename(victim, wal)
        elif stage == "after_first_read":
            os.rename(wal, victim)
            os.rename(parked, wal)

    monkeypatch.setattr(dbfile, "_TEST_SEAM", swap)
    try:
        refusal = _refuse_kind(db, kind)
        assert str(refusal).startswith(
            "db_sibling_unsafe: open -wal descriptor is not the file at ledger.db-wal:"
        ), refusal
    finally:
        holder.close()


@pytest.mark.parametrize("kind", ["E", "R"])
def test_shm_swapped_at_first_read_without_holder_refuses(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    shm = Path(f"{db}-shm")
    victim = tmp_path / "victim"
    victim.write_bytes(b"v" * 6000)
    fresh = tmp_path / "fresh"
    fresh.write_bytes(b"f" * 32768)

    def swap(stage: str, path: Path) -> None:
        if stage == "before_first_read":
            os.rename(victim, shm)
        elif stage == "after_first_read":
            os.rename(shm, victim)
            os.rename(fresh, shm)

    monkeypatch.setattr(dbfile, "_TEST_SEAM", swap)
    refusal = _refuse_kind(db, kind)
    assert str(refusal).startswith(
        "db_sibling_unsafe: open -shm descriptor is not the file at ledger.db-shm:"
    ), refusal


@pytest.mark.parametrize("kind", ["E", "R"])
def test_shm_renamed_away_and_back_with_holder_is_accepted(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    holder = sqlite3.connect(db)
    holder.execute("SELECT count(*) FROM events").fetchone()
    shm = Path(f"{db}-shm")
    parked = tmp_path / "parked-shm"
    victim = tmp_path / "victim"
    before = b"v" * 6000
    victim.write_bytes(before)

    def swap(stage: str, path: Path) -> None:
        if stage == "before_first_read":
            os.rename(shm, parked)
            os.rename(victim, shm)
        elif stage == "after_first_read":
            os.rename(shm, victim)
            os.rename(parked, shm)

    monkeypatch.setattr(dbfile, "_TEST_SEAM", swap)
    try:
        conn = _checked(db, kind)
        conn.close()
        assert victim.read_bytes() == before, "the shared -shm node never opens the victim"
    finally:
        holder.close()


@pytest.mark.parametrize("kind", ["E", "R"])
def test_held_file_renamed_onto_shm_after_first_read_refuses(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    shm = Path(f"{db}-shm")
    victim = tmp_path / "victim"
    victim.write_bytes(b"v" * 6000)
    log = tmp_path / "held.log"

    def swap(stage: str, path: Path) -> None:
        if stage == "before_first_read":
            os.rename(victim, shm)
        elif stage == "after_first_read":
            os.rename(shm, victim)
            os.rename(log, shm)

    monkeypatch.setattr(dbfile, "_TEST_SEAM", swap)
    with log.open("ab") as held_log:
        held_log.write(b"log")
        held_log.flush()
        refusal = _refuse_kind(db, kind)
    assert str(refusal).startswith(
        "db_sibling_unsafe: open -shm descriptor is not the file at ledger.db-shm:"
    ), refusal


@pytest.mark.parametrize("kind", ["E", "R"])
def test_two_threads_opening_the_same_path_together_are_both_accepted(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    seen = _record_main_fd(monkeypatch)
    at_seam = threading.Barrier(2)
    armed = threading.Barrier(2)
    opened = threading.Barrier(2)

    def meet(stage: str, path: Path) -> None:
        if stage == "before_connect":
            at_seam.wait(timeout=10)

    def opener() -> sqlite3.Connection:
        armed.wait(timeout=10)  # both threads' captures are armed
        conn = _opener(db, kind)()
        opened.wait(timeout=10)  # both opens land before either disarms
        return conn

    monkeypatch.setattr(dbfile, "_TEST_SEAM", meet)
    outcomes: dict[int, object] = {}

    def run(index: int) -> None:
        try:
            conn, _mode = dbfile.checked_connect(db, opener)
            outcomes[index] = conn.execute("SELECT count(*) FROM events").fetchone()[0]
            conn.close()
        except BaseException as exc:
            outcomes[index] = exc

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
        assert not thread.is_alive()
    assert outcomes == {0: 1, 1: 1}
    first_reads = {}
    for thread_id, fd in seen:
        first_reads.setdefault(thread_id, fd)
    assert len(first_reads) == 2
    assert len(set(first_reads.values())) == 2, "each thread names its own descriptor"


_APPENDER = r"""
import select, sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=10)
print("ready", flush=True)
n = 0
while not select.select([sys.stdin], [], [], 0)[0]:
    try:
        conn.execute("INSERT INTO hammer (n) VALUES (?)", (n,))
    except sqlite3.OperationalError as exc:
        # WAL can answer busy without waiting (another connection's recovery).
        if "locked" not in str(exc) and "busy" not in str(exc):
            raise
        continue
    n += 1
conn.close()
print(n, flush=True)
"""


def test_cross_process_writers_never_refuse_in_process_connects(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _create_hammer_db(db)
    children = []
    try:
        for _ in range(2):
            children.append(_start_child(_APPENDER, str(db)))

        def reader(kind: str) -> Callable[[], None]:
            def work() -> None:
                conn = store.connect(db) if kind == "E" else store._connect_readonly(db)
                try:
                    conn.execute("SELECT count(*) FROM hammer").fetchone()
                finally:
                    conn.close()

            return work

        total, errors = _hammer(2.0, [reader(kind) for kind in ("E", "R") * 3])
    finally:
        written = [int(_stop_child(child).strip() or 0) for child in children]
    assert errors == []
    assert total >= 6
    assert all(count > 0 for count in written), written


def test_opener_opening_two_connections_refuses(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    extra: list[sqlite3.Connection] = []

    def opens_two() -> sqlite3.Connection:
        extra.append(sqlite3.connect(db))
        return sqlite3.connect(db)

    try:
        refusal = _refuse(db, opens_two)
        assert str(refusal).startswith(
            "db_fd_unknown: connect opened 2 connections, expected 1:"
        ), refusal
    finally:
        for conn in extra:
            conn.close()


def test_opener_returning_another_open_connection_is_unattributable(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    already = sqlite3.connect(db)
    already.execute("SELECT 1").fetchone()
    fresh: list[sqlite3.Connection] = []

    def returns_another() -> sqlite3.Connection:
        fresh.append(sqlite3.connect(db))
        return already

    try:
        refusal = _refuse(db, returns_another)
        assert refusal.code == "db_fd_unattributable", refusal
    finally:
        for conn in fresh:
            conn.close()


def _case_variant(db: Path) -> list[Path]:
    folder = db.parent
    return [
        folder / db.name.upper(),
        folder.with_name(folder.name.lower()) / db.name,
    ]


@pytest.mark.parametrize("kind", ["E", "R"])
def test_holder_under_a_case_variant_spelling_does_not_change_the_verdict(
    tmp_path: Path, kind: str
) -> None:
    db = tmp_path / "Spell" / "ledger.db"
    db.parent.mkdir()
    _seed(db).close()
    for variant in _case_variant(db):
        same_file = variant.exists() and os.path.samefile(variant, db)
        if not same_file:
            # A case-sensitive volume: the variant is another database, with its
            # own -shm node; the guard's verdict on ``db`` is still accept.
            variant.parent.mkdir(exist_ok=True)
            _seed(variant).close()
        holder = sqlite3.connect(str(variant))
        try:
            holder.execute("SELECT count(*) FROM events").fetchone()
            conn = _checked(db, kind)
            try:
                assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
                # Compare while the connection is open: the last connection on a
                # database removes its -shm when it closes.
                shared = _path_id(Path(f"{variant}-shm")) == _path_id(Path(f"{db}-shm"))
                assert shared is same_file
            finally:
                conn.close()
        finally:
            holder.close()


def _second_folder_spelling(db: Path) -> Path:
    for prefix in ("/System/Volumes/Data", "/proc/self/root"):
        candidate = Path(prefix + str(db))
        if candidate.exists() and os.path.samefile(candidate, db):
            return candidate
    pytest.fail("no second spelling of the folder names the same file on this platform")


@pytest.mark.parametrize("kind", ["E", "R"])
def test_holder_under_a_second_folder_spelling_does_not_change_the_verdict(
    tmp_path: Path, kind: str
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    spelling = _second_folder_spelling(db)
    holder = sqlite3.connect(str(spelling))
    try:
        holder.execute("SELECT count(*) FROM events").fetchone()
        conn = _checked(db, kind)
        conn.close()
    finally:
        holder.close()


@pytest.mark.parametrize("kind", ["E", "R"])
def test_shared_shm_node_holding_another_file_refuses(
    tmp_path: Path, kind: str
) -> None:
    db = tmp_path / "ledger.db"
    old = tmp_path / "old.db"
    _seed(old).close()
    holder = sqlite3.connect(old)
    holder.execute("SELECT count(*) FROM events").fetchone()  # node and -shm are old.db's
    os.rename(old, db)
    Path(f"{db}-shm").write_bytes(b"\0" * 32768)  # a regular file nothing holds
    try:
        refusal = _refuse_kind(db, kind)
        assert str(refusal).startswith(
            "db_sibling_unsafe: open -shm descriptor is not the file at ledger.db-shm:"
        ), refusal
    finally:
        holder.close()


@pytest.mark.parametrize("kind", ["E", "R"])
def test_rename_between_descriptor_and_name_reads_refuses_as_swap(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db = tmp_path / "ledger.db"
    replacement = tmp_path / "replacement.db"
    _make_database(db, "at-path")
    _make_database(replacement, "replacement")
    real_lstat = os.lstat
    armed = False

    def arm(stage: str, path: Path) -> None:
        nonlocal armed
        if stage == "after_connect":
            armed = True

    def renaming_lstat(path, *args, **kwargs):
        nonlocal armed
        if armed and Path(path) == db:
            armed = False
            os.replace(replacement, db)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(dbfile, "_TEST_SEAM", arm)
    monkeypatch.setattr(os, "lstat", renaming_lstat)
    refusal = _refuse_kind(db, kind)
    assert str(refusal).startswith(
        "db_swapped: opened descriptor is not the file at the database path:"
    ), refusal


@pytest.mark.parametrize("kind", ["E", "R"])
def test_replacement_holding_the_name_from_after_precheck_through_check_refuses(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db = tmp_path / "ledger.db"
    replacement = tmp_path / "replacement.db"
    held = tmp_path / "ledger.held"
    _make_database(db, "at-path")
    _make_database(replacement, "replacement")
    stages: list[str] = []

    def swap(stage: str, path: Path) -> None:
        stages.append(stage)
        if stage == "before_connect":
            os.rename(db, held)
            os.rename(replacement, db)
        elif stage == "before_first_read":
            os.rename(db, replacement)
            os.rename(held, db)

    monkeypatch.setattr(dbfile, "_TEST_SEAM", swap)
    refusal = _refuse_kind(db, kind)
    assert str(refusal).startswith(
        "db_swapped: opened descriptor is not the file the precheck found"
    ), refusal
    assert stages == ["before_connect", "after_connect"]


@pytest.mark.parametrize("kind", ["C", "E", "R"])
def test_main_written_by_another_creator_after_precheck_is_accepted(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db = tmp_path / "ledger.db"

    def create(stage: str, path: Path) -> None:
        if stage == "before_connect":
            _make_database(db, "seeded")

    monkeypatch.setattr(dbfile, "_TEST_SEAM", create)
    conn = _checked(db, kind)
    try:
        assert _value(conn) == "seeded"
    finally:
        conn.close()


def test_main_created_by_connect_then_swapped_refuses(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    replacement = tmp_path / "replacement.db"
    _make_database(replacement, "replacement")

    def swap(stage: str, path: Path) -> None:
        if stage == "after_connect":
            os.replace(replacement, db)

    monkeypatch.setattr(dbfile, "_TEST_SEAM", swap)
    refusal = _refuse_kind(db, "C")
    assert str(refusal).startswith(
        "db_swapped: opened descriptor is not the file at the database path:"
    ), refusal


@pytest.mark.parametrize(
    "change, detail",
    [
        ("absent", "-wal is absent after the first read"),
        ("symlink", "-wal is a symbolic link"),
        ("fifo", "-wal is not a regular file"),
        ("hardlink", "-wal has 2 links"),
    ],
)
def test_wal_name_unsafe_after_first_read_refuses(
    tmp_path: Path, monkeypatch, change: str, detail: str
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    wal = Path(f"{db}-wal")
    parked = tmp_path / "parked-wal"

    def alter(stage: str, path: Path) -> None:
        if stage != "after_first_read":
            return
        if change == "hardlink":
            os.link(wal, tmp_path / "second-link")
            return
        os.rename(wal, parked)
        if change == "symlink":
            os.symlink(parked, wal)
        elif change == "fifo":
            os.mkfifo(wal)

    monkeypatch.setattr(dbfile, "_TEST_SEAM", alter)
    refusal = _refuse_kind(db, "R")
    assert str(refusal).startswith(f"db_sibling_unsafe: {detail}:"), refusal


def test_wal_not_open_after_wal_read_refuses(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    monkeypatch.setattr(_sqlite_handle, "wal_fd", lambda _handle: None)
    refusal = _refuse_kind(db, "R")
    assert str(refusal).startswith(
        "db_sibling_unsafe: -wal is not open after WAL read:"
    ), refusal


def _journal_writer(db: Path) -> Callable[[], sqlite3.Connection]:
    """An opener that leaves a rollback journal open (a write transaction)."""

    def opener() -> sqlite3.Connection:
        conn = sqlite3.connect(db, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO identity_value VALUES ('pending')")
        return conn

    return opener


def test_open_rollback_journal_bound_to_its_name_is_accepted(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _make_database(db, "at-path")
    conn, mode = dbfile.checked_connect(db, _journal_writer(db))
    try:
        assert mode == "delete"
        assert Path(f"{db}-journal").exists(), "the journal must be open for this case"
        conn.execute("ROLLBACK")
    finally:
        conn.close()


def test_open_rollback_journal_swapped_at_its_name_refuses(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _make_database(db, "at-path")
    other = tmp_path / "other-journal"
    other.write_bytes(b"other")

    def swap(stage: str, path: Path) -> None:
        if stage == "after_first_read":
            os.replace(other, Path(f"{db}-journal"))

    monkeypatch.setattr(dbfile, "_TEST_SEAM", swap)
    refusal = _refuse(db, _journal_writer(db))
    assert str(refusal).startswith(
        "db_sibling_unsafe: open -journal descriptor is not the file at ledger.db-journal:"
    ), refusal


def test_open_rollback_journal_anchor_failure_is_unattributable(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _make_database(db, "at-path")
    monkeypatch.setattr(_sqlite_handle, "_journal_filename", lambda main_name: main_name)
    refusal = _refuse(db, _journal_writer(db))
    assert str(refusal).startswith(
        "db_fd_unattributable: -journal file object does not hold"
    ), refusal


# --- H: the handle path on each platform ----------------------------------------


def test_capture_hook_is_installed_and_self_tested() -> None:
    lib = _sqlite_handle.ensure_hook()
    assert _sqlite_handle._hook is not None and _sqlite_handle._hook.error is None
    assert lib.sqlite3_libversion().decode("ascii") == sqlite3.sqlite_version
    with _sqlite_handle.capturing() as captured:
        conn = sqlite3.connect(":memory:")
    conn.close()
    assert len(captured) == 1


@pytest.mark.parametrize(
    "kind, journal",
    [
        ("C", "fresh"),
        ("E", "wal"),
        ("R", "wal"),
        ("E", "rollback"),
        ("R", "rollback"),
        ("I", "wal"),
        ("I", "rollback"),
    ],
)
def test_handle_reads_name_the_files_at_their_names(
    tmp_path: Path, kind: str, journal: str
) -> None:
    db = tmp_path / "ledger.db"
    if journal != "fresh":
        _seed(db).close()
    if journal == "rollback":
        _set_rollback_mode(db)
    _sqlite_handle.ensure_hook()
    with _sqlite_handle.capturing() as captured:
        conn = _opener(db, kind)()
    try:
        assert len(captured) == 1
        handle = captured[0]
        assert _sqlite_handle.vfs_name(handle).startswith("unix")
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        assert _fd_id(_sqlite_handle.main_fd(handle)) == _path_id(db)
        if journal == "wal" and kind != "I":
            assert mode == "wal"
            assert _fd_id(_sqlite_handle.wal_fd(handle)) == _path_id(Path(f"{db}-wal"))
            assert _fd_id(_sqlite_handle.shm_fd(handle)) == _path_id(Path(f"{db}-shm"))
        else:
            assert _sqlite_handle.journal_fd(handle) is None
            assert _sqlite_handle.shm_fd(handle) is None
    finally:
        conn.close()


@pytest.mark.parametrize("kind", ["C", "E", "R", "I"])
def test_unavailable_capture_hook_refuses_every_kind(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db = tmp_path / "ledger.db"
    if kind != "C":
        _seed(db).close()

    def hidden() -> None:
        raise OSError("SQLite symbols hidden by test")

    monkeypatch.setattr(_sqlite_handle, "_hook", None)
    monkeypatch.setattr(_sqlite_handle, "_load_library", hidden)
    refusal = _refuse_kind(db, kind)
    assert str(refusal).startswith(
        "db_fd_unattributable: connection capture unavailable: SQLite library unavailable:"
    ), refusal
    if kind == "C":
        assert not db.exists(), "a refused create must not create the database"


def test_capture_hook_from_another_sqlite_build_refuses(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    monkeypatch.setattr(_sqlite_handle, "_hook", None)
    monkeypatch.setattr(_sqlite_handle.sqlite3, "sqlite_version", "0.0.0")
    refusal = _refuse_kind(db, "E")
    assert refusal.code == "db_fd_unattributable"
    assert "the sqlite3 module uses 0.0.0" in str(refusal)


def test_capture_self_test_failure_refuses_in_a_fresh_process(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    program = (
        "import sqlite3, sys\n"
        "import ledger._sqlite_handle as h\n"
        "from ledger import dbfile\n"
        "real = sqlite3.connect\n"
        "def unseen(*a, **k):\n"
        "    saved, h._local.captured = h._local.captured, None\n"
        "    try:\n"
        "        return real(*a, **k)\n"
        "    finally:\n"
        "        h._local.captured = saved\n"
        "sqlite3.connect = unseen\n"
        "try:\n"
        "    dbfile.checked_connect(dbfile.Path(sys.argv[1]), lambda: real(sys.argv[1]))\n"
        "except dbfile.UnsafeDatabaseFile as e:\n"
        "    print(e)\n"
        "else:\n"
        "    print('accepted without a working capture')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, str(db)],
        cwd=ROOT, text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(
        "db_fd_unattributable: connection capture unavailable: "
        "capture self-test saw 0 connections, expected 1:"
    ), result.stdout


@pytest.mark.parametrize("kind", ["E", "R"])
def test_main_anchor_failure_refuses_without_reading_the_descriptor(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    reads: list[int] = []
    real_int = _sqlite_handle._int

    def recording_int(address: int) -> int:
        reads.append(address)
        return real_int(address)

    # Read zPath from the pointer slot before it: another pointer, never dereferenced.
    monkeypatch.setattr(
        _sqlite_handle, "_OFF_ZPATH", _sqlite_handle._UnixFile.pPreallocatedUnused.offset
    )
    monkeypatch.setattr(_sqlite_handle, "_int", recording_int)
    refusal = _refuse_kind(db, kind)
    assert str(refusal).startswith(
        "db_fd_unattributable: main file object does not hold"
    ), refusal
    assert reads == [], "no descriptor may be read before the main anchor holds"


@pytest.mark.parametrize("kind", ["E", "R"])
def test_non_unix_vfs_refuses(tmp_path: Path, monkeypatch, kind: str) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    monkeypatch.setattr(_sqlite_handle, "vfs_name", lambda _handle: "memdb")
    refusal = _refuse_kind(db, kind)
    assert str(refusal).startswith(
        "db_fd_unattributable: connection uses the 'memdb' VFS, not unix:"
    ), refusal


def test_vfs_file_object_smaller_than_the_read_layout_refuses(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    monkeypatch.setattr(_sqlite_handle, "_MIN_OS_FILE", 1 << 20)
    refusal = _refuse_kind(db, "E")
    assert str(refusal).startswith("db_fd_unattributable: VFS file object is"), refusal


def test_wal_anchor_failure_refuses(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    monkeypatch.setattr(_sqlite_handle, "_wal_filename", lambda main_name: main_name)
    refusal = _refuse_kind(db, "R")
    assert str(refusal).startswith(
        "db_fd_unattributable: -wal file object does not hold"
    ), refusal


def test_shm_anchor_failure_refuses_without_reading_the_shm_descriptor(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    in_shm = threading.local()
    shm_reads: list[int] = []
    real_int = _sqlite_handle._int
    real_shm_fd = _sqlite_handle.shm_fd

    def recording_int(address: int) -> int:
        if getattr(in_shm, "active", False):
            shm_reads.append(address)
        return real_int(address)

    def tracked_shm_fd(handle: int):
        in_shm.active = True
        try:
            return real_shm_fd(handle)
        finally:
            in_shm.active = False

    # Read the main pInode from the pVfs slot: another pointer, never dereferenced.
    monkeypatch.setattr(_sqlite_handle, "_OFF_PINODE", _sqlite_handle._UnixFile.pVfs.offset)
    monkeypatch.setattr(_sqlite_handle, "_int", recording_int)
    monkeypatch.setattr(_sqlite_handle, "shm_fd", tracked_shm_fd)
    refusal = _refuse_kind(db, "R")
    assert str(refusal).startswith(
        "db_fd_unattributable: -shm node does not belong to the connection's database file:"
    ), refusal
    assert shm_reads == [], "hShm must not be read before the pInode anchor holds"


_LAYOUT_NAMES = (
    "_OFF_PINODE",
    "_OFF_H",
    "_OFF_ZPATH",
    "_OFF_PSHM",
    "_SHM_NODE",
    "_NODE_PINODE",
    "_NODE_HSHM",
    "_MIN_OS_FILE",
)


def _layout_with_pointer_width(width: int) -> dict[str, int]:
    """The module's os_unix.c offsets when every pointer is ``width`` bytes.

    A fresh interpreter replaces ``ctypes.c_void_p`` with an unsigned integer
    of that width before importing the module, so the module computes its
    offsets for that pointer size; the host's int and short sizes stay.
    """
    program = (
        "import ctypes, json, sys\n"
        "ctypes.c_void_p = {4: ctypes.c_uint32, 8: ctypes.c_uint64}[int(sys.argv[1])]\n"
        "import ledger._sqlite_handle as h\n"
        "print(json.dumps({n: getattr(h, n) for n in sys.argv[2:]}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, str(width), *_LAYOUT_NAMES],
        cwd=ROOT, text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_unix_file_offsets_follow_the_32_bit_abi() -> None:
    # os_unix.c on a 4-byte-pointer ABI: h at 12, eFileLock 16, ctrlFlags 18,
    # lastErrno 20, lockingContext 24, pPreallocatedUnused 28, zPath 32, pShm 36.
    assert _layout_with_pointer_width(4) == {
        "_OFF_PINODE": 8,
        "_OFF_H": 12,
        "_OFF_ZPATH": 32,
        "_OFF_PSHM": 36,
        "_SHM_NODE": 0,
        "_NODE_PINODE": 0,
        "_NODE_HSHM": 12,
        "_MIN_OS_FILE": 40,
    }


def test_unix_file_offsets_on_64_bit_are_unchanged() -> None:
    # On 64-bit the struct-derived offsets equal the pointer multiples read before.
    expected = {
        "_OFF_PINODE": 2 * 8,
        "_OFF_H": 3 * 8,
        "_OFF_ZPATH": 7 * 8,
        "_OFF_PSHM": 8 * 8,
        "_SHM_NODE": 0,
        "_NODE_PINODE": 0,
        "_NODE_HSHM": 3 * 8,
        "_MIN_OS_FILE": 9 * 8,
    }
    assert _layout_with_pointer_width(8) == expected
    native = {name: getattr(_sqlite_handle, name) for name in _LAYOUT_NAMES}
    assert native == _layout_with_pointer_width(ctypes.sizeof(ctypes.c_void_p))


def _run_child(program: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", program, *args],
        cwd=ROOT, text=True, capture_output=True, timeout=120, check=False,
    )


def test_exit_handler_registered_before_the_first_open_is_refused(
    tmp_path: Path,
) -> None:
    # Accepted limit. The capture hook is cancelled through atexit when it is
    # installed, so SQLite never calls it after teardown frees it. Exit handlers
    # run last-registered first: one registered after the first open runs while
    # the hook is live and opens the ledger; one registered before it runs after
    # the cancel, captures no connection, and is refused.
    db = tmp_path / "ledger.db"
    program = (
        "import atexit, sys\n"
        "from pathlib import Path\n"
        "from ledger import open_ledger\n"
        "def opener(label):\n"
        "    def at_exit():\n"
        "        try:\n"
        "            open_ledger(Path(sys.argv[1])).close()\n"
        "        except BaseException as exc:\n"
        "            print(f'{label} exit open refused: {exc}', flush=True)\n"
        "        else:\n"
        "            print(f'{label} exit open accepted', flush=True)\n"
        "    return at_exit\n"
        "atexit.register(opener('early'))\n"
        "open_ledger(Path(sys.argv[1])).close()\n"
        "atexit.register(opener('late'))\n"
        "print('first open accepted', flush=True)\n"
    )
    result = _run_child(program, str(db))
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[:2] == ["first open accepted", "late exit open accepted"], result.stdout
    assert len(lines) == 3, result.stdout
    assert lines[2].startswith(
        "early exit open refused: db_fd_unknown: connect opened 0 connections, expected 1: "
    ), result.stdout


_TEARDOWN_CONNECTS = r'''
import os, sqlite3, sys
from pathlib import Path
from ledger import open_ledger

open_ledger(Path(sys.argv[1])).close()


class LateConnect:
    def __init__(self, name):
        self.name = name

    def __del__(self, connect=sqlite3.connect, write=os.write, error=BaseException):
        try:
            connect(":memory:").close()
        except error as exc:
            write(1, ("late connect refused " + self.name + ": " + str(exc) + "\n").encode())
        else:
            write(1, ("late connect accepted " + self.name + "\n").encode())


for name, module in list(sys.modules.items()):
    try:
        setattr(module, "_late_connect", LateConnect(name))
    except Exception:
        pass
print("main done", flush=True)
'''


def test_connects_during_interpreter_teardown_never_reach_a_freed_hook(
    tmp_path: Path,
) -> None:
    # After a checked open, every loaded module holds an object that connects
    # to SQLite when teardown collects it, so connects run while teardown
    # clears the hook's module and frees its callback. The hook is cancelled at
    # exit, before teardown, so SQLite never calls it then: no connect is
    # refused by a failing hook and the process does not crash (SIGSEGV).
    result = _run_child(_TEARDOWN_CONNECTS, str(tmp_path / "ledger.db"))
    output = result.stdout + result.stderr
    tail = output.splitlines()[-4:]
    assert result.returncode == 0, f"child exited {result.returncode}: {tail}"
    lines = result.stdout.splitlines()
    assert "main done" in lines, tail
    assert any(line.startswith("late connect accepted ") for line in lines), tail
    refused = [line for line in output.splitlines() if "automatic extension" in line]
    assert refused == [], refused[:4]


_SUBINTERPRETER_OPEN = r'''
import sqlite3, sys
from pathlib import Path

# A legacy (shared-GIL) subinterpreter, through the API each Python has.
if sys.version_info >= (3, 13):
    import _interpreters as interpreters
    interp = interpreters.create("legacy")
else:
    import _xxsubinterpreters as interpreters
    interp = interpreters.create(isolated=False)
code = (
    "import sys\n"
    "sys.path.insert(0, " + repr(sys.argv[1]) + ")\n"
    "from pathlib import Path\n"
    "from ledger import open_ledger\n"
    "open_ledger(Path(" + repr(sys.argv[2]) + ")).close()\n"
    "print('subinterpreter open accepted', flush=True)\n"
)
interpreters.run_string(interp, code)
interpreters.destroy(interp)
print("subinterpreter destroyed", flush=True)
sqlite3.connect(":memory:").close()
print("main connect accepted", flush=True)
from ledger import open_ledger
open_ledger(Path(sys.argv[2])).close()
print("main open accepted", flush=True)
'''


def test_hook_installed_in_a_destroyed_subinterpreter_is_not_called_after(
    tmp_path: Path,
) -> None:
    # A subinterpreter's first open installs the hook with a callback that
    # interpreter owns. Its exit handlers run when it is destroyed and cancel
    # the hook, so a later connect in the main interpreter does not call into
    # the destroyed interpreter (SIGSEGV), and the main interpreter's own first
    # open installs its own hook.
    result = _run_child(_SUBINTERPRETER_OPEN, str(ROOT), str(tmp_path / "ledger.db"))
    output = result.stdout + result.stderr
    tail = output.splitlines()[-4:]
    assert result.returncode == 0, f"child exited {result.returncode}: {tail}"
    assert result.stdout.splitlines() == [
        "subinterpreter open accepted",
        "subinterpreter destroyed",
        "main connect accepted",
        "main open accepted",
    ], output[-3000:]


def test_anchors_hold_for_threaded_connects(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()

    def worker(kind: str) -> Callable[[], None]:
        def work() -> None:
            conn = _checked(db, kind)
            try:
                conn.execute("SELECT count(*) FROM events").fetchone()
            finally:
                conn.close()

        return work

    total, errors = _hammer(1.5, [worker(kind) for kind in ("E", "R") * 3])
    assert errors == []
    assert total >= 6
