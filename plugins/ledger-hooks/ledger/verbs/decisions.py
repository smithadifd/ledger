"""List decision records with their current standing.

Writes go through ``ledger append`` (kinds ``decision.recorded``,
``decision.standing``, ``decision.result``); the standing guard runs in the
store for every writer.
"""
from __future__ import annotations

from ledger.verbs import (
    EXIT_NOT_FOUND,
    CliError,
    Context,
    Result,
    UsageError,
    Verb,
    open_db,
    resolve_limit,
)


def add_arguments(parser) -> None:
    parser.add_argument("--item", help="decisions recorded on this item")
    parser.add_argument("--tag", help="decisions carrying TYPE:ID, from any item")
    parser.add_argument("--standing", choices=("lean", "settled", "superseded"))
    parser.add_argument("--limit", type=int)


def _tag(raw: str | None) -> tuple[str, str] | None:
    if raw is None:
        return None
    kind, sep, ident = raw.partition(":")
    if not sep or not kind or not ident:
        raise UsageError("--tag must be TYPE:ID")
    return kind, ident


def _line(row) -> str:
    tags = ",".join(f"{tag['type']}:{tag['id']}" for tag in row["tags"])
    return (
        f"{row['decision_event_id']}\t{row['item_id']}\t{row['standing']}\t"
        f"{row['phase']}\t{row['who']}\t{tags}\t{row['verbatim']}"
    )


def run(args, ctx: Context) -> Result:
    limit = resolve_limit(args.limit)
    tag = _tag(args.tag)
    lg = open_db(ctx, write=False)
    try:
        rows = lg.decision_records(item_id=args.item, tag=tag)
    finally:
        lg.close()
    if args.standing:
        rows = [row for row in rows if row["standing"] == args.standing]
    if not rows:
        raise CliError("no matching decisions", code="empty", exit_code=EXIT_NOT_FOUND)
    capped = rows[:limit]
    return Result(
        data={"decisions": capped, "limit": limit, "truncated": len(rows) > limit},
        text="\n".join(_line(row) for row in capped),
    )


VERB = Verb(
    name="decisions",
    help="list decision records with their current standing",
    add_arguments=add_arguments,
    run=run,
)
