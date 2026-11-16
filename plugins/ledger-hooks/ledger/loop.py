"""No sequence of generic CLI write verbs, without `restore --accept-loop-events`, closes an item, links a merge, or writes a loop-reserved event kind. In-process callers are trusted; the boundary defended is the CLI/process boundary.

Internal writer for item-loop events that generic ledger verbs cannot append.
Each external side effect is represented by an intent before it runs and a
done event after it finishes.  A resumed caller reads ``operation_state`` and
reconciles a started operation instead of repeating it.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from . import lease
from .schema import LOOP_OPERATIONS, ValidationError
from .store import Event, Ledger, canonical_dumps

_LOOP_WRITER_CAPABILITY = object()


@dataclass(frozen=True)
class OperationState:
    state: str
    intent: Event | None
    done: Event | None


@dataclass(frozen=True)
class OperationClaim:
    event: Event
    created: bool


def _metadata(
    actor: str | None,
    harness: str | None,
    model: str | None,
    session: str | None,
) -> dict[str, str | None]:
    return {"actor": actor, "harness": harness, "model": model, "session": session}


def _require_operation(operation: str) -> None:
    if operation not in LOOP_OPERATIONS:
        raise ValidationError(f"bad-type:operation:{operation}")


def operation_state(
    ledger: Ledger,
    item_id: str,
    operation: str,
    operation_id: str | None = None,
) -> OperationState:
    """Return absent, started, or finished for one (or the latest) operation."""
    _require_operation(operation)
    matching = [
        event
        for event in ledger.events(item_id=item_id)
        if event.kind in ("loop.intent", "loop.done")
        and event.payload["operation"] == operation
        and (operation_id is None or event.payload["operation_id"] == operation_id)
    ]
    if not matching:
        return OperationState("absent", None, None)
    if operation_id is None:
        operation_id = matching[-1].payload["operation_id"]
        matching = [e for e in matching if e.payload["operation_id"] == operation_id]
    intent = next((e for e in matching if e.kind == "loop.intent"), None)
    done = next((e for e in matching if e.kind == "loop.done"), None)
    return OperationState("finished" if done else "started", intent, done)


def held_by_needs_you(conn: sqlite3.Connection, item_id: str) -> bool:
    """True when the item's current state is the needs-you held mapping's block.

    That is its latest ``item.state`` when the mapping appended it (key
    ``needs-you:<item>:item.state:blocked:...``), or, with no state event, a
    decision an import or sync created blocked (a Held entry's first mapping).
    Mirrors the markdown import's ``held_by_needs_you``, restricted to
    decisions.
    """
    state = conn.execute(
        "SELECT payload, idempotency_key FROM events "
        "WHERE item_id = ? AND kind = 'item.state' ORDER BY id DESC LIMIT 1",
        (item_id,),
    ).fetchone()
    if state is not None:
        return (json.loads(state[0]).get("status") == "blocked"
                and (state[1] or "").startswith(f"needs-you:{item_id}:item.state:blocked:"))
    created = conn.execute(
        "SELECT payload, idempotency_key FROM events "
        "WHERE item_id = ? AND kind = 'item.created' ORDER BY id ASC LIMIT 1",
        (item_id,),
    ).fetchone()
    if created is None:
        return False
    payload = json.loads(created[0])
    return (payload.get("type") == "decision" and payload.get("status") == "blocked"
            and (created[1] or "").startswith("import:"))


_HOLDER_SQL = (
    "COALESCE(NULLIF(json_extract(claim.payload, '$.owner'), ''), "
    "NULLIF(json_extract(claim.payload, '$.claim'), ''))"
)


def _latest_claim(conn: sqlite3.Connection, item_id: str) -> dict[str, Any] | None:
    """The item's latest claim-bearing event: its id, holder and lease end, else None.

    A claim-bearing event is an ``item.state`` that names a holder (its
    ``owner``, else its ``claim``): ``ledger claim``, the loop's take, a
    takeover, the session hand-off and a restamp all write one. An
    ``item.state`` that names nobody (a generic ``ledger state --status
    open`` or a bare ``--status claimed``) is not one, so it neither grants
    nor removes the loop's authority. ``expires_at`` reads like ``v_leases``,
    from this event rather than the item's latest state: the event's own
    stamp, or a later ``item.progress`` stamp recorded after it; an
    unstamped claim has none and never runs out.
    """
    cursor = conn.execute(
        f"SELECT claim.id AS claim_event_id, {_HOLDER_SQL} AS holder, "
        "CASE WHEN json_extract(claim.payload, '$.expires_at') IS NULL THEN NULL "
        "ELSE MAX(json_extract(claim.payload, '$.expires_at'), COALESCE(("
        "SELECT MAX(json_extract(progress.payload, '$.expires_at')) FROM events AS progress "
        "WHERE progress.kind = 'item.progress' AND progress.item_id = claim.item_id "
        "AND progress.id > claim.id), json_extract(claim.payload, '$.expires_at'))) "
        "END AS expires_at "
        "FROM events AS claim "
        f"WHERE claim.item_id = ? AND claim.kind = 'item.state' AND {_HOLDER_SQL} IS NOT NULL "
        "ORDER BY claim.id DESC LIMIT 1",
        (item_id,),
    )
    cursor.row_factory = sqlite3.Row
    row = cursor.fetchone()
    return None if row is None else dict(row)


def check_holder(
    conn: sqlite3.Connection,
    item_id: str,
    holder: str,
    refused: str,
    *,
    live: bool,
) -> None:
    """The loop's one claim check, run inside the transaction of the write it guards.

    DESIGN: loop authority is read from the item's latest claim-bearing
    event (``_latest_claim``), never from the item's current status. That
    event must exist and name ``holder``. A missing claim is never
    ownership, and a generic state write that names no holder neither
    grants nor removes authority: after a bare ``ledger state --status
    open`` the holder keeps the item until another holder's claim names
    them, and a holder who had lost it does not get it back.

    ``lease.ClaimRefused`` (code ``claim_lost``) is raised, so the write
    rolls back, when the item is closed, has no claim-bearing event, or its
    latest one names another holder. With ``live`` (a step's intent or
    result, and the close), that claim's lease must also not have run out;
    an unstamped claim never runs out. Without ``live`` (the heartbeat's
    renewal), the holder's own expired lease that nobody has taken over
    passes, and is renewed. ``refused`` ends the refusal message: what was
    not written.
    """
    row = conn.execute(
        "SELECT status FROM v_items WHERE item_id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise ValidationError(f"missing-item:{item_id}")
    if row["status"] == "closed":
        raise lease.ClaimRefused("claim_lost", f"item {item_id} is closed; {refused}")
    claim = _latest_claim(conn, item_id)
    if claim is None:
        raise lease.ClaimRefused(
            "claim_lost", f"item {item_id} has no claim naming {holder}; {refused}"
        )
    if claim["holder"] != holder:
        raise lease.ClaimRefused(
            "claim_lost", f"item {item_id} is claimed by {claim['holder']}, not {holder}; {refused}"
        )
    if live and lease.is_expired(claim["expires_at"]):
        raise lease.ClaimRefused(
            "claim_lost",
            f"{holder}'s claim on item {item_id} expired at {claim['expires_at']}; {refused}",
        )


def _holds(conn: sqlite3.Connection, item_id: str, holder: str) -> bool:
    """True when ``holder``'s name is on the item's current claim."""
    row = conn.execute(
        "SELECT status, owner, claim FROM v_items WHERE item_id = ?", (item_id,)
    ).fetchone()
    return row is not None and row["status"] == "claimed" and (row["owner"] or row["claim"]) == holder


def _restamp_handed_off(ledger: Ledger, item_id: str, holder: str, idempotency_key: str) -> Event | None:
    """Inside the caller's transaction: give ``holder``'s claim a fresh lease when it has none.

    That is ``holder``'s own claim when it is unstamped (a session step
    handed off, ``claim_operation`` with ``hand_off``) or its lease has run
    out with nobody taking it over. A live lease, another holder's claim, or
    no claim is left as it is and nothing is written.
    """
    conn = ledger._conn
    held = lease.current_lease(conn, item_id)
    if (
        not _holds(conn, item_id, holder)
        or held is None
        or (held["expires_at"] is not None and not lease.is_expired(held["expires_at"]))
    ):
        return None
    ttl = lease.check_ttl(lease.ttl_for_db(ledger.path), "ttl_seconds")
    return ledger.append(
        kind="item.state",
        payload={"status": "claimed", "claim": holder, "expires_at": lease.expires_at(ttl)},
        item_id=item_id,
        idempotency_key=idempotency_key,
    )


def restamp_claim(ledger: Ledger, item_id: str, holder: str, *, idempotency_key: str) -> Event | None:
    """Give ``holder``'s claim a fresh lease when it has none, in ONE transaction.

    The loop calls this when it resumes in runner mode: a claim a session
    step left unstamped, or the holder's own lease that ran out while no
    loop was running and nobody took it over, is stamped again. Any other
    claim state (a live lease, another holder's, none) is left as it is:
    nothing is written and ``None`` is returned.
    """
    with ledger.transaction():
        return _restamp_handed_off(ledger, item_id, holder, idempotency_key)


def manage_item(
    ledger: Ledger,
    item_id: str,
    *,
    idempotency_key: str,
    source: str | None = None,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> Event:
    """Hand an item to the loop.

    Refused while the item is under the needs-you held mapping's own block: a
    Held needs-you decision is not loop work, and the mapping never writes
    state onto a loop-managed item, so it could never release that block.
    """
    payload = {"source": source} if source else {}

    def item_exists(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT 1 FROM events WHERE item_id = ? AND kind = 'item.created' LIMIT 1",
            (item_id,),
        ).fetchone()
        if row is None:
            raise ValidationError(f"missing-item:{item_id}")
        if held_by_needs_you(conn, item_id):
            raise ValidationError(f"held-needs-you-item:{item_id}")
        pr_link = conn.execute(
            "SELECT 1 FROM events "
            "WHERE item_id = ? AND kind = 'item.linked' "
            "AND json_extract(payload, '$.rel') = 'pr' LIMIT 1",
            (item_id,),
        ).fetchone()
        if pr_link is not None:
            raise ValidationError(f"loop-pr-link-already-exists:{item_id}")

    return ledger._append_loop_event(
        _LOOP_WRITER_CAPABILITY,
        ignore_retry_attribution=True,
        kind="loop.managed",
        payload=payload,
        item_id=item_id,
        idempotency_key=idempotency_key,
        precondition=item_exists,
        **_metadata(actor, harness, model, session),
    )


def take_item(
    ledger: Ledger,
    item_id: str,
    holder: str,
    *,
    idempotency_key: str,
    claim_key: str,
    ttl_seconds: int | None = None,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> Event:
    """Hand an item to the loop and claim it for ``holder``, in ONE transaction.

    The claim check (``lease.guard_claim``), the ``loop.managed`` write and
    the claim write commit together or not at all, so a refused claim leaves
    nothing written: the refused seat is not set up to build the item, and
    the holder can still close it.

    DESIGN: the loop's claim is a lease, exactly like a ``ledger claim``: it
    carries ``expires_at`` (``ttl_seconds`` from now, else the ledger's
    configured claim TTL; a bad config refuses the take, as it refuses
    ``ledger claim``). The running loop keeps the lease alive by recording
    progress (``record_progress``) while a step runs; a loop that stops
    renewing loses the item once the lease runs out. The same holder's
    claim, stamped or not, is rewritten with a fresh stamp. Another holder's
    live claim is refused; an expired one is taken over and the claim names
    ``prior_holder``.

    DESIGN: while a session-mode step is outstanding the claim carries no
    expiry. A session step outlives the process that started it, so no
    heartbeat can renew it: ``claim_operation`` with ``hand_off`` rewrites
    the holder's claim unstamped in the same transaction as the step's
    intent. Recording the step's result (``finish_operation`` with
    ``holder``) or the loop resuming in runner mode (``restamp_claim``)
    stamps it again with a fresh lease. So a live session is never taken
    over, and a dead runner loop is still reclaimed after the TTL.
    Reclaiming a dead session-mode claim is not handled here (a follow-up).

    ``claim_key`` must be unique per take: a takeover can recur on one item.
    """
    ttl = lease.check_ttl(
        ttl_seconds if ttl_seconds is not None else lease.ttl_for_db(ledger.path),
        "ttl_seconds",
    )
    with ledger.transaction():
        # Every read below is under the transaction's write lock, so the
        # claim check sees exactly what the writes will land on.
        conn = ledger._conn
        moment = lease.now()
        row = conn.execute("SELECT * FROM v_items WHERE item_id = ?", (item_id,)).fetchone()
        held = lease.current_lease(conn, item_id)
        prior = lease.takeover_holder(row, held, holder, moment)
        lease.guard_claim(row, held, holder, moment, prior)
        manage_item(
            ledger,
            item_id,
            idempotency_key=idempotency_key,
            **_metadata(actor, harness, model, session),
        )
        payload: dict[str, Any] = {
            "status": "claimed",
            "claim": holder,
            "expires_at": lease.expires_at(ttl, at=moment),
        }
        if prior:
            payload["prior_holder"] = prior
        # DESIGN (LC3 D4): like ``ledger claim``, the take names the spec
        # version it builds against, read under this transaction's lock; an
        # item with no spec stamps none.
        if row is not None and row["spec_version"] is not None:
            payload["spec_version"] = row["spec_version"]
        return ledger.append(
            kind="item.state",
            payload=payload,
            item_id=item_id,
            idempotency_key=claim_key,
        )


def record_progress(
    ledger: Ledger,
    item_id: str,
    text: str,
    *,
    idempotency_key: str,
    phase: str | None = None,
    ttl_seconds: int | None = None,
    holder: str | None = None,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> Event:
    """Record loop progress on an item and re-stamp its claim's lease.

    The event carries a fresh ``expires_at`` (``ttl_seconds`` from now, else
    the ledger's configured claim TTL), which ``v_leases`` reads as the new
    end of the lease. The stamp is part of the payload, so give each beat of
    progress its own key: a retry under one key with a later stamp is a
    different payload and is refused as a key conflict.

    With ``holder`` (the loop's heartbeat always passes it), the write is a
    renewal only of ``holder``'s own claim, checked inside the append
    transaction (``check_holder``): when the item is closed, has no
    claim-bearing event, or its latest one names another holder (a
    takeover), nothing is written and ``lease.ClaimRefused`` (code
    ``claim_lost``) is raised, so a heartbeat never extends another holder's
    lease. A generic state write that names no holder (a bare ``ledger
    state --status claimed`` or ``--status open``) does not change who
    holds the claim, so it does not stop the heartbeat. The holder's own
    expired lease that nobody has taken over is renewed.
    """
    ttl = ttl_seconds if ttl_seconds is not None else lease.ttl_for_db(ledger.path)
    payload: dict[str, Any] = {"text": text, "expires_at": lease.expires_at(ttl)}
    if phase:
        payload["phase"] = phase

    def still_held(conn: sqlite3.Connection) -> None:
        check_holder(conn, item_id, str(holder), "claim not renewed", live=False)

    return ledger.append(
        kind="item.progress",
        payload=payload,
        item_id=item_id,
        idempotency_key=idempotency_key,
        precondition=still_held if holder is not None else None,
        **_metadata(actor, harness, model, session),
    )


def claim_operation(
    ledger: Ledger,
    item_id: str,
    operation: str,
    operation_id: str,
    *,
    target: str | None = None,
    head: str | None = None,
    asked_event_id: int | None = None,
    source: str | None = None,
    holder: str | None = None,
    hand_off: bool = False,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> OperationClaim:
    """Atomically append an operation intent and report whether this caller won.

    With ``holder``, the intent passes ``check_holder`` (live) in the same
    transaction: nothing is written when another holder has the claim, the
    item is closed, or the holder's own lease has run out. With ``hand_off``
    (a session-mode step), the same transaction rewrites the holder's own
    claim unstamped, so it does not expire while the session works (see
    ``take_item``). A retried intent writes nothing.
    """
    _require_operation(operation)
    if hand_off and holder is None:
        raise ValidationError("hand-off-needs-holder")
    payload: dict[str, Any] = {"operation": operation, "operation_id": operation_id}
    for key, value in (
        ("target", target),
        ("head", head),
        ("asked_event_id", asked_event_id),
        ("source", source),
    ):
        if value is not None:
            payload[key] = value
    created = False

    def managed(conn: sqlite3.Connection) -> None:
        nonlocal created
        row = conn.execute(
            "SELECT 1 FROM events WHERE item_id = ? AND kind = 'loop.managed' LIMIT 1",
            (item_id,),
        ).fetchone()
        if row is None:
            raise ValidationError(f"loop-item-not-managed:{item_id}")
        if holder is not None:
            check_holder(conn, item_id, holder, f"{operation} intent not written", live=True)
        created = True

    with ledger.transaction():
        event = ledger._append_loop_event(
            _LOOP_WRITER_CAPABILITY,
            ignore_retry_attribution=True,
            kind="loop.intent",
            payload=payload,
            item_id=item_id,
            idempotency_key=f"loop:{item_id}:{operation}:{operation_id}:intent",
            precondition=managed,
            **_metadata(actor, harness, model, session),
        )
        if created and hand_off and _holds(ledger._conn, item_id, str(holder)):
            ledger.append(
                kind="item.state",
                payload={"status": "claimed", "claim": holder},
                item_id=item_id,
                idempotency_key=f"loop:{item_id}:{operation}:{operation_id}:hand-off",
            )
    return OperationClaim(event, created)


def start_operation(
    ledger: Ledger,
    item_id: str,
    operation: str,
    operation_id: str,
    *,
    target: str | None = None,
    head: str | None = None,
    asked_event_id: int | None = None,
    source: str | None = None,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> Event:
    return claim_operation(
        ledger,
        item_id,
        operation,
        operation_id,
        target=target,
        head=head,
        asked_event_id=asked_event_id,
        source=source,
        actor=actor,
        harness=harness,
        model=model,
        session=session,
    ).event


def finish_operation(
    ledger: Ledger,
    item_id: str,
    operation: str,
    operation_id: str,
    *,
    target: str | None = None,
    head: str | None = None,
    verdict: str | None = None,
    outcome: str | None = None,
    asked_event_id: int | None = None,
    source: str | None = None,
    holder: str | None = None,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> Event:
    """Append an operation's ``loop.done`` against its intent.

    With ``holder`` (every build, review and merge result the loop records,
    reconciled or not), the result passes ``check_holder`` (live) in the
    same transaction: a caller whose claim was taken over, closed or ran out
    writes nothing. The same transaction gives a handed-off (unstamped)
    claim a fresh lease, because the session step it covered is recorded.
    """
    _require_operation(operation)
    current = operation_state(ledger, item_id, operation, operation_id)
    if current.intent is None:
        raise ValidationError(f"loop-intent-missing:{operation_id}")
    payload: dict[str, Any] = {
        "operation": operation,
        "operation_id": operation_id,
        "intent_event_id": current.intent.id,
    }
    for key, value in (
        ("target", target),
        ("head", head),
        ("verdict", verdict),
        ("outcome", outcome),
        ("asked_event_id", asked_event_id),
        ("source", source),
    ):
        if value is not None:
            payload[key] = value

    def intent_matches(conn: sqlite3.Connection) -> None:
        nonlocal created
        row = conn.execute(
            "SELECT kind, item_id, payload FROM events WHERE id = ?",
            (current.intent.id,),
        ).fetchone()
        if (
            row is None
            or row["kind"] != "loop.intent"
            or row["item_id"] != item_id
            or row["payload"] != canonical_dumps(current.intent.payload)
        ):
            raise ValidationError(f"loop-intent-mismatch:{operation_id}")
        if holder is not None:
            check_holder(conn, item_id, holder, f"{operation} result not recorded", live=True)
        created = True

    created = False
    with ledger.transaction():
        event = ledger._append_loop_event(
            _LOOP_WRITER_CAPABILITY,
            ignore_retry_attribution=True,
            kind="loop.done",
            payload=payload,
            item_id=item_id,
            idempotency_key=f"loop:{item_id}:{operation}:{operation_id}:done",
            precondition=intent_matches,
            **_metadata(actor, harness, model, session),
        )
        if created and holder is not None:
            _restamp_handed_off(
                ledger, item_id, holder, f"loop:{item_id}:{operation}:{operation_id}:restamp"
            )
    return event


def close_item(
    ledger: Ledger,
    item_id: str,
    merge_done_event_id: int,
    *,
    idempotency_key: str,
    outcome: str | None = None,
    source: str | None = None,
    holder: str | None = None,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> Event:
    """Append ``loop.closed``; with ``holder``, only past ``check_holder`` (live)."""
    payload: dict[str, Any] = {"merge_done_event_id": merge_done_event_id}
    if outcome:
        payload["outcome"] = outcome
    if source:
        payload["source"] = source

    def merged(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT kind, item_id, json_extract(payload, '$.operation') AS operation "
            "FROM events WHERE id = ?",
            (merge_done_event_id,),
        ).fetchone()
        if (
            row is None
            or row["kind"] != "loop.done"
            or row["item_id"] != item_id
            or row["operation"] != "merge"
        ):
            raise ValidationError("loop-merge-done-required")
        if holder is not None:
            check_holder(conn, item_id, holder, "item not closed", live=True)

    return ledger._append_loop_event(
        _LOOP_WRITER_CAPABILITY,
        ignore_retry_attribution=True,
        kind="loop.closed",
        payload=payload,
        item_id=item_id,
        idempotency_key=idempotency_key,
        precondition=merged,
        **_metadata(actor, harness, model, session),
    )


__all__ = [
    "OperationState",
    "check_holder",
    "close_item",
    "finish_operation",
    "manage_item",
    "operation_state",
    "record_progress",
    "restamp_claim",
    "start_operation",
    "take_item",
]
