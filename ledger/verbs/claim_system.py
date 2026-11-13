"""Claim a database for its system (ledger.system)."""
from __future__ import annotations

from ledger.schema import SYSTEM_CLAIM_KIND, ValidationError, validate_payload
from ledger.store import UnsettledClaimError
from ledger.verbs import Context, Result, UsageError, Verb, actor_kwargs, open_db

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
        # one transaction: it skips the write for the same system, refuses
        # another system (SharedDatabaseError, exit 3, code shared_database)
        # and returns whether it wrote. The words come from that return, so
        # two callers claiming the same name at once are not both told
        # "claimed". The claim carries the caller's attribution, as every
        # other write verb's event does.
        try:
            wrote = lg.claim_system(name, **actor_kwargs(ctx))
        except UnsettledClaimError as refusal:
            # DESIGN: report the outcome the transaction reached. The library
            # raises this for an I/O error or a checkpoint that cannot finish,
            # either of which can come after the claim committed: two callers
            # racing on a fresh database each checkpoint after their commit,
            # and one checkpoint finds the other running. claim_outcome
            # re-reads the committed claim and re-runs the open-time file
            # checks: this name on record, through a path that still names the
            # opened file as its only link, exits 0 (claimed when this call's
            # write is known to have committed it, already claimed otherwise);
            # anything else re-raises the refusal (exit 3, shared_database),
            # since a checkpoint held through another path to the file leaves
            # the claim unseen there.
            wrote = lg.claim_outcome(name, refusal)
        path = str(lg.path)
    finally:
        lg.close()
    if wrote:
        return Result(
            data={"system": name, "path": path, "claimed": True},
            text=f"claimed for {name}: {path}",
        )
    return Result(
        data={"system": name, "path": path, "claimed": False},
        text=f"already claimed for {name}: {path}",
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
