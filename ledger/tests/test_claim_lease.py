"""A claim carries a lease (SL37).

The clock seam is ``ledger.lease.now``: the ``clock`` fixture replaces it, so a
test moves time by choosing the instant a claim is recorded at. Nothing here
sleeps. The SQL views read the database's own clock, so "advance past expiry"
means the claim was recorded far enough in the past.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from ledger import lease, loop, store
from ledger.store import open_ledger

from test_cli import invoke, seed

HOUR = 3600


@pytest.fixture
def clock(monkeypatch):
    real = lease.now
    real_stamp = store._now

    class Clock:
        def at(self, seconds_from_now: int) -> None:
            # Both the lease logic and the events' recorded_at see the moved clock.
            moved = lambda: real() + timedelta(seconds=seconds_from_now)  # noqa: E731
            monkeypatch.setattr(lease, "now", moved)
            monkeypatch.setattr(store, "_now", lambda: lease.stamp(moved()))

        def real(self) -> None:
            monkeypatch.setattr(lease, "now", real)
            monkeypatch.setattr(store, "_now", real_stamp)

    return Clock()


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "ledger.db")
    seed(monkeypatch, path, "it-1")
    return path


def run(monkeypatch, db, agent, *argv):
    code, out, err = invoke(
        monkeypatch, ["--json", "--db", db, *argv], env={"LEDGER_AGENT": agent}
    )
    return code, (json.loads(out) if out.strip() else None), err


def ready_ids(db: str) -> list[str]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [r[0] for r in conn.execute("SELECT item_id FROM v_ready")]
    finally:
        conn.close()


def doctor_expired(monkeypatch, db: str) -> list[dict]:
    code, body, err = run(monkeypatch, db, "ops", "doctor")
    assert code == 0, err
    return body["data"]["expired_claims"]["items"]


def test_claim_carries_default_ttl_expiry(db, monkeypatch, clock) -> None:
    code, body, err = run(monkeypatch, db, "alice", "claim", "it-1")
    assert code == 0, err
    stamp = body["data"]["event"]["payload"]["expires_at"]
    recorded = lease.parse_stamp(body["data"]["event"]["recorded_at"])
    assert abs((lease.parse_stamp(stamp) - recorded).total_seconds() - lease.DEFAULT_TTL_SECONDS) < 5


def test_configured_ttl_and_flag(db, monkeypatch, tmp_path) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"claim_ttl_seconds": 600}))
    code, body, err = run(monkeypatch, db, "alice", "claim", "it-1")
    assert code == 0, err
    p = body["data"]["event"]
    assert abs((lease.parse_stamp(p["payload"]["expires_at"]) - lease.parse_stamp(p["recorded_at"])).total_seconds() - 600) < 5
    code, body, _ = run(monkeypatch, db, "alice", "claim", "it-1", "--ttl", "60")
    p = body["data"]["event"]
    assert abs((lease.parse_stamp(p["payload"]["expires_at"]) - lease.parse_stamp(p["recorded_at"])).total_seconds() - 60) < 5
    (tmp_path / "config.json").write_text(json.dumps({"claim_ttl_seconds": 0}))
    code, body, _ = run(monkeypatch, db, "alice", "claim", "it-1")
    assert code == 2 and "claim_ttl_seconds" in body["error"]["message"]


def test_expired_claim_is_taken_over_and_names_prior_holder(db, monkeypatch, clock) -> None:
    clock.at(-5 * HOUR)
    assert run(monkeypatch, db, "alice", "claim", "it-1")[0] == 0
    clock.real()
    assert ready_ids(db) == ["it-1"]
    code, body, err = run(monkeypatch, db, "bob", "claim", "it-1")
    assert code == 0, err
    payload = body["data"]["event"]["payload"]
    assert payload["prior_holder"] == "alice"
    assert payload["owner"] == "bob"
    # bob's fresh claim is live: alice is refused and the row is not ready.
    assert ready_ids(db) == []
    code, body, _ = run(monkeypatch, db, "alice", "claim", "it-1")
    assert code == 3 and body["error"]["code"] == "claimed"


def test_live_claim_refuses_a_second_actor(db, monkeypatch, clock) -> None:
    assert run(monkeypatch, db, "alice", "claim", "it-1")[0] == 0
    code, body, _ = run(monkeypatch, db, "bob", "claim", "it-1")
    assert code == 3 and "already claimed by alice" in body["error"]["message"]
    assert ready_ids(db) == []


def test_progress_extends_the_lease(db, monkeypatch, clock) -> None:
    clock.at(-5 * HOUR)
    assert run(monkeypatch, db, "alice", "claim", "it-1")[0] == 0
    clock.real()
    assert ready_ids(db) == ["it-1"]  # expired, as it stands
    lg = open_ledger(db)
    try:
        loop.record_progress(lg, "it-1", "still building", idempotency_key="p-1", actor="alice")
    finally:
        lg.close()
    assert ready_ids(db) == []
    code, body, _ = run(monkeypatch, db, "bob", "claim", "it-1")
    assert code == 3 and "already claimed by alice" in body["error"]["message"]
    assert doctor_expired(monkeypatch, db) == []


def test_progress_before_the_claim_does_not_extend_it(db, monkeypatch, clock) -> None:
    lg = open_ledger(db)
    try:
        loop.record_progress(lg, "it-1", "earlier", idempotency_key="p-0", actor="alice")
    finally:
        lg.close()
    clock.at(-5 * HOUR)
    assert run(monkeypatch, db, "alice", "claim", "it-1")[0] == 0
    clock.real()
    assert ready_ids(db) == ["it-1"]


def test_doctor_lists_expired_claims_with_holder_and_age_and_never_fails(db, monkeypatch, clock) -> None:
    seed(monkeypatch, db, "it-2", "synthetic-two")
    assert run(monkeypatch, db, "bob", "claim", "it-2")[0] == 0  # live
    clock.at(-5 * HOUR)
    assert run(monkeypatch, db, "alice", "claim", "it-1")[0] == 0
    clock.real()
    expired = doctor_expired(monkeypatch, db)
    assert [row["item_id"] for row in expired] == ["it-1"]
    row = expired[0]
    assert row["holder"] == "alice"
    assert 4.9 * HOUR < row["age_seconds"] < 5.1 * HOUR
    assert 0.9 * HOUR < row["expired_for_seconds"] < 1.1 * HOUR
    code, out, err = invoke(monkeypatch, ["--db", db, "doctor"])
    assert code == 0
    assert "expired claim: it-1\tholder=alice" in out
    assert "it-2" not in out


def test_doctor_never_lists_a_live_claim(db, monkeypatch, clock) -> None:
    assert run(monkeypatch, db, "alice", "claim", "it-1")[0] == 0
    assert doctor_expired(monkeypatch, db) == []


def test_claim_without_expiry_never_expires(db, monkeypatch, clock) -> None:
    """A claim recorded before leases existed has no ``expires_at``: it never expires."""
    lg = open_ledger(db)
    try:
        lg.append(
            kind="item.state",
            item_id="it-1",
            payload={"status": "claimed", "owner": "alice", "claim": "alice"},
            idempotency_key="legacy-claim",
            harness="fixture",
        )
    finally:
        lg.close()
    clock.at(365 * 24 * HOUR)
    assert ready_ids(db) == []
    assert doctor_expired(monkeypatch, db) == []
    code, body, _ = run(monkeypatch, db, "bob", "claim", "it-1")
    assert code == 3 and "already claimed by alice" in body["error"]["message"]
    # Progress on such a claim does not start a lease either.
    lg = open_ledger(db)
    try:
        loop.record_progress(lg, "it-1", "beat", idempotency_key="p-9", ttl_seconds=1)
    finally:
        lg.close()
    assert ready_ids(db) == []


def test_holder_leasing_a_plain_claim_on_an_unmanaged_item_gets_an_expiry(db, monkeypatch, clock) -> None:
    """The loop never managed this item, so the holder's re-claim is a lease, not a loop claim."""
    lg = open_ledger(db)
    try:
        lg.append(
            kind="item.state",
            item_id="it-1",
            payload={"status": "claimed", "owner": "alice", "claim": "alice"},
            idempotency_key="plain-claim",
            harness="fixture",
        )
    finally:
        lg.close()
    clock.at(-5 * HOUR)
    code, body, err = run(monkeypatch, db, "alice", "claim", "it-1")
    assert code == 0, err
    assert "expires_at" in body["data"]["event"]["payload"]
    clock.real()
    assert ready_ids(db) == ["it-1"]
    code, body, err = run(monkeypatch, db, "bob", "claim", "it-1")
    assert code == 0, err
    assert body["data"]["event"]["payload"]["prior_holder"] == "alice"


def test_expired_claim_holds_no_capacity(db, monkeypatch, clock) -> None:
    seed(monkeypatch, db, "it-2", "synthetic-two")
    clock.at(-5 * HOUR)
    assert run(monkeypatch, db, "alice", "claim", "it-1", "--capacity", "1")[0] == 0
    clock.real()
    code, _, err = run(monkeypatch, db, "bob", "claim", "it-2", "--capacity", "1")
    assert code == 0, err


def test_migration_adds_views_only() -> None:
    sql = (Path(lease.__file__).parent / "migrations" / "0006_claim_lease.sql").read_text()
    code = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    assert "CREATE TABLE" not in code and "ALTER TABLE" not in code
    assert "DROP TABLE" not in code and "INSERT" not in code.upper()


def test_expired_claim_is_ready_and_live_claim_is_not(db, monkeypatch, clock) -> None:
    seed(monkeypatch, db, "it-2", "synthetic-two")
    assert run(monkeypatch, db, "bob", "claim", "it-2")[0] == 0
    clock.at(-5 * HOUR)
    assert run(monkeypatch, db, "alice", "claim", "it-1")[0] == 0
    clock.real()
    assert ready_ids(db) == ["it-1"]


def test_claim_changed_between_the_reads_is_refused(db, monkeypatch, clock) -> None:
    clock.at(-5 * HOUR)
    assert run(monkeypatch, db, "alice", "claim", "it-1")[0] == 0
    clock.real()
    real = lease.current_lease
    calls = []

    def flaky(conn, item_id):
        calls.append(item_id)
        # The pre-lock read sees no lease; by the in-lock read the claim has expired.
        return None if len(calls) == 1 else real(conn, item_id)

    monkeypatch.setattr(lease, "current_lease", flaky)
    code, body, _ = run(monkeypatch, db, "bob", "claim", "it-1")
    assert code == 3 and body["error"]["code"] == "claim_changed"
    assert len(calls) == 2


@pytest.mark.parametrize("kind,payload", [
    ("item.state", {"status": "claimed", "claim": "x", "expires_at": "2026-02-30T00:00:00.000Z"}),
    ("item.progress", {"text": "beat", "expires_at": "2026-13-01T00:00:00.000Z"}),
])
def test_impossible_stamp_is_refused_at_append(db, monkeypatch, kind, payload) -> None:
    code, _body, _ = run(monkeypatch, db, "x", "append", "--kind", kind, "--item", "it-1",
                         "--payload", json.dumps(payload))
    assert code != 0
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("SELECT count(*) FROM events WHERE kind = ?", (kind,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_preexisting_bad_stamp_does_not_crash_capacity_or_hide_doctor_rows(db, monkeypatch, clock) -> None:
    from ledger import schema

    seed(monkeypatch, db, "it-2", "synthetic-two")
    seed(monkeypatch, db, "it-3", "synthetic-three")
    clock.at(-5 * HOUR)
    assert run(monkeypatch, db, "alice", "claim", "it-1")[0] == 0
    clock.real()
    monkeypatch.setitem(schema._LEASE_STAMP, "calendar_stamp", False)  # a row written before the check
    lg = open_ledger(db)
    try:
        lg.append(kind="item.state", item_id="it-2", idempotency_key="bad", harness="fixture",
                  payload={"status": "claimed", "claim": "mallory", "expires_at": "2026-02-30T00:00:00.000Z"})
    finally:
        lg.close()
    monkeypatch.setitem(schema._LEASE_STAMP, "calendar_stamp", True)
    code, _body, err = run(monkeypatch, db, "bob", "claim", "it-3", "--capacity", "5")
    assert code == 0, err
    items = doctor_expired(monkeypatch, db)
    assert {row["item_id"] for row in items} == {"it-1", "it-2"}
    unreadable = next(r for r in items if r["item_id"] == "it-2")
    assert unreadable["unreadable_stamp"] is True
    # No measured age exists for an unreadable stamp: null, never a fake 0.
    assert unreadable["age_seconds"] is None and unreadable["expired_for_seconds"] is None
    readable = next(r for r in items if r["item_id"] == "it-1")
    assert readable["age_seconds"] >= 5 * HOUR - 60


@pytest.mark.parametrize("ttl", [lease.MAX_TTL_SECONDS + 1, 999999999999])
def test_ttl_above_the_cap_is_a_usage_error(db, monkeypatch, tmp_path, ttl) -> None:
    code, body, _ = run(monkeypatch, db, "alice", "claim", "it-1", "--ttl", str(ttl))
    assert code == 2 and "--ttl" in body["error"]["message"]
    (tmp_path / "config.json").write_text(json.dumps({"claim_ttl_seconds": ttl}))
    code, body, _ = run(monkeypatch, db, "alice", "claim", "it-1")
    assert code == 2 and "claim_ttl_seconds" in body["error"]["message"]
    code, _, _ = run(monkeypatch, db, "alice", "claim", "it-1", "--ttl", str(lease.MAX_TTL_SECONDS))
    assert code == 0


def test_doctor_text_renders_an_unreadable_stamp_as_unreadable(db, monkeypatch) -> None:
    from ledger import schema

    monkeypatch.setitem(schema._LEASE_STAMP, "calendar_stamp", False)
    lg = open_ledger(db)
    try:
        lg.append(kind="item.state", item_id="it-1", idempotency_key="bad", harness="fixture",
                  payload={"status": "claimed", "claim": "alice", "expires_at": "2026-02-30T00:00:00.000Z"})
    finally:
        lg.close()
    code, out, _ = invoke(monkeypatch, ["--db", db, "doctor"])
    assert code == 0
    assert "expired claim: it-1\tholder=alice\tstamp=unreadable" in out
    assert "age=0s" not in out


def test_future_dated_impossible_stamp_is_reported_unreadable(db, monkeypatch) -> None:
    """An impossible stamp is parsed before expiry is decided, so a future-dated one is not hidden."""
    from ledger import schema

    seed(monkeypatch, db, "it-2", "synthetic-two")
    monkeypatch.setitem(schema._LEASE_STAMP, "calendar_stamp", False)  # a row written before the check
    lg = open_ledger(db)
    try:
        lg.append(kind="item.state", item_id="it-2", idempotency_key="bad-future", harness="fixture",
                  payload={"status": "claimed", "claim": "mallory", "expires_at": "9999-02-30T00:00:00.000Z"})
    finally:
        lg.close()
    monkeypatch.setitem(schema._LEASE_STAMP, "calendar_stamp", True)
    items = doctor_expired(monkeypatch, db)
    assert [row["item_id"] for row in items] == ["it-2"]
    assert items[0]["unreadable_stamp"] is True
    assert items[0]["age_seconds"] is None and items[0]["expired_for_seconds"] is None
    code, out, _ = invoke(monkeypatch, ["--db", db, "doctor"])
    assert code == 0 and "expired claim: it-2\tholder=mallory\tstamp=unreadable" in out
    # v_ready reads the stamp as live, so for capacity the claim still holds its slot.
    assert ready_ids(db) == ["it-1"]
    code, body, _ = run(monkeypatch, db, "bob", "claim", "it-1", "--capacity", "1")
    assert code == 3 and body["error"]["code"] == "capacity"


def test_loop_held_changed_between_the_reads_is_refused(db, monkeypatch) -> None:
    """The unstamped re-claim decision is re-checked under the lock, like the takeover."""
    from ledger.verbs import claim as claim_verb

    real = claim_verb._loop_held_by
    calls = []

    def flaky(conn, row, item_id, requester):
        calls.append(item_id)
        # The pre-lock read sees a loop-held claim; the in-lock read does not.
        return True if len(calls) == 1 else real(conn, row, item_id, requester)

    monkeypatch.setattr(claim_verb, "_loop_held_by", flaky)
    code, body, _ = run(monkeypatch, db, "alice", "claim", "it-1")
    assert code == 3 and body["error"]["code"] == "claim_changed"
    assert len(calls) == 2


def events(db):
    with sqlite3.connect(db) as conn:
        return conn.execute("SELECT * FROM events ORDER BY id").fetchall()


@pytest.mark.parametrize("expected", ["stale", "none"])
def test_claim_checks_expected_revision_under_lock(db, monkeypatch, expected) -> None:
    seen = events(db)[-1][0]
    real_append = store.Ledger.append

    def racing_append(self, **kwargs):
        other = open_ledger(db)
        try:
            real_append(
                other, kind="item.note", item_id="it-1",
                payload={"text": "concurrent update"}, idempotency_key="racing-note",
                actor="writer-b", session="writer-b-session",
            )
        finally:
            other.close()
        return real_append(self, **kwargs)

    monkeypatch.setattr(store.Ledger, "append", racing_append)
    code, body, _ = run(
        monkeypatch, db, "alice", "claim", "it-1",
        "--expect-revision", str(seen) if expected == "stale" else expected,
    )
    assert code == 5, "claim must refuse a stale revision under the write lock"
    assert body["error"]["code"] == "stale_revision"
    revision = str(seen) if expected == "stale" else expected
    assert body["error"]["message"] == (
        f"stale revision for item it-1: expected {revision}, current is {seen + 1} "
        f"(event {seen + 1} written by actor=writer-b session=writer-b-session)"
    )
    assert [json.loads(row[3]) for row in events(db)[1:]] == [
        {"text": "concurrent update"}
    ]


@pytest.mark.parametrize("terminal", ["closed", "stopped", "blocked"])
def test_claim_refuses_non_claimable_item_without_writing(db, monkeypatch, terminal) -> None:
    lg = open_ledger(db)
    try:
        if terminal == "stopped":
            lg.append(
                kind="item.stopped", item_id="it-1",
                payload={"point": "work stopped", "phase": "build"},
                idempotency_key="stop",
            )
        else:
            lg.append(
                kind="item.state", item_id="it-1",
                payload={"status": terminal}, idempotency_key="terminal",
            )
    finally:
        lg.close()
    before = events(db)
    code, body, _ = run(monkeypatch, db, "alice", "claim", "it-1")
    assert code == 3, f"claim must refuse item_{terminal} before writing"
    assert body["error"]["code"] == f"item_{terminal}"
    assert events(db) == before


@pytest.mark.parametrize(
    "first,second",
    [
        (["--claim", "worker-a"], ["--claim", "worker-b"]),
        (["--owner", "shared", "--claim", "worker-a"],
         ["--owner", "shared", "--claim", "worker-b"]),
        (["--owner", "worker-a", "--claim", "shared"],
         ["--owner", "worker-b", "--claim", "shared"]),
    ],
)
def test_claim_identity_is_owner_and_claim(db, monkeypatch, first, second) -> None:
    code, body, err = run(monkeypatch, db, "shared-actor", "claim", "it-1", *first)
    assert code == 0, err
    payload = body["data"]["event"]["payload"]
    expected_owner = first[first.index("--owner") + 1] if "--owner" in first else "worker-a"
    assert payload["owner"] == expected_owner, "claim-only owner must be the claim name"
    before = events(db)
    code, body, _ = run(monkeypatch, db, "shared-actor", "claim", "it-1", *second)
    assert code == 3, "different owner or claim must conflict even on one actor"
    assert body["error"]["code"] == "claimed"
    assert events(db) == before


def test_claim_matching_names_join_claim_only_lease(db, monkeypatch) -> None:
    lg = open_ledger(db)
    try:
        lg.append(
            kind="item.state", item_id="it-1",
            payload={"status": "claimed", "claim": "worker-a"},
            idempotency_key="parent-claim",
        )
    finally:
        lg.close()
    code, body, err = run(
        monkeypatch, db, "shared-actor", "claim", "it-1",
        "--owner", "worker-a", "--claim", "worker-a",
    )
    assert code == 0, err
    payload = body["data"]["event"]["payload"]
    assert (payload["owner"], payload["claim"]) == ("worker-a", "worker-a")


def test_bare_claim_keeps_actor_defaults_and_accepts_current_revision(db, monkeypatch) -> None:
    code, body, err = run(
        monkeypatch, db, "alice", "claim", "it-1",
        "--expect-revision", str(events(db)[-1][0]),
    )
    assert code == 0, err
    payload = body["data"]["event"]["payload"]
    assert (payload["owner"], payload["claim"]) == ("alice", "alice")
    assert run(monkeypatch, db, "alice", "claim", "it-1")[0] == 0


@pytest.mark.parametrize("revision", ["0", "-1", "None", " none", "1 ", "01", "1.0", "x"])
def test_claim_malformed_revision_writes_nothing(db, monkeypatch, revision) -> None:
    before = events(db)
    code, body, _ = run(
        monkeypatch, db, "alice", "claim", "it-1", "--expect-revision", revision,
    )
    assert code == 2
    assert body["error"]["code"] == "usage"
    assert events(db) == before


def test_claim_unknown_status_writes_nothing(db, monkeypatch) -> None:
    from ledger.verbs import claim as claim_verb

    real_open = claim_verb.open_db

    def unreadable_status(ctx, *, write):
        lg = real_open(ctx, write=write)
        lg._conn.execute(
            "CREATE TEMP VIEW v_items AS SELECT item_id, type, title, "
            "'unknown' AS status, owner, claim, created_event_id, state_event_id, "
            "created_at, updated_at, spec_version FROM main.v_items"
        )
        return lg

    monkeypatch.setattr(claim_verb, "open_db", unreadable_status)
    before = events(db)
    code, body, _ = run(monkeypatch, db, "alice", "claim", "it-1")
    assert code == 3, "unknown status must refuse item_status before writing"
    assert body["error"]["code"] == "item_status"
    assert events(db) == before


def test_claim_expired_same_owner_different_claim_names_prior_holder(db, monkeypatch, clock) -> None:
    clock.at(-5 * HOUR)
    assert run(
        monkeypatch, db, "alice", "claim", "it-1", "--owner", "shared", "--claim", "first",
    )[0] == 0
    clock.real()
    code, body, err = run(
        monkeypatch, db, "alice", "claim", "it-1", "--owner", "shared", "--claim", "second",
    )
    assert code == 0, err
    assert body["data"]["event"]["payload"]["prior_holder"] == "shared"


def test_claim_stopped_item_can_be_explicitly_reopened(db, monkeypatch) -> None:
    lg = open_ledger(db)
    try:
        lg.append(
            kind="item.stopped", item_id="it-1",
            payload={"point": "work stopped", "phase": "build"}, idempotency_key="stop",
        )
        lg.append(
            kind="item.state", item_id="it-1",
            payload={"status": "open"}, idempotency_key="reopen",
        )
    finally:
        lg.close()
    code, body, err = run(monkeypatch, db, "alice", "claim", "it-1")
    assert code == 0, err
    assert body["data"]["event"]["payload"]["status"] == "claimed"
