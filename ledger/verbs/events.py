"""List events across all items, newest first, or print events by id.

``--id N`` (repeatable) prints exactly the events with those ids, in id order
(oldest first), each once. It reads by id alone: with any other filter or
``--limit`` it is refused as a usage error, and an id no event has is refused
by name (``not_found``, exit 1) with nothing printed.

The text form is one event per line, tab-separated. A text column (kind,
item, actor) prints ``-`` when empty and escapes every non-printable
character (tab, newline, other controls) the way ``repr`` writes it, so a
stored value never splits a line or a column.
"""
from __future__ import annotations

import json
import re
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
MAX_EVENT_ID = 2**63 - 1  # SQLite integer ceiling
_EVENT_ID = re.compile(r"[1-9][0-9]*")
# The options --id refuses beside it: (flag, args attribute).
_FILTERS = (
    ("--since", "since"),
    ("--until", "until"),
    ("--kind", "kind"),
    ("--item", "item"),
    ("--actor", "actor"),
    ("--limit", "limit"),
)
_COLUMNS = (
    "SELECT id, kind, item_id, actor, harness, model, session, "
    "recorded_at, payload FROM v_events"
)


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
    parser.add_argument(
        "--id",
        action="append",
        dest="event_id",
        metavar="N",
        help=(
            "print exactly the event with this id; repeatable; printed in id "
            "order; an unknown id is refused; not with any other filter or --limit"
        ),
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


def text_cell(value: object) -> str:
    """One text column: ``-`` when empty, else the value with every
    non-printable character escaped as ``repr`` writes it (a tab as ``\\t``,
    a newline as ``\\n``), so it never splits a line or a column."""
    if value is None or value == "":
        return "-"
    text = value if isinstance(value, str) else str(value)
    return "".join(ch if ch.isprintable() else repr(ch)[1:-1] for ch in text)


def _line(event: dict) -> str:
    return (
        f"{text_cell(event['recorded_at'])}\t{text_cell(event['kind'])}\t"
        f"{text_cell(event['item_id'])}\t{text_cell(event['actor'])}\t"
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


def _event_ids(raw: list[str]) -> list[int]:
    """The ``--id`` values as event ids, each once, in id order."""
    ids: set[int] = set()
    for value in raw:
        if _EVENT_ID.fullmatch(value) is None:
            raise UsageError(f"--id must be a positive event id, got {value!r}")
        if len(value) > len(str(MAX_EVENT_ID)) or int(value) > MAX_EVENT_ID:
            raise UsageError(f"--id is too large to be an event id: {value}")
        ids.add(int(value))
    return sorted(ids)


def _by_id(args, ctx: Context) -> Result:
    given = [flag for flag, name in _FILTERS if getattr(args, name) is not None]
    if given:
        raise UsageError(f"--id cannot be combined with {', '.join(given)}")
    wanted = _event_ids(args.event_id)
    lg = open_db(ctx, write=False)
    try:
        found = []
        missing = []
        for event_id in wanted:
            row = lg._conn.execute(f"{_COLUMNS} WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                missing.append(str(event_id))
            else:
                found.append(_event(row))
    finally:
        lg.close()
    if missing:
        raise CliError(
            f"event not found: {', '.join(missing)}",
            code="not_found",
            exit_code=EXIT_NOT_FOUND,
        )
    return Result(
        data={"events": found, "limit": None, "truncated": False},
        text="\n".join(_line(event) for event in found),
    )


def run(args, ctx: Context) -> Result:
    if args.event_id is not None:
        return _by_id(args, ctx)
    limit = _limit(args.limit)
    since = when_option(args.since, "--since", end_of_day=False)
    until = when_option(args.until, "--until", end_of_day=True)
    if since is not None and until is not None and since > until:
        raise UsageError("--since is after --until")
    kinds = set(args.kind) if args.kind else None
    lg = open_db(ctx, write=False)
    try:
        matched = []
        for row in lg._conn.execute(f"{_COLUMNS} ORDER BY id DESC"):
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
    help="list events across all items, newest first, or print events by --id",
    add_arguments=add_arguments,
    run=run,
)
