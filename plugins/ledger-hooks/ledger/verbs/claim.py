"""Claim an item, with optional per-status capacity."""
from __future__ import annotations

from ledger import lease
from ledger.store import item_is_loop_managed
from ledger.verbs import (
    EXIT_NOT_FOUND,
    EXIT_REFUSED,
    CliError,
    Context,
    Result,
    UsageError,
    Verb,
    actor_kwargs,
    event_dict,
    new_key,
    open_db,
    require_item,
)

CLAIM_STATUS = "claimed"


def add_arguments(parser) -> None:
    parser.add_argument("item")
    parser.add_argument(
        "--capacity",
        type=int,
        help="refuse if this many items already have the claimed status",
    )
    parser.add_argument("--owner")
    parser.add_argument("--claim")
    parser.add_argument(
        "--ttl",
        type=int,
        help="lease length in seconds (default: claim_ttl_seconds in config.json, else 4 hours)",
    )


def _loop_held_by(conn, row, item_id: str, requester) -> bool:
    """True when ``requester`` holds the loop's own claim on a loop-managed item.

    The loop's claim is the current claim event when that event carries no
    ``expires_at``: the loop take writes none. This reads the claim's own
    event, not ``v_leases`` (progress extends the view's lease). A claim that
    ``ledger claim`` stamped after the loop released the item is a lease, so
    renewing it re-stamps it.
    """
    if row is None or row["status"] != CLAIM_STATUS or not requester:
        return False
    if (row["owner"] or row["claim"]) != requester:
        return False
    if not item_is_loop_managed(conn, item_id):
        return False
    claim_event = conn.execute(
        "SELECT json_extract(payload, '$.expires_at') IS NULL FROM events WHERE id = ?",
        (row["state_event_id"],),
    ).fetchone()
    return claim_event is not None and bool(claim_event[0])


def run(args, ctx: Context) -> Result:
    if args.capacity is not None and args.capacity < 0:
        raise UsageError("--capacity must be >= 0")
    owner = args.owner or ctx.actor
    claim = args.claim or ctx.actor
    if args.ttl is not None:
        try:
            lease.check_ttl(args.ttl, "--ttl")
        except lease.LeaseConfigError as exc:
            raise UsageError(str(exc)) from exc
    payload = {"status": CLAIM_STATUS}
    if owner:
        payload["owner"] = owner
    if claim:
        payload["claim"] = claim
    lg = open_db(ctx, write=True)
    try:
        require_item(lg, args.item)
        try:
            ttl = args.ttl if args.ttl is not None else lease.ttl_for_db(lg.path)
        except lease.LeaseConfigError as exc:
            raise UsageError(str(exc)) from exc
        moment = lease.now()
        requester = owner or claim
        seen = lg._conn.execute(
            "SELECT * FROM v_items WHERE item_id = ?", (args.item,)
        ).fetchone()
        # DESIGN: the holder of a loop-managed item re-claiming it gets the
        # claim re-written without ``expires_at``: a lease stamped onto the
        # loop's claim would expire under the running loop (SL47 gives loop
        # claims a renewed lease).
        loop_held = _loop_held_by(lg._conn, seen, args.item, requester)
        if not loop_held:
            payload["expires_at"] = lease.expires_at(ttl, at=moment)
        prior = (
            lease.takeover_holder(
                seen, lease.current_lease(lg._conn, args.item), requester, moment
            )
            if seen is not None
            else None
        )
        if prior:
            payload["prior_holder"] = prior
        # DESIGN (LC3 D4): the claim names the spec version it builds
        # against, the item's current one; an item with no spec stamps none.
        spec_version = seen["spec_version"] if seen is not None else None
        if spec_version is not None:
            payload["spec_version"] = spec_version

        def precondition(conn) -> None:
            rows = [dict(row) for row in conn.execute("SELECT * FROM v_items")]
            current = next((row for row in rows if row["item_id"] == args.item), None)
            if current is None:
                raise CliError(
                    f"item not found: {args.item}",
                    code="not_found",
                    exit_code=EXIT_NOT_FOUND,
                )
            if _loop_held_by(conn, current, args.item, requester) != loop_held:
                raise CliError(
                    "the claim changed while this claim was being recorded; retry",
                    code="claim_changed",
                    exit_code=EXIT_REFUSED,
                )
            if current["spec_version"] != spec_version:
                raise CliError(
                    "the spec changed while this claim was being recorded; retry",
                    code="claim_changed",
                    exit_code=EXIT_REFUSED,
                )
            try:
                lease.guard_claim(
                    current,
                    lease.current_lease(conn, args.item),
                    requester,
                    moment,
                    payload.get("prior_holder"),
                )
            except lease.ClaimRefused as exc:
                raise CliError(str(exc), code=exc.code, exit_code=EXIT_REFUSED) from exc
            if args.capacity is not None:
                # An expired claim is open again, so it holds no capacity. An
                # unreadable stamp counts as expired only where v_ready's text
                # comparison also reads it as expired.
                dead = {
                    r["item_id"]
                    for r in lease.expired_leases(conn, at=moment)
                    if not r.get("unreadable_stamp")
                    or lease.is_expired(r["expires_at"], at=moment)
                }
                held = [
                    row
                    for row in rows
                    if row["status"] == CLAIM_STATUS
                    and row["item_id"] != args.item
                    and row["item_id"] not in dead
                ]
                if len(held) >= args.capacity:
                    raise CliError(
                        f"capacity {args.capacity} already reached for status {CLAIM_STATUS}",
                        code="capacity",
                        exit_code=EXIT_REFUSED,
                    )

        event = lg.append(
            kind="item.state",
            payload=payload,
            item_id=args.item,
            idempotency_key=new_key(),
            precondition=precondition,
            **actor_kwargs(ctx),
        )
    finally:
        lg.close()
    return Result(
        data={"item_id": args.item, "event": event_dict(event)},
        text=f"claimed\t{args.item}\tevent={event.id}"
        + (f"\tprior_holder={prior}" if prior else "")
        + ("\tlease=none (loop-managed)" if loop_held else ""),
    )


VERB = Verb(
    name="claim",
    help="claim an item (optional per-status capacity)",
    add_arguments=add_arguments,
    run=run,
    writes=True,
)
