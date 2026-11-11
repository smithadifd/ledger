"""Payload validation, idempotency, views, hash chain, and append-only triggers."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from ledger import (
    ChainBroken,
    IdempotencyConflict,
    ValidationError,
    known_kinds,
    open_ledger,
)
from ledger.store import Ledger, LedgerError, connect, event_digest, load_migrations, migrate

VALID_PAYLOADS: dict[str, dict] = {
    "item.created": {"type": "ticket", "title": "synthetic-one"},
    "item.state": {"status": "open"},
    "item.linked": {"rel": "pr", "target": "org/repo#1"},
    "item.note": {"text": "synthetic note"},
    "decision.asked": {
        "question": "pick a colour?",
        "options": [{"label": "red"}, {"label": "blue", "description": "cool"}],
        "recommendation": "blue",
        "default": "red",
    },
    "decision.elected": {
        "question": "pick a colour?",
        "chosen": "blue",
        "chosen_description": "cool",
        "rejected": ["red"],
        "scope": "palette",
        "quote_ref": 1,
    },
    "decision.held": {"asked_event_id": 1, "status": "held"},
    "utterance.captured": {"text": "please pick blue", "prompt_id": "p1"},
    "session.opened": {"purpose": "synthetic"},
    "session.closed": {"outcome": "done"},
    "decision.recorded": {
        "who": "synthetic owner",
        "who_kind": "person",
        "standing": "lean",
        "effect": "resume from the plan",
        "tags": [{"type": "ticket", "id": "item-1"}],
        "verbatim": "lean towards the plan",
        "phase": "plan",
    },
    "outside.recorded": {"what": "merge", "ref": "org/repo#1"},
    "item.progress": {"text": "halfway"},
    "item.tried": {"what": "a smaller patch", "outcome": "too narrow"},
    "review.recorded": {"round": 1, "verdict": "changes", "findings": ["missing test"]},
    "item.stopped": {"point": "waiting on review", "phase": "review", "skills": ["review"]},
    "guidance.set": {
        "pool": "synthetic-pool",
        "direction": "burn",
        "reason": "synthetic reason",
        "starts": "2026-01-01",
        "expires": "2026-01-31",
    },
}

# Kinds whose payload names an earlier decision; test_decisions covers them.
NEEDS_DECISION = {"decision.standing", "decision.result"}

ITEM_SCOPED = {
    "item.created",
    "item.state",
    "item.linked",
    "item.note",
    "decision.asked",
    "decision.elected",
    "decision.held",
    "decision.recorded",
    "item.progress",
    "item.tried",
    "review.recorded",
    "item.stopped",
    "guidance.set",
}


def _db(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


def _append(lg, kind: str, key: str, item_id: str | None = "item-1"):
    kwargs = {
        "kind": kind,
        "payload": VALID_PAYLOADS[kind],
        "idempotency_key": key,
        "harness": "pytest",
        "actor": "tester",
    }
    if kind in ITEM_SCOPED:
        kwargs["item_id"] = item_id
    return lg.append(**kwargs)


def test_every_v1_kind_is_registered() -> None:
    assert set(known_kinds()) == set(VALID_PAYLOADS) | NEEDS_DECISION | {
        "loop.closed",
        "loop.done",
        "loop.intent",
        "loop.managed",
        "ledger.system",
        # set_spec alone writes it (test_item_spec covers it).
        "item.spec",
        # attach_document alone writes it (test_doc_verb covers it).
        "doc.attached",
        # the entity verb alone writes them (test_entities covers them).
        "entity.registered",
        "entity.state",
    }


@pytest.mark.parametrize("kind", sorted(VALID_PAYLOADS))
def test_valid_payload_appends(tmp_path: Path, kind: str) -> None:
    lg = open_ledger(_db(tmp_path))
    event = _append(lg, kind, f"k-{kind}")
    assert event.kind == kind
    assert event.payload == VALID_PAYLOADS[kind]
    lg.verify()


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"point": "waiting"}, "missing-key:phase"),
        ({"point": "waiting", "phase": 1}, "bad-type:phase"),
    ],
)
def test_stopped_phase_errors_name_allowed_values(tmp_path, payload, reason) -> None:
    lg = open_ledger(_db(tmp_path))
    with pytest.raises(ValidationError) as caught:
        lg.append(
            kind="item.stopped",
            payload=payload,
            idempotency_key=f"k-{reason}",
            item_id="item-1",
        )
    assert str(caught.value) == f"{reason}; allowed: orient, plan, build, review, close"


def test_unknown_kind_refused(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    with pytest.raises(ValidationError, match="unknown-kind:item.exploded"):
        lg.append(
            kind="item.exploded",
            payload={"title": "nope"},
            idempotency_key="k-unknown",
            item_id="item-1",
        )
    assert lg.events() == []


def test_unknown_payload_key_refused(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    with pytest.raises(ValidationError, match="unknown-key:secret"):
        lg.append(
            kind="item.created",
            payload={"type": "ticket", "title": "x", "secret": "nope"},
            idempotency_key="k-extra",
            item_id="item-1",
        )
    assert lg.events() == []


def test_unknown_nested_key_refused(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    with pytest.raises(ValidationError, match="unknown-key:"):
        lg.append(
            kind="decision.asked",
            payload={
                "question": "q",
                "options": [{"label": "a", "extra": 1}],
            },
            idempotency_key="k-nested",
            item_id="item-1",
        )


def test_missing_required_key_refused(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    with pytest.raises(ValidationError, match="missing-key:title"):
        lg.append(
            kind="item.created",
            payload={"type": "ticket"},
            idempotency_key="k-missing",
            item_id="item-1",
        )


def test_bad_item_type_refused(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    with pytest.raises(ValidationError, match="bad-type:type"):
        lg.append(
            kind="item.created",
            payload={"type": "epic", "title": "x"},
            idempotency_key="k-type",
            item_id="item-1",
        )


def test_item_created_requires_item_id(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    with pytest.raises(ValidationError, match="missing-key:item_id"):
        lg.append(
            kind="item.created",
            payload={"type": "ticket", "title": "x"},
            idempotency_key="k-no-item",
        )


def test_idempotent_reappend_returns_original(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    first = _append(lg, "item.created", "k-same")
    second = _append(lg, "item.created", "k-same")
    assert second.id == first.id
    assert second.hash == first.hash
    assert second.recorded_at == first.recorded_at
    assert len(lg.events()) == 1


def test_lookup_helpers_read_one_event_and_one_item(tmp_path: Path) -> None:
    from ledger import loop
    from ledger.store import item_is_loop_managed

    lg = open_ledger(_db(tmp_path))
    created = lg.append(kind="item.created", item_id="T1",
                        payload={"type": "ticket", "title": "synthetic-one"},
                        idempotency_key="k-created", harness="fixture")
    assert lg.event_by_key("k-created") == created
    assert lg.event_by_key("k-absent") is None
    assert lg.item("T1")["title"] == "synthetic-one"
    assert lg.item("T2") is None
    assert lg.is_loop_managed("T1") is False
    lg._append_loop_event(loop._LOOP_WRITER_CAPABILITY, kind="loop.managed", payload={},
                          item_id="T1", idempotency_key="k-managed", harness="fixture")
    assert lg.is_loop_managed("T1") is True
    assert item_is_loop_managed(lg._conn, "T1") is True
    lg.close()


def _pair(title: str = "synthetic-one", status: str = "closed") -> list[dict]:
    return [
        {"kind": "item.created", "item_id": "T1", "idempotency_key": "k-created",
         "payload": {"type": "ticket", "title": title}, "harness": "fixture"},
        {"kind": "item.state", "item_id": "T1", "idempotency_key": "k-state",
         "payload": {"status": status}, "harness": "fixture"},
    ]


def test_append_many_lands_every_event_in_one_chained_transaction(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    first, second = lg.append_many(_pair())
    assert (first.id, second.id) == (1, 2)
    assert second.prev_hash == first.hash
    assert [e.id for e in lg.events()] == [1, 2]
    assert lg.item("T1")["status"] == "closed"
    lg.verify()
    again = lg.append_many(_pair())
    assert [e.id for e in again] == [1, 2]
    assert len(lg.events()) == 2
    lg.close()


def test_append_many_failure_in_a_later_event_leaves_none(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    pair = _pair()

    def refuse(conn: sqlite3.Connection) -> None:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        raise RuntimeError("refused after the first insert")

    pair[1]["precondition"] = refuse
    with pytest.raises(RuntimeError, match="refused after the first insert"):
        lg.append_many(pair)
    assert lg.events() == []
    with pytest.raises(ValidationError):
        lg.append_many(_pair(status="not-a-status"))
    assert lg.events() == []
    single = lg.append(kind="item.created", item_id="T2", idempotency_key="k-single",
                       payload={"type": "ticket", "title": "after"}, harness="fixture")
    assert single.id == 1 and single.prev_hash == "0" * 64
    lg.verify()
    lg.close()


def test_append_many_applies_the_loop_guard_per_event(tmp_path: Path) -> None:
    from ledger import loop

    lg = open_ledger(_db(tmp_path))
    lg.append(kind="item.created", item_id="T1", idempotency_key="k-seed",
              payload={"type": "ticket", "title": "synthetic-one"}, harness="fixture")
    lg._append_loop_event(loop._LOOP_WRITER_CAPABILITY, kind="loop.managed", payload={},
                          item_id="T1", idempotency_key="k-managed", harness="fixture")
    note = {"kind": "item.note", "item_id": "T1", "idempotency_key": "k-note",
            "payload": {"text": "synthetic note"}, "harness": "fixture"}
    with pytest.raises(ValidationError, match="reserved-transition:loop-close"):
        lg.append_many([note, _pair()[1]])
    assert [e.idempotency_key for e in lg.events()] == ["k-seed", "k-managed"]
    lg.close()


class _InterruptAfterBegin:
    """Connection proxy: the first BEGIN IMMEDIATE succeeds, then an interrupt lands."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._real = conn
        self.armed = True

    def execute(self, sql: str, *args):
        result = self._real.execute(sql, *args)
        if self.armed and sql == "BEGIN IMMEDIATE":
            self.armed = False
            raise KeyboardInterrupt
        return result

    def __getattr__(self, name: str):
        return getattr(self._real, name)


@pytest.mark.parametrize("interrupted", ["append_many", "append"])
def test_an_interrupt_right_after_begin_rolls_back(tmp_path: Path, interrupted: str) -> None:
    lg = open_ledger(_db(tmp_path))
    proxy = _InterruptAfterBegin(lg._conn)
    lg._conn = proxy
    with pytest.raises(KeyboardInterrupt):
        if interrupted == "append_many":
            lg.append_many(_pair())
        else:
            lg.append(**_pair()[0])
    assert proxy.armed is False
    assert lg._conn.in_transaction is False
    single = lg.append(kind="item.created", item_id="T0", idempotency_key="k-after",
                       payload={"type": "ticket", "title": "after"}, harness="fixture")
    assert single.id == 1
    assert [e.id for e in lg.append_many(_pair())] == [2, 3]
    lg.verify()
    lg.close()


def test_append_many_retries_a_busy_begin(tmp_path: Path) -> None:
    import threading

    db = _db(tmp_path)
    lg = open_ledger(db)
    lg._conn.execute("PRAGMA busy_timeout = 50")
    holder = sqlite3.connect(str(db), isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.2, lambda: holder.execute("ROLLBACK"))
    release.start()
    try:
        assert [e.id for e in lg.append_many(_pair())] == [1, 2]
    finally:
        release.join()
        holder.close()
    lg.verify()
    lg.close()


def test_append_many_refuses_to_nest(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    pair = _pair()
    nested: list[BaseException] = []

    def nest(conn: sqlite3.Connection) -> None:
        try:
            lg.append_many([{"kind": "item.note", "item_id": "T1", "idempotency_key": "k-n",
                             "payload": {"text": "nested"}, "harness": "fixture"}])
        except BaseException as exc:
            nested.append(exc)
            raise

    pair[1]["precondition"] = nest
    with pytest.raises(LedgerError, match="append_many cannot nest"):
        lg.append_many(pair)
    assert type(nested[0]) is LedgerError
    assert lg.events() == []
    assert [e.id for e in lg.append_many(_pair())] == [1, 2]
    lg.close()


@pytest.mark.parametrize("path", ["append_many", "append"])
def test_a_caller_transaction_is_refused_and_left_open(tmp_path: Path, path: str) -> None:
    """Neither path joins, commits or rolls back a transaction it did not open."""
    lg = open_ledger(_db(tmp_path))
    lg._conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(LedgerError, match="needs a connection outside any transaction"):
        if path == "append_many":
            lg.append_many(_pair())
        else:
            lg.append(**_pair()[0])
    assert lg._conn.in_transaction is True
    assert lg._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    lg._conn.execute("ROLLBACK")
    assert lg.events() == []
    lg.close()


def test_append_retries_a_busy_begin_without_a_spurious_rollback(tmp_path: Path) -> None:
    """A busy BEGIN opened no transaction, so the retry path must not roll back."""
    import threading

    db = _db(tmp_path)
    lg = open_ledger(db)
    lg._conn.execute("PRAGMA busy_timeout = 50")
    holder = sqlite3.connect(str(db), isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.2, lambda: holder.execute("ROLLBACK"))
    release.start()
    try:
        assert lg.append(**_pair()[0]).id == 1
    finally:
        release.join()
        holder.close()
    lg.verify()
    lg.close()


def test_idempotency_conflict_on_different_payload(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _append(lg, "item.created", "k-clash")
    with pytest.raises(IdempotencyConflict, match="already recorded"):
        lg.append(
            kind="item.created",
            payload={"type": "ticket", "title": "other-title"},
            idempotency_key="k-clash",
            item_id="item-1",
        )
    assert len(lg.events()) == 1


def test_update_refused(tmp_path: Path) -> None:
    path = _db(tmp_path)
    lg = open_ledger(path)
    _append(lg, "item.created", "k-upd")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 5000")
    with pytest.raises(sqlite3.IntegrityError, match="UPDATE refused"):
        conn.execute("UPDATE events SET actor = 'mutated'")
    conn.close()
    lg.verify()
    assert lg.events()[0].actor == "tester"


def test_delete_refused(tmp_path: Path) -> None:
    path = _db(tmp_path)
    lg = open_ledger(path)
    _append(lg, "item.created", "k-del")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 5000")
    with pytest.raises(sqlite3.IntegrityError, match="DELETE refused"):
        conn.execute("DELETE FROM events")
    conn.close()
    assert len(lg.events()) == 1


def test_removing_update_trigger_lets_update_succeed(tmp_path: Path) -> None:
    """The UPDATE-refused test is load-bearing on the trigger: drop it and UPDATE works."""
    path = _db(tmp_path)
    lg = open_ledger(path)
    event = _append(lg, "item.created", "k-mut-upd")
    lg.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("UPDATE events SET actor = 'mutated' WHERE id = ?", (event.id,))
    conn.commit()
    assert conn.execute("SELECT actor FROM events WHERE id = ?", (event.id,)).fetchone()[0] == "mutated"
    conn.close()


def test_removing_delete_trigger_lets_delete_succeed(tmp_path: Path) -> None:
    """The DELETE-refused test is load-bearing on the trigger: drop it and DELETE works."""
    path = _db(tmp_path)
    lg = open_ledger(path)
    _append(lg, "item.created", "k-mut-del")
    lg.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER events_no_delete")
    conn.execute("DELETE FROM events")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    conn.close()


def test_hash_chain_verifies(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    first = _append(lg, "item.created", "k-a")
    second = _append(lg, "item.state", "k-b")
    assert first.prev_hash == "0" * 64
    assert second.prev_hash == first.hash
    assert second.id == first.id + 1
    lg.verify()


def test_chain_break_detected(tmp_path: Path) -> None:
    path = _db(tmp_path)
    lg = open_ledger(path)
    _append(lg, "item.created", "k-a")
    _append(lg, "item.state", "k-b")
    lg.close()
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("UPDATE events SET hash = ? WHERE id = 2", ("ff" * 32,))
    conn.commit()
    conn.close()
    lg = open_ledger(path)
    with pytest.raises(ChainBroken, match="hash mismatch"):
        lg.verify()


def test_views_ready_blocked_and_open_decisions(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    lg.append(
        kind="item.created",
        payload={"type": "ticket", "title": "ready-item"},
        idempotency_key="c-ready",
        item_id="ready-1",
        harness="pytest",
    )
    lg.append(
        kind="item.created",
        payload={"type": "ticket", "title": "blocked-item"},
        idempotency_key="c-blocked",
        item_id="blocked-1",
        harness="pytest",
    )
    lg.append(
        kind="item.state",
        payload={"status": "blocked", "blocked_by": ["ready-1"]},
        idempotency_key="s-blocked",
        item_id="blocked-1",
        harness="pytest",
    )
    lg.append(
        kind="item.created",
        payload={"type": "decision", "title": "colour?"},
        idempotency_key="c-dec",
        item_id="dec-1",
        harness="pytest",
    )
    asked = lg.append(
        kind="decision.asked",
        payload=VALID_PAYLOADS["decision.asked"],
        idempotency_key="ask-1",
        item_id="dec-1",
        harness="pytest",
    )
    ready_ids = {row["item_id"] for row in lg.ready()}
    blocked_ids = {row["item_id"] for row in lg.blocked()}
    assert "ready-1" in ready_ids
    assert "blocked-1" not in ready_ids
    assert blocked_ids == {"blocked-1"}
    assert [row["item_id"] for row in lg.open_decisions()] == ["dec-1"]
    lg.append(
        kind="decision.elected",
        payload=dict(
            VALID_PAYLOADS["decision.elected"], asked_event_id=asked.id
        ),
        idempotency_key="elect-1",
        item_id="dec-1",
        harness="pytest",
    )
    assert lg.open_decisions() == []
    items = {row["item_id"]: row for row in lg.items()}
    assert items["ready-1"]["status"] == "open"
    assert items["blocked-1"]["status"] == "blocked"
    assert items["ready-1"]["title"] == "ready-item"


def test_schema_version_recorded_and_migrate_is_idempotent(tmp_path: Path) -> None:
    path = _db(tmp_path)
    open_ledger(path).close()
    open_ledger(path).close()
    conn = sqlite3.connect(path)
    rows = list(conn.execute("SELECT version, name FROM schema_version ORDER BY version"))
    conn.close()
    assert rows == [
        (1, "0001_initial.sql"),
        (2, "0002_open_decisions_ask_link.sql"),
        (3, "0003_loop_writer_identity.sql"),
        (4, "0004_open_decisions_held.sql"),
        (5, "0005_decision_records.sql"),
        (6, "0006_claim_lease.sql"),
        (7, "0007_actor_operator.sql"),
        (8, "0008_item_spec_version.sql"),
        (9, "0009_entities.sql"),
        (10, "0010_child_of.sql"),
    ]


def test_loop_identity_migration_preserves_existing_events(tmp_path: Path) -> None:
    path = _db(tmp_path)
    conn = connect(path)
    migrate(conn, load_migrations()[:2])
    legacy = Ledger(conn, path)
    created = legacy.append(
        kind="item.created",
        payload={"type": "ticket", "title": "existing item"},
        item_id="existing-1",
        idempotency_key="existing-created",
    )
    closed = legacy.append(
        kind="item.state",
        payload={"status": "closed"},
        item_id="existing-1",
        idempotency_key="existing-closed",
    )
    before = [(event.id, event.hash) for event in legacy.events()]
    legacy.close()

    upgraded = open_ledger(path)
    after = [(event.id, event.hash) for event in upgraded.events()]

    assert after == before == [(created.id, created.hash), (closed.id, closed.hash)]
    assert upgraded.items()[0]["status"] == "closed"
    upgraded.verify()


def test_v1_migration_preserves_other_ask_with_shared_question_id(
    tmp_path: Path,
) -> None:
    path = _db(tmp_path)
    conn = connect(path)
    migrate(conn, load_migrations()[:1])
    lg = Ledger(conn, path)
    lg.append(
        kind="item.created",
        payload={"type": "decision", "title": "two questions"},
        idempotency_key="create",
        item_id="dec-1",
        harness="pytest",
    )
    ask_a = lg.append(
        kind="decision.asked",
        payload={
            "question": "first?",
            "options": [{"label": "yes"}],
            "question_id": "shared",
        },
        idempotency_key="ask-a",
        item_id="dec-1",
        harness="pytest",
    )
    ask_b = lg.append(
        kind="decision.asked",
        payload={
            "question": "second?",
            "options": [{"label": "no"}],
            "question_id": "shared",
        },
        idempotency_key="ask-b",
        item_id="dec-1",
        harness="pytest",
    )
    lg.append(
        kind="decision.elected",
        payload={
            "question": "second?",
            "chosen": "no",
            "chosen_description": "not now",
            "rejected": ["yes"],
            "scope": "release",
            "quote_ref": 1,
            "question_id": "shared",
            "asked_event_id": ask_b.id,
        },
        idempotency_key="elect-b",
        item_id="dec-1",
        harness="pytest",
    )
    lg.close()

    lg = open_ledger(path)
    assert [row["asked_event_id"] for row in lg.open_decisions()] == [ask_a.id]
    lg.close()


def test_unknown_payload_version_refused(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    with pytest.raises(ValidationError, match="unknown-version:item.created:2"):
        lg.append(
            kind="item.created",
            payload={"type": "ticket", "title": "x"},
            idempotency_key="k-v2",
            item_id="item-1",
            payload_version=2,
        )


def _unlocked_copy(src: Path, dest: Path) -> None:
    src_conn = sqlite3.connect(src)
    dest_conn = sqlite3.connect(dest)
    src_conn.backup(dest_conn)
    src_conn.close()
    dest_conn.execute("DROP TRIGGER IF EXISTS events_no_update")
    dest_conn.execute("DROP TRIGGER IF EXISTS events_no_delete")
    dest_conn.commit()
    dest_conn.close()


def _row_body(row: sqlite3.Row, *, prev_hash: str | None = None, payload: dict | None = None) -> dict:
    payload_obj = json.loads(row["payload"]) if payload is None else payload
    return {
        "actor": row["actor"],
        "channel": row["channel"],
        "harness": row["harness"],
        "id": int(row["id"]),
        "idempotency_key": row["idempotency_key"],
        "item_id": row["item_id"],
        "kind": row["kind"],
        "model": row["model"],
        "payload": payload_obj,
        "payload_version": int(row["payload_version"]),
        "prev_hash": row["prev_hash"] if prev_hash is None else prev_hash,
        "provenance_class": row["provenance_class"],
        "recorded_at": row["recorded_at"],
        "session": row["session"],
    }


def test_verify_detects_prev_hash_linkage_break(tmp_path: Path) -> None:
    path = _db(tmp_path)
    lg = open_ledger(path)
    _append(lg, "item.created", "k-a")
    _append(lg, "item.state", "k-b")
    lg.close()
    copy = tmp_path / "linkage.db"
    _unlocked_copy(path, copy)
    conn = sqlite3.connect(copy)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM events WHERE id = 2").fetchone()
    fake_prev = "ab" * 32
    digest = event_digest(_row_body(row, prev_hash=fake_prev))
    conn.execute(
        "UPDATE events SET prev_hash = ?, hash = ? WHERE id = 2",
        (fake_prev, digest),
    )
    conn.commit()
    conn.close()
    lg = open_ledger(copy)
    with pytest.raises(ChainBroken, match="prev_hash"):
        lg.verify()


def test_verify_detects_schema_invalid_payload(tmp_path: Path) -> None:
    path = _db(tmp_path)
    lg = open_ledger(path)
    _append(lg, "item.created", "k-a")
    lg.close()
    copy = tmp_path / "payload.db"
    _unlocked_copy(path, copy)
    conn = sqlite3.connect(copy)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM events WHERE id = 1").fetchone()
    bad_payload = {"type": "ticket", "title": "synthetic-one", "nope": 1}
    digest = event_digest(_row_body(row, payload=bad_payload))
    conn.execute(
        "UPDATE events SET payload = ?, hash = ? WHERE id = 1",
        (json.dumps(bad_payload, sort_keys=True, separators=(",", ":")), digest),
    )
    conn.commit()
    conn.close()
    lg = open_ledger(copy)
    with pytest.raises((ChainBroken, ValidationError)):
        lg.verify()


def test_electing_one_question_leaves_the_other_open(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    lg.append(
        kind="item.created",
        payload={"type": "decision", "title": "two questions"},
        idempotency_key="c-dec",
        item_id="dec-1",
        harness="pytest",
    )
    asked_q = lg.append(
        kind="decision.asked",
        payload={
            "question": "q",
            "options": [{"label": "yes"}],
            "question_id": "qid-q",
        },
        idempotency_key="ask-q",
        item_id="dec-1",
        harness="pytest",
    )
    lg.append(
        kind="decision.asked",
        payload={
            "question": "q2",
            "options": [{"label": "no"}],
            "question_id": "qid-q2",
        },
        idempotency_key="ask-q2",
        item_id="dec-1",
        harness="pytest",
    )
    lg.append(
        kind="decision.elected",
        payload={
            "question": "q",
            "chosen": "yes",
            "chosen_description": "",
            "rejected": [],
            "scope": "s",
            "quote_ref": 1,
            "question_id": "qid-q",
            "asked_event_id": asked_q.id,
        },
        idempotency_key="elect-q",
        item_id="dec-1",
        harness="pytest",
    )
    assert [row["question"] for row in lg.open_decisions()] == ["q2"]


def test_v_items_first_created_wins_on_duplicate_item_id(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    lg.append(
        kind="item.created",
        payload={"type": "ticket", "title": "first"},
        idempotency_key="c-1",
        item_id="same",
        harness="pytest",
    )
    lg.append(
        kind="item.created",
        payload={"type": "ticket", "title": "second"},
        idempotency_key="c-2",
        item_id="same",
        harness="pytest",
    )
    items = lg.items()
    assert len(items) == 1
    assert items[0]["item_id"] == "same"
    assert items[0]["title"] == "first"


def test_lone_surrogate_payload_is_validation_error(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    with pytest.raises(ValidationError, match="bad-type:payload"):
        lg.append(
            kind="item.created",
            payload={"type": "ticket", "title": "\ud800"},
            idempotency_key="k-surr",
            item_id="item-1",
            harness="pytest",
        )
    assert lg.events() == []


def test_v_ready_excludes_unresolved_blocker(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    lg.append(
        kind="item.created",
        payload={"type": "ticket", "title": "blocker"},
        idempotency_key="c-blocker",
        item_id="blocker-1",
        harness="pytest",
    )
    lg.append(
        kind="item.created",
        payload={"type": "ticket", "title": "blocked"},
        idempotency_key="c-blocked",
        item_id="blocked-1",
        harness="pytest",
    )
    lg.append(
        kind="item.linked",
        payload={"rel": "blocks", "target": "blocked-1"},
        idempotency_key="l-blocks",
        item_id="blocker-1",
        harness="pytest",
    )
    ready_ids = {row["item_id"] for row in lg.ready()}
    assert "blocker-1" in ready_ids
    assert "blocked-1" not in ready_ids
    lg.append(
        kind="item.state",
        payload={"status": "closed"},
        idempotency_key="s-close",
        item_id="blocker-1",
        harness="pytest",
    )
    ready_ids = {row["item_id"] for row in lg.ready()}
    assert "blocked-1" in ready_ids
    assert "blocker-1" not in ready_ids


def test_raising_precondition_leaves_no_event_and_no_chain_change(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    first = _append(lg, "item.created", "k-created", "item-1")
    before = lg.events()
    before_hash = first.hash

    def boom(conn) -> None:
        raise RuntimeError("precondition failed")

    with pytest.raises(RuntimeError, match="precondition failed"):
        lg.append(
            kind="item.note",
            payload={"text": "should not land"},
            item_id="item-1",
            idempotency_key="k-boom",
            harness="pytest",
            actor="tester",
            precondition=boom,
        )
    after = lg.events()
    assert len(after) == len(before)
    assert after[-1].hash == before_hash
    assert after[-1].id == first.id
    lg.verify()
