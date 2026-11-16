"""Record an item.state event."""
from __future__ import annotations

from ledger.schema import ITEM_STATUSES
from ledger.verbs import (
    Context,
    Result,
    Verb,
    actor_kwargs,
    event_dict,
    add_expect_revision_argument,
    expect_revision_value,
    new_key,
    open_db,
    require_item,
)


def add_arguments(parser) -> None:
    parser.add_argument("item")
    parser.add_argument("--status", required=True, choices=ITEM_STATUSES)
    parser.add_argument("--owner")
    parser.add_argument("--claim")
    parser.add_argument("--blocked-by", action="append", default=[])
    add_expect_revision_argument(parser)


def run(args, ctx: Context) -> Result:
    expect = expect_revision_value(args)
    payload = {"status": args.status}
    if args.owner:
        payload["owner"] = args.owner
    if args.claim:
        payload["claim"] = args.claim
    if args.blocked_by:
        payload["blocked_by"] = list(args.blocked_by)
    lg = open_db(ctx, write=True)
    try:
        require_item(lg, args.item)
        event = lg.append(
            kind="item.state",
            payload=payload,
            item_id=args.item,
            idempotency_key=new_key(),
            expect_revision=expect,
            **actor_kwargs(ctx),
        )
    finally:
        lg.close()
    return Result(
        data={"item_id": args.item, "event": event_dict(event)},
        text=f"state\t{args.item}\t{args.status}\tevent={event.id}",
    )


VERB = Verb(
    name="state",
    help="set an item's current status",
    add_arguments=add_arguments,
    run=run,
    writes=True,
)
