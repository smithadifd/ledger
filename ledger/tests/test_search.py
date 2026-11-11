"""``ledger index`` / ``ledger search``: a derived search.db beside the ledger.

Every ledger and markdown file here is synthetic and built under tmp_path.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import stat
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ledger import search as search_mod
from ledger.cli import main
from ledger.store import load_migrations, open_ledger
from ledger.verbs import EXIT_NOT_FOUND, EXIT_OK, EXIT_REFUSED, EXIT_USAGE

DOCS = {
    "standards/security.md": (
        "# Security\n\nIntro text about care.\n\n"
        "## Secrets\nNever print tokens.\n\n"
        "## Backup posture\nThe ledger uses the online backup API and an hourly export.\n\n"
        "```\n## not a heading inside a fence\n```\n"
    ),
    "standards/routing.md": "# Routing\n\n## Lanes\nBuilders and reviewers take separate lanes.\n",
    "decisions/0034-state-ledger.md": (
        "# 0034 — Agent state architecture: a local SQLite ledger\n\n"
        "## Decision\nOne local SQLite file per system; markdown keeps rationale.\n\n"
        "## Consequences\nThe markdown loop keeps running.\n"
    ),
    "decisions/0005-feed-posture.md": (
        "# 0005 — Feed reader posture\n\n## Context\nThe reader subscribes to Atom feeds.\n"
    ),
    "roadmaps/plan.md": (
        "# Plan\n\n## Notes\nzephyr zephyr zephyr notes\n"
    ),
    "roadmaps/unlinked.md": "# Unlinked\n\n## Body\nnarwhal\n",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def invoke(monkeypatch, argv: list[str]) -> tuple[int, str, str]:
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def run_json(monkeypatch, db: Path, *argv: str) -> tuple[int, dict]:
    code, out, err = invoke(monkeypatch, ["--json", "--db", str(db), *argv])
    assert "Traceback" not in err
    return code, json.loads(out)


def _append(lg, kind: str, item_id: str | None, payload: dict, key: str):
    return lg.append(kind=kind, item_id=item_id, payload=payload,
                     idempotency_key=key, harness="fixture")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "harness"
    for rel, text in DOCS.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return base


@pytest.fixture
def db(tmp_path: Path, root: Path, monkeypatch) -> Path:
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    path = tmp_path / "state" / "home.db"
    with open_ledger(path) as lg:
        _append(lg, "item.created", "T-feed",
                {"type": "ticket", "title": "Fix feed-condenser digest"}, "k1")
        _append(lg, "item.note", "T-feed", {"text": "background in roadmaps/plan.md:3"}, "k2")
        _append(lg, "item.created", "T-zephyr",
                {"type": "ticket", "title": "Zephyr rollout"}, "k3")
        _append(lg, "item.note", "T-zephyr",
                {"text": "a longer note about the rollout schedule and its owners"}, "k4")
        _append(lg, "item.created", "T-done",
                {"type": "ticket", "title": "Unrelated chores", "status": "closed"}, "k5")
        _append(lg, "decision.asked", "T-feed", {
            "question": "Which digest cadence?",
            "options": [{"label": "daily"}, {"label": "weekly"}],
        }, "k6")
        _append(lg, "utterance.captured", None,
                {"text": "the quokka plan is approved", "origin": "person"}, "k7")
        _append(lg, "utterance.captured", None, {"text": "the wombat words"}, "k8")
    (path.parent / "config.json").write_text(
        json.dumps({"search_roots": [str(root)]}), encoding="utf-8")
    return path


def top(monkeypatch, db: Path, query: str, *extra: str) -> dict:
    code, body = run_json(monkeypatch, db, "search", query, *extra)
    assert code == EXIT_OK, body
    return body["data"]["cards"][0]


# --- the three orientation queries -------------------------------------------


def test_decide_about_query_finds_the_adr(db, monkeypatch):
    card = top(monkeypatch, db, "what did we decide about state architecture")
    assert card["doc_type"] == "adr"
    assert card["id"].startswith("decisions/0034-state-ledger.md")


def test_where_does_query_finds_the_standard_section(db, monkeypatch, root):
    card = top(monkeypatch, db, "where does backup posture live")
    assert card["doc_type"] == "standard"
    assert card["id"] == "standards/security.md#backup-posture"
    assert card["title"] == "Security § Backup posture"
    assert card["path"] == str((root / "standards/security.md").resolve())
    assert "[backup]" in card["snippet"]


def test_open_on_query_finds_the_item(db, monkeypatch):
    card = top(monkeypatch, db, "what is open on feed-condenser")
    assert (card["doc_type"], card["id"]) == ("item", "T-feed")
    assert set(card) == {"score", "doc_type", "id", "title", "snippet",
                         "item_id", "status", "date"}


def test_title_match_outranks_body_match(db, monkeypatch):
    # The linked plan says "zephyr" three times in a short body; the item says
    # it once, in its title. Mutation: title weight 1.0 puts the plan first.
    code, body = run_json(monkeypatch, db, "search", "zephyr")
    assert code == EXIT_OK
    ids = [card["id"] for card in body["data"]["cards"]]
    assert ids[:2] == ["T-zephyr", "roadmaps/plan.md#notes"]


# --- the ledger is never written ----------------------------------------------


def test_index_and_search_leave_the_ledger_bytes_identical(db, monkeypatch):
    before = _sha(db)
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert body["data"]["rebuilt"] is True
    assert _sha(db) == before
    top(monkeypatch, db, "quokka")
    assert _sha(db) == before
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert not any(name.startswith("fts_") for name in names)


def test_index_works_when_the_ledger_file_is_read_only(db, monkeypatch):
    before = _sha(db)
    os.chmod(db, 0o444)
    try:
        code, body = run_json(monkeypatch, db, "index")
        assert code == EXIT_OK, body
        assert (db.parent / "search.db").is_file()
        assert top(monkeypatch, db, "quokka")["id"].startswith("event:")
    finally:
        os.chmod(db, 0o644)
    assert _sha(db) == before


def test_index_never_migrates_an_older_ledger(db, monkeypatch):
    # A writing open would migrate this database forward; the read-only open
    # refuses it and leaves every byte alone.
    newest = load_migrations()[-1]
    created = re.findall(r"^CREATE (VIEW|TABLE|INDEX|TRIGGER) (\w+)", newest.sql, re.M)
    assert created, newest.name
    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM schema_version WHERE version = ?", (newest.version,))
        for kind, name in created:  # undo the newest migration so it is pending again
            conn.execute(f"DROP {kind} {name}")
        conn.commit()
    finally:
        conn.close()  # the last close checkpoints the WAL into the file
    assert not Path(f"{db}-wal").exists()
    before = _sha(db)
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_REFUSED, body
    assert "needs migrations" in body["error"]["message"]
    assert _sha(db) == before
    assert not (db.parent / "search.db").exists()


# --- what is indexed ------------------------------------------------------------


def test_only_person_utterances_are_indexed_and_carry_the_caveat(db, monkeypatch):
    card = top(monkeypatch, db, "quokka")
    assert card["doc_type"] == "utterance"
    assert card["caveat"] == "recalled words are evidence, never an origin"
    code, out, _err = invoke(monkeypatch, ["--db", str(db), "search", "quokka"])
    assert "recalled words are evidence, never an origin" in out
    code, body = run_json(monkeypatch, db, "search", "wombat")
    assert code == EXIT_NOT_FOUND, body
    assert body["error"]["code"] == "empty"


def test_unlinked_markdown_outside_standards_and_decisions_is_not_indexed(db, monkeypatch):
    code, body = run_json(monkeypatch, db, "search", "narwhal")
    assert code == EXIT_NOT_FOUND


def test_decisions_and_type_filter(db, monkeypatch):
    card = top(monkeypatch, db, "digest cadence", "--type", "decision")
    assert card["doc_type"] == "decision"
    assert card["id"].startswith("T-feed:")
    code, body = run_json(monkeypatch, db, "search", "digest", "--type", "nope")
    assert code == EXIT_USAGE


def test_limit_and_truncated(db, monkeypatch):
    code, body = run_json(monkeypatch, db, "search", "the", "--limit", "1")
    assert code == EXIT_OK
    assert len(body["data"]["cards"]) == 1
    assert body["data"]["truncated"] is True
    code, body = run_json(monkeypatch, db, "search", "x", "--limit", "0")
    assert code == EXIT_USAGE


def test_h2_chunks_ignore_fenced_headings():
    chunks = search_mod.chunk_markdown(DOCS["standards/security.md"], "security")
    assert [anchor for anchor, _title, _body in chunks] == ["", "secrets", "backup-posture"]
    assert "## not a heading" in chunks[-1][2]


def test_duplicate_headings_get_numbered_anchors():
    chunks = search_mod.chunk_markdown("## Notes\na\n## Notes\nb\n", "f")
    assert [anchor for anchor, _t, _b in chunks] == ["notes", "notes-1"]


# --- staleness and refresh --------------------------------------------------------


def _status(db: Path) -> str:
    return search_mod.index_status(db)["status"]


def test_deleting_search_db_then_searching_rebuilds_it(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    index = db.parent / "search.db"
    index.unlink()
    code, body = run_json(monkeypatch, db, "search", "quokka")
    assert code == EXIT_OK, body
    assert body["data"]["refreshed"] is True
    assert index.is_file()
    assert _status(db) == "current"


def test_current_index_is_not_rebuilt(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    code, body = run_json(monkeypatch, db, "index")
    assert body["data"] == {"path": str(db.parent / "search.db"), "rebuilt": False,
                            "status": "current"}
    code, body = run_json(monkeypatch, db, "search", "quokka")
    assert body["data"]["refreshed"] is False
    code, body = run_json(monkeypatch, db, "index", "--rebuild")
    assert body["data"]["rebuilt"] is True


@pytest.mark.parametrize("change", ["edit", "add", "delete", "link", "roots"])
def test_any_source_change_makes_the_index_stale(db, root, monkeypatch, change, tmp_path):
    run_json(monkeypatch, db, "index")
    assert _status(db) == "current"
    if change == "edit":
        path = root / "standards/routing.md"
        path.write_text(path.read_text() + "\nmore words\n")
        os.utime(path, ns=(1, 1))
    elif change == "add":
        (root / "standards/new.md").write_text("# New\n")
    elif change == "delete":
        (root / "decisions/0005-feed-posture.md").unlink()
    elif change == "link":
        # A path named in an item payload, created after the index was built.
        with open_ledger(db) as lg:
            _append(lg, "item.note", "T-feed", {"text": "see roadmaps/later.md"}, "k9")
        run_json(monkeypatch, db, "index")
        assert _status(db) == "current"
        (root / "roadmaps/later.md").write_text("# Later\n\n## Part\nplatypus\n")
    elif change == "roots":
        other = tmp_path / "other"
        other.mkdir()
        (db.parent / "config.json").write_text(
            json.dumps({"search_roots": [str(root), str(other)]}))
    assert _status(db) == "stale"
    if change == "link":
        assert top(monkeypatch, db, "platypus")["id"] == "roadmaps/later.md#part"


def _index_rows(db: Path) -> int:
    conn = sqlite3.connect((db.parent / "search.db").as_uri() + "?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM fts_corpus").fetchone()[0]
    finally:
        conn.close()


def test_trailing_events_are_behind_not_stale_and_search_finds_them(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "a fresh note about the capybara"}, "k9")
    status = search_mod.index_status(db)
    assert status["status"] == "behind"
    assert status["reasons"] == []
    assert status["new_events"] == 1
    code, body = run_json(monkeypatch, db, "doctor")
    assert code == EXIT_OK
    assert body["data"]["search"]["status"] != "stale"
    code, out, _err = invoke(monkeypatch, ["--db", str(db), "doctor"])
    assert "search: current to event 8, 1 new event will be indexed on the next search" in out
    assert "stale" not in out
    code, body = run_json(monkeypatch, db, "search", "capybara")
    assert code == EXIT_OK, body
    assert body["data"]["cards"][0]["id"] == "T-feed"
    assert body["data"]["refreshed"] is True
    assert _status(db) == "current"
    code, body = run_json(monkeypatch, db, "search", "capybara")
    assert body["data"]["refreshed"] is False


def test_index_verb_catches_up_when_behind(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.created", "T-new", {"type": "ticket", "title": "new"}, "k9")
    code, body = run_json(monkeypatch, db, "index")
    assert body["data"]["rebuilt"] is True
    assert _status(db) == "current"


def test_behind_with_an_empty_ledger_index(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    path = tmp_path / "state" / "empty.db"
    with open_ledger(path):
        pass
    run_json(monkeypatch, path, "index")
    assert _status(path) == "current"
    with open_ledger(path) as lg:
        _append(lg, "item.created", "T-a", {"type": "ticket", "title": "a"}, "k1")
    assert search_mod.index_status(path)["status"] == "behind"


@pytest.mark.parametrize("changes", [{"last_event_hash": "f" * 64}, {"last_event_id": False}])
def test_an_empty_ledger_index_with_a_bad_record_reads_as_stale(tmp_path, monkeypatch, changes):
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    path = tmp_path / "state" / "empty.db"
    with open_ledger(path):
        pass
    run_json(monkeypatch, path, "index")
    with open_ledger(path) as lg:
        _append(lg, "item.created", "T-a", {"type": "ticket", "title": "a"}, "k1")
    _rewrite_stored_manifest(path, **changes)
    assert _status(path) == "stale"


def _rewrite_stored_manifest(db: Path, **changes) -> None:
    conn = sqlite3.connect(db.parent / "search.db")
    try:
        manifest = json.loads(
            conn.execute("SELECT value FROM manifest WHERE key = 'manifest'").fetchone()[0])
        manifest.update(changes)
        conn.execute("UPDATE manifest SET value = ? WHERE key = 'manifest'",
                     (json.dumps(manifest, sort_keys=True),))
        conn.commit()
    finally:
        conn.close()


def test_a_rewritten_event_log_still_reads_as_stale(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "after the build"}, "k9")
    # The event at the recorded id now has a different hash than the index saw.
    _rewrite_stored_manifest(db, last_event_hash="0" * 64)
    status = search_mod.index_status(db)
    assert status["status"] == "stale"
    assert "last_event_hash changed" in status["reasons"]
    code, body = run_json(monkeypatch, db, "search", "quokka")
    assert body["data"]["refreshed"] is True
    assert _status(db) == "current"


def test_a_truncated_event_log_still_reads_as_stale(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    _rewrite_stored_manifest(db, last_event_id=999)
    status = search_mod.index_status(db)
    assert status["status"] == "stale"
    assert "last_event_id changed" in status["reasons"]


@pytest.mark.parametrize("bad", [None, "8", -1, True])
def test_an_unreadable_event_position_reads_as_stale(db, monkeypatch, bad):
    run_json(monkeypatch, db, "index")
    _rewrite_stored_manifest(db, last_event_id=bad)
    assert _status(db) == "stale"


def test_a_changed_ranker_is_stale_even_with_trailing_events(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "after the build"}, "k9")
    _rewrite_stored_manifest(db, ranker="other")
    assert _status(db) == "stale"


def test_a_behind_index_is_never_answered_from(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    before = _index_rows(db)
    with open_ledger(db) as lg:
        _append(lg, "decision.asked", "T-feed", {
            "question": "Which marmoset policy?", "options": [{"label": "a"}, {"label": "b"}],
        }, "k9")
    code, body = run_json(monkeypatch, db, "search", "marmoset")
    assert code == EXIT_OK, body
    assert body["data"]["cards"], body
    assert _index_rows(db) == before + 1


# --- doctor -------------------------------------------------------------------------


def test_doctor_reports_search_state_and_never_fails_on_it(db, monkeypatch):
    code, body = run_json(monkeypatch, db, "doctor")
    assert code == EXIT_OK
    assert body["data"]["search"]["status"] == "missing"
    run_json(monkeypatch, db, "index")
    code, body = run_json(monkeypatch, db, "doctor")
    assert body["data"]["search"]["status"] == "current"
    config = db.parent / "config.json"
    good_config = config.read_text()
    config.write_text("{not json")
    code, body = run_json(monkeypatch, db, "doctor")
    assert code == EXIT_OK
    assert body["data"]["search"]["status"] == "unknown"
    code, out, _err = invoke(monkeypatch, ["--db", str(db), "doctor"])
    assert code == EXIT_OK
    # An unusable config is a warning: ledger index refuses, so the line names
    # the reason and its remedy instead of saying the index rebuilds itself.
    assert f"search: unknown (warn: ledger index refuses: unreadable ledger config {config}: " in out
    assert "to clear it, make config.json readable, valid JSON)" in out
    assert "rebuilds it" not in out
    config.write_text(good_config)
    (db.parent / "search.db").write_bytes(b"not a database at all " * 50)
    code, body = run_json(monkeypatch, db, "doctor")
    assert code == EXIT_OK
    assert body["data"]["search"]["status"] == "stale"


def _doctor_search_line(monkeypatch, db: Path) -> str:
    code, out, err = invoke(monkeypatch, ["--db", str(db), "doctor"])
    assert code == EXIT_OK, err
    lines = [line for line in out.splitlines() if line.startswith("search: ")]
    assert len(lines) == 1, out
    return lines[0]


def _warn_git(db, monkeypatch, root, tmp_path):
    run_json(monkeypatch, db, "index")
    (db.parent / ".git").mkdir()
    return "current", (
        f"refusing to use search.db inside a git work tree: {db.parent}",
        "move the ledger database to a directory outside the git work tree",
    )


def _warn_git_behind(db, monkeypatch, root, tmp_path):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "after the build"}, "k9")
    (db.parent / ".git").mkdir()
    return "behind", (
        f"refusing to use search.db inside a git work tree: {db.parent}",
        "move the ledger database to a directory outside the git work tree",
    )


def _warn_directory(db, monkeypatch, root, tmp_path):
    (db.parent / "search.db").mkdir()
    return "missing", (
        f"search.db is a directory, not a regular file: {db.parent / 'search.db'}",
        "move that directory away from the index path",
    )


def _warn_unreadable_config(db, monkeypatch, root, tmp_path):
    run_json(monkeypatch, db, "index")
    (db.parent / "config.json").write_text("{not json")
    return "unknown", (
        f"unreadable ledger config {db.parent / 'config.json'}: ",
        "make config.json readable, valid JSON",
    )


def _warn_config_not_object(db, monkeypatch, root, tmp_path):
    (db.parent / "config.json").write_text("[]")
    return "missing", (
        f"ledger config {db.parent / 'config.json'} must be a JSON object",
        "make config.json a JSON object",
    )


def _warn_roots_not_list(db, monkeypatch, root, tmp_path):
    (db.parent / "config.json").write_text(json.dumps({"search_roots": "not-a-list"}))
    return "missing", (
        "search_roots must be a list of absolute paths",
        "set search_roots in config.json to a list of absolute paths",
    )


def _warn_roots_not_absolute(db, monkeypatch, root, tmp_path):
    run_json(monkeypatch, db, "index")
    (db.parent / "config.json").write_text(json.dumps({"search_roots": ["relative/path"]}))
    return "unknown", (
        "search_roots entry is not absolute: relative/path",
        "make every search_roots entry in config.json an absolute path",
    )


def _warn_unstattable_index(db, monkeypatch, root, tmp_path):
    index = db.parent / "search.db"
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if Path(path) == index:
            raise PermissionError(13, "Permission denied", str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(search_mod.os, "lstat", lstat)
    return None, (
        f"cannot stat the search index {index}: ",
        "make the index path's directory readable",
    )


@pytest.mark.parametrize("make", [
    _warn_git, _warn_git_behind, _warn_directory, _warn_unreadable_config,
    _warn_config_not_object, _warn_roots_not_list, _warn_roots_not_absolute,
    _warn_unstattable_index,
])
def test_doctor_warn_line_names_the_reason_and_its_remedy(db, root, tmp_path, monkeypatch, make):
    status, (reason, remedy) = make(db, monkeypatch, root, tmp_path)
    found = search_mod.index_status(db)
    assert found["severity"] == "warn", found
    if status is not None:
        assert found["status"] == status, found
    line = _doctor_search_line(monkeypatch, db)
    assert line.startswith(f"search: {found['status']} (warn: ledger index refuses: {reason}"), line
    assert f"; to clear it, {remedy}" in line, line
    assert line.endswith(")"), line
    # Never the self-rebuild claim, in either of its forms.
    assert "rebuilds it" not in line, line
    assert "will be indexed on the next search" not in line, line
    # The JSON form is unchanged: the same status, severity and reasons.
    code, body = run_json(monkeypatch, db, "doctor")
    assert code == EXIT_OK
    assert body["data"]["search"] == found


def test_doctor_warn_line_with_an_unknown_reason_prints_it_and_points_to_json(db, monkeypatch):
    def fake_status(_db, _fallback=None):
        return {"path": "x", "status": "missing", "reasons": ["a reason doctor has no remedy for"],
                "severity": "warn"}

    monkeypatch.setattr(search_mod, "index_status", fake_status)
    assert _doctor_search_line(monkeypatch, db) == (
        "search: missing (warn: a reason doctor has no remedy for; see ledger doctor --json)")


def test_doctor_warn_line_with_no_reason_says_so(db, monkeypatch):
    def fake_status(_db, _fallback=None):
        return {"path": "x", "status": "missing", "reasons": [], "severity": "warn"}

    monkeypatch.setattr(search_mod, "index_status", fake_status)
    assert _doctor_search_line(monkeypatch, db) == (
        "search: missing (warn: no reason given; see ledger doctor --json)")


def test_doctor_unreadable_index_state_prints_the_error_and_points_to_json(db, monkeypatch):
    def broken_status(_db, _fallback=None):
        raise RuntimeError("the state could not be read")

    monkeypatch.setattr(search_mod, "index_status", broken_status)
    assert _doctor_search_line(monkeypatch, db) == (
        "search: unknown (warn: RuntimeError: the state could not be read; "
        "see ledger doctor --json)")


def test_doctor_unknown_state_prints_its_own_reason_beside_a_known_one(db, monkeypatch):
    def fake_status(_db, _fallback=None):
        return {"path": "x", "status": "unknown", "severity": "warn", "reasons": [
            "an unforeseen failure",
            f"refusing to use search.db inside a git work tree: {db.parent}",
        ]}

    monkeypatch.setattr(search_mod, "index_status", fake_status)
    assert _doctor_search_line(monkeypatch, db) == (
        f"search: unknown (warn: ledger index refuses: refusing to use search.db inside a git "
        f"work tree: {db.parent}; to clear it, move the ledger database to a directory outside "
        f"the git work tree; an unforeseen failure; see ledger doctor --json)")


def test_doctor_stale_reasons_are_not_repeated_on_a_warn_line(db, root, monkeypatch):
    run_json(monkeypatch, db, "index")
    (root / "standards/new.md").write_text("# New\n")
    (db.parent / ".git").mkdir()
    found = search_mod.index_status(db)
    assert (found["status"], found["severity"]) == ("stale", "warn")
    assert _doctor_search_line(monkeypatch, db) == (
        f"search: stale (warn: ledger index refuses: refusing to use search.db inside a git "
        f"work tree: {db.parent}; to clear it, move the ledger database to a directory outside "
        f"the git work tree)")


def _info_missing(db, monkeypatch, root):
    pass


def _info_stale(db, monkeypatch, root):
    run_json(monkeypatch, db, "index")
    (root / "standards/new.md").write_text("# New\n")


@pytest.mark.parametrize("make, status", [(_info_missing, "missing"), (_info_stale, "stale")])
def test_doctor_info_line_keeps_the_rebuild_parenthetical(db, root, monkeypatch, make, status):
    make(db, monkeypatch, root)
    found = search_mod.index_status(db)
    assert (found["status"], found["severity"]) == (status, "info")
    assert _doctor_search_line(monkeypatch, db) == f"search: {status} (ledger index rebuilds it)"


def test_doctor_behind_line_names_the_new_events_without_a_warning(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "after the build"}, "k9")
    assert search_mod.index_status(db)["severity"] == "info"
    assert _doctor_search_line(monkeypatch, db) == (
        "search: current to event 8, 1 new event will be indexed on the next search")


def test_doctor_current_line_has_no_hint(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    assert search_mod.index_status(db)["severity"] == "ok"
    assert _doctor_search_line(monkeypatch, db) == "search: current"


# --- never exported, never committed -------------------------------------------------


def test_export_and_backup_output_lack_search_db(db, monkeypatch, tmp_path):
    run_json(monkeypatch, db, "index")
    assert (db.parent / "search.db").is_file()
    export_dir = tmp_path / "export"
    code, body = run_json(monkeypatch, db, "export", "--dir", str(export_dir))
    assert code == EXIT_OK, body
    names = sorted(p.name for p in export_dir.rglob("*"))
    assert "search.db" not in names
    assert all(n.endswith(".json") for n in names), names
    backup = tmp_path / "backup" / "home-backup.db"
    code, body = run_json(monkeypatch, db, "backup", "--to", str(backup))
    assert code == EXIT_OK, body
    assert sorted(p.name for p in backup.parent.iterdir()) == ["home-backup.db"]
    with sqlite3.connect(backup) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "fts_corpus" not in tables


def test_index_refuses_a_ledger_directory_inside_a_git_work_tree(db, monkeypatch):
    (db.parent / ".git").mkdir()
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_REFUSED
    assert body["error"]["code"] == "search"
    assert not (db.parent / "search.db").exists()


def test_search_db_is_private_to_the_owner(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    mode = stat.S_IMODE((db.parent / "search.db").stat().st_mode)
    assert mode & 0o077 == 0
    assert not list(db.parent.glob(".search.db.*.partial"))


# --- roots ------------------------------------------------------------------------------


# The fallback root a caller passes in (``search_fallback_root``) is checked
# where the caller lives: the toolkit's ``scripts/ledger`` command suite.


def test_no_root_and_no_resolver_indexes_the_ledger_only(db, monkeypatch):
    (db.parent / "config.json").unlink()
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert body["data"]["roots"] == []
    assert "search_roots" in body["data"]["note"]
    assert top(monkeypatch, db, "quokka")["doc_type"] == "utterance"


@pytest.mark.parametrize("roots", ["not-a-list", ["relative/path"], [""]])
def test_bad_search_roots_are_refused(db, monkeypatch, roots):
    (db.parent / "config.json").write_text(json.dumps({"search_roots": roots}))
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_REFUSED
    assert body["error"]["code"] == "search"


def test_linked_paths_outside_every_root_are_not_indexed(db, monkeypatch, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n\n## Secret\nkiwifruit\n")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": f"see {outside}"}, "k9")
    code, body = run_json(monkeypatch, db, "search", "kiwifruit")
    assert code == EXIT_NOT_FOUND
