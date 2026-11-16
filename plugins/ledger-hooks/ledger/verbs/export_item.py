"""Print one item's events; after close, without its ephemeral events.

The backup export (``ledger export``) keeps every event so a restore can
replay the hash chain. This is the per-item view a reader pulls from.
``--ticket`` prints the recorded ``### Ticket:`` section once instead. An
item whose source is a row key ``row:<ID>`` is read at the locator of its
newest valid ``also at <locator>`` note: ``path:line@commit``, or ``path#ID``
(the ``### Ticket: ID`` section of ``path`` at the current ``HEAD``).

An item whose source names no file (an empty source, a source that is neither
a locator nor a row key such as a follow-up's ``PARENT:slug``, or a row key
with no valid ``also at`` note) and that has a current spec prints that spec
instead: its version, then each field it records (title, size, merge class),
then the body and the acceptance. A source that names a file reads, and
refuses, exactly as before; a spec never stands in for a broken locator. An
item with neither a readable file nor a spec keeps its named refusal. With
``--json`` the spec case adds a ``spec`` object to ``data``: ``version``,
``title``, ``size``, ``merge_class``, ``body_md`` and ``acceptance``, the
absent ones null; the locator case has no ``spec`` key. A spec event that
cannot be read is refused ``spec_unreadable`` (exit 3), printing no text.

The view ends with one completeness line (``--json``: a ``completeness``
object in ``data``) naming the events returned, the events on the item and
the events decayed, so a reader knows whether it holds the whole record.
"""
from __future__ import annotations

import json

from typing import Any

from ledger.export import TicketSourceNamesNoFile, item_export_counted, item_ticket_text
from ledger.schema import SPEC_MERGE_CLASSES, SPEC_SIZES
from ledger.store import Event
from ledger.verbs import (
    EXIT_NOT_FOUND,
    EXIT_REFUSED,
    CliError,
    Context,
    Result,
    Verb,
    open_db,
    require_item,
)
from ledger.verbs.spec import current_spec


def add_arguments(parser) -> None:
    parser.add_argument("item")
    parser.add_argument(
        "--ticket",
        action="store_true",
        help=(
            "print the recorded ticket section once: a ### Ticket: heading (one "
            "[tag] or lowercase lead word may precede the id) or a bare ### ID "
            "heading inside a ## Tickets section; source is read from the "
            "current repository; an item whose source names no file prints its "
            "current spec instead"
        ),
    )


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _spec_view(item_id: str, event: Event) -> dict[str, Any]:
    """The spec fields to print, or a refusal when the event cannot be read."""
    payload = event.payload
    version = payload.get("version")
    readable = (
        type(version) is int
        and version >= 1
        and _nonempty_text(payload.get("title"))
        and _nonempty_text(payload.get("body_md"))
        and (payload.get("acceptance") is None or _nonempty_text(payload.get("acceptance")))
        and payload.get("size") in (None, *SPEC_SIZES)
        and payload.get("merge_class") in (None, *SPEC_MERGE_CLASSES)
    )
    if not readable:
        raise CliError(
            f"spec_unreadable: item {item_id} has a spec (event {event.id}) "
            "that cannot be read",
            code="spec_unreadable",
            exit_code=EXIT_REFUSED,
        )
    return {
        "version": version,
        "title": payload["title"],
        "size": payload.get("size"),
        "merge_class": payload.get("merge_class"),
        "body_md": payload["body_md"],
        "acceptance": payload.get("acceptance"),
    }


def _spec_text(spec: dict[str, Any]) -> str:
    lines = [f"spec: v{spec['version']}", f"title: {spec['title']}"]
    for name in ("size", "merge_class"):
        if spec[name] is not None:
            lines.append(f"{name}: {spec[name]}")
    # The recorded text keeps its trailing newlines in ``spec`` (JSON); the
    # printed sections are separated by one blank line and end in one newline.
    lines.extend(["", "## Body", spec["body_md"].rstrip("\n")])
    if spec["acceptance"] is not None:
        lines.extend(["", "## Acceptance", spec["acceptance"].rstrip("\n")])
    return "\n".join(lines) + "\n"


def run(args, ctx: Context) -> Result:
    lg = open_db(ctx, write=False)
    try:
        require_item(lg, args.item)
        if args.ticket:
            try:
                text = item_ticket_text(lg, args.item)
            except TicketSourceNamesNoFile as exc:
                # The source names no file. A current spec stands in for it;
                # with none, the named refusal stands as it always did.
                event = current_spec(lg, args.item)
                if event is None:
                    raise CliError(
                        str(exc), code="not_found", exit_code=EXIT_NOT_FOUND,
                    ) from None
                spec = _spec_view(args.item, event)
                text = _spec_text(spec)
                return Result(
                    data={"item_id": args.item, "ticket": text, "spec": spec},
                    text=text,
                )
            except ValueError as exc:
                raise CliError(
                    str(exc), code="not_found", exit_code=EXIT_NOT_FOUND,
                ) from None
            return Result(data={"item_id": args.item, "ticket": text}, text=text)
        records, completeness = item_export_counted(lg, args.item)
    finally:
        lg.close()
    line = (
        f"completeness: {completeness['events_returned']} events returned, "
        f"{completeness['events_on_item']} events on the item, "
        f"{completeness['events_decayed']} decayed"
    )
    text = "\n".join([*(json.dumps(record, sort_keys=True) for record in records), line])
    return Result(
        data={"item_id": args.item, "events": records, "completeness": completeness},
        text=text,
    )


VERB = Verb(
    name="export-item",
    help="print one item's events (ephemeral ones decay after close)",
    add_arguments=add_arguments,
    run=run,
)
