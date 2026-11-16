"""``backup --to`` refuses a destination that already exists, as ``restore`` does.

A backup used to replace whatever file sat at the destination: a text file
became a database, and an older backup was lost, with exit 0 either way.
Now an existing destination is refused with a named error, exit 3, and left
byte for byte as it was; ``--overwrite`` (library: ``overwrite=True``) is the
explicit way to replace it.
"""
from __future__ import annotations

import errno
import io
import os
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

import ledger.backup as backup_module
from ledger.backup import backup_db, integrity_check
from ledger.cli import main
from ledger.store import LedgerError, open_ledger
from ledger.verbs import EXIT_REFUSED

ITEM = "T-1"
OTHER = "OTHER-1"
NOT_A_DB = b"notadb\n"
SQLITE_MAGIC = b"SQLite format 3\x00"


def _ledger_with(path: Path, item_id: str) -> Path:
    with open_ledger(path) as lg:
        lg.append(
            kind="item.created",
            payload={"type": "ticket", "title": f"item {item_id}"},
            item_id=item_id,
            idempotency_key=f"seed-{item_id}",
        )
    return path


def _items(db: Path) -> list[str]:
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT item_id FROM events WHERE kind = 'item.created' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def _cli(monkeypatch, db: Path, *argv: str) -> tuple[int, str, str]:
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["--db", str(db), *argv])
    return code, out.getvalue(), err.getvalue()


def _no_partial(folder: Path) -> bool:
    return not list(folder.glob(".*.partial"))


def test_cli_backup_refuses_an_existing_non_database_file(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "existing.txt"
    dest.write_bytes(NOT_A_DB)
    code, out, err = _cli(monkeypatch, live, "backup", "--to", str(dest))
    assert code == EXIT_REFUSED, err
    assert "backup target already exists" in err
    assert "--overwrite" in err
    assert len(err.strip().splitlines()) == 1
    assert "integrity=ok" not in out
    assert dest.read_bytes() == NOT_A_DB
    assert _no_partial(tmp_path)


def test_cli_backup_refuses_an_existing_backup(tmp_path: Path, monkeypatch) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = _ledger_with(tmp_path / "copy.db", OTHER)
    before = dest.read_bytes()
    code, _out, err = _cli(monkeypatch, live, "backup", "--to", str(dest))
    assert code == EXIT_REFUSED, err
    assert "backup target already exists" in err
    assert dest.read_bytes() == before
    assert _items(dest) == [OTHER]
    assert _no_partial(tmp_path)


def test_library_backup_refuses_an_existing_file_and_changes_nothing(
    tmp_path: Path,
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "existing.txt"
    dest.write_bytes(NOT_A_DB)
    before = os.lstat(dest)
    with pytest.raises(LedgerError, match="backup target already exists"):
        backup_db(live, dest)
    after = os.lstat(dest)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert dest.read_bytes() == NOT_A_DB
    assert sorted(os.listdir(tmp_path)) == ["existing.txt", "ledger.db"]


def test_cli_backup_overwrite_replaces_an_existing_non_database_file(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "existing.txt"
    dest.write_bytes(NOT_A_DB)
    code, out, err = _cli(monkeypatch, live, "backup", "--to", str(dest), "--overwrite")
    assert code == 0, err
    assert "integrity=ok" in out
    assert dest.read_bytes()[:16] == SQLITE_MAGIC
    assert integrity_check(dest) == "ok"
    assert _items(dest) == [ITEM]
    assert _no_partial(tmp_path)


def test_cli_backup_overwrite_replaces_an_existing_backup(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = _ledger_with(tmp_path / "copy.db", OTHER)
    code, _out, err = _cli(monkeypatch, live, "backup", "--to", str(dest), "--overwrite")
    assert code == 0, err
    assert _items(dest) == [ITEM]
    assert _no_partial(tmp_path)


def test_a_refused_existing_target_is_refused_before_any_copy_starts(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "existing.txt"
    dest.write_bytes(NOT_A_DB)

    def copy_started(*_args, **_kwargs):
        raise AssertionError("a partial copy was started for a refused target")

    monkeypatch.setattr(backup_module.tempfile, "mkstemp", copy_started)
    with pytest.raises(LedgerError, match="backup target already exists"):
        backup_db(live, dest)
    assert dest.read_bytes() == NOT_A_DB


def test_library_backup_overwrite_replaces_an_existing_backup(tmp_path: Path) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = _ledger_with(tmp_path / "copy.db", OTHER)
    result = backup_db(live, dest, overwrite=True)
    assert result.integrity == "ok"
    assert _items(dest) == [ITEM]


def test_cli_backup_to_an_absent_target_still_works(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "new-folder" / "copy.db"
    code, out, err = _cli(monkeypatch, live, "backup", "--to", str(dest))
    assert code == 0, err
    assert "integrity=ok" in out
    assert integrity_check(dest) == "ok"
    assert _items(dest) == [ITEM]
    assert _no_partial(dest.parent)


def test_library_backup_to_an_absent_target_still_works(tmp_path: Path) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "copy.db"
    assert backup_db(live, dest).integrity == "ok"
    assert _items(dest) == [ITEM]


def test_overwrite_never_allows_the_live_database_as_target(tmp_path: Path) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    before = live.read_bytes()
    with pytest.raises(LedgerError, match="backup target is the live database"):
        backup_db(live, live, overwrite=True)
    assert live.read_bytes() == before


def test_a_file_that_takes_the_name_during_the_copy_is_not_replaced(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "copy.db"
    real_integrity = backup_module.integrity_check

    def check_then_appear(partial: Path) -> str:
        verdict = real_integrity(partial)
        dest.write_bytes(NOT_A_DB)
        return verdict

    monkeypatch.setattr(backup_module, "integrity_check", check_then_appear)
    with pytest.raises(LedgerError, match="backup target already exists"):
        backup_db(live, dest)
    assert dest.read_bytes() == NOT_A_DB
    assert _no_partial(tmp_path)


def test_a_late_overwrite_still_replaces_a_file_that_took_the_name(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "copy.db"
    real_integrity = backup_module.integrity_check

    def check_then_appear(partial: Path) -> str:
        verdict = real_integrity(partial)
        dest.write_bytes(NOT_A_DB)
        return verdict

    monkeypatch.setattr(backup_module, "integrity_check", check_then_appear)
    backup_db(live, dest, overwrite=True)
    assert _items(dest) == [ITEM]


def test_a_name_that_cannot_be_read_is_refused_before_the_copy(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "copy.db"

    def unreadable(_path: Path):
        raise PermissionError("denied")

    # The live-database guard reads the same name and refuses it on its own
    # (see test_backup_dest_identity); take it out so the existing-target
    # check is the one that reads.
    monkeypatch.setattr(backup_module, "_refuse_live_destination", lambda *_a: None)
    monkeypatch.setattr(backup_module, "file_identity", unreadable)
    with pytest.raises(LedgerError, match="backup target cannot be checked"):
        backup_db(live, dest)
    assert not dest.exists()
    assert _no_partial(tmp_path)


def _link_unsupported(code: int):
    def refuse(_src, _dst, *_args, **_kwargs):
        raise OSError(code, os.strerror(code))

    return refuse


def test_a_file_that_appears_just_before_the_publish_is_not_replaced(
    tmp_path: Path, monkeypatch
) -> None:
    # The name is free at the early check and free when the publish starts;
    # the file takes it in the instant before the link is made.
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "copy.db"
    real_link = os.link

    def appear_then_link(src, dst, *args, **kwargs):
        Path(dst).write_bytes(NOT_A_DB)
        return real_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "link", appear_then_link)
    with pytest.raises(LedgerError, match="backup target already exists"):
        backup_db(live, dest)
    assert dest.read_bytes() == NOT_A_DB
    assert _no_partial(tmp_path)


def test_overwrite_publishes_by_rename_never_by_link(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = _ledger_with(tmp_path / "copy.db", OTHER)

    def no_link(*_args, **_kwargs):
        raise AssertionError("an overwrite published by hard link")

    monkeypatch.setattr(os, "link", no_link)
    backup_db(live, dest, overwrite=True)
    assert _items(dest) == [ITEM]


def test_a_published_backup_has_one_name_and_the_partial_is_gone(
    tmp_path: Path,
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "copy.db"
    backup_db(live, dest)
    assert os.lstat(dest).st_nlink == 1
    assert _no_partial(tmp_path)


@pytest.mark.parametrize(
    "code",
    sorted({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK}),
)
def test_a_filesystem_without_hard_links_still_publishes_an_absent_target(
    tmp_path: Path, monkeypatch, code: int
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "copy.db"
    monkeypatch.setattr(os, "link", _link_unsupported(code))
    assert backup_db(live, dest).integrity == "ok"
    assert _items(dest) == [ITEM]
    assert _no_partial(tmp_path)


def test_without_hard_links_a_file_present_at_the_publish_is_still_refused(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "copy.db"
    real_integrity = backup_module.integrity_check

    def check_then_appear(partial: Path) -> str:
        verdict = real_integrity(partial)
        dest.write_bytes(NOT_A_DB)
        return verdict

    monkeypatch.setattr(backup_module, "integrity_check", check_then_appear)
    monkeypatch.setattr(os, "link", _link_unsupported(errno.EPERM))
    with pytest.raises(LedgerError, match="backup target already exists"):
        backup_db(live, dest)
    assert dest.read_bytes() == NOT_A_DB
    assert _no_partial(tmp_path)


def test_another_link_failure_is_not_taken_for_missing_hard_link_support(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "copy.db"
    monkeypatch.setattr(os, "link", _link_unsupported(errno.EIO))
    with pytest.raises(OSError) as info:
        backup_db(live, dest)
    assert info.value.errno == errno.EIO
    assert not dest.exists()
    assert _no_partial(tmp_path)


def _names(folder: Path) -> list[str]:
    return sorted(os.listdir(folder))


def test_without_hard_links_a_file_that_appears_just_before_the_claim_is_not_replaced(
    tmp_path: Path, monkeypatch
) -> None:
    # The name is free at the early check and when the publish starts; the
    # file takes it in the instant before the exclusive create.
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "copy.db"
    real_open = os.open

    def appear_then_open(path, flags, *args, **kwargs):
        if Path(path) == dest:
            dest.write_bytes(NOT_A_DB)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "link", _link_unsupported(errno.EPERM))
    monkeypatch.setattr(os, "open", appear_then_open)
    with pytest.raises(LedgerError, match="backup target already exists"):
        backup_db(live, dest)
    assert dest.read_bytes() == NOT_A_DB
    assert _names(tmp_path) == ["copy.db", "ledger.db"]


def test_without_hard_links_an_absent_target_leaves_no_placeholder_or_partial(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "copy.db"
    monkeypatch.setattr(os, "link", _link_unsupported(errno.EXDEV))
    assert backup_db(live, dest).integrity == "ok"
    assert _names(tmp_path) == ["copy.db", "ledger.db"]
    assert dest.read_bytes()[:16] == SQLITE_MAGIC
    assert integrity_check(dest) == "ok"
    assert _items(dest) == [ITEM]
    assert os.lstat(dest).st_nlink == 1


def test_without_hard_links_a_failed_rename_removes_the_placeholder(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "copy.db"
    seen: list[bool] = []

    def failing_replace(_src, dst, *_args, **_kwargs):
        seen.append(Path(dst).exists())
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(os, "link", _link_unsupported(errno.EPERM))
    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(OSError) as info:
        backup_db(live, dest)
    assert info.value.errno == errno.EIO
    assert seen == [True], "the claim was not in place when the rename ran"
    assert _names(tmp_path) == ["ledger.db"]


def test_without_hard_links_a_failed_rename_never_removes_a_file_that_is_not_ours(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "copy.db"

    def swap_then_fail(_src, dst, *_args, **_kwargs):
        # Another process replaces our placeholder, then the rename fails.
        os.unlink(dst)
        Path(dst).write_bytes(NOT_A_DB)
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(os, "link", _link_unsupported(errno.EPERM))
    monkeypatch.setattr(os, "replace", swap_then_fail)
    with pytest.raises(OSError):
        backup_db(live, dest)
    assert dest.read_bytes() == NOT_A_DB
    assert _no_partial(tmp_path)
