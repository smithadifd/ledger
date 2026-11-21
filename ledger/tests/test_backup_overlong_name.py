"""An overlong backup destination is refused cleanly, with no leftover file.

Since the temp is ``.ledger-backup.<name>.<id>.partial``, a destination
name of 230 ASCII characters overflows the file name limit at mkstemp.
That case is a ``LedgerError`` (CLI exit 3, one stderr line naming the
limit), with no traceback and no file left in the destination directory.
A 200-character name still backs up. A destination name of 120 copies of
U+00E9 follows whatever outcome mkstemp itself gives on this file system.
Every ledger and file here is synthetic and built under tmp_path.
"""
from __future__ import annotations

import errno
import io
import os
import re
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

import ledger.backup as backup_module
from ledger.backup import backup_db, integrity_check
from ledger.cli import main
from ledger.store import LedgerError, open_ledger
from ledger.verbs import EXIT_OK, EXIT_REFUSED

ITEM = "T-1"
LONG_NAME = "a" * 230
FITTING_NAME = "b" * 200
MULTIBYTE_NAME = "\u00e9" * 120
_RANDOM = r"[a-z0-9_]{8}"


def _ledger_with(path: Path, item_id: str) -> Path:
    with open_ledger(path) as lg:
        lg.append(
            kind="item.created",
            payload={"type": "ticket", "title": f"item {item_id}"},
            item_id=item_id,
            idempotency_key=f"seed-{item_id}",
        )
    return path


def _cli(monkeypatch, db: Path, *argv: str) -> tuple[int, str, str]:
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["--db", str(db), *argv])
    return code, out.getvalue(), err.getvalue()


def _no_partial(folder: Path) -> bool:
    return not list(folder.glob(".*.partial"))


def _temp_name_accepted(folder: Path, dest_name: str) -> bool:
    """True when this file system creates the backup temp for ``dest_name``."""
    try:
        handle, name = tempfile.mkstemp(
            prefix=f"{backup_module.BACKUP_TEMP_PREFIX}{dest_name}.",
            suffix=backup_module.BACKUP_TEMP_SUFFIX,
            dir=folder,
        )
    except OSError as exc:
        if exc.errno == errno.ENAMETOOLONG:
            return False
        raise
    os.close(handle)
    Path(name).unlink()
    return True


def test_cli_backup_refuses_a_230_character_destination_name(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / LONG_NAME
    before = sorted(os.listdir(tmp_path))
    caught: BaseException | None = None
    try:
        code, out, err = _cli(monkeypatch, live, "backup", "--to", str(dest))
    except BaseException as exc:
        caught = exc
        code, out, err = -1, "", str(exc)
    assert caught is None, caught
    assert code == EXIT_REFUSED, err
    assert "file name limit" in err
    assert len(err.strip().splitlines()) == 1
    assert "Traceback" not in err
    assert "OSError" not in err
    assert "integrity=ok" not in out
    assert not os.path.lexists(dest)
    assert sorted(os.listdir(tmp_path)) == before
    assert _no_partial(tmp_path)


def test_cli_backup_to_a_200_character_destination_name_still_works(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / FITTING_NAME
    code, out, err = _cli(monkeypatch, live, "backup", "--to", str(dest))
    assert code == EXIT_OK, err
    assert err == ""
    assert "integrity=ok" in out
    assert dest.is_file()
    assert integrity_check(dest) == "ok"
    assert _no_partial(tmp_path)


def test_library_backup_refuses_an_overlong_name_before_any_file_is_created(
    tmp_path: Path,
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / LONG_NAME
    bystander = tmp_path / "keep.txt"
    bystander.write_bytes(b"keep\n")
    before = sorted(os.listdir(tmp_path))
    raised: BaseException | None = None
    try:
        backup_db(live, dest)
    except BaseException as exc:
        raised = exc
    assert isinstance(raised, LedgerError), raised
    assert "file name limit" in str(raised)
    assert not isinstance(raised, OSError)
    assert not os.path.lexists(dest)
    assert sorted(os.listdir(tmp_path)) == before
    assert bystander.read_bytes() == b"keep\n"


def test_an_overlong_name_does_not_open_the_source_for_the_copy(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / LONG_NAME
    opened: list[str] = []

    def boom(*_args, **_kwargs):
        opened.append("opened")
        raise AssertionError("source opened")

    monkeypatch.setattr(backup_module, "checked_connect", boom)
    raised: BaseException | None = None
    try:
        backup_db(live, dest)
    except BaseException as exc:
        raised = exc
    assert isinstance(raised, LedgerError), raised
    assert "file name limit" in str(raised)
    assert opened == []


def test_library_backup_to_a_200_character_name_keeps_the_full_dest_in_the_temp(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / FITTING_NAME
    seen: dict = {}
    real_mkstemp = tempfile.mkstemp

    def wrap(*args, **kwargs):
        handle, name = real_mkstemp(*args, **kwargs)
        seen["name"] = Path(name).name
        return handle, name

    monkeypatch.setattr(backup_module.tempfile, "mkstemp", wrap)
    result = backup_db(live, dest)
    assert result.integrity == "ok"
    assert re.fullmatch(
        rf"\.ledger-backup\.{re.escape(FITTING_NAME)}\.{_RANDOM}\.partial",
        seen["name"],
    )
    assert integrity_check(dest) == "ok"
    assert _no_partial(tmp_path)


def test_another_mkstemp_failure_is_not_taken_for_a_name_too_long(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "copy.db"

    def boom(*_args, **_kwargs):
        raise OSError(errno.EACCES, os.strerror(errno.EACCES))

    monkeypatch.setattr(backup_module.tempfile, "mkstemp", boom)
    with pytest.raises(OSError) as info:
        backup_db(live, dest)
    assert info.value.errno == errno.EACCES
    assert not isinstance(info.value, LedgerError)
    assert not dest.exists()
    assert _no_partial(tmp_path)


def test_multibyte_destination_name_follows_mkstemp(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / MULTIBYTE_NAME
    accepted = _temp_name_accepted(tmp_path, dest.name)
    caught: BaseException | None = None
    try:
        code, out, err = _cli(monkeypatch, live, "backup", "--to", str(dest))
    except BaseException as exc:
        caught = exc
        code, out, err = -1, "", str(exc)
    assert caught is None, caught
    if accepted:
        assert code == EXIT_OK, err
        assert err == ""
        assert "integrity=ok" in out
        assert dest.is_file()
        assert integrity_check(dest) == "ok"
        assert _no_partial(tmp_path)
        return
    assert code == EXIT_REFUSED, err
    assert "file name limit" in err
    assert len(err.strip().splitlines()) == 1
    assert "Traceback" not in err
    assert "OSError" not in err
    assert not os.path.lexists(dest)
    assert _no_partial(tmp_path)
