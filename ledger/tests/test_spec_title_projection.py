"""Ticket title projections use the current spec title.

CARD-SPEC-TITLE1: ``card``, ``query``, ``ready`` and children rows print the
newest ``item.spec`` title when that field is a non-empty string, else the
``item.created`` title. A missing or non-string spec title is never shown.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ledger.store import open_ledger
from ledger.verbs.spec import set_spec

from test_cli import invoke, seed

ITEM = "it-title"
MINTER = "first minter title"
WINNING = "winning spec title"
SECOND = "second spec title"
BODY = "Build the thing.\n"


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "ledger.db")


def _spec(monkeypatch, db: str, item_id: str, title: str, body: str = BODY) -> None:
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "spec", "set", item_id, "--title", title, "--body", body],
    )
    assert code == 0, err


def _card(monkeypatch, db: str, item_id: str = ITEM) -> tuple[int, str, str]:
    return invoke(monkeypatch, ["--db", db, "card", item_id])


def _card_json(monkeypatch, db: str, item_id: str = ITEM) -> dict:
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "card", item_id])
    assert code == 0, err
    return json.loads(out)["data"]


def _title_line(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("title: "):
            return line
    raise AssertionError(f"no title line in:\n{text}")


def _rewrite_latest_spec_title(db: str, title: object | None) -> None:
    """Plant a spec payload the writer would refuse, then restore the guard."""
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP TRIGGER events_no_update")
        row = conn.execute(
            "SELECT id, payload FROM events WHERE kind = 'item.spec' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        payload = json.loads(row[1])
        if title is None:
            payload.pop("title", None)
        else:
            payload["title"] = title
        conn.execute(
            "UPDATE events SET payload = ? WHERE id = ?",
            (json.dumps(payload, separators=(",", ":")), row[0]),
        )
        conn.execute(
            "CREATE TRIGGER events_no_update BEFORE UPDATE ON events "
            "BEGIN SELECT RAISE(ABORT, 'events are append-only: UPDATE refused'); END;"
        )
        conn.commit()
    finally:
        conn.close()


def test_card_prints_the_current_spec_title(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, ITEM, MINTER)
    _spec(monkeypatch, db, ITEM, WINNING)
    code, out, err = _card(monkeypatch, db)
    assert code == 0, err
    assert _title_line(out) == f"title: {WINNING}"
    assert "spec v1" in out.splitlines()
    data = _card_json(monkeypatch, db)
    assert data["title"] == WINNING
    with open_ledger(db, create=False) as lg:
        assert lg.item(ITEM)["title"] == MINTER


def test_card_without_a_spec_prints_the_created_title(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, ITEM, MINTER)
    code, out, err = _card(monkeypatch, db)
    assert code == 0, err
    assert _title_line(out) == f"title: {MINTER}"
    assert _card_json(monkeypatch, db)["title"] == MINTER


def test_card_title_follows_the_newest_spec_version(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, ITEM, MINTER)
    _spec(monkeypatch, db, ITEM, WINNING)
    assert _title_line(_card(monkeypatch, db)[1]) == f"title: {WINNING}"
    _spec(monkeypatch, db, ITEM, SECOND, body="Build the thing.\nThen test it.\n")
    code, out, err = _card(monkeypatch, db)
    assert code == 0, err
    assert _title_line(out) == f"title: {SECOND}"
    assert "spec v2" in out.splitlines()
    assert _card_json(monkeypatch, db)["title"] == SECOND


def test_query_and_ready_print_the_current_spec_title(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, ITEM, MINTER)
    _spec(monkeypatch, db, ITEM, WINNING)
    for verb in ("query", "ready"):
        code, out, err = invoke(monkeypatch, ["--db", db, verb])
        assert code == 0, err
        [row] = [line for line in out.splitlines() if line.startswith(ITEM + "\t")]
        assert row.split("\t")[3] == WINNING
        code, raw, err = invoke(monkeypatch, ["--json", "--db", db, verb])
        assert code == 0, err
        [found] = [
            entry
            for entry in json.loads(raw)["data"]["items"]
            if entry["item_id"] == ITEM
        ]
        assert found["title"] == WINNING


def test_child_row_prints_the_current_spec_title(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-anchor", "anchor")
    seed(monkeypatch, db, "it-kid", MINTER)
    _spec(monkeypatch, db, "it-kid", WINNING)
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "link", "it-kid", "--rel", "child-of", "--target", "it-anchor"],
    )
    assert code == 0, err
    code, out, err = _card(monkeypatch, db, "it-anchor")
    assert code == 0, err
    assert f"- it-kid: {WINNING} [open] holder: -" in out.splitlines()
    data = _card_json(monkeypatch, db, "it-anchor")
    assert data["children"][0]["title"] == WINNING


def test_a_missing_spec_title_falls_back_to_created(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, ITEM, MINTER)
    _spec(monkeypatch, db, ITEM, WINNING)
    _rewrite_latest_spec_title(db, None)
    code, out, err = _card(monkeypatch, db)
    assert code == 0, err
    assert _title_line(out) == f"title: {MINTER}"
    assert _card_json(monkeypatch, db)["title"] == MINTER


def test_an_empty_spec_title_falls_back_to_created(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, ITEM, MINTER)
    _spec(monkeypatch, db, ITEM, WINNING)
    _rewrite_latest_spec_title(db, "")
    code, out, err = _card(monkeypatch, db)
    assert code == 0, err
    assert _title_line(out) == f"title: {MINTER}"


def test_a_non_string_spec_title_falls_back_to_created(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, ITEM, MINTER)
    _spec(monkeypatch, db, ITEM, WINNING)
    _rewrite_latest_spec_title(db, 1)
    code, out, err = _card(monkeypatch, db)
    assert code == 0, err
    assert _title_line(out) == f"title: {MINTER}"


def test_a_spec_title_is_escaped_like_other_inline_fields(
    tmp_path: Path, monkeypatch
) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, ITEM, MINTER)
    _spec(monkeypatch, db, ITEM, WINNING)
    planted = "T\u001b[31m\nX"
    _rewrite_latest_spec_title(db, planted)
    code, out, err = _card(monkeypatch, db)
    assert code == 0, err
    assert "\u001b" not in out
    assert _title_line(out) == r"title: T\u001b[31m\nX"
    assert _card_json(monkeypatch, db)["title"] == planted


def test_a_note_does_not_supply_the_title(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, ITEM, MINTER)
    code, _out, err = invoke(
        monkeypatch, ["--db", db, "note", ITEM, "--text", "not a title"]
    )
    assert code == 0, err
    with open_ledger(db) as lg:
        set_spec(lg, ITEM, {"title": WINNING, "body_md": BODY}, harness="pytest")
    code, out, err = _card(monkeypatch, db)
    assert code == 0, err
    assert _title_line(out) == f"title: {WINNING}"
