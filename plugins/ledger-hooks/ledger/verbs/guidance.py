"""Set, list or withdraw pool guidance (a dated burn or protect note)."""
from __future__ import annotations

from datetime import date, datetime, timezone

from ledger import guidance
from ledger.schema import GUIDANCE_DIRECTIONS
from ledger.verbs import (
    EXIT_REFUSED,
    CliError,
    Context,
    Result,
    Verb,
    actor_kwargs,
    new_key,
    open_db,
)


def add_arguments(parser) -> None:
    parser.add_argument("action", choices=("set", "list", "withdraw"))
    parser.add_argument("--pool")
    parser.add_argument("--direction", choices=GUIDANCE_DIRECTIONS)
    parser.add_argument("--reason")
    parser.add_argument("--starts", help="ISO date; defaults to today (UTC)")
    parser.add_argument("--expires", help="ISO date, inclusive")


def _need(args, *names: str) -> None:
    missing = [name for name in names if not getattr(args, name)]
    if missing:
        raise CliError(
            "missing " + ", ".join("--" + name for name in missing),
            code="usage", exit_code=2,
        )


def run(args, ctx: Context) -> Result:
    today = datetime.now(timezone.utc).date()
    if args.action == "list":
        lg = open_db(ctx, write=False)
        try:
            rows = guidance.in_force(lg, today)
        finally:
            lg.close()
        return Result(
            data=[{**row.__dict__, "starts": row.starts.isoformat(),
                   "expires": row.expires.isoformat()} for row in rows],
            text="\n".join(
                f"{row.pool}\t{row.direction}\t{row.starts}..{row.expires}\t{row.reason}"
                for row in rows
            ),
        )
    _need(args, "pool", "direction")
    lg = open_db(ctx, write=True)
    try:
        if args.action == "withdraw":
            item_id = guidance.item_id_for(args.pool, args.direction)
            if lg.item(item_id) is None:
                raise CliError(f"no guidance item: {item_id}", code="not_found", exit_code=1)
            lg.append(kind="item.state", payload={"status": "closed"}, item_id=item_id,
                      idempotency_key=new_key(), **actor_kwargs(ctx))
            return Result(data={"item_id": item_id}, text=f"withdrawn\t{item_id}")
        _need(args, "reason", "expires")
        try:
            row = guidance.record_guidance(
                lg, pool=args.pool, direction=args.direction, reason=args.reason,
                starts=args.starts or today, expires=args.expires, **actor_kwargs(ctx),
            )
        except guidance.GuidanceError as exc:
            raise CliError(str(exc), code="refused", exit_code=EXIT_REFUSED) from exc
    finally:
        lg.close()
    return Result(
        data={"item_id": row.item_id, "starts": row.starts.isoformat(),
              "expires": row.expires.isoformat()},
        text=f"guidance\t{row.item_id}\t{row.starts}..{row.expires}",
    )


VERB = Verb(
    name="guidance",
    help="set, list or withdraw dated pool guidance (burn or protect)",
    add_arguments=add_arguments,
    run=run,
    writes=True,
)
