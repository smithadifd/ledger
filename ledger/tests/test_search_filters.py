"""``ledger search`` filters (--status, --item, --initiative, --since, --until)
and the ranker named in the index manifest.

Every document below matches the text "harbor lantern"; each filter test
shows a document that matches on text but not on the filter is excluded.
Everything is synthetic and built under tmp_path.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ledger import search as search_mod
from ledger.cli import main
from ledger.store import open_ledger
from ledger.verbs import EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE

TEXT = "harbor lantern"
# 2026-05-01T00:00:00Z, the standard's mtime.
STANDARD_MTIME_NS = 1_777_593_600 * 1_000_000_000
STANDARD_ID = "standards/harbor.md#lantern"
DECISION_ID = "T-open:4"


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


def _append(lg, kind: str, item_id: str | None, payload: dict, key: str, at: str):
    return lg.append(kind=kind, item_id=item_id, payload=payload,
                     idempotency_key=key, harness="fixture", recorded_at=at)


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    root = tmp_path / "harness"
    standard = root / "standards" / "harbor.md"
    standard.parent.mkdir(parents=True)
    standard.write_text("# Guidance\n\n## Lantern\nharbor lantern guidance\n", encoding="utf-8")
    os.utime(standard, ns=(STANDARD_MTIME_NS, STANDARD_MTIME_NS))
    path = tmp_path / "state" / "home.db"
    with open_ledger(path) as lg:
        _append(lg, "item.created", "INI-1",
                {"type": "initiative", "title": "Harbor lantern program"},
                "k1", "2026-01-01T09:00:00.000Z")
        _append(lg, "item.created", "T-open",
                {"type": "ticket", "title": "Harbor lantern alpha"},
                "k2", "2026-01-10T09:00:00.000Z")
        _append(lg, "item.linked", "T-open", {"rel": "item", "target": "INI-1"},
                "k3", "2026-01-11T09:00:00.000Z")
        # Event 4: the decision is the item's newest event, so it dates T-open.
        _append(lg, "decision.asked", "T-open",
                {"question": "Which harbor lantern color?", "options": [{"label": "red"}]},
                "k4", "2026-02-15T12:00:00.000Z")
        _append(lg, "item.created", "T-closed",
                {"type": "ticket", "title": "Harbor lantern beta", "status": "closed"},
                "k5", "2026-03-10T09:00:00.000Z")
        # A blocks link names the initiative too, but only rel=item makes an
        # item part of it.
        _append(lg, "item.linked", "T-closed", {"rel": "blocks", "target": "INI-1"},
                "k6", "2026-03-10T10:00:00.000Z")
    (path.parent / "config.json").write_text(
        json.dumps({"search_roots": [str(root)]}), encoding="utf-8")
    return path


def ids(monkeypatch, db: Path, *extra: str) -> set[str]:
    code, body = run_json(monkeypatch, db, "search", TEXT, "--limit", "50", *extra)
    if code == EXIT_NOT_FOUND:
        return set()
    assert code == EXIT_OK, body
    return {card["id"] for card in body["data"]["cards"]}


def test_without_filters_every_fixture_matches_the_text(db, monkeypatch):
    assert ids(monkeypatch, db) == {"INI-1", "T-open", DECISION_ID, "T-closed", STANDARD_ID}


def test_status_filter_excludes_other_statuses_and_itemless_docs(db, monkeypatch):
    assert ids(monkeypatch, db, "--status", "open") == {"INI-1", "T-open", DECISION_ID}
    assert ids(monkeypatch, db, "--status", "closed") == {"T-closed"}
    code, body = run_json(monkeypatch, db, "search", TEXT, "--status", "done")
    assert code == EXIT_USAGE, body


def test_item_filter_keeps_only_that_items_documents(db, monkeypatch):
    assert ids(monkeypatch, db, "--item", "T-open") == {"T-open", DECISION_ID}
    assert ids(monkeypatch, db, "--item", "T-closed") == {"T-closed"}


def test_initiative_filter_keeps_items_linked_with_rel_item(db, monkeypatch):
    # INI-1 itself and T-closed (a blocks link) match the text, not the filter.
    assert ids(monkeypatch, db, "--initiative", "INI-1") == {"T-open", DECISION_ID}
    assert ids(monkeypatch, db, "--initiative", "INI-other") == set()


def test_since_filter_compares_event_time_item_newest_event_and_mtime(db, monkeypatch):
    # T-open's newest event (the decision) is 2026-02-15, before the bound.
    assert ids(monkeypatch, db, "--since", "2026-03-01") == {"T-closed", STANDARD_ID}
    # Inclusive, and an offset is honoured: 14:00+02:00 is 12:00Z exactly.
    assert ids(monkeypatch, db, "--since", "2026-02-15T14:00:00+02:00") == {
        "T-open", DECISION_ID, "T-closed", STANDARD_ID}


def test_until_filter_keeps_the_whole_of_a_bare_date(db, monkeypatch):
    # The decision is at 12:00Z on 2026-02-15; a bare --until keeps that day.
    assert ids(monkeypatch, db, "--until", "2026-02-15") == {"INI-1", "T-open", DECISION_ID}
    assert ids(monkeypatch, db, "--until", "2026-02-15T11:59:59Z") == {"INI-1"}


def test_filters_combine_with_each_other_and_the_text(db, monkeypatch):
    assert ids(monkeypatch, db, "--status", "open", "--since", "2026-02-01") == {
        "T-open", DECISION_ID}
    assert ids(monkeypatch, db, "--type", "decision", "--initiative", "INI-1") == {DECISION_ID}
    code, body = run_json(monkeypatch, db, "search", "color", "--item", "T-closed")
    assert code == EXIT_NOT_FOUND, body


@pytest.mark.parametrize("flag,value", [
    ("--since", "last tuesday"),
    ("--until", "2026-13-01"),
    ("--since", ""),
    ("--until", "2026-02-30"),
    ("--until", "20260215"),
    ("--since", "2026-W07"),
    ("--since", "2026-W07-1"),
    ("--since", "2026-046"),
    ("--since", "20260215T1000"),
    ("--since", "0001-01-01T00:00:00+14:00"),
    ("--until", "9999-12-31T23:00:00-05:00"),
])
def test_an_unparseable_date_is_a_usage_error(db, monkeypatch, flag, value):
    code, body = run_json(monkeypatch, db, "search", TEXT, flag, value)
    assert code == EXIT_USAGE, body
    assert body["error"]["code"] == "usage"
    assert flag in body["error"]["message"]


def test_since_after_until_is_a_usage_error(db, monkeypatch):
    code, body = run_json(monkeypatch, db, "search", TEXT,
                          "--since", "2026-03-01", "--until", "2026-02-01")
    assert code == EXIT_USAGE, body
    assert body["error"]["message"] == "--since is after --until"


def test_cards_carry_the_structured_fields(db, monkeypatch):
    code, body = run_json(monkeypatch, db, "search", TEXT, "--limit", "50",
                          "--until", "2026-12-31")
    assert code == EXIT_OK, body
    data = body["data"]
    cards = {card["id"]: card for card in data["cards"]}
    decision = cards[DECISION_ID]
    assert (decision["item_id"], decision["status"], decision["date"]) == (
        "T-open", "open", "2026-02-15T12:00:00.000Z")
    item = cards["T-open"]
    assert (item["item_id"], item["status"], item["date"]) == (
        "T-open", "open", "2026-02-15T12:00:00.000Z")
    standard = cards[STANDARD_ID]
    assert standard["date"] == "2026-05-01T00:00:00.000000Z"
    assert "item_id" not in standard and "status" not in standard
    assert data["filters"] == {"until": "2026-12-31T23:59:59.999999Z"}
    assert data["ranker"] == "bm25"


def test_parse_when_reads_dates_as_utc():
    assert search_mod.parse_when("2026-02-15") == datetime(2026, 2, 15, tzinfo=UTC)
    assert search_mod.parse_when("2026-02-15", end_of_day=True) == datetime(
        2026, 2, 15, 23, 59, 59, 999999, tzinfo=UTC)
    assert search_mod.parse_when("2026-02-15T10:00") == datetime(2026, 2, 15, 10, tzinfo=UTC)
    with pytest.raises(ValueError):
        search_mod.parse_when("yesterday")


# --- the manifest names the ranker ----------------------------------------------


def _manifest(db: Path) -> dict:
    return search_mod.stored_manifest(db.parent / "search.db")


def _rewrite_manifest(db: Path, manifest: dict) -> None:
    conn = sqlite3.connect(db.parent / "search.db")
    try:
        conn.execute("UPDATE manifest SET value = ? WHERE key = 'manifest'",
                     (json.dumps(manifest, sort_keys=True),))
        conn.commit()
    finally:
        conn.close()


def test_the_manifest_names_the_ranker(db, monkeypatch):
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert _manifest(db)["ranker"] == "bm25"
    assert search_mod.index_status(db)["status"] == "current"


@pytest.mark.parametrize("ranker", [None, "embeddings"])
def test_an_index_without_this_ranker_is_stale_and_rebuilt(db, monkeypatch, ranker):
    run_json(monkeypatch, db, "index")
    manifest = _manifest(db)
    if ranker is None:
        del manifest["ranker"]
    else:
        manifest["ranker"] = ranker
    _rewrite_manifest(db, manifest)
    status = search_mod.index_status(db)
    assert (status["status"], status["reasons"]) == ("stale", ["ranker changed"])
    code, body = run_json(monkeypatch, db, "search", TEXT, "--status", "open")
    assert code == EXIT_OK, body
    assert body["data"]["refreshed"] is True
    assert _manifest(db)["ranker"] == "bm25"


# --- a stale index is rebuilt before a filtered search -------------------------------


def test_a_stale_index_is_rebuilt_before_a_filtered_search(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    assert search_mod.index_status(db)["status"] == "current"
    with open_ledger(db) as lg:
        # T-closed reopens and joins the initiative after the index was built.
        _append(lg, "item.state", "T-closed", {"status": "open"},
                "k7", "2026-04-01T09:00:00.000Z")
        _append(lg, "item.linked", "T-closed", {"rel": "item", "target": "INI-1"},
                "k8", "2026-04-01T10:00:00.000Z")
    assert search_mod.index_status(db)["status"] == "stale"
    code, body = run_json(monkeypatch, db, "search", TEXT, "--limit", "50",
                          "--status", "open", "--initiative", "INI-1")
    assert code == EXIT_OK, body
    assert body["data"]["refreshed"] is True
    assert {card["id"] for card in body["data"]["cards"]} == {"T-open", DECISION_ID, "T-closed"}
    closed = next(card for card in body["data"]["cards"] if card["id"] == "T-closed")
    assert (closed["status"], closed["date"]) == ("open", "2026-04-01T10:00:00.000Z")
