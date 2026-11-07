"""Record an item.linked event.

``--rel duplicate-of`` marks ``item`` a duplicate of ``--target`` and closes
it: the link and the close land in one transaction or not at all. It refuses
an item that is already closed, a target that is not an item or is the item
itself, and a target that is itself closed as a duplicate. Both cards show
the relation (the kept item's as ``duplicated-by``). There is no merge verb.

``--rel child-of`` makes ``item`` a child of the anchor named by the target.
An item has at most one current anchor (its latest ``child-of`` link) and the
anchors form a forest. A second anchor is refused (exit 3) unless ``--move``
is given; a move appends a new link that names the anchor it left, and no
earlier event changes. A link to the current anchor writes nothing. A link
that would make the item its own ancestor is refused. The checks and the
append share one write transaction.
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
CHILD_OF = "child-of"
DEFAULT_REL = "url"

EPILOG = """\
The target is given once: after the item, or with --target. Without --rel the
relation is url, so a local file path or a web address needs no flag.

examples:
  ledger link it-1a2b3c4d5e6f ~/notes/brief.md
  ledger link it-1a2b3c4d5e6f --rel pr --target https://github.com/owner/repo/pull/12
  ledger link it-1a2b3c4d5e6f --rel branch feat/retry
  ledger link it-1a2b3c4d5e6f --rel branch feat/retry-2 --idempotency-key run-7-link-1
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
    parser.add_argument(
        "--move",
        action="store_true",
        help=f"with --rel {CHILD_OF}: replace the item's current anchor with "
        "the target (without it a second anchor is refused)",
    )
    parser.add_argument(
        "--idempotency-key",
        metavar="KEY",
        help="make a retry safe: the same key and content returns the stored "
        "event and writes nothing; the same key with different content is refused",
    )


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


def _check_duplicate_state(lg, loser: str, kept: str) -> None:
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


def _duplicate(args, ctx: Context) -> Result:
    loser, kept = args.item, args.target
    # One user key names the whole call: the link event is written under the
    # key itself (the same slot a plain link uses, so the two modes collide on
    # one key) and the close under <key>:close, so a retry finds both and
    # writes nothing.
    base_key = getattr(args, "idempotency_key", None) or new_key()
    link_key = base_key
    close_key = f"{base_key}:close"
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
            link_found = lg.event_by_key(link_key)
            stored = (link_found is not None, lg.event_by_key(close_key) is not None)
            # A lone event under the key is this call's half-written first
            # event only when kind, rel, item and target all match. Anything
            # else (a plain link, a note, another item or target) is another
            # write intent: the appends below refuse it as a conflict.
            link_is_ours = (
                link_found is not None
                and link_found.kind == "item.linked"
                and link_found.item_id == loser
                and link_found.payload.get("rel") == DUPLICATE_OF
                and link_found.payload.get("target") == kept
            )
            if (stored == (True, False) and link_is_ours) or stored == (False, True):
                # One event of the pair exists under this key; finishing or
                # duplicating it could not be proven safe, so write nothing.
                raise CliError(
                    f"idempotency key {base_key!r} names only one of the two events "
                    "a duplicate-of link writes; use a different key",
                    code="partial_write",
                    exit_code=EXIT_REFUSED,
                )
            # A retry (both stored) skips the state checks: they would refuse
            # the item the first call already closed. The appends below return
            # the stored events, or refuse by name when the content differs.
            if stored == (False, False):
                _check_duplicate_state(lg, loser, kept)
            link = lg.append(
                kind="item.linked",
                payload={"rel": DUPLICATE_OF, "target": kept},
                item_id=loser,
                idempotency_key=link_key,
                **actor_kwargs(ctx),
            )
            closed = lg.append(
                kind="item.state",
                payload={"status": "closed", "reason": f"duplicate of {kept}"},
                item_id=loser,
                idempotency_key=close_key,
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


def _child_of_events(lg) -> dict[str, object]:
    """Each item's newest ``child-of`` link event: its current anchor."""
    newest: dict[str, object] = {}
    for event in lg.events(kind="item.linked"):
        if event.payload.get("rel") == CHILD_OF:
            newest[event.item_id] = event
    return newest


def _child_of(args, ctx: Context) -> Result:
    child, anchor = args.item, args.target
    move = bool(getattr(args, "move", False))
    key = getattr(args, "idempotency_key", None) or new_key()
    if child == anchor:
        raise CliError(
            f"an item cannot be its own anchor: {child}",
            code="self_link",
            exit_code=EXIT_REFUSED,
        )
    lg = open_db(ctx, write=True)
    try:
        require_item(lg, child)
        # One write transaction: the checks read the state the append lands on.
        with lg.transaction():
            stored = lg.event_by_key(key)
            if (
                stored is not None
                and stored.kind == "item.linked"
                and stored.item_id == child
                and stored.payload.get("rel") == CHILD_OF
                and stored.payload.get("target") == anchor
            ):
                # A retry of this very call: return what it wrote.
                return _child_of_result(child, anchor, stored, "linked")
            if lg.item(anchor) is None:
                raise CliError(
                    f"item not found: {anchor}",
                    code="not_found",
                    exit_code=EXIT_NOT_FOUND,
                )
            current = _child_of_events(lg)
            have = current.get(child)
            if have is not None and have.payload["target"] == anchor:
                return _child_of_result(child, anchor, have, "unchanged")
            if have is not None and not move:
                raise CliError(
                    f"{child} is already a child of {have.payload['target']}; "
                    "pass --move to change its anchor",
                    code="second_anchor",
                    exit_code=EXIT_REFUSED,
                )
            node, seen = anchor, set()
            while node in current and node not in seen:
                seen.add(node)
                node = current[node].payload["target"]
                if node == child:
                    raise CliError(
                        f"{anchor} is already below {child}; "
                        f"linking {child} under it would make a cycle",
                        code="cycle",
                        exit_code=EXIT_REFUSED,
                    )
            payload = {"rel": CHILD_OF, "target": anchor}
            if have is not None:
                payload["moved_from"] = have.payload["target"]
            event = lg.append(
                kind="item.linked",
                payload=payload,
                item_id=child,
                idempotency_key=key,
                **actor_kwargs(ctx),
            )
    finally:
        lg.close()
    return _child_of_result(child, anchor, event, "linked")


def _child_of_result(child: str, anchor: str, event, verb: str) -> Result:
    return Result(
        data={"item_id": child, "event": event_dict(event), "result": verb},
        text=f"{verb}\t{child}\t{CHILD_OF}\t{anchor}\tevent={event.id}",
    )


def run(args, ctx: Context) -> Result:
    args.target = _target(args)
    if getattr(args, "move", False) and args.rel != CHILD_OF:
        raise UsageError(f"--move is accepted only with --rel {CHILD_OF}")
    if args.rel == DUPLICATE_OF:
        return _duplicate(args, ctx)
    if args.rel == CHILD_OF:
        return _child_of(args, ctx)
    lg = open_db(ctx, write=True)
    try:
        require_item(lg, args.item)
        event = lg.append(
            kind="item.linked",
            payload={"rel": args.rel, "target": args.target},
            item_id=args.item,
            idempotency_key=getattr(args, "idempotency_key", None) or new_key(),
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
