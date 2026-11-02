"""The operator is a stamped field (SL38).

The CLI composes ``actor`` as ``<operator>/<agent>`` from LEDGER_OPERATOR and
LEDGER_AGENT and the views split it back into ``operator``. ``claim`` keeps
the full actor as owner and claim, so the lease holder stays per agent.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from ledger.store import Ledger, connect, load_migrations, migrate, open_ledger
from ledger.verbs import EXIT_REFUSED

from test_cli import invoke


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "ledger.db")


def _run(monkeypatch, db: str, operator: str | None, agent: str | None, *argv):
    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", db, *argv],
        env={"LEDGER_OPERATOR": operator, "LEDGER_AGENT": agent},
    )
    return code, (json.loads(out) if out.strip() else None), err


def _rows(db: str, sql: str, *params):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def _new(monkeypatch, db, operator, agent, item="it-1"):
    code, body, err = _run(
        monkeypatch, db, operator, agent,
        "new", "--id", item, "--type", "ticket", "--title", "synthetic",
    )
    assert code == 0, err
    return body["data"]["event"]


def test_both_variables_stamp_the_composed_actor_and_the_views_split_it(
    tmp_path, monkeypatch
) -> None:
    db = _db(tmp_path)
    event = _new(monkeypatch, db, "alice", "seat-1")
    assert event["actor"] == "alice/seat-1"
    [row] = _rows(db, "SELECT actor, operator FROM v_events WHERE id = ?", event["id"])
    assert row == {"actor": "alice/seat-1", "operator": "alice"}


def test_no_operator_stamps_the_bare_agent_and_the_views_give_an_empty_operator(
    tmp_path, monkeypatch
) -> None:
    db = _db(tmp_path)
    event = _new(monkeypatch, db, None, "seat-1")
    assert event["actor"] == "seat-1"
    [row] = _rows(db, "SELECT actor, operator FROM v_events WHERE id = ?", event["id"])
    assert row == {"actor": "seat-1", "operator": ""}


def test_operator_alone_stamps_the_operator(tmp_path, monkeypatch) -> None:
    # D1: operator alone is stamped bare; D2 reads a bare actor as no operator.
    db = _db(tmp_path)
    event = _new(monkeypatch, db, "alice", None)
    assert event["actor"] == "alice"
    [row] = _rows(db, "SELECT operator FROM v_events WHERE id = ?", event["id"])
    assert row == {"operator": ""}


def test_neither_variable_stamps_no_actor(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    event = _new(monkeypatch, db, None, None)
    assert event["actor"] is None
    [row] = _rows(db, "SELECT operator FROM v_events WHERE id = ?", event["id"])
    assert row == {"operator": ""}


def test_claim_records_the_composed_actor_and_another_agent_of_the_operator_is_refused(
    tmp_path, monkeypatch
) -> None:
    # The lease holder stays per agent: a second agent of the same operator
    # cannot take the first agent's live lease.
    db = _db(tmp_path)
    _new(monkeypatch, db, "alice", "seat-1")
    code, body, err = _run(monkeypatch, db, "alice", "seat-1", "claim", "it-1")
    assert code == 0, err
    event = body["data"]["event"]
    assert event["actor"] == "alice/seat-1"
    assert (event["payload"]["owner"], event["payload"]["claim"]) == (
        "alice/seat-1",
        "alice/seat-1",
    )
    [lease] = _rows(db, "SELECT holder, operator FROM v_leases WHERE item_id = 'it-1'")
    assert lease == {"holder": "alice/seat-1", "operator": "alice"}

    code, body, _err = _run(monkeypatch, db, "alice", "seat-2", "claim", "it-1")
    assert code == EXIT_REFUSED
    assert "already claimed by alice/seat-1" in body["error"]["message"]
    [item] = _rows(db, "SELECT owner, claim FROM v_items WHERE item_id = 'it-1'")
    assert item == {"owner": "alice/seat-1", "claim": "alice/seat-1"}


def test_claim_without_operator_keeps_the_agent_as_owner_and_claim(
    tmp_path, monkeypatch
) -> None:
    db = _db(tmp_path)
    _new(monkeypatch, db, None, "seat-1")
    code, body, err = _run(monkeypatch, db, None, "seat-1", "claim", "it-1")
    assert code == 0, err
    payload = body["data"]["event"]["payload"]
    assert (payload["owner"], payload["claim"]) == ("seat-1", "seat-1")
    [lease] = _rows(db, "SELECT holder, operator FROM v_leases WHERE item_id = 'it-1'")
    assert lease == {"holder": "seat-1", "operator": ""}


def test_explicit_owner_and_claim_still_win(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    _new(monkeypatch, db, "alice", "seat-1")
    code, body, err = _run(
        monkeypatch, db, "alice", "seat-1",
        "claim", "it-1", "--owner", "bob", "--claim", "seat-9",
    )
    assert code == 0, err
    payload = body["data"]["event"]["payload"]
    assert (payload["owner"], payload["claim"]) == ("bob", "seat-9")


def test_a_slash_in_either_part_is_refused_and_writes_nothing(
    tmp_path, monkeypatch
) -> None:
    db = _db(tmp_path)
    _new(monkeypatch, db, "alice", "seat-1")
    for operator, agent, name in (
        ("ali/ce", "seat-1", "LEDGER_OPERATOR"),
        ("alice", "seat/1", "LEDGER_AGENT"),
        (None, "seat/1", "LEDGER_AGENT"),
    ):
        code, body, _err = _run(
            monkeypatch, db, operator, agent,
            "note", "it-1", "--text", "should not land",
        )
        assert code == EXIT_REFUSED
        assert body["error"]["code"] == "invalid_actor"
        assert name in body["error"]["message"]
    assert [r["kind"] for r in _rows(db, "SELECT kind FROM events")] == ["item.created"]


def test_older_events_read_as_an_empty_operator_after_the_migration(tmp_path) -> None:
    path = tmp_path / "ledger.db"
    conn = connect(path)
    migrate(conn, load_migrations()[:6])
    legacy = Ledger(conn, path)
    created = legacy.append(
        kind="item.created",
        payload={"type": "ticket", "title": "older item"},
        item_id="old-1",
        idempotency_key="old-created",
        actor="seat-1",
        harness="pytest",
    )
    before = [(event.id, event.hash) for event in legacy.events()]
    legacy.close()

    upgraded = open_ledger(path)
    try:
        assert [(event.id, event.hash) for event in upgraded.events()] == before
        upgraded.verify()
    finally:
        upgraded.close()
    [row] = _rows(str(path), "SELECT actor, operator FROM v_events WHERE id = ?", created.id)
    assert row == {"actor": "seat-1", "operator": ""}
