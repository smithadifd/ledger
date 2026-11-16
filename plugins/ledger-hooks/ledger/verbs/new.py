"""Create an item (item.created)."""
from __future__ import annotations

import hashlib
import uuid

from ledger.store import IdempotencyConflict
from ledger.schema import ITEM_STATUSES, ITEM_TYPES
from ledger.verbs import (
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
)

def add_arguments(parser) -> None:
    parser.add_argument("--type", required=True, choices=ITEM_TYPES)
    parser.add_argument("--title", required=True)
    parser.add_argument("--id", dest="item_id", help="item id (generated if omitted)")
    parser.add_argument("--status", choices=ITEM_STATUSES)
    parser.add_argument(
        "--source",
        help=(
            "where the item comes from (surrounding whitespace trimmed); a repeat "
            "through new from the same source is the same item, and a different "
            "--id or title for that source is refused naming the existing item"
        ),
    )


def canonical_source(source: str) -> str:
    """The identity form of a source: surrounding whitespace trimmed, nothing else."""
    return source.strip()


def run(args, ctx: Context) -> Result:
    source = None
    if args.source is not None:
        source = canonical_source(args.source)
        if not source:
            raise UsageError("--source must not be empty")
        try:
            source.encode("utf-8")
        except UnicodeEncodeError:
            raise UsageError("--source must be valid UTF-8 text") from None
    if source is None:
        key = new_key()
        item_id = args.item_id or ("it-" + uuid.uuid4().hex[:12])
    else:
        key = f"item.created:{source}"
        # A sourced item with no --id gets an id derived from its key, so a
        # repeat names the same item and lands as the existing no-op.
        item_id = args.item_id or ("it-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12])
    payload = {"type": args.type, "title": args.title}
    if source is not None:
        payload["source"] = source
    if args.status:
        payload["status"] = args.status
    lg = open_db(ctx, write=True)
    try:

        def precondition(conn) -> None:
            existing = [row["item_id"] for row in conn.execute("SELECT item_id FROM v_items")]
            if item_id in existing:
                raise CliError(
                    f"item already exists: {item_id}",
                    code="conflict",
                    exit_code=EXIT_REFUSED,
                )

        def prior_event():
            return next(
                (e for e in lg.events(kind="item.created") if e.idempotency_key == key), None
            )

        def settle(prior):
            # Same payload and item: the existing no-op, whoever asks. Anything
            # else is the existing conflict, named by the item that holds the source.
            if prior.payload == payload and prior.item_id == item_id:
                return prior
            raise CliError(
                f"already created from this source as {prior.item_id}",
                code="conflict",
                exit_code=EXIT_REFUSED,
            )

        prior = prior_event() if source is not None else None
        if prior is not None:
            event = settle(prior)
        else:
            try:
                event = lg.append(
                    kind="item.created",
                    payload=payload,
                    item_id=item_id,
                    idempotency_key=key,
                    precondition=precondition,
                    **actor_kwargs(ctx),
                )
            except IdempotencyConflict:
                prior = prior_event() if source is not None else None
                if prior is None:
                    raise
                event = settle(prior)
    finally:
        lg.close()
    return Result(
        data={"item_id": item_id, "event": event_dict(event)},
        text=f"created\t{item_id}\tevent={event.id}\t{args.type}\t{args.title}",
    )


VERB = Verb(
    name="new",
    help="create an item",
    add_arguments=add_arguments,
    run=run,
    writes=True,
)
