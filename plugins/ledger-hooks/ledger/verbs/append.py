"""Append a validated event of any v1 kind."""
from __future__ import annotations

import json

from ledger.schema import ValidationError, schema_for, strip_directory_ref_slash
from ledger.verbs import (
    Context,
    Result,
    UsageError,
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
    parser.add_argument("--kind", required=True)
    parser.add_argument("--payload", required=True, help="JSON object")
    parser.add_argument("--item", dest="item_id")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--channel")
    parser.add_argument("--provenance-class")
    parser.add_argument("--payload-version", type=int, default=1)
    add_expect_revision_argument(parser)


def run(args, ctx: Context) -> Result:
    expect = expect_revision_value(args)
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        raise UsageError(f"payload is not JSON: {exc}") from exc
    if type(payload) is not dict:
        raise UsageError("payload must be a JSON object")
    payload = strip_directory_ref_slash(args.kind, payload, args.payload_version)
    lg = open_db(ctx, write=True)
    try:
        if args.item_id and args.kind != "item.created":
            try:
                item_mode = schema_for(args.kind, args.payload_version).get("item_id")
            except ValidationError:
                item_mode = None
            if item_mode == "required":
                require_item(lg, args.item_id)
        event = lg.append(
            kind=args.kind,
            payload=payload,
            item_id=args.item_id,
            idempotency_key=args.idempotency_key or new_key(),
            payload_version=args.payload_version,
            channel=args.channel,
            provenance_class=args.provenance_class,
            expect_revision=expect,
            **actor_kwargs(ctx),
        )
    finally:
        lg.close()
    return Result(
        data={"event": event_dict(event)},
        text=f"appended\t{event.kind}\tevent={event.id}\titem={event.item_id or '-'}",
    )


VERB = Verb(
    name="append",
    help="append a validated event of any v1 kind",
    add_arguments=add_arguments,
    run=run,
    writes=True,
)
