"""Record an item.note event."""
from __future__ import annotations

import argparse

from ledger.verbs import (
    Context,
    Result,
    UsageError,
    Verb,
    actor_kwargs,
    event_dict,
    add_expect_revision_argument,
    expect_revision_value,
    new_key,
    open_db,
    require_item,
)


EPILOG = """\
The text is given once: after the item, or with --text, never both.

examples:
  ledger note it-1a2b3c4d5e6f "tried the retry; it worked"
  ledger note it-1a2b3c4d5e6f --text "the same note, flag form"
"""

USAGE = "note takes exactly one text: note ITEM TEXT (or note ITEM --text TEXT)"


def add_arguments(parser) -> None:
    parser.epilog = EPILOG
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.add_argument("item")
    parser.add_argument("text", nargs="*", metavar="TEXT", help="the note's text")
    parser.add_argument(
        "--text", dest="text_option", metavar="TEXT", help="the note's text, as a flag"
    )
    add_expect_revision_argument(parser)


def _text(args) -> str:
    positional = list(getattr(args, "text", None) or [])
    option = getattr(args, "text_option", None)
    if len(positional) + (option is not None) != 1:
        raise UsageError(USAGE)
    return positional[0] if positional else option


def run(args, ctx: Context) -> Result:
    text = _text(args)
    expect = expect_revision_value(args)
    lg = open_db(ctx, write=True)
    try:
        require_item(lg, args.item)
        event = lg.append(
            kind="item.note",
            payload={"text": text},
            item_id=args.item,
            idempotency_key=new_key(),
            expect_revision=expect,
            **actor_kwargs(ctx),
        )
    finally:
        lg.close()
    return Result(
        data={"item_id": args.item, "event": event_dict(event)},
        text=f"note\t{args.item}\tevent={event.id}",
    )


VERB = Verb(
    name="note",
    help="append a note to an item",
    add_arguments=add_arguments,
    run=run,
    writes=True,
    late_positional="text",
)
