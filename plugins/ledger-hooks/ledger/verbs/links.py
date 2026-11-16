"""``ledger links ITEM``: every link that names an item, in both directions.

Outgoing rows are the item's own ``item.linked`` events: the relation and the
target the link names. Incoming rows are other items' ``item.linked`` events
whose target is this item's id, whatever the relation. Each list is in event
order, oldest first. A link an item holds to itself is in both lists.

Every row names its other end (the target of an outgoing link, the item that
holds an incoming one) and how that end resolves:

- ``item``: the other end is an item; its title is printed beside it.
- ``unresolved``: the other end should be an item (an incoming link's
  holder, or an outgoing link whose relation names an item: ``item``,
  ``blocks``, ``duplicate-of``, ``child-of``) but no item has that id. The row
  is printed, never dropped.
- ``external``: an outgoing link whose relation names something outside the
  ledger (``pr``, ``branch``, ``session``, ``url``) and whose target is not an
  item id.

An unknown item is refused by name (``not_found``, exit 1), as ``card`` and
``export-item`` refuse it. A known item that no link names exits 1 with
``empty``, as the other listings do. The verb only reads.
"""
from __future__ import annotations

from typing import Any

from ledger.verbs import (
    EXIT_NOT_FOUND,
    CliError,
    Context,
    Result,
    Verb,
    open_db,
    require_item,
)
from ledger.verbs.events import text_cell

# The relations whose target is an item id (see ``ledger link``).
ITEM_RELS = frozenset({"item", "blocks", "duplicate-of", "child-of"})

RESOLVED = "item"
UNRESOLVED = "unresolved"
EXTERNAL = "external"


def add_arguments(parser) -> None:
    parser.add_argument("item", help="the item whose links to print")


def _resolve(
    titles: dict[str, Any], other: Any, *, names_item: bool
) -> tuple[str, Any]:
    """How the other end of a link resolves, and its title when it is an item."""
    if isinstance(other, str) and other in titles:
        return RESOLVED, titles[other]
    return (UNRESOLVED if names_item else EXTERNAL), None


def _line(direction: str, rel: Any, other: Any, row: dict[str, Any]) -> str:
    return "\t".join(
        (
            direction,
            text_cell(rel),
            text_cell(other),
            row["resolution"],
            text_cell(row["title"]),
            f"event={row['event_id']}",
        )
    )


def run(args, ctx: Context) -> Result:
    item_id = args.item
    lg = open_db(ctx, write=False)
    try:
        item = require_item(lg, item_id)
        # The links are read before the titles: an item created between the
        # two reads still resolves, so a row is never marked unresolved for an
        # item that existed when its link was read.
        linked = lg.events(kind="item.linked")
        titles = {row["item_id"]: row["title"] for row in lg.items()}
    finally:
        lg.close()
    outgoing: list[dict[str, Any]] = []
    incoming: list[dict[str, Any]] = []
    for event in linked:
        rel = event.payload.get("rel")
        target = event.payload.get("target")
        if event.item_id == item_id:
            resolution, title = _resolve(titles, target, names_item=rel in ITEM_RELS)
            outgoing.append(
                {
                    "event_id": event.id,
                    "rel": rel,
                    "target": target,
                    "resolution": resolution,
                    "title": title,
                }
            )
        if target == item_id:
            resolution, title = _resolve(titles, event.item_id, names_item=True)
            incoming.append(
                {
                    "event_id": event.id,
                    "rel": rel,
                    "source": event.item_id,
                    "resolution": resolution,
                    "title": title,
                }
            )
    if not outgoing and not incoming:
        raise CliError(
            f"no links name {item_id}", code="empty", exit_code=EXIT_NOT_FOUND
        )
    lines = [_line("out", row["rel"], row["target"], row) for row in outgoing]
    lines += [_line("in", row["rel"], row["source"], row) for row in incoming]
    return Result(
        data={
            "item_id": item_id,
            "title": item["title"],
            "outgoing": outgoing,
            "incoming": incoming,
        },
        text="\n".join(lines),
    )


VERB = Verb(
    name="links",
    help="print the links that name an item, outgoing and incoming",
    add_arguments=add_arguments,
    run=run,
)
