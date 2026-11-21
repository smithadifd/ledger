"""A backup's temp never has a name the search-index cleanup matches.

The search-index refresh removes files beside the index whose name matches
``.search.db.<id>.partial``. A backup's temp used to be named from the
destination (``.<name>.<id>.partial``), so a destination named ``search.db``
or ``search.db.<anything>`` made a temp the refresh could remove while the
backup was still writing it. The temp now starts with a fixed
``.ledger-backup.`` prefix. Every ledger and file here is synthetic and built
under tmp_path.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from ledger import dbfile
from ledger.backup import backup_db, integrity_check
from ledger.search import _is_index_partial_name
from ledger.store import LedgerError
from ledger.verbs import EXIT_OK

from test_search import db, invoke, root  # noqa: F401

# The random part tempfile.mkstemp adds: eight characters.
_RANDOM = r"[a-z0-9_]{8}"


def _watch_temp(monkeypatch, seen: dict, during=None) -> None:
    """Record the backup's own temp the moment its connection is first checked.

    The temp exists by then (created empty, mode 0600). ``during`` runs while
    it does, before the backup writes into it.
    """

    def seam(stage: str, path: Path) -> None:
        # The ledger, the index and the destination have no leading dot; the
        # backup's temp is the one hidden file opened first.
        if stage == "before_connect" and path.name.startswith(".") and "temp" not in seen:
            seen["temp"] = path
            seen["mode"] = stat.S_IMODE(path.lstat().st_mode)
            if during is not None:
                during()

    monkeypatch.setattr(dbfile, "_TEST_SEAM", seam)


@pytest.mark.parametrize("name", ["search.db", "search.db.x", ".search.db.x", "copy.db"])
def test_the_temp_for_any_destination_name_is_not_an_index_partial(
    db: Path, monkeypatch, name: str
) -> None:
    dest = db.parent / name
    before = sorted(os.listdir(db.parent))
    assert not dest.exists()
    seen: dict = {}
    _watch_temp(monkeypatch, seen)
    result = backup_db(db, dest)
    assert result.integrity == "ok"
    temp = seen["temp"]
    assert temp.parent == dest.parent
    assert not _is_index_partial_name(temp.name), "the index cleanup matches the backup's temp"
    assert re.fullmatch(rf"\.ledger-backup\.{re.escape(name)}\.{_RANDOM}\.partial", temp.name)
    assert seen["mode"] == 0o600
    assert integrity_check(dest) == "ok"
    assert sorted(os.listdir(db.parent)) == sorted([*before, name])


def test_an_index_refresh_while_a_backup_runs_leaves_its_temp(db: Path, monkeypatch) -> None:
    dest = db.parent / "search.db.x"
    seen: dict = {}

    def refresh_now() -> None:
        code, _out, err = invoke(monkeypatch, ["--db", str(db), "index", "--rebuild"])
        seen["code"] = code
        seen["err"] = err
        seen["present"] = seen["temp"].is_file()

    _watch_temp(monkeypatch, seen, refresh_now)
    # A temp the refresh removes makes the backup refuse (db_swapped).
    refusal = None
    try:
        result = backup_db(db, dest)
    except dbfile.UnsafeDatabaseFile as exc:
        refusal = exc
    assert seen["code"] == EXIT_OK, seen["err"]
    assert seen["present"], "an index refresh removed the backup's temp"
    assert refusal is None, f"the backup was refused: {refusal}"
    assert result.integrity == "ok"
    assert integrity_check(dest) == "ok"
    assert not [n for n in os.listdir(db.parent) if n.endswith(".partial")]


def test_a_failed_backup_removes_its_temp_and_touches_nothing_else(
    db: Path, monkeypatch
) -> None:
    dest = db.parent / "search.db.x"
    bystander = db.parent / ".search.db.stale.partial"
    bystander.write_bytes(b"")
    before = sorted(os.listdir(db.parent))
    seen: dict = {}
    _watch_temp(monkeypatch, seen)
    monkeypatch.setattr("ledger.backup.integrity_check", lambda _path: "bad page 1")
    with pytest.raises(LedgerError, match="integrity_check: bad page 1"):
        backup_db(db, dest)
    assert seen["temp"].name.startswith(".ledger-backup.search.db.x.")
    assert not os.path.lexists(seen["temp"]), "the failed backup left its temp"
    assert not dest.exists()
    assert sorted(os.listdir(db.parent)) == before
