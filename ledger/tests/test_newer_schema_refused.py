"""A database written by a newer schema is refused on every open path.

Forward-only: a reader refuses any ``schema_version`` row it does not package
(``load_migrations()``), before it migrates, writes or reads anything. A
database with only known versions, or a fresh one, opens as before. The
read-write open refuses before it changes anything in the file, and every
write re-checks under its own write lock, so a newer writer that migrates the
database after an open is never written into. Every database here is built by
the module's own writer under tmp_path and closed.
"""
from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ledger import open_ledger, store
from ledger.backup import backup_db
from ledger.cli import main
from ledger.restore import replay_events
from ledger.store import (
    BUSY_TIMEOUT_MS,
    Ledger,
    LedgerError,
    connect,
    load_migrations,
    migrate,
    open_ledger_readonly,
)
from ledger.verbs import EXIT_REFUSED
from ledger.verbs import doctor as doctor_verb

HIGHEST = max(m.version for m in load_migrations())
NEWER = HIGHEST + 1


def _versions(db: Path) -> list[tuple[int, str]]:
    conn = sqlite3.connect(db)
    try:
        return list(conn.execute("SELECT version, name FROM schema_version ORDER BY version"))
    finally:
        conn.close()


def _add_version(db: Path, version: int) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO schema_version (version, name) VALUES (?, ?)",
            (version, f"{version:04d}_from_a_newer_checkout.sql"),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def current(tmp_path: Path) -> Path:
    """A closed, fully migrated database with one event and no side files."""
    db = tmp_path / "ledger" / "home.db"
    lg = open_ledger(db)
    lg.append(kind="item.created", item_id="T1", payload={"type": "ticket", "title": "one"},
              idempotency_key="k-T1", harness="fixture")
    lg.close()
    return db


@pytest.fixture
def newer(current: Path) -> Path:
    """The same database after a newer checkout recorded version HIGHEST + 1."""
    _add_version(current, NEWER)
    return current


def _assert_names_both(message: str, unknown: int, highest: int) -> None:
    assert f"{unknown:04d}" in message
    assert f"highest packaged version is {highest:04d}" in message
    assert "update the checkout" in message


def test_read_write_open_refuses_a_newer_schema_naming_both_versions(newer: Path) -> None:
    with pytest.raises(LedgerError) as exc:
        open_ledger(newer)
    _assert_names_both(str(exc.value), NEWER, HIGHEST)


def test_read_only_open_refuses_a_newer_schema_naming_both_versions(newer: Path) -> None:
    with pytest.raises(LedgerError) as exc:
        open_ledger_readonly(newer)
    _assert_names_both(str(exc.value), NEWER, HIGHEST)


def test_refusal_writes_nothing_on_either_path(newer: Path) -> None:
    before_bytes = newer.read_bytes()
    before_rows = _versions(newer)
    for opener in (open_ledger, open_ledger_readonly):
        with pytest.raises(LedgerError):
            opener(newer)
        assert newer.read_bytes() == before_bytes
        assert _versions(newer) == before_rows
    assert before_rows[-1][0] == NEWER
    assert sorted(p.name for p in newer.parent.iterdir()) == [newer.name]


def test_a_newer_schema_is_refused_even_when_this_reader_also_has_pending_migrations(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ledger" / "home.db"
    conn = connect(db)
    migrate(conn, load_migrations()[:2])
    conn.close()
    _add_version(db, NEWER)
    before_bytes = db.read_bytes()
    before_rows = _versions(db)

    with pytest.raises(LedgerError) as exc:
        open_ledger(db)
    _assert_names_both(str(exc.value), NEWER, HIGHEST)
    with pytest.raises(LedgerError) as exc:
        open_ledger_readonly(db)
    _assert_names_both(str(exc.value), NEWER, HIGHEST)

    assert db.read_bytes() == before_bytes
    assert _versions(db) == before_rows
    assert [v for v, _ in before_rows] == [1, 2, NEWER]


def test_a_concurrent_newer_writer_is_caught_inside_the_migration_transaction(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger" / "home.db"
    conn = connect(db)
    migrate(conn, load_migrations()[:2])
    conn.close()
    real = store.applied_versions
    calls: list[int] = []

    def racing(c: sqlite3.Connection) -> set[int]:
        calls.append(1)
        if len(calls) == 2:
            # The re-read inside BEGIN IMMEDIATE: a newer checkout got in first.
            c.execute(
                "INSERT INTO schema_version (version, name) VALUES (?, ?)",
                (NEWER, f"{NEWER:04d}_from_a_newer_checkout.sql"),
            )
        return real(c)

    monkeypatch.setattr(store, "applied_versions", racing)

    with pytest.raises(LedgerError) as exc:
        open_ledger(db)
    _assert_names_both(str(exc.value), NEWER, HIGHEST)
    assert len(calls) == 2
    assert [v for v, _ in _versions(db)] == [1, 2]


def test_an_unknown_version_below_the_highest_is_refused_too(
    current: Path, monkeypatch
) -> None:
    # This reader packages 1,2,3,5; the database records 4. Version 4 is below
    # the highest packaged version (5), so a max-only comparison would pass it.
    packaged = [m for m in load_migrations() if m.version != 4]
    assert max(m.version for m in packaged) == HIGHEST > 4
    monkeypatch.setattr(store, "load_migrations", lambda: packaged)
    before_bytes = current.read_bytes()

    with pytest.raises(LedgerError) as exc:
        open_ledger(current)
    _assert_names_both(str(exc.value), 4, HIGHEST)
    with pytest.raises(LedgerError) as exc:
        open_ledger_readonly(current)
    _assert_names_both(str(exc.value), 4, HIGHEST)
    assert current.read_bytes() == before_bytes


def test_a_database_with_only_known_versions_opens_as_before(current: Path) -> None:
    before_bytes = current.read_bytes()
    with open_ledger_readonly(current) as lg:
        assert isinstance(lg, Ledger)
        assert [row["item_id"] for row in lg.items()] == ["T1"]
    assert current.read_bytes() == before_bytes
    with open_ledger(current) as lg:
        assert [row["item_id"] for row in lg.items()] == ["T1"]
    assert [v for v, _ in _versions(current)] == [m.version for m in load_migrations()]


def test_a_fresh_database_opens_and_migrates_as_before(tmp_path: Path) -> None:
    db = tmp_path / "ledger" / "fresh.db"
    with open_ledger(db) as lg:
        assert lg.items() == []
    assert [v for v, _ in _versions(db)] == [m.version for m in load_migrations()]
    with open_ledger_readonly(db) as lg:
        assert lg.items() == []


# The refusal changes nothing in the file; every write re-checks under its lock.


def _delete_mode_newer_ledger(db: Path) -> Path:
    """A real ledger that a newer checkout migrated and left in rollback-journal mode."""
    open_ledger(db).close()
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        assert conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
        conn.execute(
            "INSERT INTO schema_version (version, name) VALUES (?, ?)",
            (NEWER, f"{NEWER:04d}_from_a_newer_checkout.sql"),
        )
    finally:
        conn.close()
    return db


def _sha256(db: Path) -> str:
    return hashlib.sha256(db.read_bytes()).hexdigest()


def _journal_header(db: Path) -> bytes:
    """File-format header bytes 18-19: 1,1 for a rollback journal, 2,2 for WAL."""
    return db.read_bytes()[18:20]


@pytest.mark.parametrize("opener", [open_ledger, open_ledger_readonly],
                         ids=["open_ledger", "open_ledger_readonly"])
def test_refusing_a_newer_schema_leaves_the_file_byte_identical(tmp_path: Path, opener) -> None:
    db = _delete_mode_newer_ledger(tmp_path / "ledger" / "home.db")
    before_sha = _sha256(db)
    before_files = sorted(p.name for p in db.parent.iterdir())
    assert before_files == [db.name]
    assert _journal_header(db) == b"\x01\x01"

    with pytest.raises(LedgerError) as exc:
        opener(db)
    _assert_names_both(str(exc.value), NEWER, HIGHEST)

    # Read with no connection first: the bytes, the header's journal mode and
    # the directory listing (no -wal, -shm or -journal left behind).
    assert _sha256(db) == before_sha
    assert _journal_header(db) == b"\x01\x01"
    assert sorted(p.name for p in db.parent.iterdir()) == before_files
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        conn.close()


def _spec(n: int) -> dict:
    return {"kind": "item.created", "item_id": f"T{n}",
            "payload": {"type": "ticket", "title": f"item {n}"},
            "idempotency_key": f"k-T{n}", "harness": "fixture"}


def _write_append(lg: Ledger, events: list) -> None:
    lg.append(**_spec(1))


def _write_append_many(lg: Ledger, events: list) -> None:
    lg.append_many([_spec(1), _spec(2)])


def _write_transaction(lg: Ledger, events: list) -> None:
    with lg.transaction():
        lg.append(**_spec(1))


def _write_claim_system(lg: Ledger, events: list) -> None:
    lg.claim_system("home")


def _write_replay_events(lg: Ledger, events: list) -> None:
    replay_events(lg, events)


WRITE_PATHS = {
    "append": _write_append,
    "append_many": _write_append_many,
    "transaction": _write_transaction,
    "claim_system": _write_claim_system,
    "replay_events": _write_replay_events,
}


def _event_count(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()


@pytest.fixture
def source_events(tmp_path: Path) -> list:
    """One exported-shape event from another ledger, for the replay path."""
    with open_ledger(tmp_path / "source" / "source.db") as src:
        src.append(**_spec(1))
        return src.events()


@pytest.mark.parametrize("write", list(WRITE_PATHS))
def test_every_write_is_refused_once_a_newer_writer_migrates_the_open_database(
    tmp_path: Path, source_events: list, write: str
) -> None:
    db = tmp_path / "ledger" / "home.db"
    lg = open_ledger(db)
    try:
        assert _event_count(db) == 0
        # The newer writer: a second connection commits a schema version this
        # code does not package, after this handle's open-time checks passed.
        newer = sqlite3.connect(db, isolation_level=None)
        try:
            newer.execute(
                "INSERT INTO schema_version (version, name) VALUES (?, ?)",
                (NEWER, f"{NEWER:04d}_from_a_newer_checkout.sql"),
            )
        finally:
            newer.close()

        with pytest.raises(LedgerError) as exc:
            WRITE_PATHS[write](lg, source_events)
        _assert_names_both(str(exc.value), NEWER, HIGHEST)
        assert lg._conn.in_transaction is False
    finally:
        lg.close()
    assert _event_count(db) == 0
    assert [v for v, _ in _versions(db)][-1] == NEWER


def test_read_only_opens_take_no_write_lock(current: Path) -> None:
    holder = sqlite3.connect(current, isolation_level=None, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    try:
        for opener in (open_ledger_readonly, open_ledger):
            started = time.monotonic()
            with opener(current) as lg:
                assert [row["item_id"] for row in lg.items()] == ["T1"]
            assert time.monotonic() - started < BUSY_TIMEOUT_MS / 1000, opener.__name__
        assert holder.in_transaction is True
    finally:
        holder.execute("ROLLBACK")
        holder.close()


def _wal_mode_newer_ledger(db: Path) -> Path:
    """A real ledger that a newer checkout migrated and left in WAL mode."""
    open_ledger(db).close()
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute(
            "INSERT INTO schema_version (version, name) VALUES (?, ?)",
            (NEWER, f"{NEWER:04d}_from_a_newer_checkout.sql"),
        )
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    return db


def _is_journal_mode_wal_assign(sql: str) -> bool:
    compact = "".join(sql.lower().split())
    return "pragmajournal_mode=" in compact and compact.split(
        "pragmajournal_mode=", 1)[1].startswith("wal")


def _is_mutating_sql(sql: str) -> bool:
    compact = "".join(sql.lower().split())
    if compact.startswith("pragmajournal_mode="):
        return True
    token = sql.strip().split(None, 1)[0].upper() if sql.strip() else ""
    return token in {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER", "REPLACE"}


@pytest.mark.parametrize("opener", [open_ledger, open_ledger_readonly],
                         ids=["open_ledger", "open_ledger_readonly"])
def test_refusing_a_wal_newer_schema_leaves_the_file_byte_identical(
    tmp_path: Path, opener
) -> None:
    db = _wal_mode_newer_ledger(tmp_path / "ledger" / "home.db")
    before_sha = _sha256(db)
    before_files = sorted(p.name for p in db.parent.iterdir())
    assert before_files == [db.name]
    assert _journal_header(db) == b"\x02\x02"

    with pytest.raises(LedgerError) as exc:
        opener(db)
    _assert_names_both(str(exc.value), NEWER, HIGHEST)

    assert _sha256(db) == before_sha
    assert _journal_header(db) == b"\x02\x02"
    after_files = sorted(p.name for p in db.parent.iterdir())
    if opener.__name__ == "open_ledger":
        assert after_files == before_files
    else:
        extra = set(after_files) - set(before_files)
        assert extra <= {f"{db.name}-wal", f"{db.name}-shm"}
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_open_refuses_when_a_newer_writer_migrates_between_precheck_and_connect(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger" / "home.db"
    open_ledger(db).close()
    setup = sqlite3.connect(db, isolation_level=None)
    try:
        assert setup.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    finally:
        setup.close()
    before_events = _event_count(db)
    before_versions = [v for v, _ in _versions(db)]
    statements: list[str] = []
    real_connect = store.connect

    def racing(path: Path) -> sqlite3.Connection:
        newer = sqlite3.connect(db, isolation_level=None)
        try:
            newer.execute(
                "INSERT INTO schema_version (version, name) VALUES (?, ?)",
                (NEWER, f"{NEWER:04d}_from_a_newer_checkout.sql"),
            )
        finally:
            newer.close()
        conn = real_connect(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(store, "connect", racing)

    with pytest.raises(LedgerError) as exc:
        open_ledger(db)
    _assert_names_both(str(exc.value), NEWER, HIGHEST)
    assert [v for v, _ in _versions(db)] == before_versions + [NEWER]
    assert _event_count(db) == before_events
    writes = [s for s in statements if _is_mutating_sql(s)]
    assert writes == []


def test_already_wal_current_database_does_not_issue_wal_pragma(
    current: Path, monkeypatch
) -> None:
    probe = sqlite3.connect(f"{current.as_uri()}?mode=ro", uri=True)
    try:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        probe.close()

    seen: list[str] = []
    real_sqlite_connect = store.sqlite3.connect

    def tracing_connect(*args, **kwargs):
        conn = real_sqlite_connect(*args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(store.sqlite3, "connect", tracing_connect)

    with open_ledger(current) as lg:
        assert [row["item_id"] for row in lg.items()] == ["T1"]
    assigns = [s for s in seen if _is_journal_mode_wal_assign(s)]
    assert assigns == []



# Backup and doctor open the database with their own connections, not
# through open_ledger; each refuses a newer schema before it copies, writes or
# reports anything. (The toolkit's markdown import does too; its own suite
# checks that.)


def _cli(monkeypatch, argv: list[str], entry=main) -> tuple[int, str, str]:
    for key, value in {"LEDGER_HARNESS": "pytest", "LEDGER_AGENT": "tester",
                       "LEDGER_MODEL": "test-model", "LEDGER_SESSION": "sess-1"}.items():
        monkeypatch.setenv(key, value)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = entry(argv)
    return code, out.getvalue(), err.getvalue()


def test_backup_refuses_a_newer_schema_and_leaves_no_target(newer: Path, tmp_path: Path) -> None:
    before_sha = _sha256(newer)
    dest = tmp_path / "backups" / "home.db"

    with pytest.raises(LedgerError) as exc:
        backup_db(newer, dest)
    _assert_names_both(str(exc.value), NEWER, HIGHEST)

    assert not dest.exists()
    assert not dest.with_name(dest.name + ".partial").exists()
    assert not dest.parent.exists()
    assert _sha256(newer) == before_sha
    assert sorted(p.name for p in newer.parent.iterdir()) == [newer.name]


def test_backup_of_only_known_versions_still_succeeds(current: Path, tmp_path: Path) -> None:
    dest = tmp_path / "backups" / "home.db"
    assert backup_db(current, dest).integrity == "ok"
    assert [v for v, _ in _versions(dest)] == [m.version for m in load_migrations()]


def test_doctor_refuses_a_newer_schema(newer: Path, monkeypatch) -> None:
    before_sha = _sha256(newer)

    code, out, err = _cli(monkeypatch, ["--json", "--db", str(newer), "doctor"])
    assert code == EXIT_REFUSED, (out, err)
    body = json.loads(out)
    assert body["data"] is None
    _assert_names_both(body["error"]["message"], NEWER, HIGHEST)
    assert _sha256(newer) == before_sha


def test_doctor_on_only_known_versions_reports_as_before(current: Path, monkeypatch) -> None:
    code, out, err = _cli(monkeypatch, ["--json", "--db", str(current), "doctor"])
    assert code == 0, (out, err)
    assert json.loads(out)["data"]["schema_version"] == HIGHEST


# A newer migration committed between the version check and the read it
# guards. The seam is the verb module's own ``_recorded_versions`` name,
# wrapped here so a second connection commits right after the check returns.
# The ledger is in WAL mode, as the store leaves it, so a reader holding a
# transaction keeps its snapshot while the writer commits.


def _commit_after_version_check(monkeypatch, module, commit) -> list[int]:
    real = module._recorded_versions
    calls: list[int] = []

    def racing(conn: sqlite3.Connection) -> set[int]:
        versions = real(conn)
        calls.append(1)
        if len(calls) == 1:
            commit()
        return versions

    monkeypatch.setattr(module, "_recorded_versions", racing)
    return calls


def test_doctor_reports_the_snapshot_its_version_check_passed_on(
    current: Path, monkeypatch
) -> None:
    assert _journal_header(current) == b"\x02\x02"
    calls = _commit_after_version_check(
        monkeypatch, doctor_verb, lambda: _add_version(current, NEWER))

    code, out, err = _cli(monkeypatch, ["--json", "--db", str(current), "doctor"])
    assert code == 0, (out, err)
    assert json.loads(out)["data"]["schema_version"] == HIGHEST
    assert calls == [1]
    assert [v for v, _ in _versions(current)][-1] == NEWER


def test_backup_refuses_a_newer_migration_committed_after_its_version_check(
    current: Path, tmp_path: Path, monkeypatch
) -> None:
    # Backup's pre-check is store's, so the seam is store's own name; the
    # copy is made on a later connection and so records the newer version.
    dest = tmp_path / "backups" / "home.db"
    calls = _commit_after_version_check(
        monkeypatch, store, lambda: _add_version(current, NEWER))

    with pytest.raises(LedgerError) as exc:
        backup_db(current, dest)
    _assert_names_both(str(exc.value), NEWER, HIGHEST)

    assert calls == [1]
    assert [v for v, _ in _versions(current)][-1] == NEWER
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".partial").exists()
