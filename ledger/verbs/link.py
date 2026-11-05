"""Record an item.linked event.

``--rel duplicate-of`` marks ``item`` a duplicate of ``--target`` and closes
it: the link and the close land in one transaction or not at all. It refuses
an item that is already closed, a target that is not an item or is the item
itself, and a target that is itself closed as a duplicate. Both cards show
the relation (the kept item's as ``duplicated-by``). There is no merge verb.
"""
from __future__ import annotations

import argparse

from ledger.schema import LINK_RELS
from ledger.verbs import (
    EXIT_NOT_FOUND,
    EXIT_REFUSED,
    CliError,
    Context,
    Result,
    UsageError,
    Verb,
    actor_kwargs,
    event_dict,
    new_key,
    open_db,
    require_item,
)

DUPLICATE_OF = "duplicate-of"
DEFAULT_REL = "url"

EPILOG = """\
The target is given once: after the item, or with --target. Without --rel the
relation is url, so a local file path or a web address needs no flag.

examples:
  ledger link it-1a2b3c4d5e6f /home/me/notes/brief.md
  ledger link it-1a2b3c4d5e6f --rel pr --target https://github.com/owner/repo/pull/12
  ledger link it-1a2b3c4d5e6f --rel branch feat/retry
"""

USAGE = (
    "link takes exactly one target: link ITEM TARGET [--rel REL] "
    "(or link ITEM --target TARGET [--rel REL]); a relation goes after --rel, "
    "never as a bare word"
)


def add_arguments(parser) -> None:
    parser.epilog = EPILOG
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.add_argument("item")
    parser.add_argument(
        "positional_target",
        nargs="*",
        metavar="TARGET",
        help="a path, URL, PR, branch, session or item id (or give --target)",
    )
    parser.add_argument(
        "--rel",
        default=DEFAULT_REL,
        choices=LINK_RELS,
        help=f"the relation (default {DEFAULT_REL}); "
        f"{DUPLICATE_OF} also closes the item as a duplicate of the target",
    )
    parser.add_argument("--target", help="the target, as a flag")


def _target(args) -> str:
    """The one target the argv names: positional or ``--target``, never both."""
    named = list(getattr(args, "positional_target", None) or [])
    option = getattr(args, "target", None)
    if option is not None:
        named.append(option)
    if len(named) != 1:
        raise UsageError(USAGE)
    return named[0]


def _closed_as_duplicate(lg, item_id: str) -> bool:
    return any(
        event.payload.get("rel") == DUPLICATE_OF
        for event in lg.events(item_id=item_id, kind="item.linked")
    )


def _duplicate(args, ctx: Context) -> Result:
    loser, kept = args.item, args.target
    if loser == kept:
        raise CliError(
            f"an item cannot be a duplicate of itself: {loser}",
            code="self_link",
            exit_code=EXIT_REFUSED,
        )
    lg = open_db(ctx, write=True)
    try:
        require_item(lg, loser)
        # One write transaction: the checks read the state the appends land on.
        with lg.transaction():
            row = lg.item(loser)
            if row is None:
                raise CliError(
                    f"item not found: {loser}", code="not_found", exit_code=EXIT_NOT_FOUND
                )
            if row["status"] == "closed":
                raise CliError(
                    f"{loser} is already closed", code="closed", exit_code=EXIT_REFUSED
                )
            target = lg.item(kept)
            if target is None:
                raise CliError(
                    f"item not found: {kept}", code="not_found", exit_code=EXIT_NOT_FOUND
                )
            if target["status"] == "closed" and _closed_as_duplicate(lg, kept):
                raise CliError(
                    f"{kept} is itself closed as a duplicate; link to the item it duplicates",
                    code="duplicate_chain",
                    exit_code=EXIT_REFUSED,
                )
            link = lg.append(
                kind="item.linked",
                payload={"rel": DUPLICATE_OF, "target": kept},
                item_id=loser,
                idempotency_key=new_key(),
                **actor_kwargs(ctx),
            )
            closed = lg.append(
                kind="item.state",
                payload={"status": "closed", "reason": f"duplicate of {kept}"},
                item_id=loser,
                idempotency_key=new_key(),
                **actor_kwargs(ctx),
            )
    finally:
        lg.close()
    return Result(
        data={
            "item_id": loser,
            "event": event_dict(link),
            "closed_event": event_dict(closed),
        },
        text=(
            f"linked\t{loser}\t{DUPLICATE_OF}\t{kept}\tevent={link.id}\n"
            f"closed\t{loser}\tevent={closed.id}"
        ),
    )


def run(args, ctx: Context) -> Result:
    args.target = _target(args)
    if args.rel == DUPLICATE_OF:
        return _duplicate(args, ctx)
    lg = open_db(ctx, write=True)
    try:
        require_item(lg, args.item)
        event = lg.append(
            kind="item.linked",
            payload={"rel": args.rel, "target": args.target},
            item_id=args.item,
            idempotency_key=new_key(),
            **actor_kwargs(ctx),
        )
    finally:
        lg.close()
    return Result(
        data={"item_id": args.item, "event": event_dict(event)},
        text=f"linked\t{args.item}\t{args.rel}\t{args.target}\tevent={event.id}",
    )


VERB = Verb(
    name="link",
    help="link an item to a PR, branch, session, URL, or other item",
    add_arguments=add_arguments,
    run=run,
    writes=True,
    late_positional="positional_target",
)
