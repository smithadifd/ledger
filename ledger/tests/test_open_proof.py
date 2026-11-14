"""A write is reported only while the database path still names the files the open proved.

``checked_connect`` returns an ``OpenProof``, and every write checks it again
with ``verify_bound`` once the write is durable and before it is reported:
main, ``-wal``, ``-shm``, then main again. These probes put another file at
one of those names at the ``before_settle`` or ``bound_after_main`` seam and
require a refusal (``db_swapped`` or ``db_sibling_unsafe``), never a success.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Callable

import pytest

import ledger.dbfile as dbfile
import ledger.store as store
from ledger.backup import backup_db, integrity_check
from ledger.cli import main
from ledger.dbfile import UnsafeDatabaseFile, verify_bound
from ledger.export import export_events
from ledger.loop import manage_item
from ledger.restore import restore_export
from ledger.store import LedgerError, open_ledger
from ledger.verbs import EXIT_OK, EXIT_REFUSED, discover_verbs

ITEM = "T-1"

# Every action that writes the database, as test_loop_writer_identity
# enumerates them: the registry's writing verbs, plus ``doc add`` (``doc`` is
# a read verb with one write action) and ``restore``.
WRITE_ACTIONS = (
    "append",
    "append-many",
    "capture",
    "claim",
    "claim-system",
    "doc",
    "elect",
    "entity",
    "guidance",
    "link",
    "new",
    "note",
    "pick",
    "restore",
    "spec",
    "state",
)


def _same(path: Path, target: Path) -> bool:
    return path.name == target.name and path.parent.resolve() == target.parent.resolve()


def _identity(path: Path | str) -> tuple[int, int]:
    found = os.lstat(path)
    return found.st_dev, found.st_ino


def _max_id(db: Path) -> int:
    """The newest event id at ``db``, read on a connection of its own."""
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone() is None:
            return 0
        return conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]
    finally:
        conn.close()


def _created_items(db: Path) -> list[str]:
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT item_id FROM events WHERE kind = 'item.created' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def _ledger_with(path: Path, item_id: str) -> Path:
    """A valid, closed ledger at ``path`` holding one item."""
    with open_ledger(path) as lg:
        lg.append(
            kind="item.created",
            payload={"type": "ticket", "title": f"item {item_id}"},
            item_id=item_id,
            idempotency_key=f"seed-{item_id}",
        )
    return path


def _rollback_db(path: Path, value: str) -> None:
    """A plain rollback-mode database whose one row names it."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("CREATE TABLE identity_value (value TEXT NOT NULL)")
        conn.execute("INSERT INTO identity_value VALUES (?)", (value,))
        conn.commit()
    finally:
        conn.close()


def _arm(monkeypatch, seam: Callable[[str, Path], None]) -> None:
    monkeypatch.setattr(dbfile, "_TEST_SEAM", seam)


def _swap_after_write(
    monkeypatch, db: Path, act: Callable[[], None], *, baseline: int | None = None
) -> list[str]:
    """At the first ``before_settle`` on ``db`` after a write landed, run ``act`` once."""
    fired: list[str] = []
    floor = _max_id(db) if baseline is None else baseline

    def seam(stage: str, path: Path) -> None:
        if (stage == "before_settle" and not fired and _same(path, db)
                and _max_id(db) > floor):
            fired.append(stage)
            act()

    _arm(monkeypatch, seam)
    return fired


def _note(text: str, key: str) -> dict:
    return {
        "kind": "item.note",
        "payload": {"text": text},
        "item_id": ITEM,
        "idempotency_key": key,
    }


# --- the proof itself -------------------------------------------------------


@pytest.mark.parametrize("mode", ("delete", "wal"))
def test_an_untouched_open_proves_its_files_and_stays_bound(tmp_path: Path, mode: str) -> None:
    db = tmp_path / "plain.db"
    conn = sqlite3.connect(db)
    conn.execute(f"PRAGMA journal_mode = {mode}")
    conn.execute("CREATE TABLE t (n INTEGER)")
    conn.commit()
    conn.close()
    conn, got, proof = dbfile.checked_connect(
        db, lambda: sqlite3.connect(str(db), isolation_level=None)
    )
    try:
        assert got == proof.mode == mode
        assert _same(proof.path, db)
        assert proof.main[1] == _identity(db)
        if mode == "wal":
            assert proof.wal is not None and proof.shm is not None
            assert proof.wal[1] == _identity(f"{db}-wal")
            assert proof.shm[1] == _identity(f"{db}-shm")
        else:
            assert proof.wal is None and proof.shm is None
        conn.execute("INSERT INTO t VALUES (1)")
        assert verify_bound(proof) is None
    finally:
        conn.close()


def test_open_ledger_holds_a_wal_proof_and_every_write_settles(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        proof = lg._proof
        assert proof.mode == "wal"
        assert proof.wal is not None and proof.shm is not None
        stages: list[str] = []
        _arm(monkeypatch, lambda stage, path: stages.append(stage) if _same(path, db) else None)
        lg.append(
            kind="item.created",
            payload={"type": "ticket", "title": "settled"},
            item_id=ITEM,
            idempotency_key="k-settled",
        )
        assert stages == ["before_settle", "bound_after_main", "bound_after_siblings"]


def test_the_proof_not_the_live_descriptor_is_the_reference(tmp_path: Path) -> None:
    # The proved descriptor number now holds another file, and the path names
    # that file: name and descriptor agree, but neither is the proved file.
    db = tmp_path / "proved.db"
    other = tmp_path / "other.db"
    _rollback_db(db, "proved")
    _rollback_db(other, "other")
    conn, _mode, proof = dbfile.checked_connect(
        db, lambda: sqlite3.connect(str(db), isolation_level=None)
    )
    held = os.open(other, os.O_RDWR)
    try:
        os.dup2(held, proof.main[0])
        os.replace(other, db)
        with pytest.raises(UnsafeDatabaseFile) as info:
            verify_bound(proof)
        assert info.value.code == "db_swapped"
    finally:
        os.close(held)
        conn.close()


def test_a_proof_naming_another_identity_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "proved.db"
    _rollback_db(db, "proved")
    conn, _mode, proof = dbfile.checked_connect(
        db, lambda: sqlite3.connect(str(db), isolation_level=None)
    )
    try:
        fd, (dev, ino) = proof.main
        with pytest.raises(UnsafeDatabaseFile) as info:
            verify_bound(replace(proof, main=(fd, (dev, ino + 1))))
        assert info.value.code == "db_swapped"
    finally:
        conn.close()


@pytest.mark.parametrize("change", ("hard-link", "symlink", "unlink", "rename"))
def test_each_way_the_main_name_loses_its_file_is_db_swapped(
    tmp_path: Path, change: str
) -> None:
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        if change == "hard-link":
            os.link(db, tmp_path / "alias.db")
        elif change == "symlink":
            moved = tmp_path / "moved.db"
            os.replace(db, moved)
            os.symlink(moved, db)
        elif change == "unlink":
            os.replace(db, tmp_path / "moved.db")
        else:
            os.replace(_ledger_with(tmp_path / "other.db", "OTHER-1"), db)
        with pytest.raises(UnsafeDatabaseFile) as info:
            verify_bound(lg._proof)
        assert info.value.code == "db_swapped"


@pytest.mark.parametrize("suffix", ("-wal", "-shm"))
@pytest.mark.parametrize("change", ("hard-link", "replace", "unlink"))
def test_each_way_a_side_name_loses_its_file_is_db_sibling_unsafe(
    tmp_path: Path, suffix: str, change: str
) -> None:
    db = tmp_path / "ledger.db"
    side = Path(f"{db}{suffix}")
    with open_ledger(db) as lg:
        if change == "hard-link":
            os.link(side, tmp_path / "alias")
        elif change == "replace":
            planted = tmp_path / "planted"
            planted.write_bytes(b"")
            os.replace(planted, side)
        else:
            side.unlink()
        with pytest.raises(UnsafeDatabaseFile) as info:
            verify_bound(lg._proof)
        assert info.value.code == "db_sibling_unsafe"
        assert _same(info.value.path, side)


# --- the store's settle points ----------------------------------------------


def test_append_refuses_a_swap_after_its_commit(tmp_path: Path, monkeypatch) -> None:
    db = _ledger_with(tmp_path / "ledger.db", ITEM)
    other = _ledger_with(tmp_path / "other.db", "OTHER-1")
    with open_ledger(db) as lg:
        fired = _swap_after_write(monkeypatch, db, lambda: os.replace(other, db))
        with pytest.raises(UnsafeDatabaseFile) as info:
            lg.append(**_note("after open", "k-1"))
        assert info.value.code == "db_swapped"
        assert fired == ["before_settle"]


def test_an_append_retry_refuses_a_swap_after_its_commit(tmp_path: Path, monkeypatch) -> None:
    db = _ledger_with(tmp_path / "ledger.db", ITEM)
    other = _ledger_with(tmp_path / "other.db", "OTHER-1")
    with open_ledger(db) as lg:
        lg.append(**_note("once", "k-retry"))
        fired: list[str] = []

        def seam(stage: str, path: Path) -> None:
            if stage == "before_settle" and _same(path, db) and not fired:
                fired.append(stage)
                os.replace(other, db)

        _arm(monkeypatch, seam)
        with pytest.raises(UnsafeDatabaseFile) as info:
            lg.append(**_note("once", "k-retry"))
        assert info.value.code == "db_swapped"
        assert fired == ["before_settle"]


def test_a_transaction_refuses_a_swap_after_its_commit(tmp_path: Path, monkeypatch) -> None:
    db = _ledger_with(tmp_path / "ledger.db", ITEM)
    other = _ledger_with(tmp_path / "other.db", "OTHER-1")
    with open_ledger(db) as lg:
        fired = _swap_after_write(monkeypatch, db, lambda: os.replace(other, db))
        with pytest.raises(UnsafeDatabaseFile) as info:
            lg.append_many([_note("first", "k-a"), _note("second", "k-b")])
        assert info.value.code == "db_swapped"
        assert fired == ["before_settle"]


@pytest.mark.parametrize("suffix", ("-wal", "-shm"))
def test_a_side_file_swap_after_the_commit_is_refused(
    tmp_path: Path, monkeypatch, suffix: str
) -> None:
    db = _ledger_with(tmp_path / "ledger.db", ITEM)
    planted = tmp_path / "planted"
    planted.write_bytes(b"")
    with open_ledger(db) as lg:
        fired = _swap_after_write(
            monkeypatch, db, lambda: os.replace(planted, f"{db}{suffix}")
        )
        with pytest.raises(UnsafeDatabaseFile) as info:
            lg.append(**_note("after open", "k-1"))
        assert info.value.code == "db_sibling_unsafe"
        assert _same(info.value.path, Path(f"{db}{suffix}"))
        assert fired == ["before_settle"]


def test_open_ledger_refuses_a_swap_after_migrate(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    other = _ledger_with(tmp_path / "other.db", "OTHER-1")
    fired: list[str] = []

    def seam(stage: str, path: Path) -> None:
        if stage == "before_settle" and _same(path, db) and not fired:
            fired.append(stage)
            os.replace(other, db)

    _arm(monkeypatch, seam)
    with pytest.raises(UnsafeDatabaseFile) as info:
        open_ledger(db)
    assert info.value.code == "db_swapped"
    assert fired == ["before_settle"]


def test_a_backup_onto_the_path_after_the_main_check_is_refused_by_the_last_check(
    tmp_path: Path, monkeypatch
) -> None:
    # Sol design r2's interleaving: a supported backup replaces the main path
    # after the first main check. -wal and -shm still bind; the final main
    # check refuses.
    db = _ledger_with(tmp_path / "ledger.db", ITEM)
    source = _ledger_with(tmp_path / "other.db", "OTHER-1")
    with open_ledger(db) as lg:
        stages: list[str] = []

        def seam(stage: str, path: Path) -> None:
            if not _same(path, db):
                return
            stages.append(stage)
            if stage == "bound_after_main" and "backup" not in stages:
                stages.append("backup")
                backup_db(source, db)

        _arm(monkeypatch, seam)
        with pytest.raises(UnsafeDatabaseFile) as info:
            lg.append(**_note("interleaved", "k-1"))
        assert info.value.code == "db_swapped"
        assert stages == ["before_settle", "bound_after_main", "backup", "bound_after_siblings"]


def test_claim_system_refuses_a_backup_that_lands_after_its_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    # The claim settles once at its COMMIT and again after the checkpoint
    # reports busy 0; the backup lands between the two.
    db = _ledger_with(tmp_path / "ledger.db", ITEM)
    source = _ledger_with(tmp_path / "other.db", "OTHER-1")
    with open_ledger(db) as lg:
        settles: list[str] = []

        def seam(stage: str, path: Path) -> None:
            if stage == "before_settle" and _same(path, db):
                settles.append(stage)
                if len(settles) == 2:
                    backup_db(source, db)

        _arm(monkeypatch, seam)
        with pytest.raises(UnsafeDatabaseFile) as info:
            lg.claim_system("home")
        assert info.value.code == "db_swapped"
        assert len(settles) == 2


def test_claim_system_with_no_swap_settles_after_commit_and_checkpoint(
    tmp_path: Path, monkeypatch
) -> None:
    db = _ledger_with(tmp_path / "ledger.db", ITEM)
    with open_ledger(db) as lg:
        settles: list[str] = []
        _arm(monkeypatch, lambda stage, path: settles.append(stage)
             if stage == "before_settle" and _same(path, db) else None)
        assert lg.claim_system("home") is True
        assert len(settles) == 2


# --- the close after a refusal ------------------------------------------------


def _held(path: Path | str) -> tuple[tuple[int, int], bytes]:
    return _identity(path), Path(path).read_bytes()


@pytest.mark.parametrize("suffix", ("-wal", "-shm"))
def test_a_file_moved_onto_a_side_name_survives_the_refusal_and_the_close(
    tmp_path: Path, monkeypatch, suffix: str
) -> None:
    # Another file is moved onto the -wal (or -shm) name after the write's
    # COMMIT. The write is refused, and closing the refused connection must
    # not unlink the name: it holds that file now, not the ledger's file.
    db = _ledger_with(tmp_path / "ledger.db", ITEM)
    planted = tmp_path / "planted"
    planted.write_bytes(b"not the ledger's side file")
    side = Path(f"{db}{suffix}")
    published: dict = {}

    def act() -> None:
        os.replace(planted, side)
        published["held"] = _held(side)

    with open_ledger(db) as lg:
        fired = _swap_after_write(monkeypatch, db, act)
        with pytest.raises(UnsafeDatabaseFile) as info:
            lg.append(**_note("after open", "k-1"))
        assert info.value.code == "db_sibling_unsafe"
        assert _same(info.value.path, side)
        assert fired == ["before_settle"]
    assert os.path.lexists(side), f"closing the refused connection removed {side.name}"
    assert _held(side) == published["held"]


@pytest.mark.parametrize("main_change", ("rename", "hard-link"))
def test_a_main_swap_refusal_leaves_the_swapped_in_files_side_names_alone(
    tmp_path: Path, monkeypatch, main_change: str
) -> None:
    # Foreign files are moved onto -wal and -shm, and the main name is either
    # replaced by another database ("rename") or given a second hard link
    # ("hard-link"). The write is refused db_swapped, and the close leaves
    # every name as it was. SQLite itself skips the close checkpoint, and so
    # the -wal delete, once the main file has moved; with a hard link the main
    # name still names the proved file, so only the ledger's close guard keeps
    # the side names.
    db = _ledger_with(tmp_path / "ledger.db", ITEM)
    moved = {
        "-wal": tmp_path / "other-wal",
        "-shm": tmp_path / "other-shm",
    }
    if main_change == "rename":
        moved[""] = _ledger_with(tmp_path / "other.db", "OTHER-1")
    moved["-wal"].write_bytes(b"the moved-in database's -wal")
    moved["-shm"].write_bytes(b"the moved-in database's -shm")
    expected = {suffix: _held(path) for suffix, path in moved.items()}

    def act() -> None:
        if main_change == "hard-link":
            os.link(db, tmp_path / "alias.db")
        for suffix, path in moved.items():
            os.replace(path, f"{db}{suffix}")

    with open_ledger(db) as lg:
        fired = _swap_after_write(monkeypatch, db, act)
        with pytest.raises(UnsafeDatabaseFile) as info:
            lg.append(**_note("after open", "k-1"))
        assert info.value.code == "db_swapped"
        assert fired == ["before_settle"]
    for suffix, held in expected.items():
        name = f"{db}{suffix}"
        assert os.path.lexists(name), f"closing the refused connection removed {name}"
        assert _held(name) == held


def test_a_refused_backup_source_leaves_the_planted_side_name_alone(
    tmp_path: Path, monkeypatch
) -> None:
    source = _ledger_with(tmp_path / "source.db", "SOURCE-1")
    dest = tmp_path / "out" / "copy.db"
    side = Path(f"{source}-wal")
    planted = tmp_path / "planted"
    planted.write_bytes(b"not the source's -wal")
    expected = _held(planted)
    fired: list[str] = []

    def seam(stage: str, path: Path) -> None:
        if stage == "before_settle" and _same(path, source) and not fired:
            fired.append(stage)
            os.replace(planted, side)

    _arm(monkeypatch, seam)
    with pytest.raises(UnsafeDatabaseFile) as info:
        backup_db(source, dest)
    assert fired == ["before_settle"]
    assert info.value.code == "db_sibling_unsafe"
    assert os.path.lexists(side), "closing the refused source connection removed -wal"
    assert _held(side) == expected
    assert list(dest.parent.iterdir()) == []


# --- restore ------------------------------------------------------------------


def _export(tmp_path: Path) -> Path:
    source = _ledger_with(tmp_path / "export-source.db", ITEM)
    export_dir = tmp_path / "export"
    with open_ledger(source) as lg:
        export_events(lg, export_dir)
    return export_dir


def test_restore_refuses_a_swap_and_unlinks_nothing(tmp_path: Path, monkeypatch) -> None:
    export_dir = _export(tmp_path)
    dest = tmp_path / "restored.db"
    other = _ledger_with(tmp_path / "other.db", "OTHER-1")
    other_bytes = other.read_bytes()
    other_id = _identity(other)
    fired = _swap_after_write(monkeypatch, dest, lambda: os.replace(other, dest), baseline=0)
    with pytest.raises(UnsafeDatabaseFile) as info:
        restore_export(export_dir, dest, system="home")
    assert fired == ["before_settle"]
    assert info.value.code == "db_swapped"
    assert "restore unlinked none of" in str(info.value)
    assert _identity(dest) == other_id
    assert dest.read_bytes() == other_bytes


def test_a_restore_that_fails_with_no_swap_still_removes_its_files(
    tmp_path: Path, monkeypatch
) -> None:
    export_dir = _export(tmp_path)
    dest = tmp_path / "restored.db"

    def broken_verify(self, *, system=None):
        raise LedgerError("injected verify failure")

    monkeypatch.setattr(store.Ledger, "verify", broken_verify)
    with pytest.raises(LedgerError, match="injected verify failure"):
        restore_export(export_dir, dest, system="home")
    for name in (dest, Path(f"{dest}-wal"), Path(f"{dest}-shm")):
        assert not os.path.lexists(name)


@pytest.mark.parametrize("target", ("main", "-wal"))
def test_a_destination_swap_during_restore_leaves_the_renamed_in_file(
    tmp_path: Path, monkeypatch, target: str
) -> None:
    export_dir = _export(tmp_path)
    dest = tmp_path / "restored.db"
    if target == "main":
        replacement = _ledger_with(tmp_path / "other.db", "OTHER-1")
        name, expected_code = dest, "db_swapped"
    else:
        replacement = tmp_path / "planted-wal"
        replacement.write_bytes(b"not the restore's -wal")
        name, expected_code = Path(f"{dest}-wal"), "db_sibling_unsafe"
    expected = _held(replacement)
    fired = _swap_after_write(
        monkeypatch, dest, lambda: os.replace(replacement, name), baseline=0
    )
    with pytest.raises(UnsafeDatabaseFile) as info:
        restore_export(export_dir, dest, system="home")
    assert fired == ["before_settle"]
    assert info.value.code == expected_code
    assert os.path.lexists(name), f"the failed restore removed {name}"
    assert _held(name) == expected
    assert "restore unlinked none of" in str(info.value)


def test_a_restore_of_a_claimed_export_is_checked_after_its_replay(
    tmp_path: Path, monkeypatch
) -> None:
    # An export that already carries the system claim makes no write after
    # the replay, so the replay's own COMMIT is the only place to check.
    source = _ledger_with(tmp_path / "export-source.db", ITEM)
    export_dir = tmp_path / "export"
    with open_ledger(source) as lg:
        assert lg.claim_system("home") is True
        export_events(lg, export_dir)
    dest = tmp_path / "restored.db"
    other = _ledger_with(tmp_path / "other.db", "OTHER-1")
    other_bytes = other.read_bytes()
    other_id = _identity(other)
    fired = _swap_after_write(monkeypatch, dest, lambda: os.replace(other, dest), baseline=0)
    with pytest.raises(UnsafeDatabaseFile) as info:
        restore_export(export_dir, dest, system="home")
    assert fired == ["before_settle"]
    assert info.value.code == "db_swapped"
    assert _identity(dest) == other_id
    assert dest.read_bytes() == other_bytes


def test_a_refusal_about_another_path_still_removes_the_restores_files(
    tmp_path: Path,
) -> None:
    # The loop-events callback raises an UnsafeDatabaseFile about a database
    # the restore never opened. The callback runs before the destination is
    # created, so no file of this restore exists to be left behind, and the
    # same restore can run again.
    source = _ledger_with(tmp_path / "export-source.db", ITEM)
    export_dir = tmp_path / "export"
    with open_ledger(source) as lg:
        manage_item(lg, ITEM, idempotency_key=f"manage:{ITEM}")
        export_events(lg, export_dir)
    dest = tmp_path / "restored.db"
    unrelated = tmp_path / "unrelated.db"
    calls: list[int] = []

    def callback(count: int, kinds: tuple[str, ...]) -> None:
        calls.append(count)
        assert not os.path.lexists(dest), "the destination existed before the callback"
        raise UnsafeDatabaseFile("db_swapped", "an unrelated database", unrelated)

    with pytest.raises(UnsafeDatabaseFile) as info:
        restore_export(export_dir, dest, system="home", accept_loop_events=True,
                       on_accept_loop_events=callback)
    assert calls, "the restore never reached the loop-events callback"
    assert info.value.path == unrelated
    assert "restore unlinked none of" not in str(info.value)
    for name in (dest, Path(f"{dest}-wal"), Path(f"{dest}-shm")):
        assert not os.path.lexists(name), f"the failed restore left {name}"
    result = restore_export(export_dir, dest, system="home", accept_loop_events=True)
    assert result.events > 0


# --- destination names --------------------------------------------------------


SIDE_SUFFIXES = ("-wal", "-shm", "-journal")


def _listing(folder: Path) -> list[str]:
    return sorted(entry.name for entry in folder.iterdir())


@pytest.mark.parametrize("suffix", SIDE_SUFFIXES)
def test_backup_refuses_a_destination_named_like_a_side_file(
    tmp_path: Path, suffix: str
) -> None:
    source = _ledger_with(tmp_path / "source.db", ITEM)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    before = _listing(dest_dir)
    with pytest.raises(LedgerError, match="named like a database side file"):
        backup_db(source, dest_dir / f"copy.db{suffix}")
    assert _listing(dest_dir) == before, "the refused backup wrote something"


@pytest.mark.parametrize("suffix", SIDE_SUFFIXES)
def test_restore_refuses_a_destination_named_like_a_side_file(
    tmp_path: Path, suffix: str
) -> None:
    export_dir = _export(tmp_path)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    with pytest.raises(LedgerError, match="named like a database side file"):
        restore_export(export_dir, dest_dir / f"restored.db{suffix}", system="home")
    assert _listing(dest_dir) == []


MIXED_SUFFIXES = ("-WAL", "-Shm", "-JOURNAL")


@pytest.mark.parametrize("suffix", MIXED_SUFFIXES)
def test_backup_refuses_a_mixed_case_side_file_name(
    tmp_path: Path, suffix: str
) -> None:
    source = _ledger_with(tmp_path / "source.db", ITEM)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    before = _listing(dest_dir)
    with pytest.raises(LedgerError, match="named like a database side file"):
        backup_db(source, dest_dir / f"copy.db{suffix}")
    assert _listing(dest_dir) == before, "the refused backup wrote something"


@pytest.mark.parametrize("suffix", MIXED_SUFFIXES)
def test_restore_refuses_a_mixed_case_side_file_name(
    tmp_path: Path, suffix: str
) -> None:
    export_dir = _export(tmp_path)
    dest_dir = tmp_path / "out"
    dest_dir.mkdir()
    with pytest.raises(LedgerError, match="named like a database side file"):
        restore_export(export_dir, dest_dir / f"restored.db{suffix}", system="home")
    assert _listing(dest_dir) == []


def test_backup_to_a_case_variant_of_the_live_database_refuses(
    tmp_path: Path,
) -> None:
    source = _ledger_with(tmp_path / "ledger.db", ITEM)
    with pytest.raises(LedgerError, match="backup target is the live database"):
        backup_db(source, tmp_path / "LEDGER.db")


def test_a_backup_onto_the_wal_name_with_two_connections_open_refuses(
    tmp_path: Path,
) -> None:
    # The name is refused whoever holds the database open, so the last
    # closer never finds a published backup on the -wal name.
    db = _ledger_with(tmp_path / "ledger.db", ITEM)
    source = _ledger_with(tmp_path / "other.db", "OTHER-1")
    wal = Path(f"{db}-wal")
    with open_ledger(db) as first, open_ledger(db) as second:
        first.append(**_note("first", "k-first"))
        second.append(**_note("second", "k-second"))
        held = _held(wal)
        with pytest.raises(LedgerError, match="named like a database side file"):
            backup_db(source, wal)
        assert _held(wal) == held
    assert _max_id(db) >= 3


# --- backup -------------------------------------------------------------------


def test_two_backups_to_one_destination_never_remove_each_others_partial(
    tmp_path: Path, monkeypatch
) -> None:
    first = _ledger_with(tmp_path / "first.db", "FIRST-1")
    second = _ledger_with(tmp_path / "second.db", "SECOND-1")
    dest = tmp_path / "backups" / "copy.db"
    seen: dict = {}

    def seam(stage: str, path: Path) -> None:
        # The first backup has copied into its partial and not yet published.
        if stage == "before_settle" and _same(path, first) and "second" not in seen:
            seen["second"] = None
            seen["before"] = sorted(p.name for p in dest.parent.glob("*.partial"))
            seen["second"] = backup_db(second, dest)
            seen["after"] = sorted(p.name for p in dest.parent.glob("*.partial"))
            seen["second_items"] = _created_items(dest)

    _arm(monkeypatch, seam)
    result = backup_db(first, dest)
    assert len(seen["before"]) == 1
    assert seen["after"] == seen["before"], "the second backup removed the first's partial"
    assert seen["second"].integrity == "ok"
    assert seen["second_items"] == ["SECOND-1"]
    assert result.integrity == "ok"
    assert integrity_check(dest) == "ok"
    assert _created_items(dest) == ["FIRST-1"]
    assert list(dest.parent.glob("*.partial")) == []


def test_a_source_swap_before_the_publish_is_refused_and_nothing_is_published(
    tmp_path: Path, monkeypatch
) -> None:
    source = _ledger_with(tmp_path / "source.db", "SOURCE-1")
    intruder = _ledger_with(tmp_path / "intruder.db", "INTRUDER-1")
    dest = tmp_path / "out" / "copy.db"
    fired: list[str] = []

    def seam(stage: str, path: Path) -> None:
        if stage == "before_settle" and _same(path, source) and not fired:
            fired.append(stage)
            os.replace(intruder, source)

    _arm(monkeypatch, seam)
    with pytest.raises(UnsafeDatabaseFile) as info:
        backup_db(source, dest)
    assert fired == ["before_settle"]
    assert info.value.code == "db_swapped"
    assert list(dest.parent.iterdir()) == []


# --- every write action, through the CLI ----------------------------------------


def _cli(monkeypatch, db: Path, *argv: str, stdin: str | None = None) -> tuple[int, str, str]:
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["--json", "--db", str(db), *argv])
    return code, out.getvalue(), err.getvalue()


def _prepare(action: str, tmp_path: Path, monkeypatch) -> tuple[Path, list[str], str | None]:
    """Set up ``action`` so it writes; return its database, its argv and its stdin."""
    db = _ledger_with(tmp_path / "ledger.db", ITEM)
    stdin = None
    if action == "append":
        argv = ["append", "--kind", "item.note", "--item", ITEM,
                "--payload", json.dumps({"text": "probe note"})]
    elif action == "append-many":
        argv = ["append-many", ITEM]
        stdin = json.dumps([{"kind": "item.note", "payload": {"text": "probe batch"}}])
    elif action == "capture":
        argv = ["capture", "--kind", "prompt"]
        stdin = json.dumps({"hook_event_name": "UserPromptSubmit",
                            "prompt": "probe prompt", "session_id": "s-probe"})
    elif action == "claim":
        argv = ["claim", ITEM, "--owner", "probe-owner"]
    elif action == "claim-system":
        argv = ["claim-system", "home"]
    elif action == "doc":
        attached = tmp_path / "probe-doc.md"
        attached.write_text("probe document\n", encoding="utf-8")
        argv = ["doc", "add", ITEM, str(attached), "--role", "brief"]
    elif action == "elect":
        quote = "operator chose safe"
        code, out, err = _cli(monkeypatch, db, "append", "--kind", "utterance.captured",
                              "--item", ITEM, "--payload", json.dumps({"text": quote}))
        assert code == EXIT_OK, err + out
        quote_ref = json.loads(out)["data"]["event"]["id"]
        argv = ["elect", ITEM, "--question", "choice?", "--chosen", "safe",
                "--chosen-description", "preserve the boundary", "--rejected", "forge",
                "--scope", "this item", "--quote-ref", str(quote_ref), "--quote", quote]
    elif action == "entity":
        argv = ["entity", "register", "project", "probe-entity", "--title", "Probe entity"]
    elif action == "guidance":
        argv = ["guidance", "set", "--pool", "xai-grok", "--direction", "burn",
                "--reason", "probe", "--starts", "2026-10-19", "--expires", "2026-10-25"]
    elif action == "link":
        argv = ["link", ITEM, "--rel", "pr", "--target", "owner/repo#1"]
    elif action == "new":
        argv = ["new", "--id", "P-new", "--type", "ticket", "--title", "probe new"]
    elif action == "note":
        argv = ["note", ITEM, "--text", "probe note"]
    elif action == "pick":
        asked_payload = {
            "question": "picker?",
            "options": [{"label": "safe", "description": "preserve the boundary"},
                        {"label": "forge"}],
            "scope": "this item",
        }
        code, out, err = _cli(monkeypatch, db, "append", "--kind", "decision.asked",
                              "--item", ITEM, "--payload", json.dumps(asked_payload))
        assert code == EXIT_OK, err + out
        asked = json.loads(out)["data"]["event"]["id"]
        code, out, err = _cli(monkeypatch, db, "ask-display", str(asked))
        assert code == EXIT_OK, err + out
        questions = json.loads(out)["data"]["input"]["questions"]
        hook = {
            "hook_event_name": "PostToolUse",
            "tool_name": "AskUserQuestion",
            "tool_use_id": "toolu-probe",
            "tool_input": {"questions": questions},
            "tool_response": {"answers": {questions[0]["question"]: "safe"}},
        }
        code, out, err = _cli(monkeypatch, db, "capture", "--kind", "picker",
                              stdin=json.dumps(hook))
        assert code == EXIT_OK, err + out
        argv = ["pick", ITEM, "--ask", str(asked)]
    elif action == "restore":
        export_dir = _export(tmp_path)
        db = tmp_path / "restored.db"
        argv = ["restore", "--dir", str(export_dir), "--system", "home"]
    elif action == "spec":
        argv = ["spec", "set", ITEM, "--title", "Probe spec", "--body", "probe body"]
    elif action == "state":
        argv = ["state", ITEM, "--status", "closed"]
    else:
        raise AssertionError(f"no probe for write action {action!r}")
    return db, argv, stdin


def test_the_probe_covers_every_database_writing_action() -> None:
    cli_writers = {name for name, verb in discover_verbs().items() if verb.writes}
    assert set(WRITE_ACTIONS) == cli_writers | {"doc", "restore"}


@pytest.mark.parametrize("action", WRITE_ACTIONS)
def test_every_write_action_writes_and_settles_with_no_swap(
    action: str, tmp_path: Path, monkeypatch
) -> None:
    db, argv, stdin = _prepare(action, tmp_path, monkeypatch)
    baseline = _max_id(db) if db.exists() else 0
    settles: list[str] = []
    _arm(monkeypatch, lambda stage, path: settles.append(stage)
         if stage == "before_settle" and _same(path, db) else None)
    code, out, err = _cli(monkeypatch, db, *argv, stdin=stdin)
    assert code == EXIT_OK, err + out
    assert json.loads(out)["ok"] is True
    assert _max_id(db) > baseline
    assert settles, f"{action} reported a write without settling it"


@pytest.mark.parametrize("target", ("main", "-wal"))
@pytest.mark.parametrize("action", WRITE_ACTIONS)
def test_a_swap_after_the_write_is_refused_for_every_write_action(
    action: str, target: str, tmp_path: Path, monkeypatch
) -> None:
    db, argv, stdin = _prepare(action, tmp_path, monkeypatch)
    baseline = _max_id(db) if db.exists() else 0
    if target == "main":
        replacement = _ledger_with(tmp_path / "other.db", "OTHER-1")
        name, expected = db, "db_swapped"
    else:
        replacement = tmp_path / "planted-wal"
        replacement.write_bytes(b"")
        name, expected = Path(f"{db}-wal"), "db_sibling_unsafe"
    planted = (_identity(replacement), replacement.read_bytes())
    fired = _swap_after_write(
        monkeypatch, db, lambda: os.replace(replacement, name), baseline=baseline
    )
    code, out, err = _cli(monkeypatch, db, *argv, stdin=stdin)
    assert fired == ["before_settle"], f"{action}: the probe never reached a settle after a write"
    assert code == EXIT_REFUSED, err + out
    envelope = json.loads(out)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == expected
    if target == "main":
        # The file put at the path is still there, byte for byte. (It is not
        # opened here: the displaced connection's -wal stays at the path's
        # -wal name, and SQLite would read it over this file.)
        assert _identity(db) == planted[0]
        assert db.read_bytes() == planted[1]
