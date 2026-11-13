"""Claim a database for its system (ledger.system)."""
from __future__ import annotations

from ledger.schema import SYSTEM_CLAIM_KIND, ValidationError, validate_payload
from ledger.verbs import Context, Result, UsageError, Verb, open_db

NAME_RULE = (
    "a system name is 1 to 64 characters: a lowercase letter, then lowercase "
    "letters, digits, '_' or '-'"
)


def add_arguments(parser) -> None:
    parser.add_argument(
        "name",
        metavar="NAME",
        help=(
            "the system this database belongs to, for example work or home; "
            + NAME_RULE
        ),
    )


def check_name(name: str) -> str:
    """Refuse a name the system claim's own payload schema would refuse.

    The rule is the schema's, so a name this verb takes is a name restore and
    ``verify --system`` take. It runs before the database is opened, so a bad
    name never creates a file.
    """
    try:
        validate_payload(SYSTEM_CLAIM_KIND, {"system": name})
    except ValidationError:
        raise UsageError(f"invalid system name {name!r}: {NAME_RULE}") from None
    return name


def run(args, ctx: Context) -> Result:
    name = check_name(args.name)
    lg = open_db(ctx, write=True)
    try:
        # DESIGN: the library decides. Ledger.claim_system checks and writes in
        # one transaction: it skips the write for the same system and refuses
        # another system (SharedDatabaseError, exit 3, code shared_database).
        # The read below only chooses the words; two callers claiming the same
        # name at once may both be told "claimed", and the database still
        # holds one claim.
        already = lg.system() == name
        lg.claim_system(name)
        path = str(lg.path)
    finally:
        lg.close()
    if already:
        return Result(
            data={"system": name, "path": path, "claimed": False},
            text=f"already claimed for {name}: {path}",
        )
    return Result(
        data={"system": name, "path": path, "claimed": True},
        text=f"claimed for {name}: {path}",
    )


VERB = Verb(
    name="claim-system",
    help=(
        "claim this database for a system (for example work or home); "
        "a repeat of the same name changes nothing, another name is refused. "
        + NAME_RULE
    ),
    add_arguments=add_arguments,
    run=run,
    writes=True,
)
