"""``ledger search`` indexes rulings as their own documents.

An ``item.note`` that starts with ``decided:`` is also a ``decision``
document of its own, so a ruling written on a busy item is found by its own
words; ``decision.recorded`` events, each item's current spec and each stop
point are indexed; a dotted query word is matched as a phrase. Every ledger
here is synthetic and built under tmp_path; nothing reads a live database.
"""
from __future__ import annotations

import io
import json
import re
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ledger import search as search_mod
from ledger.cli import main
from ledger.store import Event, open_ledger
from ledger.verbs import EXIT_NOT_FOUND, EXIT_OK
from ledger.verbs.spec import set_spec

PARENT = "lp-cp0-ablation-readout"
CHILD = "lp-harness-map"
# The ruling names the child by id, as an owner ruling written on a busy
# parent does.
RULING = (
    "decided: the harness map is the one picture of the harness, owned by "
    f"{CHILD}; its tooltips are attested by machine, never by a reader"
)
NOTES_ON_PARENT = 40
RULING_AT = 20
DISTRACTORS = 7


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
def busy(tmp_path: Path, monkeypatch) -> dict:
    """A parent with 40 notes, one of them the ruling, beside items whose
    titles name one word of the child's topic each."""
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    db = tmp_path / "state" / "home.db"
    with open_ledger(db) as lg:
        _append(lg, "item.created", PARENT,
                {"type": "initiative", "title": "CP0 ablation readout"}, "parent")
        _append(lg, "item.created", CHILD,
                {"type": "ticket", "title": "Harness map tooltips"}, "child")
        for n in range(DISTRACTORS):
            _append(lg, "item.created", f"hw-{n}",
                    {"type": "ticket", "title": f"Harness wrapper cleanup {n}"}, f"hw-{n}")
            _append(lg, "item.created", f"mr-{n}",
                    {"type": "ticket", "title": f"Map renderer fix {n}"}, f"mr-{n}")
        ruling_id = None
        for n in range(NOTES_ON_PARENT):
            if n == RULING_AT:
                ruling_id = _append(lg, "item.note", PARENT, {"text": RULING}, "ruling").id
            else:
                # Bookkeeping: most notes never name the topic, a few name
                # one of its words.
                tail = " with the harness checks green" if n % 10 == 3 else ""
                _append(lg, "item.note", PARENT, {
                    "text": f"round {n} receipt recorded; lane {n} closed on the readout{tail}",
                }, f"note-{n}")
    (db.parent / "config.json").write_text(json.dumps({"search_roots": []}), encoding="utf-8")
    return {"db": db, "ruling": f"{PARENT}:{ruling_id}"}


def _cards(monkeypatch, db: Path, *argv: str) -> list[dict]:
    code, body = run_json(monkeypatch, db, "search", *argv)
    if code == EXIT_NOT_FOUND:
        return []
    assert code == EXIT_OK, body
    return body["data"]["cards"]


# --- 1. a decided: note is a decision document of its own --------------------


def test_a_ruling_on_a_forty_note_parent_is_in_the_top_ten(busy, monkeypatch):
    cards = _cards(monkeypatch, busy["db"], "harness", "map")
    ids = [card["id"] for card in cards]
    assert len(cards) == 10
    # The fixture holds the problem: the parent's own document, where the
    # ruling is one note of forty, ranks past the default ten cards.
    assert PARENT not in ids
    assert busy["ruling"] in ids, ids
    card = cards[ids.index(busy["ruling"])]
    assert card["doc_type"] == "decision"
    assert card["item_id"] == PARENT
    assert card["title"] == RULING[:search_mod.DECISION_TITLE_CHARS]


def test_type_decision_isolates_the_ruling(busy, monkeypatch):
    cards = _cards(monkeypatch, busy["db"], "harness", "map", "--type", "decision")
    assert [card["id"] for card in cards] == [busy["ruling"]]


def test_the_ruling_stays_in_its_item_document(busy, monkeypatch):
    cards = _cards(monkeypatch, busy["db"], "attested", "--item", PARENT)
    assert {card["id"] for card in cards} == {PARENT, busy["ruling"]}
    cards = _cards(monkeypatch, busy["db"], "attested", "--type", "item")
    assert [card["id"] for card in cards] == [PARENT]


def _event(event_id: int, kind: str, item_id: str | None, payload: dict) -> Event:
    return Event(
        id=event_id, kind=kind, payload_version=1, payload=payload, item_id=item_id,
        idempotency_key=f"k{event_id}", actor=None, harness="fixture", model=None,
        session=None, channel=None, provenance_class=None,
        recorded_at="2026-11-20T09:00:00.000Z", prev_hash="", hash=f"h{event_id}",
    )


@pytest.mark.parametrize("text", [
    "decided: keep the one store",
    "   decided: keep the one store",
    "\n\tdecided: keep the one store",
    "decided:keep the one store",
])
def test_a_note_starting_with_decided_is_a_decision(text):
    event = _event(2, "item.note", "T-1", {"text": text})
    assert search_mod.decided_note_text(event) == text
    item = {"item_id": "T-1", "title": "One", "type": "ticket", "status": "open"}
    docs = search_mod.ledger_docs([item], [event])
    decisions = [doc for doc in docs if doc.doc_type == "decision"]
    assert [(d.doc_id, d.item_id, d.content) for d in decisions] == [("T-1:2", "T-1", text)]
    assert decisions[0].title == text.lstrip()[:search_mod.DECISION_TITLE_CHARS]


@pytest.mark.parametrize("text", [
    "Decided: keep the one store",
    "DECIDED: keep the one store",
    "decided : keep the one store",
    "decided keep the one store",
    "- decided: keep the one store",
    "`decided:` keep the one store",
    "we decided: keep the one store",
    "undecided: which store",
])
def test_a_near_miss_of_the_prefix_is_not_a_decision(text):
    event = _event(2, "item.note", "T-1", {"text": text})
    assert search_mod.decided_note_text(event) is None
    item = {"item_id": "T-1", "title": "One", "type": "ticket", "status": "open"}
    docs = search_mod.ledger_docs([item], [event])
    assert [doc.doc_type for doc in docs] == ["item"]
    # The note is still in the item's document, as before.
    assert text in docs[0].content


def test_a_payload_without_its_text_field_is_skipped_for_the_new_types():
    item = {"item_id": "T-1", "title": "One", "type": "ticket", "status": "open"}
    events = [
        _event(2, "item.note", "T-1", {}),
        _event(3, "item.note", "T-1", {"text": 7}),
        _event(4, "decision.recorded", "T-1", {"verbatim": "decided: yes"}),
        _event(5, "item.stopped", "T-1", {"phase": "build"}),
        _event(6, "item.spec", "T-1", {"title": "No body"}),
    ]
    docs = search_mod.ledger_docs([item], events)
    assert [doc.doc_type for doc in docs] == ["item"]


# --- 2. decision.recorded, the current spec and stop points ------------------


@pytest.fixture
def records(busy) -> dict:
    db = busy["db"]
    with open_ledger(db) as lg:
        recorded = _append(lg, "decision.recorded", CHILD, {
            "who": "the owner", "who_kind": "person", "standing": "lean",
            "effect": "Tooltips cite the quorum lantern source by line",
            "tags": [{"type": "initiative", "id": "INIT-QUORUM"}],
            "verbatim": "cite it by line", "phase": "plan",
        }, "recorded").id
        set_spec(lg, CHILD, {"title": "Tooltip spec", "body_md": "the obsidian draft"},
                 harness="fixture")
        spec, _created = set_spec(
            lg, CHILD,
            {"title": "Tooltip spec", "body_md": "the basalt draft", "acceptance": "a cairn"},
            harness="fixture")
        stop = _append(lg, "item.stopped", CHILD,
                       {"point": "paused at the gazetteer merge", "phase": "build"}, "stop").id
    return {**busy, "recorded": f"{CHILD}:{recorded}", "spec": f"{CHILD}:{spec.id}",
            "stop": f"{CHILD}:{stop}"}


def test_a_recorded_decision_is_a_decision_document_of_its_item(records, monkeypatch):
    cards = _cards(monkeypatch, records["db"], "quorum", "lantern")
    assert [(c["id"], c["doc_type"], c["item_id"]) for c in cards] == [
        (records["recorded"], "decision", CHILD)]
    assert cards[0]["title"] == "Tooltips cite the quorum lantern source by line"


def test_the_current_spec_is_a_spec_document_of_its_item(records, monkeypatch):
    cards = _cards(monkeypatch, records["db"], "basalt", "cairn", "--type", "spec")
    assert [(c["id"], c["doc_type"], c["item_id"], c["title"]) for c in cards] == [
        (records["spec"], "spec", CHILD, "Tooltip spec")]
    # Only the current version is indexed: the first body is not found.
    assert _cards(monkeypatch, records["db"], "obsidian") == []


def test_a_stop_point_is_a_stop_document_of_its_item(records, monkeypatch):
    cards = _cards(monkeypatch, records["db"], "gazetteer")
    assert [(c["id"], c["doc_type"], c["item_id"], c["title"]) for c in cards] == [
        (records["stop"], "stop", CHILD, "paused at the gazetteer merge")]


def test_the_new_documents_name_their_item_for_the_item_filter(records, monkeypatch):
    found = {c["id"] for c in _cards(
        monkeypatch, records["db"], "quorum", "basalt", "gazetteer", "--item", CHILD)}
    assert found == {records["recorded"], records["spec"], records["stop"]}


def _ledger_rows(db: Path) -> list[tuple]:
    conn = sqlite3.connect((db.parent / "search.db").as_uri() + "?mode=ro", uri=True)
    try:
        return sorted(
            tuple("" if v is None else str(v) for v in row)
            for row in conn.execute(
                "SELECT f.doc_type, f.doc_id, f.title, f.content, m.item_id, m.status, "
                "m.date, m.ts FROM fts_corpus f LEFT JOIN doc_meta m ON m.rowid = f.rowid"
            )
        )
    finally:
        conn.close()


def test_an_append_refresh_holds_what_a_rebuild_holds(records, monkeypatch):
    db = records["db"]
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    with open_ledger(db) as lg:
        _append(lg, "item.note", CHILD, {"text": "decided: the walrus layout"}, "late-ruling")
        set_spec(lg, CHILD, {"title": "Tooltip spec", "body_md": "the granite draft"},
                 harness="fixture")
        _append(lg, "item.stopped", CHILD,
                {"point": "paused at the second merge", "phase": "build"}, "stop-2")
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert body["data"]["refresh"] == "append"
    appended = _ledger_rows(db)
    assert sum(1 for row in appended if row[0] == "spec") == 1
    assert sum(1 for row in appended if row[0] == "stop") == 2
    code, body = run_json(monkeypatch, db, "index", "--rebuild")
    assert body["data"]["refresh"] == "rebuild"
    assert appended == _ledger_rows(db)


def test_nothing_is_written_to_the_event_log(records, monkeypatch):
    db = records["db"]
    with open_ledger(db) as lg:
        before = [event.hash for event in lg.events()]
    _cards(monkeypatch, db, "harness", "map")
    run_json(monkeypatch, db, "index", "--rebuild")
    with open_ledger(db) as lg:
        assert [event.hash for event in lg.events()] == before


# --- 3. a dotted query word is a phrase ---------------------------------------

# The query grammar before dotted words: what every query without a dot
# must still parse to.
_DOTLESS_TERM = re.compile(r"[\w][\w'-]*", re.UNICODE)


def test_a_dotted_word_is_one_quoted_phrase():
    assert search_mod.match_expression("release.notes") == '"release.notes"'
    assert search_mod.match_expression("see release.notes docs") == (
        '"see" OR "release.notes" OR "docs"')
    # A dot that is not between two word characters splits as before.
    assert search_mod.match_expression("harness map.") == '"harness" OR "map"'
    assert search_mod.match_expression(".harness. home") == '"harness" OR "home"'
    assert search_mod.match_expression("release..notes") == '"release" OR "notes"'
    assert search_mod.match_expression("v0.5.0") == '"v0.5.0"'


@pytest.mark.parametrize("query", [
    "harness map", "feed-condenser", "owner's ruling", "a-b-c d", "x' y-", "naïve café",
    "what is the harness map", "CP0 ablation readout, round 19!",
])
def test_a_query_without_a_dot_parses_as_before(query):
    assert search_mod._TERM.findall(query) == _DOTLESS_TERM.findall(query)


def test_a_dotted_query_matches_the_words_in_order(busy, monkeypatch):
    db = busy["db"]
    with open_ledger(db) as lg:
        _append(lg, "item.created", "T-dot",
                {"type": "ticket", "title": "Wombat config"}, "dot")
        _append(lg, "item.note", "T-dot", {"text": "edit the wombat.yak file"}, "dot-note")
        _append(lg, "item.created", "T-apart",
                {"type": "ticket", "title": "Yak shed"}, "apart")
        _append(lg, "item.note", "T-apart", {"text": "a yak met a wombat"}, "apart-note")
    cards = _cards(monkeypatch, db, "wombat.yak")
    assert [card["id"] for card in cards] == ["T-dot"]


# --- the format bump ----------------------------------------------------------


def test_an_index_of_the_previous_format_is_rebuilt_not_read(records, monkeypatch):
    db = records["db"]
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    # Make the index what the previous format built: no decided: note, recorded
    # decision, spec or stop documents, and format 3 in the manifest.
    index = db.parent / "search.db"
    conn = sqlite3.connect(index)
    try:
        gone = [row[0] for row in conn.execute(
            "SELECT rowid FROM fts_corpus WHERE doc_type IN ('decision', 'spec', 'stop')")]
        for rowid in gone:
            for table in ("fts_corpus", "doc_path", "doc_meta"):
                conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (rowid,))
        manifest = json.loads(conn.execute(
            "SELECT value FROM manifest WHERE key = 'manifest'").fetchone()[0])
        manifest["format"] = 3
        conn.execute("UPDATE manifest SET value = ? WHERE key = 'manifest'",
                     (json.dumps(manifest, sort_keys=True),))
        conn.commit()
    finally:
        conn.close()
    assert gone
    status = search_mod.index_status(db)
    assert status["status"] == "stale"
    assert "format changed" in status["reasons"]
    code, body = run_json(monkeypatch, db, "search", "harness", "map", "--type", "decision")
    assert code == EXIT_OK, body
    assert body["data"]["refreshed"] is True
    assert [card["id"] for card in body["data"]["cards"]] == [records["ruling"]]
