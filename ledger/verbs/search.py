"""Search the derived index; refresh it first when it is missing or stale."""
from __future__ import annotations

from ledger.schema import ITEM_STATUSES
from ledger.search import DOC_TYPES, MAX_LIMIT, SearchError, parse_when, search
from ledger.verbs import (
    EXIT_NOT_FOUND,
    EXIT_REFUSED,
    CliError,
    Context,
    Result,
    UsageError,
    Verb,
)

DEFAULT_SEARCH_LIMIT = 10


def add_arguments(parser) -> None:
    parser.add_argument("query", help="free text; each word is matched, ranked by bm25")
    parser.add_argument(
        "--limit",
        type=int,
        help=f"max cards, 1 to {MAX_LIMIT} (default {DEFAULT_SEARCH_LIMIT})",
    )
    parser.add_argument("--type", dest="doc_type", choices=DOC_TYPES)
    parser.add_argument(
        "--status",
        choices=ITEM_STATUSES,
        help="only documents of items in this current status",
    )
    parser.add_argument(
        "--item",
        metavar="ID",
        help="only this item's documents: the item, its decisions and utterances",
    )
    parser.add_argument(
        "--initiative",
        metavar="ID",
        help="only documents of items linked to this initiative (link --rel item)",
    )
    parser.add_argument(
        "--since",
        metavar="DATE",
        help="YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS][Z|+HH:MM], inclusive, UTC unless it has an offset; compares "
        "an event's recorded time, an item's newest event, a file's mtime",
    )
    parser.add_argument(
        "--until",
        metavar="DATE",
        help="ISO date or date-time, inclusive (a bare date keeps the whole day)",
    )


def _limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_SEARCH_LIMIT
    if not 1 <= value <= MAX_LIMIT:
        raise UsageError(f"--limit must be an integer from 1 to {MAX_LIMIT}")
    return value


def _when(value: str | None, flag: str, *, end_of_day: bool):
    if value is None:
        return None
    try:
        return parse_when(value, end_of_day=end_of_day)
    except (ValueError, OverflowError):
        raise UsageError(
            f"{flag} is not YYYY-MM-DD or an ISO date-time "
            f"(YYYY-MM-DDTHH:MM[:SS][Z|+HH:MM]) in range: {value!r}"
        ) from None


def _text(cards: list[dict]) -> str:
    lines = []
    for card in cards:
        lines.append(f"{card['score']:.3f}\t{card['doc_type']}\t{card['id']}\t{card['title']}")
        if card["snippet"]:
            lines.append(f"\t{card['snippet']}")
        if "caveat" in card:
            lines.append(f"\t({card['caveat']})")
    return "\n".join(lines)


def run(args, ctx: Context) -> Result:
    limit = _limit(args.limit)
    since = _when(args.since, "--since", end_of_day=False)
    until = _when(args.until, "--until", end_of_day=True)
    if since is not None and until is not None and since > until:
        raise UsageError("--since is after --until")
    try:
        data = search(
            ctx.db,
            args.query,
            limit=limit,
            doc_type=args.doc_type,
            status=args.status,
            item=args.item,
            initiative=args.initiative,
            since=since,
            until=until,
            fallback_root=ctx.search_fallback_root,
        )
    except SearchError as exc:
        raise CliError(str(exc), code="search", exit_code=EXIT_REFUSED) from exc
    if not data["cards"]:
        raise CliError("no matching documents", code="empty", exit_code=EXIT_NOT_FOUND)
    return Result(data=data, text=_text(data["cards"]))


VERB = Verb(
    name="search",
    help="full-text search over items, decisions, standards and ADRs",
    add_arguments=add_arguments,
    run=run,
)
