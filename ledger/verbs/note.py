"""Record an item.note event."""
from __future__ import annotations

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
    parser.add_argument("--text", required=True)
    add_expect_revision_argument(parser)


def run(args, ctx: Context) -> Result:
    expect = expect_revision_value(args)
    lg = open_db(ctx, write=True)
    try:
        require_item(lg, args.item)
        event = lg.append(
            kind="item.note",
            payload={"text": args.text},
            item_id=args.item,
            idempotency_key=new_key(),
            expect_revision=expect,
            **actor_kwargs(ctx),
        )
    finally:
        lg.close()
    return Result(
        data={"item_id": args.item, "event": event_dict(event)},
        text=f"note\t{args.item}\tevent={event.id}",
    )


VERB = Verb(
    name="note",
    help="append a note to an item",
    add_arguments=add_arguments,
    run=run,
    writes=True,
)
