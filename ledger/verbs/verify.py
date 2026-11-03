"""Verify the hash chain and payloads."""
from __future__ import annotations

from ledger.verbs import Context, Result, Verb, open_db


def add_arguments(parser) -> None:
    parser.add_argument(
        "--system",
        help="also require the database to be claimed for this system",
    )


def run(args, ctx: Context) -> Result:
    lg = open_db(ctx, write=False)
    try:
        lg.verify(system=args.system)
        count = len(lg.events())
    finally:
        lg.close()
    return Result(
        data={"events": count},
        text=f"ok events={count}",
    )


VERB = Verb(
    name="verify",
    help="verify the hash chain and payloads",
    add_arguments=add_arguments,
    run=run,
)
