"""Backup and restore tell a destination from the live database by file identity.

A volume can read two spellings as one name: macOS's default APFS ignores
both case and Unicode normalization, so a composed and a decomposed accented
name, or ``LEDGER.db`` and ``ledger.db``, can be one file. A hard link is
another name for the same file on any volume. Comparing spellings misses all
of these, and on a volume that tells case apart, folding case refuses a real
sibling instead. Each test reads what its own temporary folder does at run
time and asserts the outcome that volume calls for, so both halves run
somewhere: the refusals where the volume folds (macOS by default), the
allowed sibling where it tells names apart (Linux by default).
"""
from __future__ import annotations

import os
import sqlite3
import unicodedata
from pathlib import Path

import pytest

import ledger.backup as backup_module
from ledger.backup import backup_db
from ledger.export import export_events
from ledger.restore import restore_export
from ledger.store import LedgerError, open_ledger

ITEM = "T-1"
OTHER = "OTHER-1"
COMPOSED = unicodedata.normalize("NFC", "café.db")
DECOMPOSED = unicodedata.normalize("NFD", COMPOSED)
SPELLINGS = (
    pytest.param(COMPOSED, DECOMPOSED, id="composed-live-decomposed-dest"),
    pytest.param(DECOMPOSED, COMPOSED, id="decomposed-live-composed-dest"),
)


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


def _items(db: Path) -> list[str]:
    conn = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT item_id FROM events WHERE kind = 'item.created' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [row[0] for row in rows]


def _identity(path: Path) -> tuple[int, int]:
    found = os.lstat(path)
    return found.st_dev, found.st_ino


def _state(db: Path) -> tuple[tuple[int, int], list[str], list[str]]:
    """The live file's identity, its items and its folder's names."""
    return _identity(db), _items(db), sorted(os.listdir(db.parent))


def _names_one_file(variant: Path, existing: Path) -> bool:
    """Whether this test's own volume reads ``variant`` as the name of ``existing``."""
    try:
        return os.path.samefile(variant, existing)
    except FileNotFoundError:
        return False


def _export(tmp_path: Path) -> Path:
    source = _ledger_with(tmp_path / "export-source.db", ITEM)
    export_dir = tmp_path / "export"
    with open_ledger(source) as lg:
        export_events(lg, export_dir)
    return export_dir


class _UnreadableName:
    """``os`` for the backup module, with ``lstat`` of one name failing as unreadable."""

    def __init__(self, name: Path):
        # The resolved spelling, as backup and restore pass it.
        self._name = os.fspath(Path(name).parent.resolve() / Path(name).name)
        self.calls = 0

    def lstat(self, path, *args, **kwargs):
        if os.fspath(path) == self._name:
            self.calls += 1
            raise PermissionError(13, "Permission denied", self._name)
        return os.lstat(path, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(os, name)


class _ExactNames:
    """``os`` for the backup module, as on a volume that tells case apart.

    ``lstat`` finds a name only when its folder lists it byte for byte, so
    the case probe runs its other branch on any volume.
    """

    def lstat(self, path, *args, **kwargs):
        target = Path(os.fspath(path))
        if target.parent != target and target.name not in os.listdir(target.parent):
            raise FileNotFoundError(2, "No such file or directory", str(target))
        return os.lstat(path, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(os, name)


class _EntryGoneOnSecondRead:
    """``os`` for the backup module: the probed entry vanishes after its first read.

    It stands in for another process renaming the entry away between the
    case probe's two reads of it, on a volume that folds case: once the entry
    is gone, its case-swapped name is missing too, which alone would look
    like a volume that tells case apart.
    """

    def __init__(self, entry: Path):
        resolved = Path(entry).parent.resolve() / Path(entry).name
        self._entry = os.fspath(resolved)
        self._swapped = os.fspath(resolved.parent / resolved.name.swapcase())
        self.entry_reads = 0

    def lstat(self, path, *args, **kwargs):
        name = os.fspath(path)
        if name == self._entry:
            self.entry_reads += 1
            if self.entry_reads > 1:
                raise FileNotFoundError(2, "No such file or directory", name)
        elif name == self._swapped:
            raise FileNotFoundError(2, "No such file or directory", name)
        return os.lstat(path, *args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(os, name)


# --- backup -----------------------------------------------------------------


@pytest.mark.parametrize(("live_name", "dest_name"), SPELLINGS)
def test_backup_to_another_normalization_of_the_live_name(
    tmp_path: Path, live_name: str, dest_name: str
) -> None:
    live = _ledger_with(tmp_path / live_name, ITEM)
    dest = tmp_path / dest_name
    before = _state(live)
    if _names_one_file(dest, live):
        with pytest.raises(LedgerError, match="backup target is the live database"):
            backup_db(live, dest)
        assert _state(live) == before, "the refused backup touched the live database"
    else:
        # The volume keeps both spellings apart: the destination is a new file.
        backup_db(live, dest)
        assert _identity(dest) != _identity(live)
        assert _items(dest) == [ITEM]
        assert _state(live)[:2] == before[:2]


def test_backup_to_a_case_variant_of_the_live_name(tmp_path: Path) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = tmp_path / "LEDGER.db"
    if _names_one_file(dest, live):
        before = _state(live)
        with pytest.raises(LedgerError, match="backup target is the live database"):
            backup_db(live, dest)
        assert _state(live) == before, "the refused backup touched the live database"
    else:
        # The volume tells case apart: LEDGER.db is a real sibling, another
        # ledger, and a backup may replace it.
        _ledger_with(dest, OTHER)
        assert _identity(dest) != _identity(live)
        before = _state(live)
        backup_db(live, dest)
        assert _items(dest) == [ITEM]
        assert _state(live)[:2] == before[:2]


def test_backup_to_a_hard_link_of_the_live_database_refuses(tmp_path: Path) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    out = tmp_path / "out"
    out.mkdir()
    dest = out / "copy.db"
    os.link(live, dest)
    with pytest.raises(LedgerError, match="backup target is the live database"):
        backup_db(live, dest)
    assert _identity(dest) == _identity(live)
    assert sorted(os.listdir(out)) == ["copy.db"], "the refused backup wrote something"


def test_backup_to_a_hard_link_of_the_live_wal_refuses(tmp_path: Path) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    out = tmp_path / "out"
    out.mkdir()
    dest = out / "copy.db"
    wal = Path(f"{live}-wal")
    with open_ledger(live) as lg:
        lg.append(
            kind="item.note",
            payload={"text": "held open"},
            item_id=ITEM,
            idempotency_key="held-open",
        )
        assert wal.exists(), "the open ledger holds no -wal to link"
        os.link(wal, dest)
        try:
            with pytest.raises(LedgerError, match="backup target is the live database"):
                backup_db(live, dest)
            assert _identity(dest) == _identity(wal)
            assert sorted(os.listdir(out)) == ["copy.db"], "the refused backup wrote something"
        finally:
            dest.unlink()


def test_an_ordinary_existing_backup_is_still_replaced(tmp_path: Path) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = _ledger_with(tmp_path / "copy.db", OTHER)
    backup_db(live, dest)
    assert _items(dest) == [ITEM]


@pytest.mark.parametrize("unreadable", ("dest", "live-wal"))
def test_backup_refuses_when_an_identity_cannot_be_read(
    tmp_path: Path, monkeypatch, unreadable: str
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    dest = _ledger_with(tmp_path / "copy.db", OTHER)
    name = dest if unreadable == "dest" else Path(f"{live}-wal")
    failing = _UnreadableName(name)
    monkeypatch.setattr(backup_module, "os", failing)
    with pytest.raises(
        LedgerError, match="backup target cannot be told apart from the live database"
    ):
        backup_db(live, dest)
    monkeypatch.undo()
    assert failing.calls == 1
    assert _items(dest) == [OTHER], "the refused backup replaced the destination"


# --- restore ----------------------------------------------------------------


@pytest.mark.parametrize(("live_name", "dest_name"), SPELLINGS)
def test_restore_to_another_normalization_of_an_existing_name(
    tmp_path: Path, live_name: str, dest_name: str
) -> None:
    export_dir = _export(tmp_path)
    live = _ledger_with(tmp_path / live_name, OTHER)
    dest = tmp_path / dest_name
    before = _state(live)
    if _names_one_file(dest, live):
        with pytest.raises(LedgerError, match="restore target already exists"):
            restore_export(export_dir, dest, system="home")
        assert _state(live) == before, "the refused restore touched the database"
    else:
        restore_export(export_dir, dest, system="home")
        assert _identity(dest) != _identity(live)
        assert _items(dest) == [ITEM]
        assert _state(live)[:2] == before[:2]


def test_restore_to_a_case_variant_of_an_existing_name(tmp_path: Path) -> None:
    export_dir = _export(tmp_path)
    live = _ledger_with(tmp_path / "ledger.db", OTHER)
    dest = tmp_path / "LEDGER.db"
    before = _state(live)
    if _names_one_file(dest, live):
        with pytest.raises(LedgerError, match="restore target already exists"):
            restore_export(export_dir, dest, system="home")
        assert _state(live) == before, "the refused restore touched the database"
    else:
        # The volume tells case apart: LEDGER.db is a new, separate file.
        restore_export(export_dir, dest, system="home")
        assert _identity(dest) != _identity(live)
        assert _items(dest) == [ITEM]
        assert _state(live)[:2] == before[:2]


def test_restore_to_a_hard_link_of_an_existing_database_refuses(tmp_path: Path) -> None:
    export_dir = _export(tmp_path)
    live = _ledger_with(tmp_path / "ledger.db", OTHER)
    out = tmp_path / "out"
    out.mkdir()
    dest = out / "restored.db"
    os.link(live, dest)
    with pytest.raises(LedgerError, match="restore target already exists"):
        restore_export(export_dir, dest, system="home")
    assert _items(live) == [OTHER]
    assert sorted(os.listdir(out)) == ["restored.db"], "the refused restore wrote something"


def test_restore_refuses_when_the_destination_cannot_be_read(
    tmp_path: Path, monkeypatch
) -> None:
    export_dir = _export(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    dest = out / "restored.db"
    failing = _UnreadableName(dest)
    monkeypatch.setattr(backup_module, "os", failing)
    with pytest.raises(LedgerError, match="restore target cannot be checked"):
        restore_export(export_dir, dest, system="home")
    monkeypatch.undo()
    assert failing.calls == 1
    assert os.listdir(out) == [], "the refused restore wrote something"


# --- side-file names and case ----------------------------------------------


def test_a_mixed_case_side_name_follows_the_volume(tmp_path: Path) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("an entry for the case probe\n")
    folds = _names_one_file(out / "KEEP.TXT", out / "keep.txt")
    for dest in (out / "copy.db-WAL", out / "copy.db-Shm", out / "copy.db-JOURNAL"):
        if folds:
            with pytest.raises(LedgerError, match="named like a database side file"):
                backup_db(live, dest)
            assert sorted(os.listdir(out)) == ["keep.txt"]
        else:
            backup_db(live, dest)
            assert _items(dest) == [ITEM]


@pytest.mark.parametrize("verb", ("backup", "restore"))
def test_a_folder_shown_to_tell_case_apart_allows_a_mixed_case_side_name(
    tmp_path: Path, monkeypatch, verb: str
) -> None:
    export_dir = _export(tmp_path)
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("an entry for the case probe\n")
    dest = out / "copy.db-WAL"
    monkeypatch.setattr(backup_module, "os", _ExactNames())
    if verb == "backup":
        backup_db(live, dest)
    else:
        restore_export(export_dir, dest, system="home")
    monkeypatch.undo()
    assert _items(dest) == [ITEM]


@pytest.mark.parametrize("verb", ("backup", "restore"))
@pytest.mark.parametrize("entries", ((), ("123",)), ids=("empty", "no-ascii-letter"))
def test_a_folder_that_shows_nothing_about_case_refuses_a_mixed_case_side_name(
    tmp_path: Path, monkeypatch, verb: str, entries: tuple[str, ...]
) -> None:
    export_dir = _export(tmp_path)
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    out = tmp_path / "out"
    out.mkdir()
    for entry in entries:
        (out / entry).write_text("no letter to swap\n")
    dest = out / "copy.db-WAL"
    monkeypatch.setattr(backup_module, "os", _ExactNames())
    with pytest.raises(LedgerError, match="named like a database side file"):
        if verb == "backup":
            backup_db(live, dest)
        else:
            restore_export(export_dir, dest, system="home")
    monkeypatch.undo()
    assert sorted(os.listdir(out)) == sorted(entries)


def test_an_exact_side_suffix_refuses_even_where_case_is_told_apart(
    tmp_path: Path, monkeypatch
) -> None:
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep.txt").write_text("an entry for the case probe\n")
    monkeypatch.setattr(backup_module, "os", _ExactNames())
    with pytest.raises(LedgerError, match="named like a database side file"):
        backup_db(live, out / "copy.db-wal")
    monkeypatch.undo()
    assert sorted(os.listdir(out)) == ["keep.txt"]


@pytest.mark.parametrize("verb", ("backup", "restore"))
def test_a_probed_entry_that_vanishes_between_reads_refuses_a_mixed_case_side_name(
    tmp_path: Path, monkeypatch, verb: str
) -> None:
    export_dir = _export(tmp_path)
    live = _ledger_with(tmp_path / "ledger.db", ITEM)
    out = tmp_path / "out"
    out.mkdir()
    (out / "copy.db").write_text("the only entry the case probe can try\n")
    stub = _EntryGoneOnSecondRead(out / "copy.db")
    monkeypatch.setattr(backup_module, "os", stub)
    with pytest.raises(LedgerError, match="named like a database side file"):
        if verb == "backup":
            backup_db(live, out / "copy.db-WAL")
        else:
            restore_export(export_dir, out / "copy.db-WAL", system="home")
    monkeypatch.undo()
    assert stub.entry_reads == 2, "the case probe never reached its second read"
    assert sorted(os.listdir(out)) == ["copy.db"], "the refused name was written"
