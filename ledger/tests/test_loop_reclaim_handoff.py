"""Reclaiming a dead session hand-off: ``ledger.loop.reclaim_handoff``.

A session-mode step (``claim_operation`` with ``hand_off``) leaves the
holder's claim with no expiry, so a seat that dies inside it holds the item
for good. The recovery moves that one claim to a named successor on an
explicit seat-lost declaration, never on elapsed time, and only while no
ticket pass on the item may still be live. The marker, the old operation's
``loop.done`` (outcome ``seat-lost``) and the successor's leased claim commit
together or not at all. Every refusal here also checks that the whole event
chain is unchanged.
"""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from ledger import decisions, lease, loop, store
from ledger.cli import main
from ledger.export import (
    event_filename,
    event_record,
    export_events,
    hash_body,
    verify_export,
    write_stream_digest,
)
from ledger.loop import (
    claim_operation,
    close_item,
    finish_operation,
    link_pull_request,
    open_hand_off,
    operation_state,
    reclaim_handoff,
    record_progress,
    restamp_claim,
    take_item,
)
from ledger.restore import restore_export
from ledger.schema import ValidationError, validate_payload
from ledger.store import (
    GENESIS_HASH,
    Event,
    IdempotencyConflict,
    Ledger,
    LedgerError,
    canonical_dumps,
    event_digest,
    open_ledger,
)

ITEM = "item-1"
OTHER_ITEM = "item-2"
PRED = "seat-a@s1"
SUCC = "seat-b@s2"
THIRD = "seat-c@s3"
OPERATOR = "operator-1"
EVIDENCE = "pane gone; no seat-a process under the launcher"
CONTROLLER = "director"
SEAMS = {"build": "omp-builder", "review": "review-gate", "outward": "draft-pr"}
PASS = "0123456789abcdef"
DAY = 24 * 60 * 60
_LIB = Path(__file__).resolve().parents[2]


# --- Helpers ------------------------------------------------------------------


def invoke(monkeypatch, db: str, *args: str, stdin: str | None = None, env=None) -> tuple[int, str, str]:
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["--db", db, *args])
    for name in env or {}:
        monkeypatch.delenv(name)
    return code, out.getvalue(), err.getvalue()


def ok(monkeypatch, db: str, *args: str, env=None) -> str:
    code, out, err = invoke(monkeypatch, db, *args, env=env)
    assert code == 0, err
    return out


def file_item(monkeypatch, db: str, item_id: str = ITEM, *, spec: bool = True) -> None:
    ok(monkeypatch, db, "new", "--id", item_id, "--type", "ticket", "--title", "Loop item")
    if spec:
        ok(monkeypatch, db, "spec", "set", item_id, "--title", "Loop item", "--body", "Build it.")


def all_events(db: str) -> list[Event]:
    with open_ledger(db, create=False) as ledger:
        return ledger.events()


def take(db: str, holder: str = PRED, item_id: str = ITEM, n: int = 1, ttl: int | None = None) -> Event:
    with open_ledger(db, create=False) as ledger:
        return take_item(
            ledger, item_id, holder, idempotency_key=f"manage:{item_id}",
            claim_key=f"take:{item_id}:{n}", ttl_seconds=ttl,
        )


def hand_off_v2(db: str, operation_id: str = "build-1", holder: str = PRED, item_id: str = ITEM) -> Event:
    with open_ledger(db, create=False) as ledger:
        return claim_operation(
            ledger, item_id, "build", operation_id, holder=holder, hand_off=True,
            controller=CONTROLLER, seams=dict(SEAMS),
        ).event


def hand_off_v1(db: str, operation_id: str = "op-1", holder: str = PRED, *, session: bool = True,
                operation: str = "build", item_id: str = ITEM) -> Event:
    with open_ledger(db, create=False) as ledger:
        return claim_operation(
            ledger, item_id, operation, operation_id, holder=holder, hand_off=session
        ).event


def latest_claim_id(db: str, item_id: str = ITEM) -> int:
    with open_ledger(db, create=False) as ledger:
        return loop._latest_claim(ledger._conn, item_id)["claim_event_id"]


def request(db: str, intent: Event | int | None = None, **overrides) -> dict:
    """The default recovery request: the item's open hand-off, its current claim, PRED to SUCC by an operator."""
    if intent is None:
        with open_ledger(db, create=False) as ledger:
            intent = open_hand_off(ledger, ITEM)
    values = {
        "intent_event_id": intent if isinstance(intent, int) else intent.id,
        "claim_event_id": overrides["claim_event_id"] if "claim_event_id" in overrides else latest_claim_id(db),
        "prior_holder": PRED,
        "holder": SUCC,
        "declare_seat_lost": True,
        "authority": "operator",
        "declared_by": OPERATOR,
        "evidence": EVIDENCE,
    }
    values.update(overrides)
    return values


def reclaim(db: str, item_id: str = ITEM, **fields) -> loop.HandoffReclaim:
    with open_ledger(db, create=False) as ledger:
        return reclaim_handoff(ledger, item_id, **fields)


def assert_refused(db: str, name: str, *, item_id: str = ITEM, error=ValidationError, **fields) -> BaseException:
    """The recovery is refused with ``name`` and the event chain is unchanged."""
    before = all_events(db)
    with open_ledger(db, create=False) as ledger:
        with pytest.raises(error) as caught:
            reclaim_handoff(ledger, item_id, **fields)
    if error is ValidationError:
        assert caught.value.reason.split(";")[0] == name, caught.value.reason
    else:
        assert re.match(re.escape(name), str(caught.value)), str(caught.value)
    assert all_events(db) == before, "a refused recovery must write nothing"
    return caught.value


def kinds(events: list[Event]) -> list[str]:
    return [event.kind for event in events]


@pytest.fixture
def clock(monkeypatch):
    """Move ``lease.now`` and the store's ``recorded_at`` clock together."""
    real = lease.now
    real_stamp = store._now

    class Clock:
        def at(self, seconds_from_now: int) -> None:
            moved = lambda: real() + timedelta(seconds=seconds_from_now)  # noqa: E731
            monkeypatch.setattr(lease, "now", moved)
            monkeypatch.setattr(store, "_now", lambda: lease.stamp(moved()))

        def real(self) -> None:
            monkeypatch.setattr(lease, "now", real)
            monkeypatch.setattr(store, "_now", real_stamp)

    return Clock()


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    """ITEM with spec v1, taken by PRED and handed off (version 2, build-1)."""
    path = str(tmp_path / "ledger.db")
    file_item(monkeypatch, path)
    take(path)
    hand_off_v2(path)
    return path


@pytest.fixture
def bare(tmp_path: Path, monkeypatch) -> str:
    """ITEM with spec v1, taken by PRED, no operation yet."""
    path = str(tmp_path / "ledger.db")
    file_item(monkeypatch, path)
    take(path)
    return path


def write_export(export_dir: Path, events: list[tuple]) -> None:
    """A valid hash chain on ITEM of ``(kind, payload_version, payload, key, harness)``."""
    export_dir.mkdir()
    previous = GENESIS_HASH
    for event_id, (kind, version, payload, key, harness) in enumerate(events, 1):
        event = Event(
            id=event_id, kind=kind, payload_version=version, payload=payload, item_id=ITEM,
            idempotency_key=key, actor=None, harness=harness, model=None, session=None,
            channel=None, provenance_class=None,
            recorded_at=f"2026-01-01T00:00:{event_id:02d}.000Z", prev_hash=previous, hash="",
        )
        event = replace(event, hash=event_digest(hash_body(event)))
        (export_dir / event_filename(event_id)).write_text(
            canonical_dumps(event_record(event)) + "\n", encoding="utf-8"
        )
        previous = event.hash
    (export_dir / "watermark.json").write_text(
        json.dumps({"id": len(events)}, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    write_stream_digest(export_dir, verify_export(export_dir))


def hand_off_payload(operation_id: str = "build-1") -> dict:
    return {"operation": "build", "operation_id": operation_id, "controller": CONTROLLER, "seams": dict(SEAMS)}


def marker_payload(intent_id: int, claim_id: int, holder: str = SUCC) -> dict:
    return {
        "intent_event_id": intent_id, "claim_event_id": claim_id, "prior_holder": PRED,
        "holder": holder, "declaration": "seat-lost", "authority": "operator",
        "declared_by": OPERATOR, "evidence": EVIDENCE,
    }


# --- A: a version 2 hand-off's recovery ---------------------------------------


def test_a1_recovery_writes_the_marker_the_seat_lost_done_and_the_successors_lease(
    db: str, monkeypatch
) -> None:
    (Path(db).parent / "config.json").write_text('{"claim_ttl_seconds": 900}', encoding="utf-8")
    moment = lease.now()
    monkeypatch.setattr(lease, "now", lambda: moment)
    with open_ledger(db, create=False) as ledger:
        intent = open_hand_off(ledger, ITEM)
    claim_id = latest_claim_id(db)
    before = all_events(db)
    result = reclaim(db, **request(db), actor="op/roll", harness="roll", model="m", session="sess-9")
    assert result.created is True
    written = all_events(db)[len(before):]
    assert written == [result.reclaimed, result.done, result.claim]
    marker, done, claim = written
    assert [marker.id + 1, marker.id + 2] == [done.id, claim.id]
    assert marker.kind == "loop.reclaimed" and marker.payload_version == 1
    assert marker.idempotency_key == f"loop:{ITEM}:reclaim:{intent.id}"
    assert marker.payload == marker_payload(intent.id, claim_id)
    assert (marker.actor, marker.harness, marker.session) == ("op/roll", "roll", "sess-9")
    assert done.kind == "loop.done"
    assert done.idempotency_key == f"loop:{ITEM}:build:build-1:done"
    assert done.payload == {
        "operation": "build", "operation_id": "build-1", "intent_event_id": intent.id,
        "outcome": "seat-lost", "source": f"event:{marker.id}",
    }
    assert claim.kind == "item.state"
    assert claim.idempotency_key == f"loop:{ITEM}:reclaim:{intent.id}:claim"
    assert claim.payload == {
        "status": "claimed", "claim": SUCC, "prior_holder": PRED,
        "expires_at": lease.expires_at(900, at=moment), "spec_version": 1,
    }
    with open_ledger(db, create=False) as ledger:
        assert open_hand_off(ledger, ITEM) is None
        assert operation_state(ledger, ITEM, "build", "build-1").done == done
        ledger.verify()


def test_a1_an_item_with_no_spec_stamps_no_spec_version(tmp_path: Path, monkeypatch) -> None:
    path = str(tmp_path / "ledger.db")
    file_item(monkeypatch, path, spec=False)
    take(path)
    hand_off_v2(path)
    result = reclaim(path, **request(path))
    assert "spec_version" not in result.claim.payload
    assert result.claim.payload["claim"] == SUCC


def test_a2_the_predecessor_is_fenced_and_the_successor_hands_off_anew(db: str) -> None:
    reclaim(db, **request(db))
    before = all_events(db)
    with open_ledger(db, create=False) as ledger:
        with pytest.raises(lease.ClaimRefused) as caught:
            record_progress(ledger, ITEM, "still here", idempotency_key="beat-1", holder=PRED)
        assert caught.value.code == "claim_lost"
        with pytest.raises(lease.ClaimRefused):
            claim_operation(ledger, ITEM, "review", "review-1", holder=PRED)
        with pytest.raises(lease.ClaimRefused):
            link_pull_request(ledger, ITEM, PRED, "org/repo", 7)
        # The old operation is finished: its result can never be recorded.
        with pytest.raises(IdempotencyConflict):
            finish_operation(ledger, ITEM, "build", "build-1", outcome="handback", holder=PRED)
        assert restamp_claim(ledger, ITEM, PRED, idempotency_key="restamp-old") is None
    assert all_events(db) == before, "the fenced predecessor wrote nothing"
    # The successor holds the live claim and hands off under a new operation id.
    with open_ledger(db, create=False) as ledger:
        record_progress(ledger, ITEM, "successor working", idempotency_key="beat-2", holder=SUCC)
        again = claim_operation(
            ledger, ITEM, "build", "build-2", holder=SUCC, hand_off=True,
            controller=CONTROLLER, seams=dict(SEAMS),
        )
        assert again.created is True
        assert open_hand_off(ledger, ITEM) == again.event
    assert all_events(db)[-1].payload == {"status": "claimed", "claim": SUCC}


def test_a3_a_successor_may_declare_as_itself(db: str) -> None:
    result = reclaim(db, **request(db, authority="successor", declared_by=SUCC))
    assert result.created is True
    assert result.reclaimed.payload["authority"] == "successor"
    assert result.reclaimed.payload["declared_by"] == SUCC


def test_a3_evidence_of_exactly_300_characters_is_accepted(db: str) -> None:
    assert reclaim(db, **request(db, evidence="e" * 300)).created is True


# --- B: legacy version 1 session operations, and what is never one -----------


def test_b1_a_version_1_hand_off_session_operation_is_recovered(bare: str) -> None:
    intent = hand_off_v1(bare)
    assert intent.payload_version == 1
    result = reclaim(bare, **request(bare, intent))
    assert result.created is True
    assert result.done.payload == {
        "operation": "build", "operation_id": "op-1", "intent_event_id": intent.id,
        "outcome": "seat-lost", "source": f"event:{result.reclaimed.id}",
    }
    assert result.claim.payload["claim"] == SUCC
    with open_ledger(bare, create=False) as ledger:
        assert operation_state(ledger, ITEM, "build", "op-1").state == "finished"


def test_b2_a_version_1_runner_intent_is_not_a_session_claim(bare: str) -> None:
    intent = hand_off_v1(bare, session=False)
    assert_refused(bare, "reclaim-not-session-claim", **request(bare, intent))


def test_b3_an_unrelated_unstamped_claim_is_never_a_session_hand_off(bare: str, monkeypatch) -> None:
    intent = hand_off_v1(bare, session=False)
    # A generic writer leaves PRED's claim unstamped; no hand-off rewrote it.
    ok(monkeypatch, bare, "state", ITEM, "--status", "claimed", "--claim", PRED)
    assert_refused(bare, "reclaim-not-session-claim", **request(bare, intent))


def test_b4_a_rewrite_key_a_cli_writer_chose_is_not_the_proof(bare: str, monkeypatch) -> None:
    intent = hand_off_v1(bare, session=False)
    ok(
        monkeypatch, bare, "append", "--kind", "item.state", "--item", ITEM,
        "--payload", json.dumps({"status": "claimed", "claim": PRED}),
        "--idempotency-key", f"loop:{ITEM}:build:op-1:hand-off",
    )
    assert all_events(bare)[-1].id == intent.id + 1
    assert_refused(bare, "reclaim-not-session-claim", **request(bare, intent))


def _forge(db: str, payload: dict, *, item_id: str = ITEM, key_item: str = ITEM) -> None:
    """An in-process write under the rewrite's key, with no attribution."""
    with open_ledger(db, create=False) as ledger:
        ledger.append(
            kind="item.state", payload=payload, item_id=item_id,
            idempotency_key=f"loop:{key_item}:build:op-1:hand-off",
        )


def test_b5_a_rewrite_that_is_not_the_bare_unstamped_claim_is_not_the_proof(bare: str) -> None:
    intent = hand_off_v1(bare, session=False)
    _forge(bare, {"status": "claimed", "owner": PRED})
    assert_refused(bare, "reclaim-not-session-claim", **request(bare, intent))


def test_b6_a_rewrite_outside_the_intents_transaction_is_not_the_proof(bare: str, monkeypatch) -> None:
    intent = hand_off_v1(bare, session=False)
    ok(monkeypatch, bare, "note", ITEM, "between")
    _forge(bare, {"status": "claimed", "claim": PRED})
    assert_refused(bare, "reclaim-not-session-claim", **request(bare, intent))


def test_b7_a_rewrite_on_another_item_is_not_the_proof(bare: str, monkeypatch) -> None:
    file_item(monkeypatch, bare, OTHER_ITEM, spec=False)
    intent = hand_off_v1(bare, session=False)
    _forge(bare, {"status": "claimed", "claim": PRED}, item_id=OTHER_ITEM)
    ok(monkeypatch, bare, "state", ITEM, "--status", "claimed", "--claim", PRED)
    assert_refused(bare, "reclaim-not-session-claim", **request(bare, intent))


def test_b8_a_rewrite_that_is_not_a_claim_is_not_the_proof(bare: str, monkeypatch) -> None:
    intent = hand_off_v1(bare, session=False)
    _forge(bare, {"status": "open", "claim": PRED})
    ok(monkeypatch, bare, "state", ITEM, "--status", "claimed", "--claim", PRED)
    assert_refused(bare, "reclaim-not-session-claim", **request(bare, intent))


# --- C: the request's grammar ------------------------------------------------


GRAMMAR = [
    ({"declare_seat_lost": False}, "reclaim-declaration-required"),
    ({"declare_seat_lost": None}, "reclaim-declaration-required"),
    ({"declare_seat_lost": "yes"}, "reclaim-declaration-required"),
    ({"declare_seat_lost": 1}, "reclaim-declaration-required"),
    ({"intent_event_id": None}, "missing-key:intent_event_id"),
    ({"intent_event_id": True}, "bad-type:intent_event_id"),
    ({"intent_event_id": 0}, "bad-type:intent_event_id"),
    ({"intent_event_id": "5"}, "bad-type:intent_event_id"),
    ({"intent_event_id": 2**63}, "too-large:intent_event_id"),
    ({"claim_event_id": None}, "missing-key:claim_event_id"),
    ({"claim_event_id": False}, "bad-type:claim_event_id"),
    ({"claim_event_id": -1}, "bad-type:claim_event_id"),
    ({"claim_event_id": 6.0}, "bad-type:claim_event_id"),
    ({"prior_holder": None}, "missing-key:prior_holder"),
    ({"prior_holder": ""}, "bad-type:prior_holder"),
    ({"prior_holder": 7}, "bad-type:prior_holder"),
    ({"holder": None}, "missing-key:holder"),
    ({"holder": ""}, "bad-type:holder"),
    ({"holder": ["seat-b@s2"]}, "bad-type:holder"),
    ({"holder": "seat-b"}, "reclaim-successor-invalid"),
    ({"holder": "seat-b@"}, "reclaim-successor-invalid"),
    ({"holder": "@s2"}, "reclaim-successor-invalid"),
    ({"holder": "seat b@s2"}, "reclaim-successor-invalid"),
    ({"holder": "-seat@s2"}, "reclaim-successor-invalid"),
    ({"holder": "seat-b@s2@x"}, "reclaim-successor-invalid"),
    ({"holder": "s" * 65 + "@s2"}, "reclaim-successor-invalid"),
    ({"holder": "seat@" + "s" * 65}, "reclaim-successor-invalid"),
    ({"holder": "seat-а@s2"}, "reclaim-successor-invalid"),
    ({"holder": PRED}, "reclaim-same-holder"),
    ({"authority": None}, "missing-key:authority"),
    ({"authority": "admin"}, "bad-type:authority"),
    ({"authority": "Operator"}, "bad-type:authority"),
    ({"authority": 3}, "bad-type:authority"),
    ({"authority": "successor"}, "reclaim-authority-invalid"),
    ({"authority": "successor", "declared_by": PRED}, "reclaim-authority-invalid"),
    ({"declared_by": None}, "missing-key:declared_by"),
    ({"declared_by": ""}, "bad-type:declared_by"),
    ({"declared_by": "op\nerator"}, "not-single-line:declared_by"),
    ({"declared_by": "o" * 301}, "too-long:declared_by"),
    ({"evidence": None}, "missing-key:evidence"),
    ({"evidence": ""}, "bad-type:evidence"),
    ({"evidence": 12}, "bad-type:evidence"),
    ({"evidence": "line one\nline two"}, "not-single-line:evidence"),
    ({"evidence": "e" * 301}, "too-long:evidence"),
]


@pytest.mark.parametrize(("overrides", "name"), GRAMMAR, ids=[name + "-" + str(i) for i, (_, name) in enumerate(GRAMMAR)])
def test_c1_a_request_off_the_grammar_is_refused_by_name(db: str, overrides: dict, name: str) -> None:
    assert_refused(db, name, **request(db, **overrides))


@pytest.mark.parametrize(("item_id", "name"), [("", "missing-key:item_id"), (None, "missing-key:item_id"), (5, "bad-type:item_id")])
def test_c1_the_item_id_is_a_non_empty_string(db: str, item_id, name: str) -> None:
    assert_refused(db, name, item_id=item_id, **request(db))


def test_c2_the_marker_payload_has_exactly_its_keys() -> None:
    payload = marker_payload(5, 6)
    assert validate_payload("loop.reclaimed", dict(payload)) == payload
    with pytest.raises(ValidationError, match="^unknown-key:source$"):
        validate_payload("loop.reclaimed", {**payload, "source": "x"})
    with pytest.raises(ValidationError, match="^missing-key:declaration$"):
        validate_payload("loop.reclaimed", {k: v for k, v in payload.items() if k != "declaration"})
    with pytest.raises(ValidationError, match="^bad-type:declaration"):
        validate_payload("loop.reclaimed", {**payload, "declaration": "heartbeat-stale"})
    with pytest.raises(ValidationError, match="^bad-type:holder"):
        validate_payload("loop.reclaimed", {**payload, "holder": "no-session"})
    with pytest.raises(ValidationError, match="^reclaim-same-holder$"):
        validate_payload("loop.reclaimed", {**payload, "holder": PRED})
    with pytest.raises(ValidationError, match="^reclaim-authority-invalid$"):
        validate_payload("loop.reclaimed", {**payload, "authority": "successor"})
    with pytest.raises(ValidationError, match="^unknown-version:loop.reclaimed:2$"):
        validate_payload("loop.reclaimed", dict(payload), 2)


# --- D: the item, the target and the claim -----------------------------------


def test_d1_a_missing_item(db: str) -> None:
    assert_refused(db, "missing-item:nope", item_id="nope", **request(db))


def test_d2_an_item_that_is_not_loop_managed(db: str, monkeypatch) -> None:
    file_item(monkeypatch, db, OTHER_ITEM, spec=False)
    ok(monkeypatch, db, "claim", OTHER_ITEM, "--claim", PRED)
    assert_refused(db, f"loop-item-not-managed:{OTHER_ITEM}", item_id=OTHER_ITEM, **request(db))


def test_d3_a_closed_item(db: str) -> None:
    fields = request(db)
    with open_ledger(db, create=False) as ledger:
        claim_operation(ledger, ITEM, "merge", "merge-1", holder=PRED)
        merged = finish_operation(ledger, ITEM, "merge", "merge-1", outcome="merged")
        close_item(ledger, ITEM, merged.id, idempotency_key="close-1")
    assert_refused(db, "reclaim-item-closed", **fields)


def test_d4_a_blocked_item(db: str, monkeypatch) -> None:
    fields = request(db)
    ok(monkeypatch, db, "state", ITEM, "--status", "blocked")
    assert_refused(db, "reclaim-item-blocked", **fields)


@pytest.mark.parametrize("status", ["open", "claimed"])
def test_d5_a_latest_state_that_names_nobody_is_no_current_claim(db: str, monkeypatch, status: str) -> None:
    fields = request(db)
    ok(monkeypatch, db, "state", ITEM, "--status", status)
    assert_refused(db, "reclaim-no-current-claim", **fields)


def test_d5_an_open_state_naming_the_holder_is_no_current_claim(db: str, monkeypatch) -> None:
    fields = request(db)
    ok(monkeypatch, db, "state", ITEM, "--status", "open", "--claim", PRED)
    assert_refused(db, "reclaim-no-current-claim", **{**fields, "claim_event_id": latest_claim_id(db)})


def test_d5_a_managed_item_never_claimed_has_no_current_claim(tmp_path: Path, monkeypatch) -> None:
    path = str(tmp_path / "ledger.db")
    file_item(monkeypatch, path)
    with open_ledger(path, create=False) as ledger:
        loop.manage_item(ledger, ITEM, idempotency_key="manage")
        intent = claim_operation(ledger, ITEM, "build", "build-1").event
    assert_refused(path, "reclaim-no-current-claim", **request(path, intent, claim_event_id=1))


def test_d6_a_target_that_is_not_an_intent_on_the_item(db: str, monkeypatch) -> None:
    assert_refused(db, "reclaim-handoff-missing", **request(db, 999))
    assert_refused(db, "reclaim-handoff-missing", **request(db, latest_claim_id(db)))
    file_item(monkeypatch, db, OTHER_ITEM, spec=False)
    take(db, PRED, item_id=OTHER_ITEM)
    elsewhere = hand_off_v2(db, item_id=OTHER_ITEM)
    assert_refused(db, "reclaim-handoff-missing", **request(db, elsewhere))


def test_d7_a_finished_hand_off_is_not_current(db: str) -> None:
    fields = request(db)
    with open_ledger(db, create=False) as ledger:
        finish_operation(ledger, ITEM, "build", "build-1", outcome="handback", holder=PRED)
    assert_refused(db, "reclaim-handoff-changed", **fields)
    # A finished target while a newer hand-off is open is still not current.
    hand_off_v2(db, "build-2")
    assert_refused(db, "reclaim-handoff-changed", **{**fields, "claim_event_id": latest_claim_id(db)})


def test_d7_a_finished_version_1_session_operation_is_not_current(bare: str) -> None:
    intent = hand_off_v1(bare)
    fields = request(bare, intent)
    with open_ledger(bare, create=False) as ledger:
        finish_operation(ledger, ITEM, "build", "op-1", outcome="handback")
    assert_refused(bare, "reclaim-handoff-changed", **fields)


def test_d8_two_open_hand_offs_in_a_restored_chain_are_ambiguous(tmp_path: Path) -> None:
    export_dir = tmp_path / "two-open"
    write_export(export_dir, [
        ("item.created", 1, {"type": "ticket", "title": "Restored"}, "created", "pytest"),
        ("loop.managed", 1, {}, "manage", None),
        ("loop.intent", 2, hand_off_payload("build-a"), f"loop:{ITEM}:build:build-a:intent", None),
        ("item.state", 1, {"status": "claimed", "claim": PRED}, f"loop:{ITEM}:build:build-a:hand-off", None),
        ("loop.intent", 2, hand_off_payload("build-b"), f"loop:{ITEM}:build:build-b:intent", None),
        ("item.state", 1, {"status": "claimed", "claim": PRED}, f"loop:{ITEM}:build:build-b:hand-off", None),
    ])
    dest = str(tmp_path / "restored.db")
    restore_export(export_dir, dest, system="home", accept_loop_events=True)
    for intent_id in (3, 5):
        assert_refused(dest, f"loop-hand-off-ambiguous:{ITEM}", **request(dest, intent_id, claim_event_id=6))


def test_d9_another_unfinished_session_operation_is_ambiguous(bare: str) -> None:
    legacy = hand_off_v1(bare)
    current = hand_off_v2(bare, "build-2")
    claim_id = latest_claim_id(bare)
    assert_refused(bare, "reclaim-session-ambiguous", **request(bare, current, claim_event_id=claim_id))
    assert_refused(bare, "reclaim-session-ambiguous", **request(bare, legacy, claim_event_id=claim_id))


def test_d9_two_version_1_session_operations_are_ambiguous(bare: str) -> None:
    first = hand_off_v1(bare, "op-1")
    second = hand_off_v1(bare, "op-2")
    claim_id = latest_claim_id(bare)
    for intent in (first, second):
        assert_refused(bare, "reclaim-session-ambiguous", **request(bare, intent, claim_event_id=claim_id))


def test_d9_an_open_runner_intent_beside_the_hand_off_is_not_ambiguous(db: str) -> None:
    with open_ledger(db, create=False) as ledger:
        claim_operation(ledger, ITEM, "review", "review-1", holder=PRED)
    assert reclaim(db, **request(db)).created is True


def test_d10_a_restamped_claim_is_not_a_session_claim_even_once_expired(db: str, clock) -> None:
    fields = request(db)
    with open_ledger(db, create=False) as ledger:
        assert restamp_claim(ledger, ITEM, PRED, idempotency_key="restamp-1") is not None
    fields["claim_event_id"] = latest_claim_id(db)
    assert_refused(db, "reclaim-not-session-claim", **fields)
    clock.at(lease.DEFAULT_TTL_SECONDS + 60)
    assert_refused(db, "reclaim-not-session-claim", **fields)


def test_d11_a_changed_claim_id_is_refused(db: str) -> None:
    fields = request(db)
    assert_refused(db, "reclaim-claim-changed", **{**fields, "claim_event_id": fields["claim_event_id"] - 1})


def test_d11_a_same_name_renewal_needs_the_current_claim_id(db: str, monkeypatch) -> None:
    stale = request(db)
    # PRED re-claims its own loop claim: the claim is rewritten, still unstamped.
    ok(monkeypatch, db, "claim", ITEM, "--claim", PRED, "--owner", PRED)
    renewal = all_events(db)[-1]
    assert "expires_at" not in renewal.payload
    assert_refused(db, "reclaim-claim-changed", **stale)
    result = reclaim(db, **{**stale, "claim_event_id": renewal.id})
    assert result.reclaimed.payload["claim_event_id"] == renewal.id


def test_d11_another_holder_in_between_breaks_continuity(db: str, monkeypatch) -> None:
    fields = request(db)
    ok(monkeypatch, db, "state", ITEM, "--status", "claimed", "--claim", THIRD)
    ok(monkeypatch, db, "state", ITEM, "--status", "claimed", "--claim", PRED)
    assert_refused(db, "reclaim-claim-changed", **{**fields, "claim_event_id": latest_claim_id(db)})


def test_d11_a_stamped_claim_in_between_breaks_continuity(db: str, monkeypatch) -> None:
    fields = request(db)
    ok(monkeypatch, db, "state", ITEM, "--status", "open")
    ok(monkeypatch, db, "claim", ITEM, "--claim", PRED, "--owner", PRED)
    assert "expires_at" in all_events(db)[-1].payload
    ok(monkeypatch, db, "state", ITEM, "--status", "claimed", "--claim", PRED)
    assert_refused(db, "reclaim-claim-changed", **{**fields, "claim_event_id": latest_claim_id(db)})


def test_d11_a_predecessor_other_than_the_recorded_holder_is_refused(db: str) -> None:
    assert_refused(db, "reclaim-claim-changed", **request(db, prior_holder="seat-z@s9"))
    assert_refused(db, "reclaim-claim-changed", **request(db, prior_holder="Seat-a@s1"))


@pytest.mark.parametrize("config", ['{"claim_ttl_seconds": 0}', '{"claim_ttl_seconds": "1h"}', "not json"])
def test_d12_a_bad_configured_ttl(db: str, config: str) -> None:
    (Path(db).parent / "config.json").write_text(config, encoding="utf-8")
    assert_refused(db, "reclaim-bad-ttl", **request(db))


def test_d13_generic_writers_cannot_write_a_reclaim(db: str, monkeypatch) -> None:
    before = all_events(db)
    payload = marker_payload(5, 6)
    code, _out, err = invoke(
        monkeypatch, db, "append", "--kind", "loop.reclaimed", "--item", ITEM, "--payload", json.dumps(payload)
    )
    assert code == 3 and "reserved-kind:loop.reclaimed" in err
    batch = [{"kind": "loop.reclaimed", "payload": payload}]
    code, _out, err = invoke(monkeypatch, db, "append-many", ITEM, stdin=json.dumps(batch))
    assert code == 3 and "reserved-kind:loop.reclaimed" in err
    with open_ledger(db, create=False) as ledger:
        with pytest.raises(ValidationError, match="^reserved-kind:loop.reclaimed$"):
            ledger.append(kind="loop.reclaimed", payload=payload, item_id=ITEM, idempotency_key="forge")
        with pytest.raises(ValidationError, match="^reserved-kind:loop.reclaimed$"):
            ledger.append_many([{"kind": "loop.reclaimed", "payload": payload, "item_id": ITEM, "idempotency_key": "f2"}])
        with pytest.raises(ValidationError, match="^loop-writer-capability-required$"):
            ledger._append_loop_event(object(), kind="loop.reclaimed", payload=payload, item_id=ITEM, idempotency_key="f3")
    assert all_events(db) == before


# --- P: ticket passes on the item --------------------------------------------


def pass_record(db: str, pass_id: str = PASS, *, text: str | None = None, key: str | None = None,
                kind: str = "item.note", item_id: str = ITEM) -> None:
    record = {"id": pass_id, "host": "studio", "started_at": "2026-11-22T10:00:00Z", "holder": PRED, "lease": "joined"}
    if text is None:
        text = "ticket-pass:v2 " + json.dumps(record, sort_keys=True, separators=(",", ":"))
    payload = {"text": text, "source": "ticket"} if kind == "item.note" else {"source": "ticket"}
    with open_ledger(db, create=False) as ledger:
        ledger.append(kind=kind, payload=payload, item_id=item_id, harness="ticket",
                      idempotency_key=key or f"ticket-pass/{ITEM}/{pass_id}/record")


def pass_claimed(db: str, pass_id: str = PASS, *, key: str | None = None, kind: str = "session.opened") -> None:
    with open_ledger(db, create=False) as ledger:
        ledger.append(kind=kind, payload={"purpose": f"ticket pass {pass_id} on studio at 2026-11-22T10:00:00Z"} if kind == "session.opened" else {},
                      item_id=ITEM, harness="ticket", idempotency_key=key or f"ticket-pass/{ITEM}/{pass_id}/claimed")


def pass_released(db: str, pass_id: str = PASS, *, item_id: str = ITEM, key_item: str = ITEM,
                  kind: str = "session.closed", role: str = "released") -> None:
    payload = {"outcome": f"ticket pass {pass_id} released", "source": "ticket"} if kind == "session.closed" else {"source": "ticket"}
    with open_ledger(db, create=False) as ledger:
        ledger.append(kind=kind, payload=payload, item_id=item_id, harness="ticket",
                      idempotency_key=f"ticket-pass/{key_item}/{pass_id}/{role}")


def test_p1_a_live_pass_record_blocks_the_recovery(db: str) -> None:
    pass_record(db)
    assert_refused(db, f"reclaim-pass-live:{PASS}", **request(db))


def test_p2_a_released_pass_permits_it(db: str) -> None:
    pass_record(db)
    pass_released(db)
    assert reclaim(db, **request(db)).created is True


def test_p3_an_old_shape_pass_blocks_until_released(db: str) -> None:
    pass_claimed(db)
    assert_refused(db, f"reclaim-pass-live:{PASS}", **request(db))
    pass_released(db)
    assert reclaim(db, **request(db)).created is True


def test_p4_an_end_on_another_item_does_not_end_the_pass(db: str, monkeypatch) -> None:
    file_item(monkeypatch, db, OTHER_ITEM, spec=False)
    pass_record(db)
    pass_released(db, item_id=OTHER_ITEM, key_item=ITEM)
    assert_refused(db, f"reclaim-pass-live:{PASS}", **request(db))


def test_p5_an_end_of_another_pass_does_not_end_it(db: str) -> None:
    pass_record(db)
    pass_released(db, "fedcba9876543210")
    assert_refused(db, f"reclaim-pass-live:{PASS}", **request(db))


def test_p6_an_end_written_before_the_pass_began_does_not_end_it(db: str) -> None:
    pass_released(db)
    pass_record(db)
    assert_refused(db, f"reclaim-pass-live:{PASS}", **request(db))


def test_p7_of_two_passes_the_unreleased_one_is_named(db: str) -> None:
    pass_record(db, "aaaa")
    pass_record(db, "bbbb")
    pass_released(db, "aaaa")
    assert_refused(db, "reclaim-pass-live:bbbb", **request(db))


def test_p8_an_end_with_no_pass_is_not_evidence_of_anything(db: str) -> None:
    pass_released(db, "cccc")
    assert reclaim(db, **request(db)).created is True


UNREADABLE = {
    "unknown version": dict(text='ticket-pass:v3 {"holder":"seat-a@s1","host":"h","id":"0123456789abcdef","lease":"joined","started_at":"t"}'),
    "body not json": dict(text="ticket-pass:v2 {not json"),
    "body not an object": dict(text='ticket-pass:v2 ["0123456789abcdef"]'),
    "record names another pass": dict(text='ticket-pass:v2 {"holder":"seat-a@s1","host":"h","id":"aaaa","lease":"joined","started_at":"t"}'),
    "record misses a key": dict(text='ticket-pass:v2 {"holder":"seat-a@s1","host":"h","id":"0123456789abcdef","started_at":"t"}'),
    "record has an extra key": dict(text='ticket-pass:v2 {"holder":"seat-a@s1","host":"h","id":"0123456789abcdef","lease":"joined","started_at":"t","x":1}'),
    "unframed note under the record key": dict(text="just a note"),
    "framed note under an ordinary key": dict(key="ticket-write/item-1/5/abc/0"),
    "record key of another item": dict(key=f"ticket-pass/{OTHER_ITEM}/{PASS}/record"),
    "pass id not lowercase hex": dict(key=f"ticket-pass/{ITEM}/ABCD/record", text='ticket-pass:v2 {"holder":"h","host":"h","id":"ABCD","lease":"joined","started_at":"t"}'),
    "an extra key part": dict(key=f"ticket-pass/{ITEM}/{PASS}/record/x"),
    "a note under the claimed key": dict(key=f"ticket-pass/{ITEM}/{PASS}/claimed"),
    "an unknown role": dict(key=f"ticket-pass/{ITEM}/{PASS}/paused"),
}


@pytest.mark.parametrize("case", sorted(UNREADABLE), ids=[c.replace(" ", "_") for c in sorted(UNREADABLE)])
def test_p9_pass_evidence_off_the_protocol_is_unreadable(db: str, case: str) -> None:
    pass_record(db, **UNREADABLE[case])
    assert_refused(db, f"reclaim-pass-unreadable:{ITEM}", **request(db))


def test_p9_session_records_under_the_wrong_role_are_unreadable(db: str) -> None:
    pass_claimed(db, key=f"ticket-pass/{ITEM}/{PASS}/record")
    assert_refused(db, f"reclaim-pass-unreadable:{ITEM}", **request(db))


def test_p9_an_end_on_this_item_keyed_for_another_item_is_unreadable(db: str) -> None:
    pass_record(db)
    pass_released(db, key_item=OTHER_ITEM)
    assert_refused(db, f"reclaim-pass-unreadable:{ITEM}", **request(db))


def test_p9_an_end_of_the_wrong_kind_is_unreadable(db: str) -> None:
    pass_record(db)
    pass_released(db, kind="session.opened")
    assert_refused(db, f"reclaim-pass-unreadable:{ITEM}", **request(db))


def test_p10_the_recovery_writes_no_pass_record_and_releases_no_lease(db: str) -> None:
    pass_record(db)
    pass_released(db)
    before = all_events(db)
    result = reclaim(db, **request(db))
    written = all_events(db)[len(before):]
    assert kinds(written) == ["loop.reclaimed", "loop.done", "item.state"]
    assert not any((event.idempotency_key or "").startswith("ticket-") for event in written)
    assert result.claim.payload["status"] == "claimed"


# --- T: all three or none ------------------------------------------------------


@pytest.mark.parametrize("after", [1, 2, 3], ids=["after-marker", "after-done", "after-claim"])
def test_t1_a_failure_after_any_append_leaves_nothing(db: str, monkeypatch, after: int) -> None:
    fields = request(db)
    before = all_events(db)
    real = Ledger._append_once
    calls = {"n": 0}

    def failing(self, **kwargs):
        event = real(self, **kwargs)
        calls["n"] += 1
        if calls["n"] == after:
            raise RuntimeError("injected failure")
        return event

    monkeypatch.setattr(Ledger, "_append_once", failing)
    with open_ledger(db, create=False) as ledger:
        with pytest.raises(RuntimeError, match="injected failure"):
            reclaim_handoff(ledger, ITEM, **fields)
    monkeypatch.setattr(Ledger, "_append_once", real)
    assert all_events(db) == before, "no marker, done or claim may survive the failure"
    assert reclaim(db, **fields).created is True


# --- R: retries ------------------------------------------------------------------


def test_r1_an_exact_retry_returns_the_stored_recovery_and_writes_nothing(db: str) -> None:
    fields = request(db)
    first = reclaim(db, **fields)
    before = all_events(db)
    again = reclaim(db, **fields, actor="someone-else", session="other-session")
    assert again.created is False
    assert (again.reclaimed, again.done, again.claim) == (first.reclaimed, first.done, first.claim)
    assert all_events(db) == before


def test_r2_an_exact_retry_after_the_lease_ran_out_renews_nothing(db: str, clock) -> None:
    fields = request(db)
    first = reclaim(db, **fields)
    clock.at(lease.DEFAULT_TTL_SECONDS + 600)
    before = all_events(db)
    again = reclaim(db, **fields)
    assert again.created is False and again.claim == first.claim
    assert all_events(db) == before


def test_r3_an_exact_retry_after_a_takeover_writes_nothing(db: str, clock) -> None:
    fields = request(db)
    reclaim(db, **fields)
    clock.at(lease.DEFAULT_TTL_SECONDS + 600)
    taken = take(db, THIRD, n=2)
    assert taken.payload["prior_holder"] == SUCC
    before = all_events(db)
    assert reclaim(db, **fields).created is False
    assert all_events(db) == before
    with open_ledger(db, create=False) as ledger:
        assert loop._latest_claim(ledger._conn, ITEM)["holder"] == THIRD


def test_r4_an_exact_retry_after_a_new_hand_off_writes_nothing(db: str) -> None:
    fields = request(db)
    reclaim(db, **fields)
    fresh = hand_off_v2(db, "build-2", holder=SUCC)
    before = all_events(db)
    assert reclaim(db, **fields).created is False
    assert all_events(db) == before
    with open_ledger(db, create=False) as ledger:
        assert open_hand_off(ledger, ITEM) == fresh


def test_r5_an_exact_retry_checks_nothing_current(db: str) -> None:
    fields = request(db)
    reclaim(db, **fields)
    (Path(db).parent / "config.json").write_text('{"claim_ttl_seconds": 0}', encoding="utf-8")
    assert reclaim(db, **fields).created is False


CHANGED = [
    {"holder": THIRD},
    {"claim_event_id": 1},
    {"prior_holder": "seat-z@s9"},
    {"declared_by": "operator-2"},
    {"authority": "successor", "declared_by": SUCC},
    {"evidence": "another reference"},
]


@pytest.mark.parametrize("change", CHANGED, ids=[next(iter(c)) for c in CHANGED])
def test_r6_a_changed_retry_conflicts_and_writes_nothing(db: str, change: dict) -> None:
    fields = request(db)
    reclaim(db, **fields)
    assert_refused(db, "idempotency_key", error=IdempotencyConflict, **{**fields, **change})


def _restored(tmp_path: Path, tail: list[tuple]) -> str:
    """A restored chain: ITEM handed off by PRED (intent 3, rewrite 4), then ``tail``."""
    export_dir = tmp_path / "export"
    write_export(export_dir, [
        ("item.created", 1, {"type": "ticket", "title": "Restored"}, "created", "pytest"),
        ("loop.managed", 1, {}, "manage", None),
        ("loop.intent", 2, hand_off_payload(), f"loop:{ITEM}:build:build-1:intent", None),
        ("item.state", 1, {"status": "claimed", "claim": PRED}, f"loop:{ITEM}:build:build-1:hand-off", None),
        ("loop.reclaimed", 1, marker_payload(3, 4), f"loop:{ITEM}:reclaim:3", "roll"),
        *tail,
    ])
    dest = str(tmp_path / "restored.db")
    restore_export(export_dir, dest, system="home", accept_loop_events=True)
    return dest


DONE = ("loop.done", 1, {"operation": "build", "operation_id": "build-1", "intent_event_id": 3,
                         "outcome": "seat-lost", "source": "event:5"}, f"loop:{ITEM}:build:build-1:done", "roll")
LEASE = ("item.state", 1, {"status": "claimed", "claim": SUCC, "prior_holder": PRED,
                           "expires_at": "2026-01-01T04:00:00.000Z"}, f"loop:{ITEM}:reclaim:3:claim", "roll")

INCOMPLETE = {
    "marker alone": [],
    "no successor claim": [DONE],
    "a done of another outcome": [(*DONE[:2], {**DONE[2], "outcome": "handback"}, *DONE[3:]), LEASE],
    "a done citing another event": [(*DONE[:2], {**DONE[2], "source": "event:4"}, *DONE[3:]), LEASE],
    "a claim naming another holder": [DONE, (*LEASE[:2], {**LEASE[2], "claim": THIRD}, *LEASE[3:])],
    "a claim naming another predecessor": [DONE, (*LEASE[:2], {**LEASE[2], "prior_holder": THIRD}, *LEASE[3:])],
    "an unstamped claim": [DONE, (*LEASE[:2], {k: v for k, v in LEASE[2].items() if k != "expires_at"}, *LEASE[3:])],
    "a claim that is not next": [DONE, ("item.note", 1, {"text": "between"}, "between", "pytest"), LEASE],
}


@pytest.mark.parametrize("case", sorted(INCOMPLETE), ids=[c.replace(" ", "_") for c in sorted(INCOMPLETE)])
def test_r7_an_incomplete_restored_recovery_refuses_the_retry(tmp_path: Path, case: str) -> None:
    dest = _restored(tmp_path, INCOMPLETE[case])
    assert_refused(dest, "reclaim-record-incomplete", **request(dest, 3, claim_event_id=4))


def test_r7_a_complete_restored_recovery_returns_on_retry(tmp_path: Path) -> None:
    dest = _restored(tmp_path, [DONE, LEASE])
    before = all_events(dest)
    again = reclaim(dest, **request(dest, 3, claim_event_id=4))
    assert again.created is False
    assert (again.reclaimed.id, again.done.id, again.claim.id) == (5, 6, 7)
    assert all_events(dest) == before


# --- X: concurrent writers ---------------------------------------------------


RACER = r"""
import json, sys, time
from pathlib import Path
from ledger import open_ledger
from ledger.loop import reclaim_handoff
from ledger.store import IdempotencyConflict

db, fields, go = sys.argv[1], json.loads(sys.argv[2]), Path(sys.argv[3])
deadline = time.monotonic() + 30
while not go.exists() and time.monotonic() < deadline:
    time.sleep(0.001)
with open_ledger(db, create=False) as ledger:
    try:
        result = reclaim_handoff(ledger, "item-1", **fields)
        print("created " + fields["holder"] if result.created else "retried")
    except IdempotencyConflict:
        print("conflict")
"""


def race(tmp_path: Path, db: str, requests: list[dict]) -> list[str]:
    script = tmp_path / "racer.py"
    script.write_text(RACER, encoding="utf-8")
    go = tmp_path / "go"
    env = dict(os.environ, PYTHONPATH=str(_LIB) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), db, json.dumps(fields), str(go)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for fields in requests
    ]
    go.write_text("go\n", encoding="utf-8")
    outcomes = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=60)
        assert proc.returncode == 0, stderr
        outcomes.append(stdout.strip())
    return outcomes


def reclaims(db: str) -> list[Event]:
    return [event for event in all_events(db) if event.kind == "loop.reclaimed"]


def test_x1_two_writers_racing_different_successors_leave_one_recovery(db: str, tmp_path: Path) -> None:
    fields = request(db)
    before = len(all_events(db))
    outcomes = race(tmp_path, db, [{**fields, "holder": SUCC}, {**fields, "holder": THIRD}])
    assert sorted(outcome.split(" ")[0] for outcome in outcomes) == ["conflict", "created"]
    winner = next(outcome.split(" ")[1] for outcome in outcomes if outcome.startswith("created"))
    written = all_events(db)[before:]
    assert kinds(written) == ["loop.reclaimed", "loop.done", "item.state"]
    assert written[0].payload["holder"] == winner and written[2].payload["claim"] == winner


def test_x2_two_writers_racing_one_recovery_write_it_once(db: str, tmp_path: Path) -> None:
    fields = request(db)
    before = len(all_events(db))
    outcomes = race(tmp_path, db, [fields, fields])
    assert sorted(outcomes) == [f"created {SUCC}", "retried"]
    assert kinds(all_events(db)[before:]) == ["loop.reclaimed", "loop.done", "item.state"]


def test_x3_the_guard_reads_under_the_write_lock(db: str, monkeypatch) -> None:
    """A second connection's recovery waits for the first one's guard and loses."""
    fields = request(db)
    real = loop._latest_claim
    spawned = threading.Event()
    state: dict = {}

    def second() -> None:
        try:
            with open_ledger(db, create=False) as other:
                reclaim_handoff(other, ITEM, **{**fields, "holder": THIRD})
            state["second"] = "created"
        except IdempotencyConflict:
            state["second"] = "conflict"
        except Exception as exc:  # reported in the assertion below
            state["second"] = repr(exc)

    def probe(conn, item_id):
        if not spawned.is_set():
            spawned.set()
            thread = threading.Thread(target=second)
            thread.start()
            thread.join(timeout=1.0)
            state["during_guard"] = state.get("second")
            state["thread"] = thread
        return real(conn, item_id)

    monkeypatch.setattr(loop, "_latest_claim", probe)
    with open_ledger(db, create=False) as first:
        result = reclaim_handoff(first, ITEM, **fields)
    state["thread"].join(timeout=30)
    assert state["during_guard"] is None, "a second writer finished while the first was inside its guard"
    assert result.created is True and result.claim.payload["claim"] == SUCC
    assert state["second"] == "conflict"
    assert [event.payload["holder"] for event in reclaims(db)] == [SUCC]


# --- W: a done must match the target exactly -------------------------------------


def test_w1_a_done_on_another_item_or_operation_does_not_retire_the_target(bare: str, monkeypatch) -> None:
    file_item(monkeypatch, bare, OTHER_ITEM, spec=False)
    take(bare, PRED, item_id=OTHER_ITEM)
    with open_ledger(bare, create=False) as ledger:
        claim_operation(ledger, OTHER_ITEM, "build", "op-1", holder=PRED)
        finish_operation(ledger, OTHER_ITEM, "build", "op-1", outcome="done")
    target = hand_off_v1(bare)
    with open_ledger(bare, create=False) as ledger:
        claim_operation(ledger, ITEM, "review", "op-1")
        finish_operation(ledger, ITEM, "review", "op-1", outcome="done")
    assert reclaim(bare, **request(bare, target)).created is True


def test_w1_the_same_holds_for_a_version_2_hand_off(db: str, monkeypatch) -> None:
    file_item(monkeypatch, db, OTHER_ITEM, spec=False)
    take(db, PRED, item_id=OTHER_ITEM)
    with open_ledger(db, create=False) as ledger:
        claim_operation(ledger, OTHER_ITEM, "build", "build-1", holder=PRED)
        finish_operation(ledger, OTHER_ITEM, "build", "build-1", outcome="done")
        claim_operation(ledger, ITEM, "review", "build-1")
        finish_operation(ledger, ITEM, "review", "build-1", outcome="done")
    assert reclaim(db, **request(db)).created is True


# --- G: what stays as it is ------------------------------------------------------


def test_g1_a_runner_claim_still_expires_and_is_taken_over(bare: str, clock) -> None:
    hand_off_v1(bare, session=False)
    clock.at(lease.DEFAULT_TTL_SECONDS + 60)
    taken = take(bare, THIRD, n=2)
    assert taken.payload["prior_holder"] == PRED
    assert not reclaims(bare)


def test_g2_an_unstamped_session_claim_is_never_taken_over_by_time(db: str, clock) -> None:
    before = all_events(db)
    clock.at(30 * DAY)
    with pytest.raises(lease.ClaimRefused) as caught:
        take(db, THIRD, n=2)
    assert caught.value.code == "claimed"
    assert all_events(db) == before


def test_g3_the_orderly_seat_roll_needs_no_recovery(db: str, monkeypatch) -> None:
    with open_ledger(db, create=False) as ledger:
        finish_operation(ledger, ITEM, "build", "build-1", outcome="handback", holder=PRED)
    # The hand-back restamped PRED's lease: nobody takes it before the release.
    before = all_events(db)
    with pytest.raises(lease.ClaimRefused):
        take(db, SUCC, n=2)
    assert all_events(db) == before
    ok(monkeypatch, db, "state", ITEM, "--status", "open")
    taken = take(db, SUCC, n=2)
    assert "prior_holder" not in taken.payload
    assert hand_off_v2(db, "build-2", holder=SUCC).payload_version == 2
    assert not reclaims(db)


# --- CLI ---------------------------------------------------------------------------


def cli(db: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    base.update({"PYTHONPATH": str(_LIB), "LEDGER_HARNESS": "cli-test"})
    base.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "ledger", "--db", db, *args],
        env=base, capture_output=True, text=True, timeout=60,
    )


def cli_args(fields: dict, *, declare: bool = True) -> list[str]:
    args = [
        "reclaim-handoff", ITEM,
        "--intent", str(fields["intent_event_id"]), "--claim", str(fields["claim_event_id"]),
        "--from", fields["prior_holder"], "--to", fields["holder"],
        "--authority", fields["authority"], "--declared-by", fields["declared_by"],
        "--evidence", fields["evidence"],
    ]
    return args + (["--declare-seat-lost"] if declare else [])


def test_cli1_the_verb_recovers_against_a_real_database(db: str) -> None:
    fields = request(db)
    done = cli(db, "--json", *cli_args(fields), env={"LEDGER_OPERATOR": "andrew", "LEDGER_SESSION": "s-9"})
    assert done.returncode == 0, done.stderr
    data = json.loads(done.stdout)["data"]
    assert data["created"] is True
    assert data["reclaimed"]["payload"] == marker_payload(fields["intent_event_id"], fields["claim_event_id"])
    assert (data["reclaimed"]["actor"], data["reclaimed"]["harness"], data["reclaimed"]["session"]) == ("andrew", "cli-test", "s-9")
    assert data["done"]["payload"]["outcome"] == "seat-lost"
    assert data["claim"]["payload"]["claim"] == SUCC
    again = cli(db, *cli_args(fields))
    assert again.returncode == 0, again.stderr
    ids = ",".join(str(data[name]["id"]) for name in ("reclaimed", "done", "claim"))
    assert again.stdout == f"already reclaimed\t{ITEM}\tfrom={PRED}\tto={SUCC}\tevents={ids}\n"


def test_cli2_the_verb_refuses_without_the_declaration_and_writes_nothing(db: str, tmp_path: Path) -> None:
    fields = request(db)
    before = all_events(db)
    refused = cli(db, "--json", *cli_args(fields, declare=False))
    assert refused.returncode == 3
    error = json.loads(refused.stdout)["error"]
    assert (error["code"], error["message"]) == ("validation", "reclaim-declaration-required")
    refused = cli(db, *[a for a in cli_args(fields) if a not in ("--to", SUCC)])
    assert refused.returncode == 3 and refused.stderr.strip() == "missing-key:holder"
    refused = cli(db, *cli_args({**fields, "intent_event_id": "abc"}))
    assert refused.returncode == 3 and refused.stderr.strip() == "bad-type:intent_event_id"
    refused = cli(db, *cli_args(fields), env={"LEDGER_HARNESS": ""})
    assert refused.returncode == 3 and "LEDGER_HARNESS" in refused.stderr
    assert all_events(db) == before
    missing = tmp_path / "nowhere.db"
    refused = cli(str(missing), *cli_args(fields))
    assert refused.returncode == 1 and not missing.exists()


def test_cli3_export_and_restore_keep_the_recovery_and_need_the_flag(db: str, tmp_path: Path, monkeypatch) -> None:
    fields = request(db)
    reclaim(db, **fields)
    ok(monkeypatch, db, "verify")
    export_dir = tmp_path / "export"
    with open_ledger(db, create=False) as ledger:
        export_events(ledger, export_dir)
        original = ledger.events()
    refused = tmp_path / "refused.db"
    with pytest.raises(LedgerError, match="--accept-loop-events"):
        restore_export(export_dir, refused, system="home")
    assert not refused.exists()
    dest = str(tmp_path / "restored.db")
    restore_export(export_dir, dest, system="home", accept_loop_events=True)
    with open_ledger(dest, create=False) as restored:
        restored.verify()
        assert restored.events()[: len(original)] == original
    assert reclaim(dest, **fields).created is False


def test_cli3_a_recovery_marker_alone_still_needs_the_flag(tmp_path: Path) -> None:
    export_dir = tmp_path / "marker-only"
    write_export(export_dir, [
        ("item.created", 1, {"type": "ticket", "title": "Forged"}, "created", "pytest"),
        ("loop.reclaimed", 1, marker_payload(1, 1), f"loop:{ITEM}:reclaim:1", "forger"),
    ])
    dest = tmp_path / "forged.db"
    with pytest.raises(LedgerError, match="--accept-loop-events"):
        restore_export(export_dir, dest, system="home")
    assert not dest.exists()


def test_cli4_card_and_resume_show_the_successor_and_keep_the_stop(db: str, monkeypatch) -> None:
    ok(monkeypatch, db, "append", "--kind", "item.stopped", "--item", ITEM,
       "--payload", json.dumps({"point": "built half", "phase": "build"}))
    reclaim(db, **request(db))
    card = json.loads(ok(monkeypatch, db, "--json", "card", ITEM))["data"]
    assert (card["status"], card["claim"]) == ("claimed", SUCC)
    assert (card["spec_version"], card["spec_claim_version"]) == (1, 1)
    [entry] = json.loads(ok(monkeypatch, db, "--json", "resume", "--item", ITEM))["data"]["items"]
    assert entry["status"] == "claimed"
    assert (entry["stop"]["point"], entry["stop"]["phase"]) == ("built half", "build")


def test_cli5_doctor_names_the_recovery_for_a_stale_hand_off_and_prints_no_command(
    tmp_path: Path, monkeypatch, clock
) -> None:
    path = str(tmp_path / "ledger.db")
    clock.at(-8 * DAY)
    file_item(monkeypatch, path)
    file_item(monkeypatch, path, OTHER_ITEM, spec=False)
    take(path)
    intent = hand_off_v2(path)
    ok(monkeypatch, path, "state", OTHER_ITEM, "--status", "claimed", "--owner", "old-seat")
    clock.real()
    data = json.loads(ok(monkeypatch, path, "--json", "doctor"))["data"]
    rows = {row["item_id"]: row for row in data["stale_claims"]["items"]}
    assert rows[ITEM]["release_command"] is None
    assert rows[ITEM]["session_hand_off"] == {
        "intent_event_id": intent.id, "operation": "build", "operation_id": "build-1",
    }
    assert rows[OTHER_ITEM]["session_hand_off"] is None
    assert "state --status open" in rows[OTHER_ITEM]["release_command"]
    text = ok(monkeypatch, path, "doctor")
    [line] = [ln for ln in text.splitlines() if ln.startswith(f"stale claim: {ITEM}\t")]
    assert "state --status open" not in line
    assert line.endswith(
        f"\tsession hand-off: intent event {intent.id}; recover it with reclaim-handoff "
        "--declare-seat-lost, naming the successor and the evidence (ledger reclaim-handoff --help)"
    )
    reclaim(path, **request(path))
    data = json.loads(ok(monkeypatch, path, "--json", "doctor"))["data"]
    assert [row["item_id"] for row in data["stale_claims"]["items"]] == [OTHER_ITEM]


def test_cli5_a_version_1_session_hand_off_gets_the_same_advice(tmp_path: Path, monkeypatch, clock) -> None:
    path = str(tmp_path / "ledger.db")
    clock.at(-8 * DAY)
    file_item(monkeypatch, path)
    take(path)
    intent = hand_off_v1(path)
    clock.real()
    [row] = json.loads(ok(monkeypatch, path, "--json", "doctor"))["data"]["stale_claims"]["items"]
    assert row["release_command"] is None
    assert row["session_hand_off"]["intent_event_id"] == intent.id


# --- M: session steps of other operations; merge steps; seat-lost merge dones ----


@pytest.mark.parametrize("operation", ["review", "decision", "ping"])
def test_m1_a_version_1_session_step_of_another_operation_is_recovered(bare: str, operation: str) -> None:
    intent = hand_off_v1(bare, operation=operation)
    result = reclaim(bare, **request(bare, intent))
    assert result.created is True
    assert result.done.payload == {
        "operation": operation, "operation_id": "op-1", "intent_event_id": intent.id,
        "outcome": "seat-lost", "source": f"event:{result.reclaimed.id}",
    }
    assert result.claim.payload["claim"] == SUCC
    with open_ledger(bare, create=False) as ledger:
        assert operation_state(ledger, ITEM, operation, "op-1").state == "finished"


def test_m2_a_merge_session_step_is_never_recovered(bare: str) -> None:
    intent = hand_off_v1(bare, operation="merge")
    assert_refused(bare, "reclaim-merge-step", **request(bare, intent))
    with open_ledger(bare, create=False) as ledger:
        assert operation_state(ledger, ITEM, "merge", "op-1").state == "started"


def _seat_lost_merge(db: str) -> Event:
    """A merge loop.done whose outcome is seat-lost, as a restored chain or an
    in-process caller could write one (the recovery never does)."""
    with open_ledger(db, create=False) as ledger:
        claim_operation(ledger, ITEM, "merge", "merge-1", holder=PRED)
        return finish_operation(ledger, ITEM, "merge", "merge-1", outcome="seat-lost")


def _real_merge(db: str) -> Event:
    with open_ledger(db, create=False) as ledger:
        claim_operation(ledger, ITEM, "merge", "merge-2", holder=PRED)
        return finish_operation(ledger, ITEM, "merge", "merge-2", outcome="merged")


def test_m3_close_item_takes_no_seat_lost_merge_done(bare: str) -> None:
    lost = _seat_lost_merge(bare)
    before = all_events(bare)
    with open_ledger(bare, create=False) as ledger:
        with pytest.raises(ValidationError, match="^loop-merge-done-required$"):
            close_item(ledger, ITEM, lost.id, idempotency_key="close-lost")
    assert all_events(bare) == before
    merged = _real_merge(bare)
    with open_ledger(bare, create=False) as ledger:
        close_item(ledger, ITEM, merged.id, idempotency_key="close-merged")
        assert ledger.item(ITEM)["status"] == "closed"


def _settle(db: str, source_event_id: int, key: str) -> Event:
    with open_ledger(db, create=False) as ledger:
        return ledger.append(
            kind="decision.recorded",
            payload={
                "who": "the owner", "who_kind": "person", "standing": "settled",
                "effect": "ship it", "tags": [{"type": "ticket", "id": ITEM}],
                "verbatim": "ship it", "phase": "close", "source_event_id": source_event_id,
            },
            item_id=ITEM,
            idempotency_key=key,
        )


def test_m4_a_seat_lost_merge_done_settles_no_decision(bare: str) -> None:
    lost = _seat_lost_merge(bare)
    before = all_events(bare)
    with pytest.raises(ValidationError, match="^source-event-cannot-settle:loop.done$"):
        _settle(bare, lost.id, "settle-lost")
    assert all_events(bare) == before
    merged = _real_merge(bare)
    assert _settle(bare, merged.id, "settle-merged").payload["source_event_id"] == merged.id
    rows = {
        name: {"kind": "loop.done", "provenance_class": None, "payload": json.dumps(payload)}
        for name, payload in (
            ("lost", {"operation": "merge", "outcome": "seat-lost"}),
            ("merged", {"operation": "merge", "outcome": "merged"}),
            ("bare", {"operation": "merge"}),
        )
    }
    assert [decisions.is_settling(rows[name]) for name in ("lost", "merged", "bare")] == [False, True, True]


def test_cli6_one_items_unreadable_hand_off_marks_only_its_row(tmp_path: Path, monkeypatch, clock) -> None:
    path = str(tmp_path / "ledger.db")
    clock.at(-8 * DAY)
    file_item(monkeypatch, path)
    file_item(monkeypatch, path, OTHER_ITEM, spec=False)
    take(path)
    hand_off_v2(path)
    ok(monkeypatch, path, "state", OTHER_ITEM, "--status", "claimed", "--owner", "old-seat")
    clock.real()
    real = loop.session_hand_off

    def failing(conn, item_id):
        if item_id == ITEM:
            raise sqlite3.OperationalError("injected read failure")
        return real(conn, item_id)

    monkeypatch.setattr(loop, "session_hand_off", failing)
    stale = json.loads(ok(monkeypatch, path, "--json", "doctor"))["data"]["stale_claims"]
    assert stale["status"] == "ok"
    rows = {row["item_id"]: row for row in stale["items"]}
    failed = rows[ITEM]
    assert (failed["status"], failed["reason"]) == ("unknown", "OperationalError: injected read failure")
    assert (failed["release_command"], failed["session_hand_off"]) == (None, None)
    assert "status" not in rows[OTHER_ITEM]
    assert "state --status open" in rows[OTHER_ITEM]["release_command"]
    text = ok(monkeypatch, path, "doctor")
    [line] = [ln for ln in text.splitlines() if ln.startswith(f"stale claim: {ITEM}\t")]
    assert line.endswith(
        "\tunknown: could not read whether it is a session hand-off "
        "(OperationalError: injected read failure); see ledger doctor --json"
    )
    [other] = [ln for ln in text.splitlines() if ln.startswith(f"stale claim: {OTHER_ITEM}\t")]
    assert "\trelease: " in other
