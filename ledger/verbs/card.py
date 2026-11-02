"""Render a bounded resume card for one item."""
from __future__ import annotations

import os
import tempfile

from ledger.card import format_card, project_card
from ledger.paths import resolve_db_path
from ledger.pointers import sidecar_from_env
from ledger.verbs import Context, Result, Verb, open_db, require_item


def add_arguments(parser) -> None:
    parser.add_argument("item")
    parser.add_argument(
        "--md",
        metavar="PATH",
        help="write a human markdown copy to PATH (overwrites PATH if it exists)",
    )


def run(args, ctx: Context) -> Result:
    lg = open_db(ctx, write=False)
    try:
        require_item(lg, args.item)
        card = project_card(lg, args.item, pointers=sidecar_from_env())
        text = format_card(card)
        data = card.as_dict()
    finally:
        lg.close()
    if args.md:
        dest = resolve_db_path(args.md)
        dest.parent.mkdir(parents=True, exist_ok=True)
        body = text if text.endswith("\n") else text + "\n"
        fd, tmp = tempfile.mkstemp(
            dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
            os.replace(tmp, dest)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return Result(data=data, text=text)


VERB = Verb(
    name="card",
    help="render a bounded resume card for an item",
    add_arguments=add_arguments,
    run=run,
)
