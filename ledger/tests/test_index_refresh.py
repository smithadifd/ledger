"""``ledger index`` on a behind index appends the new events; ``severity``.

Uses the fixtures of ``test_search.py``; every ledger and markdown file is
synthetic and built under tmp_path.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from ledger import search as search_mod
from ledger.store import open_ledger
from ledger.verbs import EXIT_OK, EXIT_REFUSED

from test_search import (  # noqa: F401
    _append,
    _doctor_cannot_refresh,
    _rewrite_stored_manifest,
    db,
    invoke,
    root,
    run_json,
)


def _index(db: Path) -> Path:
    return db.parent / "search.db"


def _snapshot(db: Path) -> dict:
    """Everything a search reads, without rowids, so two builds compare."""
    conn = sqlite3.connect(_index(db).as_uri() + "?mode=ro", uri=True)
    try:
        docs = sorted(
            tuple("" if v is None else str(v) for v in row)
            for row in conn.execute(
                "SELECT f.doc_type, f.doc_id, f.title, f.content, p.path, m.item_id, "
                "m.status, m.date, m.ts FROM fts_corpus f "
                "LEFT JOIN doc_path p ON p.rowid = f.rowid "
                "LEFT JOIN doc_meta m ON m.rowid = f.rowid"
            )
        )
        links = sorted(conn.execute("SELECT item_id, initiative FROM item_link"))
        manifest = json.loads(
            conn.execute("SELECT value FROM manifest WHERE key = 'manifest'").fetchone()[0])
        orphans = conn.execute(
            "SELECT (SELECT COUNT(*) FROM doc_meta WHERE rowid NOT IN "
            "(SELECT rowid FROM fts_corpus)) + (SELECT COUNT(*) FROM doc_path WHERE "
            "rowid NOT IN (SELECT rowid FROM fts_corpus))"
        ).fetchone()[0]
    finally:
        conn.close()
    return {"docs": docs, "links": links, "manifest": manifest, "orphans": orphans}


def _doctor_search(monkeypatch, db: Path) -> dict:
    code, body = run_json(monkeypatch, db, "doctor")
    assert code == EXIT_OK, body
    return body["data"]["search"]


# --- a behind index is brought up to date by appending ---------------------


def test_one_new_event_reads_behind_info_and_index_makes_it_current(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "a fresh note about the capybara"}, "k9")
    status = search_mod.index_status(db)
    assert status["status"] == "behind"
    assert status["new_events"] == 1
    assert status["severity"] == "info"
    with _doctor_cannot_refresh(monkeypatch):
        assert _doctor_search(monkeypatch, db)["severity"] == "info"
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert body["data"]["refresh"] == "append"
    assert body["data"]["new_events"] == 1
    assert body["data"]["status"] == "current"
    status = search_mod.index_status(db)
    assert status["status"] == "current"
    assert status["severity"] == "ok"
    assert _doctor_search(monkeypatch, db)["severity"] == "ok"
    code, body = run_json(monkeypatch, db, "search", "capybara")
    assert code == EXIT_OK, body
    assert body["data"]["refreshed"] is False
    assert body["data"]["cards"][0]["id"] == "T-feed"


def test_index_text_names_the_appended_events(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "one"}, "k9")
        _append(lg, "item.note", "T-feed", {"text": "two"}, "k10")
    code, out, _err = invoke(monkeypatch, ["--db", str(db), "index"])
    assert code == EXIT_OK
    assert ", appending 2 new events" in out.splitlines()[0]


def test_an_append_holds_what_a_full_rebuild_holds(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        # A note, a status change (the item's question takes the new status),
        # a new item, a question on another item, an initiative link, and a
        # person utterance on no item.
        _append(lg, "item.note", "T-feed", {"text": "the okapi follow-up"}, "k9")
        _append(lg, "item.state", "T-feed", {"status": "closed"}, "k10")
        _append(lg, "item.created", "T-new", {"type": "ticket", "title": "Gecko intake"}, "k11")
        _append(lg, "decision.asked", "T-zephyr", {
            "question": "Which ibis window?", "options": [{"label": "a"}, {"label": "b"}],
        }, "k12")
        _append(lg, "item.linked", "T-new", {"rel": "item", "target": "T-zephyr"}, "k13")
        _append(lg, "utterance.captured", None,
                {"text": "the tapir call is fine", "origin": "person"}, "k14")
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert body["data"]["refresh"] == "append"
    assert body["data"]["new_events"] == 6
    appended = _snapshot(db)
    assert appended["orphans"] == 0
    assert any(doc[0] == "decision" and doc[6] == "closed" for doc in appended["docs"])
    code, body = run_json(monkeypatch, db, "index", "--rebuild")
    assert body["data"]["refresh"] == "rebuild"
    rebuilt = _snapshot(db)
    assert appended == rebuilt
    assert body["data"]["docs"] == len(appended["docs"])


def test_a_search_on_a_behind_index_appends_without_rebuilding(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-zephyr", {"text": "the pangolin schedule"}, "k9")

    def no_rebuild(*_args, **_kwargs):
        raise AssertionError("a behind index was rebuilt from scratch")

    monkeypatch.setattr(search_mod, "build_index", no_rebuild)
    code, body = run_json(monkeypatch, db, "search", "pangolin")
    assert code == EXIT_OK, body
    assert body["data"]["refreshed"] is True
    assert body["data"]["cards"][0]["id"] == "T-zephyr"
    assert search_mod.index_status(db)["status"] == "current"


def test_an_append_leaves_no_partial_file_and_keeps_the_index_private(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "after"}, "k9")
    run_json(monkeypatch, db, "index")
    assert not list(db.parent.glob(".search.db.*.partial"))
    assert stat.S_IMODE(_index(db).stat().st_mode) & 0o077 == 0


# --- anything but behind takes the full rebuild ----------------------------


def test_a_changed_source_with_new_events_takes_the_full_rebuild(db, root, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "after"}, "k9")
    path = root / "standards/routing.md"
    path.write_text(path.read_text() + "\nthe heron lane\n")
    os.utime(path, ns=(1, 1))
    assert search_mod.index_status(db)["status"] == "stale"
    code, body = run_json(monkeypatch, db, "index")
    assert body["data"]["refresh"] == "rebuild"
    assert search_mod.index_status(db)["status"] == "current"
    code, body = run_json(monkeypatch, db, "search", "heron")
    assert body["data"]["cards"][0]["doc_type"] == "standard"


def test_a_rewritten_event_log_takes_the_full_rebuild(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "after"}, "k9")
    _rewrite_stored_manifest(db, last_event_hash="0" * 64)
    code, body = run_json(monkeypatch, db, "index")
    assert body["data"]["refresh"] == "rebuild"
    assert search_mod.index_status(db)["status"] == "current"


def test_an_index_whose_copy_reads_current_is_rebuilt_not_appended(db, monkeypatch):
    # The precheck said behind, but the copy the append works on is already
    # current (another writer caught up): nothing is appended.
    run_json(monkeypatch, db, "index")
    summary = search_mod.refresh_index(db)
    assert summary["refresh"] == "rebuild"


def test_an_error_in_the_append_falls_back_to_the_full_rebuild(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "the quetzal note"}, "k9")

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(search_mod, "_append_into", broken)
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert body["data"]["refresh"] == "rebuild"
    assert not list(db.parent.glob(".search.db.*.partial"))
    code, body = run_json(monkeypatch, db, "search", "quetzal")
    assert body["data"]["cards"][0]["id"] == "T-feed"


# --- severity ---------------------------------------------------------------


@pytest.mark.parametrize("status, severity", [
    ("current", "ok"), ("behind", "info"), ("stale", "info"), ("missing", "info"),
    ("unknown", "warn"), ("other", "warn"),
])
def test_severity_for_each_status(status, severity):
    assert search_mod.severity_for(status) == severity


def test_missing_and_stale_read_info(db, root, monkeypatch):
    assert search_mod.index_status(db)["severity"] == "info"
    run_json(monkeypatch, db, "index")
    (root / "standards/new.md").write_text("# New\n")
    status = search_mod.index_status(db)
    assert (status["status"], status["severity"]) == ("stale", "info")


def test_a_build_error_reads_warn(db, monkeypatch):
    # search_roots that no build can use: the index cannot be built.
    (db.parent / "config.json").write_text(json.dumps({"search_roots": "not-a-list"}))
    status = search_mod.index_status(db)
    assert status["status"] == "missing"
    assert status["severity"] == "warn"
    assert status["reasons"][-1] == "search_roots must be a list of absolute paths"
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_REFUSED
    assert body["error"]["code"] == "search"


def test_unusable_search_roots_with_an_index_reads_warn_not_a_raise(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    (db.parent / "config.json").write_text(json.dumps({"search_roots": "not-a-list"}))
    status = search_mod.index_status(db)
    assert status["status"] == "unknown"
    assert status["severity"] == "warn"
    assert status["reasons"] == ["search_roots must be a list of absolute paths"]
    found = _doctor_search(monkeypatch, db)
    assert (found["status"], found["severity"]) == ("unknown", "warn")
    assert found["reasons"] == ["search_roots must be a list of absolute paths"]
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_REFUSED
    assert body["error"]["code"] == "search"


def test_an_unreadable_config_with_an_index_reads_unknown_warn_in_doctor(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    (db.parent / "config.json").write_text("{not json")
    found = _doctor_search(monkeypatch, db)
    assert (found["status"], found["severity"]) == ("unknown", "warn")


def test_a_directory_at_the_index_path_reads_warn(db):
    _index(db).mkdir()
    status = search_mod.index_status(db)
    assert status["status"] == "missing"
    assert status["severity"] == "warn"
    assert status["reasons"] == [
        f"search.db is a directory, not a regular file: {_index(db)}"]


def test_an_index_inside_a_git_work_tree_reads_warn(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    assert search_mod.index_status(db)["severity"] == "ok"
    (db.parent / ".git").mkdir()
    status = search_mod.index_status(db)
    assert status["status"] == "current"
    assert status["severity"] == "warn"
    assert status["reasons"] == [
        f"refusing to use search.db inside a git work tree: {db.parent}"]


# --- a fresh install's first index ------------------------------------------


def test_an_index_without_roots_says_only_the_ledger_is_indexed_and_exits_0(db, monkeypatch):
    config = db.parent / "config.json"
    config.unlink()
    code, out, err = invoke(monkeypatch, ["--db", str(db), "index"])
    assert code == EXIT_OK, err
    lines = out.splitlines()
    assert lines[1] == (
        "only the ledger is indexed (search_roots is not set in the ledger's "
        "config.json); to index markdown too, set search_roots to a list of "
        f"absolute paths in {config}"
    )
    assert "no markdown roots" not in out


# --- leftover writer temps ---------------------------------------------------


def _stale_index_partial(
    directory: Path, middle: str, payload: bytes = b"stale-index"
) -> Path:
    """A regular file whose name matches the index writer's temp pattern."""
    path = directory / f".search.db.{middle}.partial"
    path.write_bytes(payload)
    os.utime(path, ns=(1, 1))
    return path


def test_a_planted_stale_index_partial_is_removed_on_rebuild(db, monkeypatch):
    leftover = _stale_index_partial(db.parent, "stale3")
    decoy_backup = db.parent / f".{db.name}.stale3.partial"
    decoy_backup.write_bytes(b"backup-temp")
    os.utime(decoy_backup, ns=(1, 1))
    ledger_before = db.read_bytes()
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert not leftover.exists()
    assert decoy_backup.read_bytes() == b"backup-temp"
    assert db.read_bytes() == ledger_before
    assert search_mod.index_status(db)["status"] == "current"
    assert (db.parent / "search.db").is_file()


def test_a_planted_stale_index_partial_is_removed_on_the_next_append_refresh(
    db, monkeypatch
):
    run_json(monkeypatch, db, "index")
    leftover = _stale_index_partial(db.parent, "stale1")
    decoy_backup = db.parent / f".{db.name}.stale1.partial"
    decoy_backup.write_bytes(b"backup-temp")
    os.utime(decoy_backup, ns=(1, 1))
    decoy_contains = db.parent / "note.search.db.stale1.partial"
    decoy_contains.write_bytes(b"contains-pattern")
    os.utime(decoy_contains, ns=(1, 1))
    decoy_no_dot = db.parent / "search.db.stale1.partial"
    decoy_no_dot.write_bytes(b"no-dot")
    os.utime(decoy_no_dot, ns=(1, 1))
    other_dir = db.parent / "other"
    other_dir.mkdir()
    decoy_elsewhere = _stale_index_partial(other_dir, "stale1")
    link = db.parent / ".search.db.link1.partial"
    link.symlink_to(leftover)
    named_dir = db.parent / ".search.db.dir1.partial"
    named_dir.mkdir()
    empty_middle = db.parent / ".search.db.partial"
    empty_middle.write_bytes(b"empty-middle")
    os.utime(empty_middle, ns=(1, 1))
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "the ibis note"}, "k-partial-1")
    ledger_before = db.read_bytes()
    code, out, err = invoke(monkeypatch, ["--db", str(db), "index"])
    assert code == EXIT_OK, err
    remaining = {p.name for p in db.parent.iterdir()}
    assert leftover.name not in remaining
    assert decoy_backup.read_bytes() == b"backup-temp"
    assert decoy_contains.read_bytes() == b"contains-pattern"
    assert decoy_no_dot.read_bytes() == b"no-dot"
    assert decoy_elsewhere.read_bytes() == b"stale-index"
    assert link.is_symlink()
    assert named_dir.is_dir()
    assert empty_middle.read_bytes() == b"empty-middle"
    assert db.read_bytes() == ledger_before
    assert search_mod.index_status(db)["status"] == "current"


def test_a_newer_index_partial_is_left_for_a_running_writer(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    stale = _stale_index_partial(db.parent, "stale2")
    newer = db.parent / ".search.db.newer2.partial"
    newer.write_bytes(b"running-writer")
    later = 4_000_000_000
    os.utime(newer, (later, later))
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "the ibis note"}, "k-partial-2")
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert not stale.exists()
    assert newer.read_bytes() == b"running-writer"


def test_the_running_rebuild_keeps_its_own_partial_until_it_is_published(
    db, monkeypatch
):
    leftover = _stale_index_partial(db.parent, "stale4")
    seen: dict = {}
    real = search_mod._replace_index

    def wrap(tmp, index, ledger):
        seen["own_name"] = tmp.name
        seen["own_existed"] = tmp.is_file()
        seen["stale_during"] = leftover.is_file()
        assert tmp.name != leftover.name
        real(tmp, index, ledger)

    monkeypatch.setattr(search_mod, "_replace_index", wrap)
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert seen["own_existed"] is True
    assert seen["stale_during"] is True
    assert not leftover.exists()
    assert not (db.parent / seen["own_name"]).exists()


def test_a_partial_removal_error_is_one_stderr_line_and_keeps_the_new_index(
    db, monkeypatch
):
    leftover = _stale_index_partial(db.parent, "stale5")
    real_unlink = os.unlink

    def flaky(path, *args, **kwargs):
        if Path(path) == leftover:
            raise OSError(13, "Permission denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", flaky)
    code, out, err = invoke(monkeypatch, ["--db", str(db), "index"])
    assert code == EXIT_OK, err
    assert leftover.is_file()
    assert leftover.read_bytes() == b"stale-index"
    lines = [ln for ln in err.splitlines() if ln]
    assert len(lines) == 1, err
    assert lines[0].startswith("cannot remove leftover search index partial ")
    assert leftover.name in lines[0]
    assert "\n" not in lines[0]
    assert search_mod.index_status(db)["status"] == "current"
    assert _index(db).is_file()


def test_a_listing_error_during_cleanup_leaves_the_partial_and_the_index(
    db, monkeypatch
):
    leftover = _stale_index_partial(db.parent, "stale8")
    real_replace = search_mod._replace_index
    real_listdir = os.listdir

    def replace_then_break(tmp, index, ledger):
        real_replace(tmp, index, ledger)

        def boom(path):
            if Path(path) == db.parent:
                raise OSError(13, "Permission denied")
            return real_listdir(path)

        monkeypatch.setattr(os, "listdir", boom)

    monkeypatch.setattr(search_mod, "_replace_index", replace_then_break)
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert leftover.is_file()
    assert search_mod.index_status(db)["status"] == "current"


def test_doctor_refresh_removes_a_planted_stale_index_partial(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    leftover = _stale_index_partial(db.parent, "stale6")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "the ibis note"}, "k-partial-6")
    code, body = run_json(monkeypatch, db, "doctor")
    assert code == EXIT_OK, body
    assert not leftover.exists()
    assert body["data"]["search"]["status"] == "current"


def test_search_refresh_removes_a_planted_stale_index_partial(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    leftover = _stale_index_partial(db.parent, "stale7")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "the ibis note"}, "k-partial-7")
    code, body = run_json(monkeypatch, db, "search", "ibis")
    assert code == EXIT_OK, body
    assert not leftover.exists()
    assert body["data"]["cards"][0]["id"] == "T-feed"
