"""``ledger index`` / ``ledger search``: review round 2 regressions.

Uses the fixtures and helpers of ``test_search.py``. Every file is synthetic
and built under tmp_path.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from ledger import search as search_mod
from ledger.verbs import EXIT_NOT_FOUND, EXIT_OK, EXIT_REFUSED, EXIT_USAGE

from test_search import _sha, db, invoke, root, run_json, top  # noqa: F401  (fixtures)


def test_a_type_word_alone_never_matches(db, monkeypatch):
    # doc_type is filtered by equality, never matched: "standards" appears in
    # no title or content, so the query is empty rather than every standard.
    code, body = run_json(monkeypatch, db, "search", "zzzqqq standards")
    assert code == EXIT_NOT_FOUND, body
    assert body["error"]["code"] == "empty"
    code, body = run_json(monkeypatch, db, "search", "adr")
    assert code == EXIT_NOT_FOUND, body
    # The --type filter still works on the unindexed column.
    assert top(monkeypatch, db, "backup", "--type", "standard")["doc_type"] == "standard"


def test_a_failed_build_leaves_no_partial_file_and_the_old_index_unchanged(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    index = db.parent / "search.db"
    before = _sha(index)

    def boom(*args, **kwargs):
        raise RuntimeError("injected failure after mkstemp")

    monkeypatch.setattr(search_mod, "json", SimpleNamespace(dumps=boom, loads=json.loads))
    with pytest.raises(RuntimeError, match="injected failure"):
        search_mod.build_index(db)
    assert list(db.parent.glob(".search.db.*.partial")) == []
    assert _sha(index) == before


def test_a_symlink_out_of_standards_is_not_indexed(db, root, monkeypatch, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("# Secret\n\n## Inside\nkiwisecret\n")
    (root / "standards" / "leak.md").symlink_to(secret)
    (root / "decisions" / "leak.md").symlink_to(secret)
    code, body = run_json(monkeypatch, db, "search", "kiwisecret")
    assert code == EXIT_NOT_FOUND, body


def test_a_symlink_inside_its_own_folder_is_still_indexed(db, root, monkeypatch):
    (root / "standards" / "alias.md").symlink_to(root / "standards" / "routing.md")
    assert top(monkeypatch, db, "reviewers")["path"] == str((root / "standards/routing.md").resolve())


@pytest.mark.parametrize("verb", [["index"], ["index", "--rebuild"], ["search", "quokka"]])
def test_both_verbs_refuse_once_the_ledger_directory_becomes_a_git_work_tree(db, monkeypatch, verb):
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK and search_mod.index_status(db)["status"] == "current"
    subprocess.run(["git", "init", "-q", str(db.parent)], check=True)
    code, body = run_json(monkeypatch, db, *verb)
    assert code == EXIT_REFUSED, body
    assert body["error"]["code"] == "search"
    assert "git work tree" in body["error"]["message"]


def _anchors(text: str) -> list[str]:
    return [anchor for anchor, _title, _body in search_mod.chunk_markdown(text, "f") if anchor]


def test_a_longer_fence_closes_only_on_its_own_length():
    text = (
        "## Visible\n"
        "````md\n"
        "```\n"
        "## Hidden\n"
        "```\n"
        "````\n"
        "## After\n"
        "~~~\n"
        "## Tilde hidden\n"
        "```\n"
        "## Still hidden\n"
        "~~~~\n"
        "## Last\n"
    )
    assert _anchors(text) == ["visible", "after", "last"]
    chunks = search_mod.chunk_markdown(text, "f")
    assert "## Hidden" in chunks[0][2]


def test_a_fence_line_with_trailing_text_does_not_close():
    assert _anchors("```\n## A\n``` not a close\n## B\n```\n## C\n") == ["c"]


def test_anchors_never_collide_with_an_earlier_suffixed_heading():
    assert _anchors("## Notes\na\n## Notes-1\nb\n## Notes\nc\n") == ["notes", "notes-1", "notes-2"]


def test_an_empty_heading_does_not_take_the_preamble_id():
    assert _anchors("intro\n## !!!\nx\n") == ["-1"]


@pytest.mark.parametrize("value", ["1001", str(10**30)])
def test_limit_above_the_maximum_is_a_usage_error(db, monkeypatch, value):
    code, out, err = invoke(monkeypatch, ["--json", "--db", str(db), "search", "the", "--limit", value])
    assert code == EXIT_USAGE, (out, err)
    assert "Traceback" not in err
    assert json.loads(out)["error"]["code"] == "usage"


def test_limit_at_the_maximum_is_accepted(db, monkeypatch):
    code, body = run_json(monkeypatch, db, "search", "the", "--limit", "1000")
    assert code == EXIT_OK, body
    assert body["data"]["limit"] == 1000


def test_library_search_refuses_an_out_of_range_limit(db):
    with pytest.raises(search_mod.SearchError):
        search_mod.search(db, "quokka", limit=10**30)


# --- review round 3 -------------------------------------------------------------


def test_a_standards_directory_symlinked_outside_the_root_is_not_indexed(
        db, root, monkeypatch, tmp_path):
    outside = tmp_path / "outside-standards"
    outside.mkdir()
    (outside / "elsewhere.md").write_text("# Elsewhere\n\n## Part\nmarmoset\n")
    moved = root / "standards-real"
    (root / "standards").rename(moved)
    (root / "standards").symlink_to(outside, target_is_directory=True)
    code, body = run_json(monkeypatch, db, "index", "--rebuild")
    assert code == EXIT_OK, body
    assert "standard" not in body["data"]["by_type"]
    code, body = run_json(monkeypatch, db, "search", "marmoset")
    assert code == EXIT_NOT_FOUND, body


def test_build_index_itself_refuses_a_git_work_tree(db):
    subprocess.run(["git", "init", "-q", str(db.parent)], check=True)
    with pytest.raises(search_mod.SearchError, match="git work tree"):
        search_mod.build_index(db)
    assert not (db.parent / "search.db").exists()


# --- ledger identity in the manifest ----------------------------------------------


def test_a_ledger_sharing_its_history_but_not_its_last_event_marks_the_index_stale(
        db, monkeypatch):
    # The realistic case: an older copy of the ledger, appended to until the
    # event count matches again. Every earlier hash is identical, so only the
    # FINAL event's hash can tell the two ledgers apart.
    run_json(monkeypatch, db, "index")
    assert search_mod.index_status(db)["status"] == "current"
    from ledger.store import open_ledger
    with open_ledger(db) as lg:
        original = lg.events()
    db.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    with open_ledger(db) as lg:
        for event in original[:-1]:
            lg.append(kind=event.kind, item_id=event.item_id, payload=event.payload,
                      idempotency_key=event.idempotency_key, payload_version=event.payload_version,
                      actor=event.actor, harness=event.harness, model=event.model,
                      session=event.session, channel=event.channel,
                      provenance_class=event.provenance_class, recorded_at=event.recorded_at)
        lg.append(kind="utterance.captured", item_id=None,
                  payload={"text": "a different final event"},
                  idempotency_key="different-final", harness="fixture")
        replaced = lg.events()
    assert len(replaced) == len(original)
    assert [e.hash for e in replaced[:-1]] == [e.hash for e in original[:-1]]
    assert replaced[-1].hash != original[-1].hash
    status = search_mod.index_status(db)
    assert status["status"] == "stale"
    assert "last_event_hash changed" in status["reasons"]


def test_an_index_written_without_the_event_hash_reads_as_stale(db, monkeypatch):
    import sqlite3
    run_json(monkeypatch, db, "index")
    index = db.parent / "search.db"
    stored = search_mod.stored_manifest(index)
    del stored["last_event_hash"]
    conn = sqlite3.connect(index)
    conn.execute("UPDATE manifest SET value = ? WHERE key = 'manifest'", (json.dumps(stored),))
    conn.commit()
    conn.close()
    status = search_mod.index_status(db)
    assert status["status"] == "stale"
    assert "last_event_hash changed" in status["reasons"]


def test_an_empty_ledger_has_a_stable_manifest_identity(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    from ledger.store import open_ledger
    path = tmp_path / "empty" / "home.db"
    with open_ledger(path):
        pass
    first = search_mod.current_manifest(path)
    assert first["last_event_hash"] == ""
    assert search_mod.current_manifest(path) == first
