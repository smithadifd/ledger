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
