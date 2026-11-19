"""List unblocked open items."""
from __future__ import annotations

from ledger import lease
from ledger.card import _read_snapshot
from ledger.verbs import (
    DEFAULT_LIMIT,
    EXIT_NOT_FOUND,
    CliError,
    Context,
    Result,
    Verb,
    format_items,
    item_dict,
    lease_rows,
    open_db,
    read_item,
    resolve_limit,
)


def add_arguments(parser) -> None:
    parser.add_argument(
        "--limit",
        type=int,
        help=f"max rows (default {DEFAULT_LIMIT})",
    )


def run(args, ctx: Context) -> Result:
    limit = resolve_limit(args.limit)
    lg = open_db(ctx, write=False)
    try:
        moment = lease.now()
        with _read_snapshot(lg):
            rows = lg.ready()
            leases = lease_rows(lg)
        # An expired claim is ready and reads open (lease.read_status).
        matched = [
            read_item(item_dict(row), leases.get(row["item_id"]), at=moment) for row in rows
        ]
    finally:
        lg.close()
    capped = matched[:limit]
    truncated = len(matched) > limit
    if not capped:
        raise CliError("no matching rows", code="empty", exit_code=EXIT_NOT_FOUND)
    return Result(
        data={"items": capped, "limit": limit, "truncated": truncated},
        text=format_items(capped),
    )


VERB = Verb(
    name="ready",
    help="list unblocked open items",
    add_arguments=add_arguments,
    run=run,
)
