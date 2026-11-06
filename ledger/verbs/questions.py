"""List open asks across all items, oldest first."""
from __future__ import annotations

from datetime import UTC, datetime

from ledger.search import MAX_LIMIT, in_window
from ledger.verbs import (
    DEFAULT_LIMIT,
    Context,
    Result,
    UsageError,
    Verb,
    open_db,
)
from ledger.verbs.search import UNTIL_HELP, when_option


def add_arguments(parser) -> None:
    parser.add_argument("--item", metavar="ID", help="asks recorded on this item")
    parser.add_argument("--type", metavar="TYPE", help="the item's type")
    parser.add_argument(
        "--since",
        metavar="DATE",
        help=(
            "YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS][Z|+HH:MM], inclusive, UTC unless it has an "
            "offset; compares the ask's recorded_at (the same grammar as query --since)"
        ),
    )
    parser.add_argument("--until", metavar="DATE", help=UNTIL_HELP)
    parser.add_argument(
        "--limit",
        type=int,
        help=f"max rows, 1 to {MAX_LIMIT} (default {DEFAULT_LIMIT})",
    )


def _limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if type(value) is not int or not 1 <= value <= MAX_LIMIT:
        raise UsageError(f"--limit must be an integer from 1 to {MAX_LIMIT}")
    return value


def _recorded_micros(recorded_at: str | None) -> int | None:
    if recorded_at is None:
        return None
    try:
        moment = datetime.fromisoformat(recorded_at)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    delta = moment - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _age(recorded_at: str) -> str:
    try:
        moment = datetime.fromisoformat(recorded_at)
    except (TypeError, ValueError):
        return "0m"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = max(0, int((datetime.now(UTC) - moment).total_seconds()))
    days = seconds // 86400
    if days:
        return f"{days}d"
    hours = seconds // 3600
    if hours:
        return f"{hours}h"
    return f"{seconds // 60}m"


def _flatten(text: str | None) -> str:
    if not text:
        return ""
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _line(row: dict) -> str:
    return f"{_age(row['recorded_at'])}\t{row['item_id']}\t{_flatten(row['question'])}"


def _question(row) -> dict:
    return {
        "item_id": row["item_id"],
        "asked_event_id": int(row["asked_event_id"]),
        "question": row["question"],
        "recommendation": row["recommendation"],
        "default": row["default"],
        "recorded_at": row["recorded_at"],
        "type": row["type"],
        "title": row["title"],
        "status": row["status"],
    }


def run(args, ctx: Context) -> Result:
    limit = _limit(args.limit)
    since = when_option(args.since, "--since", end_of_day=False)
    until = when_option(args.until, "--until", end_of_day=True)
    if since is not None and until is not None and since > until:
        raise UsageError("--since is after --until")
    lg = open_db(ctx, write=False)
    try:
        matched = []
        for row in lg._conn.execute(
            """
            SELECT
              d.item_id AS item_id,
              d.asked_event_id AS asked_event_id,
              d.question AS question,
              d.recommendation AS recommendation,
              d."default" AS "default",
              d.recorded_at AS recorded_at,
              i.type AS type,
              i.title AS title,
              i.status AS status
            FROM v_open_decisions AS d
            JOIN v_items AS i ON i.item_id = d.item_id
            ORDER BY d.asked_event_id ASC
            """
        ):
            if args.item and row["item_id"] != args.item:
                continue
            if args.type and row["type"] != args.type:
                continue
            if not in_window(_recorded_micros(row["recorded_at"]), since, until):
                continue
            matched.append(_question(row))
    finally:
        lg.close()
    capped = matched[:limit]
    return Result(
        data={
            "questions": capped,
            "limit": limit,
            "truncated": len(matched) > limit,
        },
        text="\n".join(_line(row) for row in capped),
    )


VERB = Verb(
    name="questions",
    help="list open asks across all items, oldest first",
    add_arguments=add_arguments,
    run=run,
)
