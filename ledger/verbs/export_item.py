"""Print one item's events; after close, without its ephemeral events.

The backup export (``ledger export``) keeps every event so a restore can
replay the hash chain. This is the per-item view a reader pulls from.
``--ticket`` prints the recorded ``### Ticket:`` section once instead.
"""
from __future__ import annotations

import json

from ledger.export import item_export, item_ticket_text
from ledger.verbs import (
    EXIT_NOT_FOUND,
    CliError,
    Context,
    Result,
    Verb,
    open_db,
    require_item,
)


def add_arguments(parser) -> None:
    parser.add_argument("item")
    parser.add_argument(
        "--ticket",
        action="store_true",
        help="print the recorded ### Ticket: section once; source is read from the current repository",
    )


def run(args, ctx: Context) -> Result:
    lg = open_db(ctx, write=False)
    try:
        require_item(lg, args.item)
        if args.ticket:
            try:
                text = item_ticket_text(lg, args.item)
            except ValueError as exc:
                raise CliError(
                    str(exc), code="not_found", exit_code=EXIT_NOT_FOUND,
                ) from None
            return Result(data={"item_id": args.item, "ticket": text}, text=text)
        records = item_export(lg, args.item)
    finally:
        lg.close()
    text = "\n".join(json.dumps(record, sort_keys=True) for record in records)
    return Result(data={"item_id": args.item, "events": records}, text=text)


VERB = Verb(
    name="export-item",
    help="print one item's events (ephemeral ones decay after close)",
    add_arguments=add_arguments,
    run=run,
)
