"""Write an integrity-checked online backup of the ledger database."""
from __future__ import annotations

from ledger.backup import backup_db
from ledger.verbs import Context, Result, UsageError, Verb


def add_arguments(parser) -> None:
    parser.add_argument(
        "--to",
        required=True,
        help="destination database file (never a copy of the live file)",
    )


def run(args, ctx: Context) -> Result:
    dest = args.to
    if not dest:
        raise UsageError("pass --to")
    result = backup_db(ctx.db, dest)
    return Result(
        data={"integrity": result.integrity, "path": str(result.path)},
        text=f"backup {result.path} integrity={result.integrity}",
    )


VERB = Verb(
    name="backup",
    help="online backup of the database with integrity_check",
    add_arguments=add_arguments,
    run=run,
)
