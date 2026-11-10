"""Query current items with filters and a bounded row cap."""
from __future__ import annotations

from ledger.search import in_window, newest_event_times
from ledger.verbs import (
    DEFAULT_LIMIT,
    EXIT_NOT_FOUND,
    CliError,
    Context,
    Result,
    UsageError,
    Verb,
    format_items,
    item_dict,
    open_db,
    resolve_limit,
)
from ledger.verbs.search import UNTIL_HELP, when_option


def add_arguments(parser) -> None:
    parser.add_argument("--item")
    parser.add_argument("--type")
    parser.add_argument("--status")
    parser.add_argument(
        "--parent",
        metavar="ITEM",
        help="only the children of ITEM (items whose current child-of anchor is ITEM)",
    )
    parser.add_argument(
        "--duplicate-of",
        dest="duplicate_of",
        metavar="ITEM",
        help="only the duplicates of ITEM (other items with a duplicate-of link to ITEM)",
    )
    parser.add_argument(
        "--blocks",
        metavar="ITEM",
        help="only the items that block ITEM (items with a blocks link to ITEM)",
    )
    parser.add_argument(
        "--since",
        metavar="DATE",
        help="YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS][Z|+HH:MM], inclusive, UTC unless it has an "
        "offset; compares an item's newest event (the same grammar as search --since)",
    )
    parser.add_argument("--until", metavar="DATE", help=UNTIL_HELP)
    parser.add_argument(
        "--limit",
        type=int,
        help=f"max rows (default {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="no row cap: every matching item (not with --limit)",
    )


def run(args, ctx: Context) -> Result:
    if args.all and args.limit is not None:
        raise UsageError("--all and --limit cannot be used together")
    limit = None if args.all else resolve_limit(args.limit)
    since = when_option(args.since, "--since", end_of_day=False)
    until = when_option(args.until, "--until", end_of_day=True)
    if since is not None and until is not None and since > until:
        raise UsageError("--since is after --until")
    lg = open_db(ctx, write=False)
    try:
        newest = {}
        if since is not None or until is not None:
            # The same date search gives an item: its newest event of any kind.
            newest = newest_event_times(
                (row["item_id"], row["recorded_at"])
                for row in lg._conn.execute(
                    "SELECT item_id, recorded_at FROM events WHERE item_id IS NOT NULL"
                )
            )
        duplicates: set[str] = set()
        blocking: set[str] = set()
        if args.duplicate_of is not None or args.blocks is not None:
            # The rows a card's Links and Blockers hold in other items' events.
            for event in lg.events(kind="item.linked"):
                rel = event.payload.get("rel")
                target = event.payload.get("target")
                if (
                    rel == "duplicate-of"
                    and target == args.duplicate_of
                    and event.item_id != target
                ):
                    duplicates.add(event.item_id)
                if rel == "blocks" and target == args.blocks:
                    blocking.add(event.item_id)
        matched = []
        for row in lg.items_with_revision():
            item = item_dict(row)
            item["revision"] = row["revision"]
            item["parent_id"] = row["parent_id"]
            item["children_open"] = row["children_open"]
            if args.item and item["item_id"] != args.item:
                continue
            if args.type and item["type"] != args.type:
                continue
            if args.status and item["status"] != args.status:
                continue
            if args.parent is not None and row.get("parent_id") != args.parent:
                continue
            if args.duplicate_of is not None and item["item_id"] not in duplicates:
                continue
            if args.blocks is not None and item["item_id"] not in blocking:
                continue
            if not in_window(newest.get(item["item_id"], (None,))[0], since, until):
                continue
            matched.append(item)
    finally:
        lg.close()
    capped = matched if limit is None else matched[:limit]
    truncated = limit is not None and len(matched) > limit
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
