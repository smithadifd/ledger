"""Print one item's events; after close, without its ephemeral events.

The backup export (``ledger export``) keeps every event so a restore can
replay the hash chain. This is the per-item view a reader pulls from.
``--ticket`` prints the recorded ``### Ticket:`` section once instead. An
item whose source is a row key ``row:<ID>`` is read at the locator of its
newest valid ``also at <locator>`` note.

The view ends with one completeness line (``--json``: a ``completeness``
object in ``data``) naming the events returned, the events on the item and
the events decayed, so a reader knows whether it holds the whole record.
"""
from __future__ import annotations

import json

from ledger.export import item_export_counted, item_ticket_text
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
        records, completeness = item_export_counted(lg, args.item)
    finally:
        lg.close()
    line = (
        f"completeness: {completeness['events_returned']} events returned, "
        f"{completeness['events_on_item']} events on the item, "
        f"{completeness['events_decayed']} decayed"
    )
    text = "\n".join([*(json.dumps(record, sort_keys=True) for record in records), line])
    return Result(
        data={"item_id": args.item, "events": records, "completeness": completeness},
        text=text,
    )


VERB = Verb(
    name="export-item",
    help="print one item's events (ephemeral ones decay after close)",
    add_arguments=add_arguments,
    run=run,
)
