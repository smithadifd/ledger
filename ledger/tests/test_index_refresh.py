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

from test_search import _append, _rewrite_stored_manifest, db, invoke, root, run_json  # noqa: F401

CONCEPTS = Path(__file__).resolve().parents[2] / "docs" / "concepts.md"


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


# --- docs --------------------------------------------------------------------


def test_concepts_documents_note_search_and_severity():
    text = CONCEPTS.read_text(encoding="utf-8")
    section = text.split("## The search index", 1)[1]
    assert "A note is searched as part of its item, not\nas its own document" in section
    for severity in ("`ok`", "`info`", "`warn`"):
        assert f"| {severity} |" in section
