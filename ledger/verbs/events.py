"""List events across all items, newest first."""
from __future__ import annotations

import json
from datetime import UTC, datetime

from ledger.search import MAX_LIMIT, in_window
from ledger.verbs import (
    DEFAULT_LIMIT,
    EXIT_NOT_FOUND,
    CliError,
    Context,
    Result,
    UsageError,
    Verb,
    open_db,
)
from ledger.verbs.search import UNTIL_HELP, when_option

PAYLOAD_WIDTH = 120


def add_arguments(parser) -> None:
    parser.add_argument(
        "--since",
        metavar="DATE",
        help=(
            "YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS][Z|+HH:MM], inclusive, UTC unless it has an "
            "offset; compares an event's recorded_at (the same grammar as query --since)"
        ),
    )
    parser.add_argument("--until", metavar="DATE", help=UNTIL_HELP)
    parser.add_argument(
        "--kind",
        action="append",
        metavar="KIND",
        help="keep events of this kind; repeatable",
    )
    parser.add_argument("--item", metavar="ID", help="events recorded on this item")
    parser.add_argument(
        "--actor",
        metavar="ACTOR",
        help="exact match on the stored actor",
    )
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


def _payload_summary(payload: object) -> str:
    compact = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    compact = compact.replace("\n", " ").replace("\r", " ")
    if len(compact) > PAYLOAD_WIDTH:
        return compact[:PAYLOAD_WIDTH] + "…"
    return compact


def _line(event: dict) -> str:
    item = event["item_id"] if event["item_id"] else "-"
    actor = event["actor"] if event["actor"] else "-"
    return (
        f"{event['recorded_at']}\t{event['kind']}\t{item}\t{actor}\t"
        f"{_payload_summary(event['payload'])}"
    )


def _event(row) -> dict:
    payload = json.loads(row["payload"])
    return {
        "id": int(row["id"]),
        "kind": row["kind"],
        "item_id": row["item_id"],
        "actor": row["actor"],
        "harness": row["harness"],
        "model": row["model"],
        "session": row["session"],
        "recorded_at": row["recorded_at"],
        "payload": payload,
    }


def run(args, ctx: Context) -> Result:
    limit = _limit(args.limit)
    since = when_option(args.since, "--since", end_of_day=False)
    until = when_option(args.until, "--until", end_of_day=True)
    if since is not None and until is not None and since > until:
        raise UsageError("--since is after --until")
    kinds = set(args.kind) if args.kind else None
    lg = open_db(ctx, write=False)
    try:
        matched = []
        for row in lg._conn.execute(
            "SELECT id, kind, item_id, actor, harness, model, session, "
            "recorded_at, payload FROM v_events ORDER BY id DESC"
        ):
            if kinds is not None and row["kind"] not in kinds:
                continue
            if args.item and row["item_id"] != args.item:
                continue
            if args.actor is not None and row["actor"] != args.actor:
                continue
            if not in_window(_recorded_micros(row["recorded_at"]), since, until):
                continue
            matched.append(_event(row))
    finally:
        lg.close()
    if not matched:
        raise CliError("no matching events", code="empty", exit_code=EXIT_NOT_FOUND)
    capped = matched[:limit]
    return Result(
        data={
            "events": capped,
            "limit": limit,
            "truncated": len(matched) > limit,
        },
        text="\n".join(_line(event) for event in capped),
    )


VERB = Verb(
    name="events",
    help="list events across all items, newest first",
    add_arguments=add_arguments,
    run=run,
)
