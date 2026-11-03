"""Query current items with filters and a bounded row cap."""
from __future__ import annotations

from ledger.verbs import (
    DEFAULT_LIMIT,
    EXIT_NOT_FOUND,
    CliError,
    Context,
    Result,
    Verb,
    format_items,
    item_dict,
    open_db,
    resolve_limit,
)


def add_arguments(parser) -> None:
    parser.add_argument("--item")
    parser.add_argument("--type")
    parser.add_argument("--status")
    parser.add_argument(
        "--limit",
        type=int,
        help=f"max rows (default {DEFAULT_LIMIT})",
    )


def run(args, ctx: Context) -> Result:
    limit = resolve_limit(args.limit)
    lg = open_db(ctx, write=False)
    try:
        matched = []
        for row in lg.items_with_revision():
            item = item_dict(row)
            item["revision"] = row["revision"]
            if args.item and item["item_id"] != args.item:
                continue
            if args.type and item["type"] != args.type:
                continue
            if args.status and item["status"] != args.status:
                continue
            matched.append(item)
    finally:
        lg.close()
    capped = matched[:limit]
    truncated = len(matched) > limit
    if not capped:
        raise CliError("no matching rows", code="empty", exit_code=EXIT_NOT_FOUND)
    return Result(
        data={
            "items": capped,
            "limit": limit,
            "truncated": truncated,
        },
        text=format_items(capped),
    )


VERB = Verb(
    name="query",
    help="list current items (filters; default row cap plus --limit)",
    add_arguments=add_arguments,
    run=run,
)
