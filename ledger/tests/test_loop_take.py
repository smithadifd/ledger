"""A take checks the current item status before either transaction write."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest

from ledger import lease
from ledger.loop import take_item
from ledger.store import open_ledger


ITEM = "take-item"


@pytest.mark.parametrize(
    ("status", "code"),
    [("closed", "item_closed"), ("blocked", "item_blocked"), ("paused", "item_status")],
)
def test_take_refuses_without_writing(tmp_path, status, code) -> None:
    with open_ledger(tmp_path / "ledger.db") as ledger:
        ledger.append(
            kind="item.created",
            payload={"type": "ticket", "title": "Take item"},
            item_id=ITEM,
            idempotency_key="created",
        )
        ledger.append(
            kind="item.state",
            payload={"status": "open" if status == "paused" else status},
            item_id=ITEM,
            idempotency_key="state",
        )
        if status == "paused":
            # As in the claim verb tests, model an unreadable/future status.
            ledger._conn.execute(
                "CREATE TEMP VIEW v_items AS SELECT item_id, type, title, "
                "'paused' AS status, owner, claim, created_event_id, state_event_id, "
                "created_at, updated_at, spec_version FROM main.v_items"
            )
        before = ledger.events()
        changes = ledger._conn.total_changes
        with pytest.raises(lease.ClaimRefused) as caught:
            take_item(ledger, ITEM, "seat", idempotency_key="managed", claim_key="take")
        assert caught.value.code == code
        assert str(caught.value) == f"item cannot be claimed in status {status!r}"
        assert ledger.events() == before, "a refused take must write no event"
        assert ledger._conn.total_changes == changes, (
            "a refused take must not execute any write, even one rolled back"
        )


@pytest.mark.parametrize("initial", ["open", "expired"])
def test_take_open_or_expired_claim_still_succeeds(tmp_path, monkeypatch, initial) -> None:
    moment = datetime(2026, 11, 24, tzinfo=UTC)
    monkeypatch.setattr(lease, "now", lambda: moment)
    with open_ledger(tmp_path / "ledger.db") as ledger:
        ledger.append(
            kind="item.created",
            payload={"type": "ticket", "title": "Take item"},
            item_id=ITEM,
            idempotency_key="created",
        )
        if initial == "expired":
            ledger.append(
                kind="item.state",
                payload={
                    "status": "claimed",
                    "claim": "predecessor",
                    "expires_at": lease.stamp(moment - timedelta(seconds=1)),
                },
                item_id=ITEM,
                idempotency_key="expired",
            )
        before = ledger.events()
        claim = take_item(
            ledger, ITEM, "seat", idempotency_key="managed", claim_key="take", ttl_seconds=60
        )
        assert [event.kind for event in ledger.events()[len(before):]] == [
            "loop.managed", "item.state",
        ]
        assert claim.payload == {
            "status": "claimed",
            "claim": "seat",
            "expires_at": lease.stamp(moment + timedelta(seconds=60)),
            **({"prior_holder": "predecessor"} if initial == "expired" else {}),
        }
        row = ledger._conn.execute(
            "SELECT status, claim FROM v_items WHERE item_id = ?", (ITEM,)
        ).fetchone()
        assert tuple(row) == ("claimed", "seat")


@pytest.mark.parametrize("status", ["closed", "blocked"])
def test_take_checks_status_after_acquiring_write_lock(tmp_path, monkeypatch, status) -> None:
    path = tmp_path / "ledger.db"
    with open_ledger(path) as ledger:
        ledger.append(
            kind="item.created",
            payload={"type": "ticket", "title": "Take item"},
            item_id=ITEM,
            idempotency_key="created",
        )
        # An adapter's earlier open read is stale when the take obtains its lock.
        assert ledger._conn.execute(
            "SELECT status FROM v_items WHERE item_id = ?", (ITEM,)
        ).fetchone()[0] == "open"
        transaction = ledger.transaction
        before = []

        @contextmanager
        def concurrent_update():
            with open_ledger(path, create=False) as writer:
                writer.append(
                    kind="item.state",
                    payload={"status": status},
                    item_id=ITEM,
                    idempotency_key="concurrent-state",
                )
            with transaction():
                before.extend(ledger.events())
                yield

        monkeypatch.setattr(ledger, "transaction", concurrent_update)
        changes = ledger._conn.total_changes
        with pytest.raises(lease.ClaimRefused) as caught:
            take_item(ledger, ITEM, "seat", idempotency_key="managed", claim_key="take")
        assert caught.value.code == f"item_{status}"
        assert ledger.events() == before, "a refused take must preserve the concurrent update"
        assert ledger._conn.total_changes == changes, (
            "a refused take must not execute any write, even one rolled back"
        )
