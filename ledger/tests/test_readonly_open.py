"""Read-only ledger opens: bytes unchanged, a read-only directory works, a live writer is read.

Every database here is built by the module's own writer under tmp_path and
closed, so it is a real checkpointed WAL database at rest (no side files).
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

from ledger import UnsafeDatabaseFile, guidance, open_ledger, store
from ledger.store import open_ledger_readonly

SIDE_SUFFIXES = ("-wal", "-shm", "-journal")
TODAY = date(2026, 10, 19)


def _side_files(db: Path) -> list[str]:
    return sorted(p.name for p in db.parent.iterdir() if p.name != db.name)


def _seed(db: Path, title: str, key: str):
    """Append one item through the module's own writer, on a handle the caller holds."""
    lg = open_ledger(db)
    lg.append(kind="item.created", item_id=key, payload={"type": "ticket", "title": title},
              idempotency_key=f"k-{key}", harness="fixture")
    return lg


@pytest.fixture
def at_rest(tmp_path: Path) -> Path:
    """A closed ledger with two items: a checkpointed WAL database, no side files."""
    db = tmp_path / "ledger" / "home.db"
    _seed(db, "first", "T1").close()
    _seed(db, "second", "T2").close()
    assert _side_files(db) == []
    return db


@contextmanager
def read_only_dir(directory: Path):
    """Make ``directory`` unwritable (0555); always restore it, so tmp cleanup works."""
    original = stat.S_IMODE(directory.stat().st_mode)
    os.chmod(directory, 0o555)
    try:
        yield directory
    finally:
        os.chmod(directory, original)


@pytest.fixture
def opened_uris(monkeypatch) -> list[str]:
    """Record the query part of every URI the read-only open connects with."""
    uris: list[str] = []
    real = store._readonly_conn

    def recording(uri):
        uris.append(uri.split("?", 1)[1])
        return real(uri)

    monkeypatch.setattr(store, "_readonly_conn", recording)
    return uris


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _titles(lg) -> list[tuple[str, str]]:
    return sorted((row["item_id"], row["title"]) for row in lg.items())


def test_writable_dir_open_never_changes_the_database_and_leaves_only_sqlite_files(at_rest):
    before = _digest(at_rest)
    with open_ledger_readonly(at_rest) as lg:
        assert _titles(lg) == [("T1", "first"), ("T2", "second")]
    assert _digest(at_rest) == before, "a read-only open must not change the database bytes"
    # Documented behaviour: SQLite coordinates a WAL reader through -wal and -shm,
    # which a mode=ro open creates in a writable directory and leaves behind.
    assert set(_side_files(at_rest)) <= {f"{at_rest.name}-wal", f"{at_rest.name}-shm"}


def test_read_only_dir_open_succeeds_returns_the_same_rows_and_leaves_no_side_files(at_rest):
    with open_ledger_readonly(at_rest) as lg:
        writable_rows = _titles(lg)
        writable_events = [(e.id, e.kind, e.item_id) for e in lg.events()]
    for suffix in SIDE_SUFFIXES:  # forget the files the writable-dir open left
        Path(f"{at_rest}{suffix}").unlink(missing_ok=True)
    before = _digest(at_rest)
    with read_only_dir(at_rest.parent):
        with open_ledger_readonly(at_rest) as lg:
            assert _titles(lg) == writable_rows == [("T1", "first"), ("T2", "second")]
            assert [(e.id, e.kind, e.item_id) for e in lg.events()] == writable_events
        assert _side_files(at_rest) == [], "a read-only directory must stay untouched"
    assert _digest(at_rest) == before


def test_read_only_dir_open_with_a_live_writer_in_a_writable_dir_reads_wal_content(at_rest):
    writer = _seed(at_rest, "third", "T3")
    try:
        with read_only_dir(at_rest.parent):
            with open_ledger_readonly(at_rest) as lg:
                assert ("T3", "third") in _titles(lg)
    finally:
        writer.close()


def test_read_and_write_reopens_refuse_reused_descriptor_while_writer_is_open(at_rest):
    writer = _seed(at_rest, "third", "T3")  # committed, still in the -wal, writer open
    expected = [("T1", "first"), ("T2", "second"), ("T3", "third")]
    try:
        assert (at_rest.parent / f"{at_rest.name}-wal").stat().st_size > 0
        with open_ledger_readonly(at_rest) as reader:
            assert _titles(reader) == expected
        writer.append(kind="item.created", item_id="T4",
                      payload={"type": "ticket", "title": "fourth"},
                      idempotency_key="k-T4", harness="fixture")
        expected.append(("T4", "fourth"))
        for opener in (open_ledger_readonly, open_ledger):
            with pytest.raises(UnsafeDatabaseFile, match=r"^db_fd_unknown:"):
                opener(at_rest)
    finally:
        writer.close()
    for opener in (open_ledger_readonly, open_ledger):
        with opener(at_rest) as reopened:
            assert _titles(reopened) == expected


@pytest.mark.parametrize("leftover, message", [
    ("-journal", "readonly database"),
    ("-shm", "readonly database"),
    # SQLite refuses a lone -wal with CANTOPEN rather than READONLY (3.53.4).
    ("-wal", "unable to open database file"),
])
def test_read_only_dir_with_a_leftover_connection_file_is_refused_not_read_immutable(
        at_rest, opened_uris, leftover, message):
    # A hot journal or a coordination file means a writer may hold the database;
    # the immutable fallback would read around it, so the open must fail instead.
    Path(f"{at_rest}{leftover}").write_bytes(b"")
    with read_only_dir(at_rest.parent):
        with pytest.raises(sqlite3.OperationalError, match=message):
            open_ledger_readonly(at_rest)
    assert opened_uris == ["mode=ro"], "the refused open must never fall back to immutable=1"


class _RefusesEveryQuery:
    """Stands in for a connection SQLite refuses as read-only on its first query."""

    def execute(self, *args, **kwargs):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    def close(self):
        pass


@pytest.mark.parametrize("leftover", ["-wal", "-shm", "-journal"])
def test_a_readonly_refusal_beside_any_leftover_file_is_reraised_not_read_immutable(
        at_rest, monkeypatch, leftover):
    # Inject the SQLite refusal at the checked-connect boundary; a leftover side
    # file must prevent a second, immutable open.
    attempts: list[bool] = []

    def refusing(_path, _opener, *, immutable=False, set_wal=False):
        attempts.append(immutable)
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(store, "checked_connect", refusing)
    Path(f"{at_rest}{leftover}").write_bytes(b"")
    with pytest.raises(sqlite3.OperationalError, match="readonly database"):
        open_ledger_readonly(at_rest)
    assert attempts == [False], "a leftover side file must block the immutable=1 fallback"


def test_a_non_readonly_failure_in_a_read_only_dir_is_reraised_not_read_immutable(
        at_rest, opened_uris):
    # Only a read-only refusal may fall back to immutable=1; any other error from
    # the mode=ro read, with no side files present, must propagate unchanged.
    at_rest.write_bytes(b"this is not a database file " * 40)
    with read_only_dir(at_rest.parent):
        with pytest.raises(sqlite3.DatabaseError, match="file is not a database"):
            open_ledger_readonly(at_rest)
        assert _side_files(at_rest) == []
    assert opened_uris == ["mode=ro"], "a non-readonly error must not fall back to immutable=1"


def _refuses_writes(lg) -> bool:
    try:
        lg.append(kind="item.created", item_id="T9", payload={"type": "ticket", "title": "nope"},
                  idempotency_key="k-T9", harness="fixture")
    except Exception as exc:  # the append wraps SQLite's refusal in a ledger error
        return "readonly database" in str(exc)
    return False


def test_a_read_only_handle_refuses_writes_in_a_writable_and_in_a_read_only_dir(at_rest):
    before = _digest(at_rest)
    with open_ledger_readonly(at_rest) as lg:
        assert _refuses_writes(lg), "mode=ro handle must not accept an append"
    for suffix in SIDE_SUFFIXES:
        Path(f"{at_rest}{suffix}").unlink(missing_ok=True)
    with read_only_dir(at_rest.parent):
        with open_ledger_readonly(at_rest) as lg:
            assert _refuses_writes(lg), "immutable handle must not accept an append"
    assert _digest(at_rest) == before


def test_guidance_reader_in_a_read_only_dir(at_rest):
    with open_ledger(at_rest) as lg:
        guidance.record_guidance(lg, harness="pytest", actor="tester", pool="xai-grok",
                                 direction="burn", reason="weekly reset soon",
                                 starts="2026-10-19", expires="2026-10-25")
    expected = [(g.pool, g.direction) for g in guidance.read_in_force(at_rest, TODAY)]
    assert expected == [("xai-grok", "burn")]
    for suffix in SIDE_SUFFIXES:
        Path(f"{at_rest}{suffix}").unlink(missing_ok=True)
    with read_only_dir(at_rest.parent):
        found = [(g.pool, g.direction) for g in guidance.read_in_force(at_rest, TODAY)]
        assert _side_files(at_rest) == []
    assert found == expected
