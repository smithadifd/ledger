"""Recover a dead session hand-off: move its claim to a named successor.

``ledger reclaim-handoff`` is the one CLI verb that writes loop-reserved
events. It parses the request and calls ``ledger.loop.reclaim_handoff``,
which holds every check and writes the marker, the old operation's
``loop.done`` (outcome ``seat-lost``) and the successor's leased claim in one
transaction, or nothing. Nothing is declared for the caller:
``--declare-seat-lost`` must be given, and every other field is the caller's
own, never read from the item.
"""
from __future__ import annotations

import re

from ledger import loop
from ledger.card import _one_line
from ledger.store import open_ledger
from ledger.verbs import Context, Result, Verb, actor_kwargs, event_dict

# A positive event id as decimal digits, at most 19 of them (2^63-1 has 19).
# Anything else is passed on as text, so the library names the refusal.
_EVENT_ID_TEXT = re.compile(r"[1-9][0-9]{0,18}")


def add_arguments(parser) -> None:
    parser.add_argument("item", help="the handed-off item")
    parser.add_argument(
        "--intent", metavar="EVENT_ID", help="the hand-off's loop.intent event id, as you read it"
    )
    parser.add_argument(
        "--claim", metavar="EVENT_ID", help="the item's current claim event id, as you read it"
    )
    parser.add_argument(
        "--from",
        dest="prior_holder",
        metavar="HOLDER",
        help="the holder whose seat is lost, exactly as the claim names it",
    )
    parser.add_argument(
        "--to", dest="holder", metavar="SEAT@SESSION", help="the successor that takes the claim"
    )
    parser.add_argument(
        "--declare-seat-lost",
        action="store_true",
        help="declare that the holder's seat is lost and its workers stopped (required)",
    )
    parser.add_argument(
        "--authority", metavar="operator|successor", help="who declares: operator or successor"
    )
    parser.add_argument(
        "--declared-by",
        metavar="NAME",
        help="the declarant; under successor authority, the successor itself",
    )
    parser.add_argument(
        "--evidence", metavar="REF", help="one line, at most 300 characters, naming the evidence"
    )


def _event_id(raw: str | None) -> int | str | None:
    if raw is None:
        return None
    return int(raw) if _EVENT_ID_TEXT.fullmatch(raw) else raw


def run(args, ctx: Context) -> Result:
    # Never creates a database: a recovery names an existing hand-off.
    lg = open_ledger(ctx.db, create=False)
    try:
        result = loop.reclaim_handoff(
            lg,
            args.item,
            intent_event_id=_event_id(args.intent),
            claim_event_id=_event_id(args.claim),
            prior_holder=args.prior_holder,
            holder=args.holder,
            declare_seat_lost=args.declare_seat_lost,
            authority=args.authority,
            declared_by=args.declared_by,
            evidence=args.evidence,
            **actor_kwargs(ctx),
        )
    finally:
        lg.close()
    marker = result.reclaimed
    events = (marker, result.done, result.claim)
    word = "reclaimed" if result.created else "already reclaimed"
    return Result(
        data={
            "item_id": args.item,
            "created": result.created,
            "reclaimed": event_dict(marker),
            "done": event_dict(result.done),
            "claim": event_dict(result.claim),
        },
        text=(
            f"{word}\t{_one_line(args.item)}"
            f"\tfrom={_one_line(marker.payload['prior_holder'])}"
            f"\tto={_one_line(marker.payload['holder'])}"
            f"\tevents={','.join(str(event.id) for event in events)}"
        ),
    )


VERB = Verb(
    name="reclaim-handoff",
    help="move a dead session hand-off's claim to a named successor (--declare-seat-lost)",
    add_arguments=add_arguments,
    run=run,
    writes=True,
)
