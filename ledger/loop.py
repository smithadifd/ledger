"""No sequence of generic CLI write verbs, without `restore --accept-loop-events`, closes an item, links a merge, or writes a loop-reserved event kind. In-process callers are trusted; the boundary defended is the CLI/process boundary. The one verb that reaches this module is the dedicated `ledger reclaim-handoff`: it calls `reclaim_handoff`, whose whole guard it cannot skip, so the only events it can write are a dead session hand-off's recovery.

Internal writer for item-loop events that generic ledger verbs cannot append.
Each external side effect is represented by an intent before it runs and a
done event after it finishes.  A resumed caller reads ``operation_state`` and
reconciles a started operation instead of repeating it.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping

from . import lease
from .receipts import _pull_request, pr_url
from .schema import (
    _EVENT_ID,
    _RECEIPT_REPO,
    LOOP_HAND_OFF_VERSION,
    LOOP_OPERATIONS,
    LOOP_RECLAIM_DECLARATION,
    LOOP_RECLAIM_KIND,
    LOOP_RECLAIM_SUCCESSOR,
    ValidationError,
    _check_field,
    validate_payload,
)
from .store import Event, Ledger, _event_from_row, canonical_dumps, item_is_loop_managed

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


@dataclass(frozen=True)
class PullRequestLink:
    """The item's pull request link, and whether this call wrote it."""

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


# The reader rule for a hand-off: the item's loop.intent events of payload
# version 2 that no loop.done on the item names by the same operation and
# operation id. Never the item's child-of links.
_OPEN_HAND_OFFS_SQL = (
    "SELECT intent.* FROM events AS intent "
    "WHERE intent.item_id = ? AND intent.kind = 'loop.intent' "
    "AND intent.payload_version = ? "
    "AND NOT EXISTS (SELECT 1 FROM events AS done "
    "WHERE done.item_id = intent.item_id AND done.kind = 'loop.done' "
    "AND json_extract(done.payload, '$.operation') = "
    "json_extract(intent.payload, '$.operation') "
    "AND json_extract(done.payload, '$.operation_id') = "
    "json_extract(intent.payload, '$.operation_id')) "
    "ORDER BY intent.id"
)


def _open_hand_off(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    """The item's one open hand-off row on ``conn``, else None.

    The one read behind ``open_hand_off`` and ``claim_operation``'s
    refusal of a second hand-off, so the reader and the refusal cannot
    disagree. Two open hand-offs (only a restored chain can hold them: the
    writer refuses a second one) raise ``loop-hand-off-ambiguous:<item>``;
    neither is picked.
    """
    cursor = conn.execute(_OPEN_HAND_OFFS_SQL, (item_id, LOOP_HAND_OFF_VERSION))
    cursor.row_factory = sqlite3.Row
    rows = cursor.fetchall()
    if len(rows) > 1:
        raise ValidationError(f"loop-hand-off-ambiguous:{item_id}")
    return rows[0] if rows else None


def open_hand_off(ledger: Ledger, item_id: str) -> Event | None:
    """The item's open hand-off, else None.

    DESIGN (the reader rule): an item's hand-off is its one ``loop.intent``
    of payload version 2 with no ``loop.done`` naming the same operation
    and operation id. With none the item is not handed off: the person
    controls it and the seams are the defaults. A version 1 intent is
    never a hand-off. Two open ones raise ``ValidationError``
    (``loop-hand-off-ambiguous:<item>``) rather than one being picked.
    """
    row = _open_hand_off(ledger._conn, item_id)
    return None if row is None else _event_from_row(row)


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


def _has_pr_link(conn: sqlite3.Connection, item_id: str) -> bool:
    """True when the item has any ``item.linked`` with rel ``pr``, whatever
    its target, form or key.

    The one read behind ``manage_item``'s refusal of an item that already
    has a pull request link and ``link_pull_request``'s refusal of a second
    one, so the two cannot disagree.
    """
    row = conn.execute(
        "SELECT 1 FROM events "
        "WHERE item_id = ? AND kind = 'item.linked' "
        "AND json_extract(payload, '$.rel') = 'pr' LIMIT 1",
        (item_id,),
    ).fetchone()
    return row is not None


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
        if _has_pr_link(conn, item_id):
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
    A dead session-mode claim is never taken here: ``reclaim_handoff``
    moves it, on an explicit seat-lost declaration, never on elapsed time.

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
    controller: str | None = None,
    seams: Mapping[str, str] | None = None,
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

    DESIGN (the hand-off): ``controller`` and ``seams`` make the intent a
    hand-off, a ``loop.intent`` of payload version 2 that names who
    controls the build and the implementation of each of the seams
    ``build``, ``review`` and ``outward``. Either field writes version 2,
    whose schema requires both and allows only the ``build`` operation;
    without them the intent is version 1, exactly as before. They are
    refused without ``hand_off`` (``hand-off-fields-need-hand-off``), so a
    hand-off is always written by the holder of the item's live claim. In
    the intent's transaction a hand-off is also refused while another one
    on the item is open (``loop-hand-off-open:<its operation id>``), by the
    same read as ``open_hand_off``. The idempotency key is the same for
    both versions, so a retry under the operation id with another
    controller, other seams or the other version is a key conflict, and
    the stored intent never changes. Any refusal writes nothing: no
    intent and no unstamped claim.
    """
    _require_operation(operation)
    if hand_off and holder is None:
        raise ValidationError("hand-off-needs-holder")
    handing_off = controller is not None or seams is not None
    if handing_off and not hand_off:
        raise ValidationError("hand-off-fields-need-hand-off")
    payload: dict[str, Any] = {"operation": operation, "operation_id": operation_id}
    for key, value in (
        ("target", target),
        ("head", head),
        ("asked_event_id", asked_event_id),
        ("source", source),
    ):
        if value is not None:
            payload[key] = value
    if controller is not None:
        payload["controller"] = controller
    if seams is not None:
        # A copy, so the caller's dict never aliases the stored payload.
        payload["seams"] = dict(seams) if type(seams) is dict else seams
    payload_version = LOOP_HAND_OFF_VERSION if handing_off else 1
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
        if handing_off:
            other = _open_hand_off(conn, item_id)
            if other is not None:
                raise ValidationError(
                    f"loop-hand-off-open:{json.loads(other['payload'])['operation_id']}"
                )
        created = True

    with ledger.transaction():
        event = ledger._append_loop_event(
            _LOOP_WRITER_CAPABILITY,
            ignore_retry_attribution=True,
            kind="loop.intent",
            payload=payload,
            payload_version=payload_version,
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


def link_pull_request(
    ledger: Ledger,
    item_id: str,
    holder: str,
    repo: str,
    pr: int,
    *,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> PullRequestLink:
    """Record the pull request of a loop-managed item, as the holder of its live claim.

    DESIGN: on a loop-managed item an ``item.linked`` with rel ``pr`` exists
    only when this writer wrote it, as the holder of the item's live claim,
    naming one pull request in the one stored form, while the item had no
    other ``pr`` link and no other item linked that pull request. The
    generic writers stay refused (``reserved-transition:loop-pr``); this
    writer goes through the store's one door for it
    (``Ledger._append_loop_pr_link``).

    The event is an ordinary ``item.linked``, payload version 1, exactly
    ``{"rel": "pr", "target": pr_url(repo, pr)}``: the lowercase URL that
    ``ledger.receipts.pr_item`` and the merge records look up. The key is
    ``loop:<item>:pr-link:<target>``.

    Grammar, checked before the transaction: ``holder`` a non-empty string
    (else ``loop-pr-link-needs-holder``); ``repo`` under the receipt repo
    rule, a lowercase ``owner/repo`` of at most 200 characters, refused and
    never folded (``bad-type:repo``, ``too-long:repo``); ``pr`` under the
    receipt number rule, an integer from 1 to 2^63-1 (``bad-type:pr``,
    ``too-large:pr``).

    An exact retry (the same item and pull request) returns the stored
    event with ``created`` false and writes nothing; it re-reads no claim,
    so ``created`` false is not proof of holding it. Retry stamps (actor,
    harness, model, session) are ignored, as for the other loop writes.

    Otherwise, inside the write's transaction, in this order: the item must
    be loop-managed (``loop-item-not-managed:<item>``); ``holder`` must
    hold its live claim (``check_holder``, live: ``claim_lost``); the item
    must have no ``pr`` link at all (``loop-pr-link-already-exists:<item>``);
    and no other item may link the pull request, compared as ``pr_item``
    compares (``loop-pr-linked-elsewhere:<that item>``). A key already
    holding another event is the store's ``IdempotencyConflict``. Every
    refusal writes nothing. The hand-off is not read and not required.
    """
    if type(holder) is not str or not holder:
        raise ValidationError("loop-pr-link-needs-holder")
    _check_field(repo, _RECEIPT_REPO, "repo")
    _check_field(pr, _EVENT_ID, "pr")
    target = pr_url(repo, pr)
    wanted = _pull_request(target)
    created = False

    def linkable(conn: sqlite3.Connection) -> None:
        nonlocal created
        managed = conn.execute(
            "SELECT 1 FROM events WHERE item_id = ? AND kind = 'loop.managed' LIMIT 1",
            (item_id,),
        ).fetchone()
        if managed is None:
            raise ValidationError(f"loop-item-not-managed:{item_id}")
        check_holder(conn, item_id, holder, "pull request link not written", live=True)
        if _has_pr_link(conn, item_id):
            raise ValidationError(f"loop-pr-link-already-exists:{item_id}")
        # The reader's comparison (receipts.pr_item): the URL or the
        # owner/repo#N form, owner and repo casefolded, the number an integer.
        for row in conn.execute(
            "SELECT item_id, payload FROM events "
            "WHERE kind = 'item.linked' AND item_id != ? ORDER BY id",
            (item_id,),
        ):
            payload = json.loads(row["payload"])
            if payload.get("rel") == "pr" and _pull_request(payload.get("target")) == wanted:
                raise ValidationError(f"loop-pr-linked-elsewhere:{row['item_id']}")
        created = True

    event = ledger._append_loop_pr_link(
        _LOOP_WRITER_CAPABILITY,
        ignore_retry_attribution=True,
        kind="item.linked",
        payload={"rel": "pr", "target": target},
        item_id=item_id,
        idempotency_key=f"loop:{item_id}:pr-link:{target}",
        precondition=linkable,
        **_metadata(actor, harness, model, session),
    )
    return PullRequestLink(event, created)


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
    """Append ``loop.closed``; with ``holder``, only past ``check_holder`` (live).

    ``merge_done_event_id`` must be a ``loop.done`` of a ``merge`` on the item
    whose outcome is not ``seat-lost``: a seat-lost done records that the seat
    was lost, not that anything merged (``loop-merge-done-required``).
    """
    payload: dict[str, Any] = {"merge_done_event_id": merge_done_event_id}
    if outcome:
        payload["outcome"] = outcome
    if source:
        payload["source"] = source

    def merged(conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT kind, item_id, json_extract(payload, '$.operation') AS operation, "
            "json_extract(payload, '$.outcome') AS outcome "
            "FROM events WHERE id = ?",
            (merge_done_event_id,),
        ).fetchone()
        if (
            row is None
            or row["kind"] != "loop.done"
            or row["item_id"] != item_id
            or row["operation"] != "merge"
            or row["outcome"] == LOOP_RECLAIM_DECLARATION
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


# --- Reclaiming a dead session hand-off ---------------------------------------


@dataclass(frozen=True)
class HandoffReclaim:
    """A session hand-off's recovery: its ``loop.reclaimed`` marker, the old
    operation's ``loop.done`` and the successor's claim, and whether this call
    wrote them (``created`` false for an exact retry)."""

    reclaimed: Event
    done: Event
    claim: Event
    created: bool


# An item's loop.intent events, of any payload version, that no loop.done on
# the item names by the same operation and operation id: the reader rule of
# _OPEN_HAND_OFFS_SQL without its version filter.
_OPEN_INTENTS_SQL = (
    "SELECT intent.* FROM events AS intent "
    "WHERE intent.item_id = ? AND intent.kind = 'loop.intent' "
    "AND NOT EXISTS (SELECT 1 FROM events AS done "
    "WHERE done.item_id = intent.item_id AND done.kind = 'loop.done' "
    "AND json_extract(done.payload, '$.operation') = "
    "json_extract(intent.payload, '$.operation') "
    "AND json_extract(done.payload, '$.operation_id') = "
    "json_extract(intent.payload, '$.operation_id')) "
    "ORDER BY intent.id"
)

# The claim-bearing events of an item between two event ids, inclusive: the
# holder each names and its own lease stamp.
_CLAIMS_BETWEEN_SQL = (
    f"SELECT claim.id AS id, {_HOLDER_SQL} AS holder, "
    "json_extract(claim.payload, '$.expires_at') AS expires_at "
    "FROM events AS claim "
    f"WHERE claim.item_id = ? AND claim.kind = 'item.state' AND {_HOLDER_SQL} IS NOT NULL "
    "AND claim.id >= ? AND claim.id <= ? ORDER BY claim.id"
)

# The ticket pass protocol (the adapter bar's): a pass begins with its record
# (an item.note) or, in the old shape, a session.opened; only its
# session.closed ends it. The key's last part names which.
_PASS_KEY_PREFIX = "ticket-pass/"
_PASS_ROLES = {"record": "item.note", "claimed": "session.opened", "released": "session.closed"}
_PASS_RECORD_FRAME = "ticket-pass:"
_PASS_RECORD_V2 = "ticket-pass:v2 "
_PASS_RECORD_KEYS = frozenset(("id", "host", "started_at", "holder", "lease"))
_PASS_ID = re.compile(r"[0-9a-f]+")


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
    cursor = conn.execute(sql, params)
    cursor.row_factory = sqlite3.Row
    return cursor.fetchall()


def _event_by_key(conn: sqlite3.Connection, key: str) -> Event | None:
    rows = _rows(conn, "SELECT * FROM events WHERE idempotency_key = ?", (key,))
    return _event_from_row(rows[0]) if rows else None


def _session_rewrite(conn: sqlite3.Connection, intent: sqlite3.Row) -> sqlite3.Row | None:
    """The claim rewrite ``claim_operation`` with ``hand_off`` wrote with
    ``intent``, else None: the proof that the operation ran in session mode.

    Only the loop writer's own rewrite counts. It is the ``item.state`` under
    the operation's ``loop:<item>:<operation>:<operation id>:hand-off`` key,
    on the intent's item, written in the intent's own transaction (so it is
    the very next event), whose payload is exactly the unstamped claim
    ``{"status": "claimed", "claim": <holder>}``, and which carries no
    harness (the loop writer stamps none; every CLI write carries
    ``LEDGER_HARNESS``). An unstamped claim without it, or an event a generic
    writer put under that key, never makes an operation a session hand-off.
    """
    payload = json.loads(intent["payload"])
    item_id = intent["item_id"]
    rows = _rows(
        conn,
        "SELECT * FROM events WHERE idempotency_key = ?",
        (f"loop:{item_id}:{payload['operation']}:{payload['operation_id']}:hand-off",),
    )
    if not rows:
        return None
    row = rows[0]
    claim = json.loads(row["payload"])
    if (
        row["kind"] != "item.state"
        or row["item_id"] != item_id
        or int(row["id"]) != int(intent["id"]) + 1
        or set(claim) != {"status", "claim"}
        or claim["status"] != "claimed"
        or row["harness"] is not None
    ):
        return None
    return row


def _is_session_operation(conn: sqlite3.Connection, intent: sqlite3.Row) -> bool:
    """A hand-off (payload version 2), or a version 1 intent with its session rewrite."""
    return (
        intent["payload_version"] == LOOP_HAND_OFF_VERSION
        or _session_rewrite(conn, intent) is not None
    )


def session_hand_off(conn: sqlite3.Connection, item_id: str) -> dict[str, Any] | None:
    """The item's first unfinished session-mode operation, else None. Read only.

    An operation is session-mode when its intent is a hand-off (payload
    version 2) or a version 1 intent whose session rewrite
    (``_session_rewrite``) proves ``hand_off``; it is unfinished while no
    ``loop.done`` on the item names its operation and operation id. Returns
    the intent's event id, operation and operation id. ``doctor`` reads it so
    it never offers a generic ``state --status open`` for such a claim.
    """
    if not item_is_loop_managed(conn, item_id):
        return None
    for intent in _rows(conn, _OPEN_INTENTS_SQL, (item_id,)):
        if _is_session_operation(conn, intent):
            payload = json.loads(intent["payload"])
            return {
                "intent_event_id": int(intent["id"]),
                "operation": payload["operation"],
                "operation_id": payload["operation_id"],
            }
    return None


def _outstanding_pass(conn: sqlite3.Connection, item_id: str) -> str | None:
    """The id of the item's first ticket pass that nothing has ended, else None.

    DESIGN (the pass protocol): a pass begins with an ``item.note`` keyed
    ``ticket-pass/<item>/<pass>/record`` whose text is ``ticket-pass:v2 ``
    and then a JSON object with exactly ``id`` (that pass), ``host``,
    ``started_at``, ``holder`` and ``lease``, or, in the old shape, with a
    ``session.opened`` keyed ``ticket-pass/<item>/<pass>/claimed``. Only the
    ``session.closed`` keyed ``ticket-pass/<item>/<pass>/released`` on the
    same item ends it, and only after it began. Any other event under the
    ``ticket-pass/`` key prefix, or any note framed ``ticket-pass:``, that is
    not exactly one of these (another item's key, another role, the wrong
    kind, a pass id that is not lowercase hex, an unknown version, a body
    that is not such an object, a record naming another pass) is never read
    as completion: ``reclaim-pass-unreadable:<item>`` is raised.
    """
    unreadable = f"reclaim-pass-unreadable:{item_id}"
    prefix = f"{_PASS_KEY_PREFIX}{item_id}/"
    live: dict[str, None] = {}
    for row in _rows(
        conn,
        "SELECT kind, idempotency_key, payload FROM events WHERE item_id = ? ORDER BY id",
        (item_id,),
    ):
        key = row["idempotency_key"] or ""
        text = json.loads(row["payload"]).get("text") if row["kind"] == "item.note" else None
        framed = isinstance(text, str) and text.startswith(_PASS_RECORD_FRAME)
        if not key.startswith(_PASS_KEY_PREFIX) and not framed:
            continue
        parts = key[len(prefix):].split("/") if key.startswith(prefix) else []
        if (
            len(parts) != 2
            or _PASS_ID.fullmatch(parts[0]) is None
            or _PASS_ROLES.get(parts[1]) != row["kind"]
        ):
            raise ValidationError(unreadable)
        pass_id, role = parts
        if role == "record":
            if not framed or not text.startswith(_PASS_RECORD_V2):
                raise ValidationError(unreadable)
            try:
                record = json.loads(text[len(_PASS_RECORD_V2):])
            except ValueError:
                raise ValidationError(unreadable) from None
            if type(record) is not dict or set(record) != _PASS_RECORD_KEYS or record["id"] != pass_id:
                raise ValidationError(unreadable)
        if role == "released":
            live.pop(pass_id, None)
        else:
            live[pass_id] = None
    return next(iter(live), None)


def _reclaim_guard(ledger: Ledger, item_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Every check of a new recovery, in order, inside the write's transaction.

    Run as the marker's precondition, so every read is under the
    transaction's write lock and sees exactly what the three appends land
    on. Each refusal raises ``ValidationError`` and the transaction rolls
    back. Returns what the appends need: the TTL, the item's current spec
    version and the target's operation and operation id.
    """
    conn = ledger._conn
    try:
        ttl = lease.check_ttl(lease.ttl_for_db(ledger.path), "ttl_seconds")
    except lease.LeaseConfigError as exc:
        raise ValidationError("reclaim-bad-ttl") from exc
    items = _rows(conn, "SELECT * FROM v_items WHERE item_id = ?", (item_id,))
    if not items:
        raise ValidationError(f"missing-item:{item_id}")
    item = items[0]
    if not item_is_loop_managed(conn, item_id):
        raise ValidationError(f"loop-item-not-managed:{item_id}")
    if item["status"] == "closed":
        raise ValidationError("reclaim-item-closed")
    if item["status"] == "blocked":
        raise ValidationError("reclaim-item-blocked")
    # The current named claim: the item's latest state is its latest
    # claim-bearing event, and it is a claim.
    claim = _latest_claim(conn, item_id)
    if (
        claim is None
        or item["status"] != "claimed"
        or item["state_event_id"] != claim["claim_event_id"]
    ):
        raise ValidationError("reclaim-no-current-claim")
    # The target: the exact intent the declarant observed, on this item.
    found = _rows(conn, "SELECT * FROM events WHERE id = ?", (payload["intent_event_id"],))
    if not found or found[0]["kind"] != "loop.intent" or found[0]["item_id"] != item_id:
        raise ValidationError("reclaim-handoff-missing")
    intent = found[0]
    # A merge step is never recovered: its seat-lost loop.done would name the
    # operation every merge reader looks for (close_item, decisions), and no
    # merge happened.
    if json.loads(intent["payload"])["operation"] == "merge":
        raise ValidationError("reclaim-merge-step")
    open_intents = _rows(conn, _OPEN_INTENTS_SQL, (item_id,))
    if intent["payload_version"] == LOOP_HAND_OFF_VERSION:
        # The reader rule, unchanged: two open hand-offs refuse
        # loop-hand-off-ambiguous:<item>; a finished one is not current.
        current = _open_hand_off(conn, item_id)
        if current is None or current["id"] != intent["id"]:
            raise ValidationError("reclaim-handoff-changed")
    elif all(row["id"] != intent["id"] for row in open_intents):
        raise ValidationError("reclaim-handoff-changed")
    # The target is the item's only unfinished session-mode operation.
    for row in open_intents:
        if row["id"] != intent["id"] and _is_session_operation(conn, row):
            raise ValidationError("reclaim-session-ambiguous")
    rewrite = _session_rewrite(conn, intent)
    if rewrite is None:
        raise ValidationError("reclaim-not-session-claim")
    if claim["expires_at"] is not None:
        raise ValidationError("reclaim-not-session-claim")
    if claim["claim_event_id"] != payload["claim_event_id"]:
        raise ValidationError("reclaim-claim-changed")
    # Continuous ownership: every claim-bearing event from the session
    # rewrite to the current claim names the predecessor, unstamped.
    for between in _rows(
        conn, _CLAIMS_BETWEEN_SQL, (item_id, int(rewrite["id"]), claim["claim_event_id"])
    ):
        if between["holder"] != payload["prior_holder"] or between["expires_at"] is not None:
            raise ValidationError("reclaim-claim-changed")
    pass_id = _outstanding_pass(conn, item_id)
    if pass_id is not None:
        raise ValidationError(f"reclaim-pass-live:{pass_id}")
    target = json.loads(intent["payload"])
    return {
        "ttl": ttl,
        "spec_version": item["spec_version"],
        "operation": target["operation"],
        "operation_id": target["operation_id"],
    }


def _reclaimed_record(
    conn: sqlite3.Connection, item_id: str, marker: Event
) -> tuple[Event, Event]:
    """The ``loop.done`` and the claim the recovery ``marker`` was written
    with, checked against it; ``reclaim-record-incomplete`` unless both are
    there exactly as ``reclaim_handoff`` writes them, right after it."""
    incomplete = "reclaim-record-incomplete"
    intent_id = marker.payload["intent_event_id"]
    found = _rows(conn, "SELECT * FROM events WHERE id = ?", (intent_id,))
    if not found or found[0]["kind"] != "loop.intent" or found[0]["item_id"] != item_id:
        raise ValidationError(incomplete)
    target = json.loads(found[0]["payload"])
    operation, operation_id = target["operation"], target["operation_id"]
    done = _event_by_key(conn, f"loop:{item_id}:{operation}:{operation_id}:done")
    claim = _event_by_key(conn, f"loop:{item_id}:reclaim:{intent_id}:claim")
    if done is None or claim is None:
        raise ValidationError(incomplete)
    if (
        done.kind != "loop.done"
        or done.item_id != item_id
        or done.id != marker.id + 1
        or done.payload != {
            "operation": operation,
            "operation_id": operation_id,
            "intent_event_id": intent_id,
            "outcome": LOOP_RECLAIM_DECLARATION,
            "source": f"event:{marker.id}",
        }
    ):
        raise ValidationError(incomplete)
    if (
        claim.kind != "item.state"
        or claim.item_id != item_id
        or claim.id != done.id + 1
        or claim.payload.get("status") != "claimed"
        or claim.payload.get("claim") != marker.payload["holder"]
        or claim.payload.get("prior_holder") != marker.payload["prior_holder"]
        or "expires_at" not in claim.payload
    ):
        raise ValidationError(incomplete)
    return done, claim


def reclaim_handoff(
    ledger: Ledger,
    item_id: str,
    *,
    intent_event_id: int | None = None,
    claim_event_id: int | None = None,
    prior_holder: str | None = None,
    holder: str | None = None,
    declare_seat_lost: bool = False,
    authority: str | None = None,
    declared_by: str | None = None,
    evidence: str | None = None,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> HandoffReclaim:
    """Move a dead session hand-off's claim to a named successor, in ONE transaction.

    DESIGN (the invariant): a recovery transfers one exact, unfinished
    session-mode claim from its recorded predecessor to one named successor
    only after an explicit seat-lost declaration and while no ticket pass
    may remain live. The marker, the old operation's completion and the
    successor's claim commit together or not at all, and a retry never
    grants authority again.

    The ledger cannot see a process die. The caller declares it
    (``declare_seat_lost=True``), on an ``authority`` of ``operator`` or
    ``successor`` (a successor declares as itself: ``declared_by`` equals
    ``holder``), with ``evidence`` (one line, at most 300 characters). That
    is attribution, not authentication. The caller first stops the
    predecessor and its workers and ends its ticket pass; nothing here ends
    a pass or releases a parent's lease.

    Grammar, checked before the transaction: no declaration is
    ``reclaim-declaration-required``; ``holder`` is
    ``<seat>@<session>``, each part one ASCII token of 1 to 64 characters
    (``reclaim-successor-invalid``); then the ``loop.reclaimed`` schema:
    ``intent_event_id`` and ``claim_event_id`` positive integers, never
    booleans; ``prior_holder`` a non-empty string, compared exactly;
    ``authority`` ``operator`` or ``successor``; ``declared_by`` and
    ``evidence`` one line of 1 to 300 characters (``missing-key:<field>``,
    ``bad-type:<field>``, ``too-long:<field>``, ``not-single-line:<field>``,
    ``too-large:<field>``); ``reclaim-same-holder``;
    ``reclaim-authority-invalid``. The store refuses an ``item_id`` that is
    not a non-empty string (``missing-key:item_id``, ``bad-type:item_id``)
    before anything else is read.

    An exact retry (the same request, under the key
    ``loop:<item>:reclaim:<intent_event_id>``) checks the stored marker, done
    and claim (``reclaim-record-incomplete`` unless all three are there as
    this writes them) and returns them with ``created`` false, writing
    nothing and checking nothing current: it renews no lease, so ``created``
    false is not proof of holding the claim. Any other request under that
    key is an ``IdempotencyConflict``. Retry stamps (actor, harness, model,
    session) are ignored, as for the other loop writes.

    The target may be a version 2 hand-off (always a build) or a version 1
    session step of any operation but ``merge``: a merge step refuses
    ``reclaim-merge-step``, since its seat-lost ``loop.done`` would read as a
    merge.

    Otherwise ``_reclaim_guard`` runs inside the transaction, then three
    events are written: the ``loop.reclaimed`` marker; the target's
    ``loop.done`` (outcome ``seat-lost``, source ``event:<marker id>``),
    with no holder check and no restamp, under the key ``finish_operation``
    uses, so the old operation can never record another result; and the
    successor's leased claim (``claim`` the successor, ``prior_holder``, the
    item's current ``spec_version`` when it has one, and ``expires_at`` the
    configured claim TTL from now), keyed ``loop:<item>:reclaim:<intent
    event id>:claim``. Every refusal writes nothing.
    """
    if declare_seat_lost is not True:
        raise ValidationError("reclaim-declaration-required")
    if type(holder) is str and holder and re.fullmatch(LOOP_RECLAIM_SUCCESSOR, holder) is None:
        raise ValidationError("reclaim-successor-invalid")
    request = {
        "intent_event_id": intent_event_id,
        "claim_event_id": claim_event_id,
        "prior_holder": prior_holder,
        "holder": holder,
        "declaration": LOOP_RECLAIM_DECLARATION,
        "authority": authority,
        "declared_by": declared_by,
        "evidence": evidence,
    }
    payload = validate_payload(
        LOOP_RECLAIM_KIND, {key: value for key, value in request.items() if value is not None}
    )
    marker_key = f"loop:{item_id}:reclaim:{payload['intent_event_id']}"
    metadata = _metadata(actor, harness, model, session)
    facts: dict[str, Any] = {}
    created = False

    def guard(conn: sqlite3.Connection) -> None:
        nonlocal created
        facts.update(_reclaim_guard(ledger, item_id, payload))
        created = True

    with ledger.transaction():
        marker = ledger._append_loop_event(
            _LOOP_WRITER_CAPABILITY,
            ignore_retry_attribution=True,
            kind=LOOP_RECLAIM_KIND,
            payload=payload,
            item_id=item_id,
            idempotency_key=marker_key,
            precondition=guard,
            **metadata,
        )
        if not created:
            # An exact retry: the store found the marker under its key and
            # ran no guard. The stored recovery is returned; nothing is written.
            done, claim = _reclaimed_record(ledger._conn, item_id, marker)
            return HandoffReclaim(marker, done, claim, created=False)
        operation, operation_id = facts["operation"], facts["operation_id"]
        done = ledger._append_loop_event(
            _LOOP_WRITER_CAPABILITY,
            ignore_retry_attribution=True,
            kind="loop.done",
            payload={
                "operation": operation,
                "operation_id": operation_id,
                "intent_event_id": payload["intent_event_id"],
                "outcome": LOOP_RECLAIM_DECLARATION,
                "source": f"event:{marker.id}",
            },
            item_id=item_id,
            idempotency_key=f"loop:{item_id}:{operation}:{operation_id}:done",
            **metadata,
        )
        lease_payload: dict[str, Any] = {
            "status": "claimed",
            "claim": payload["holder"],
            "prior_holder": payload["prior_holder"],
            "expires_at": lease.expires_at(facts["ttl"]),
        }
        if facts["spec_version"] is not None:
            lease_payload["spec_version"] = facts["spec_version"]
        claim = ledger.append(
            kind="item.state",
            payload=lease_payload,
            item_id=item_id,
            idempotency_key=f"{marker_key}:claim",
            **metadata,
        )
    return HandoffReclaim(marker, done, claim, created=True)


__all__ = [
    "HandoffReclaim",
    "OperationState",
    "PullRequestLink",
    "check_holder",
    "close_item",
    "finish_operation",
    "link_pull_request",
    "manage_item",
    "open_hand_off",
    "operation_state",
    "reclaim_handoff",
    "record_progress",
    "restamp_claim",
    "session_hand_off",
    "start_operation",
    "take_item",
]
