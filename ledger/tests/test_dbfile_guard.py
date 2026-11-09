"""Database opens are bound to safe files and their descriptors."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

import ledger.backup as backup_mod
import ledger.dbfile as dbfile
import ledger.verbs.doctor as doctor_mod
from ledger import open_ledger, resolve_db_path
from ledger.backup import backup_db
from ledger.cli import main
from ledger.dbfile import UnsafeDatabaseFile
from ledger.store import open_ledger_readonly
from ledger.verbs import EXIT_REFUSED

ROOT = Path(__file__).resolve().parents[2]


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


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_planted_sibling_symlink_is_refused(tmp_path: Path, suffix: str) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    victim = tmp_path / "victim"
    victim.write_bytes(b"victim")
    os.symlink(victim, Path(f"{db}{suffix}"))

    with pytest.raises(UnsafeDatabaseFile, match=r"^db_sibling_unsafe:"):
        open_ledger_readonly(db)
    assert victim.read_bytes() == b"victim"


def test_planted_shm_hard_link_is_refused_without_touching_victim(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    victim = tmp_path / "victim"
    before = b"v" * 6000
    victim.write_bytes(before)
    os.link(victim, Path(f"{db}-shm"))

    with pytest.raises(UnsafeDatabaseFile, match=r"^db_sibling_unsafe:"):
        open_ledger_readonly(db)
    assert victim.read_bytes() == before


def test_main_descriptor_swap_after_connect_is_refused(tmp_path: Path, monkeypatch) -> None:
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
    with pytest.raises(UnsafeDatabaseFile, match=r"^db_swapped:"):
        open_ledger_readonly(db)
    assert fired


def test_unrelated_descriptor_opened_during_connect_is_refused(tmp_path: Path, monkeypatch) -> None:
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
        with pytest.raises(UnsafeDatabaseFile, match=r"^db_fd_unknown:"):
            open_ledger_readonly(db)
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
        with pytest.raises(UnsafeDatabaseFile, match=r"^db_sibling_unsafe:"):
            open_ledger_readonly(db)
        assert fired
    finally:
        holder.close()


def test_unavailable_descriptor_table_refuses(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    monkeypatch.setattr(dbfile, "_FD_ROOTS", (tmp_path / "missing-fds",))

    with pytest.raises(UnsafeDatabaseFile, match=r"^db_fd_listing_unavailable:"):
        open_ledger_readonly(db)


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


@pytest.mark.parametrize("kind", ["create", "existing", "readonly", "immutable"])
def test_sqlite_connect_opens_exactly_one_main_descriptor(
    tmp_path: Path, kind: str
) -> None:
    db = tmp_path / f"{kind}.db"
    if kind != "create":
        _seed(db).close()
    before = dbfile.snapshot(db)
    if kind == "readonly":
        conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    elif kind == "immutable":
        conn = sqlite3.connect(f"{db.as_uri()}?immutable=1", uri=True)
    else:
        conn = sqlite3.connect(db)
    try:
        after = dbfile.snapshot(db)
        changed = {fd: value for fd, value in after.items() if before.get(fd) != value}
        assert len(changed) == 1
    finally:
        conn.close()


@pytest.mark.parametrize("readonly", [False, True])
def test_first_wal_read_opens_exactly_two_side_descriptors(
    tmp_path: Path, readonly: bool
) -> None:
    db = tmp_path / f"wal-{readonly}.db"
    _seed(db).close()
    before = dbfile.snapshot(db)
    conn = sqlite3.connect(
        f"{db.as_uri()}?mode=ro" if readonly else str(db),
        uri=readonly,
    )
    try:
        connected = dbfile.snapshot(db)
        main = {fd: value for fd, value in connected.items() if before.get(fd) != value}
        assert len(main) == 1
        conn.execute("PRAGMA journal_mode").fetchone()
        read = dbfile.snapshot(db)
        side = {fd: value for fd, value in read.items() if connected.get(fd) != value}
        assert len(side) == 2
    finally:
        conn.close()


def test_two_back_to_back_descriptor_snapshots_are_equal() -> None:
    assert dbfile.snapshot(Path("descriptor-probe")) == dbfile.snapshot(Path("descriptor-probe"))


def _set_rollback_mode(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode = DELETE").fetchone()[0] == "delete"
    finally:
        conn.close()


def test_rename_away_and_back_with_reused_descriptor_refuses(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    replacement = tmp_path / "replacement.db"
    held = tmp_path / "ledger.held"

    def make_database(path: Path, value: str) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.execute("PRAGMA journal_mode = DELETE")
            conn.execute("CREATE TABLE identity_value (value TEXT NOT NULL)")
            conn.execute("INSERT INTO identity_value VALUES (?)", (value,))
            conn.commit()
        finally:
            conn.close()

    make_database(db, "database-at-path")
    make_database(replacement, "replacement-database")
    original = sqlite3.connect(db)
    original.execute("SELECT * FROM identity_value").fetchall()
    first_replacement = sqlite3.connect(replacement)
    first_replacement.execute("SELECT 1").fetchall()
    locked_replacement = sqlite3.connect(replacement, isolation_level=None)
    locked_replacement.execute("BEGIN")
    locked_replacement.execute("SELECT * FROM identity_value").fetchall()
    first_replacement.close()
    stages: list[str] = []
    returned: list[sqlite3.Connection] = []

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
    try:
        with pytest.raises(UnsafeDatabaseFile) as exc_info:
            conn, _mode = dbfile.checked_connect(db, lambda: sqlite3.connect(db))
            returned.append(conn)
        assert exc_info.value.code == "db_fd_unknown"
        assert stages == ["before_connect", "after_connect"]
        assert returned == [], "a refused connect must never return its connection"
    finally:
        for conn in returned:
            conn.close()
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
    returned: list[sqlite3.Connection] = []

    def swap_after_connect(stage: str, path: Path) -> None:
        nonlocal fired
        if stage == "after_connect" and path == db:
            os.replace(replacement, db)
            fired = True

    monkeypatch.setattr(dbfile, "_TEST_SEAM", swap_after_connect)
    try:
        with pytest.raises(UnsafeDatabaseFile) as exc_info:
            conn, _mode = dbfile.checked_connect(
                db,
                lambda: sqlite3.connect(
                    f"{db.as_uri()}?mode=rw", uri=True, isolation_level=None
                ),
            )
            returned.append(conn)
        assert exc_info.value.code == "db_fd_unknown"
        assert "connect opened 0 descriptors, expected 1" in str(exc_info.value)
        assert fired
        assert returned == [], "a refused connect must never return its connection"
    finally:
        for conn in returned:
            conn.close()
        held_original.rollback()
        held_original.close()
        held_replacement.close()


def test_zero_new_descriptor_refuses_when_name_points_to_another_file(
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

    with pytest.raises(
        UnsafeDatabaseFile,
        match=r"^db_fd_unknown: connect opened 0 descriptors, expected 1:",
    ):
        dbfile.checked_connect(db, return_parked_after_swap)


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


def test_rollback_first_read_refuses_unrelated_new_descriptor(
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
        with pytest.raises(
            UnsafeDatabaseFile,
            match=r"^db_fd_unknown: first read opened 1 unexpected descriptors:",
        ):
            dbfile.checked_connect(db, lambda: sqlite3.connect(db))
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


def test_descriptor_replaced_at_same_number_counts_as_new(
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
        with pytest.raises(
            UnsafeDatabaseFile,
            match=r"^db_fd_unknown: connect opened 2 descriptors, expected 1:",
        ):
            dbfile.checked_connect(db, lambda: sqlite3.connect(db))
        assert fired


def test_unrelated_descriptor_after_first_wal_read_is_refused(
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
        with pytest.raises(
            UnsafeDatabaseFile,
            match=(
                r"^db_fd_unknown: new descriptor does not uniquely match -wal or -shm:"
            ),
        ):
            open_ledger_readonly(db)
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
    path = tmp_path / "ledger.db"
    before = {10: (1, 2)}
    monkeypatch.setattr(dbfile, "snapshot", lambda _path: {10: (1, 3)})

    with pytest.raises(
        UnsafeDatabaseFile,
        match=r"^db_fd_unknown: main descriptor changed during first read:",
    ):
        dbfile._verify_siblings(
            path, before, (10, (1, 2)), "delete", immutable=False
        )


def test_doctor_summary_does_not_hide_database_refusal(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _seed(db).close()
    victim = tmp_path / "victim"
    victim.write_bytes(b"victim")
    os.symlink(victim, Path(f"{db}-shm"))

    with pytest.raises(UnsafeDatabaseFile, match=r"^db_sibling_unsafe:"):
        doctor_mod._summary(db)
