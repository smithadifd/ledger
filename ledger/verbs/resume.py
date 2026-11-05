"""Print where work stopped, and the command that continues it.

``ledger resume`` lists, for each open item that has an ``item.stopped``
event, the newest stop (its point, the skills the next session invokes, the
files it reads) and the literal commands that continue: ``resume --item`` and
``card``. ``--item ID`` shows that one item. ``--brief`` prepends the short
form of the agent contract (docs/agent-contract.md) so a session-start hook
can print both into context.

The verb never writes: the database is opened read-only. A database that does
not exist yet is not an error: the output says there are no open items and the
exit status is 0 (an unknown ``--item`` is ``not_found``, exit 1). Every
command printed carries the database's resolved path, so it runs from any
directory.
"""
from __future__ import annotations

import shlex
from typing import Any

from ledger.paths import resolve_db_path
from ledger.store import open_ledger_readonly
from ledger.verbs import EXIT_NOT_FOUND, CliError, Context, Result, Verb, require_item

NO_OPEN_ITEMS = "no open items"

# The contract's short form. Every write trigger on docs/agent-contract.md
# appears in it by the page's own words; a test reads both so the two cannot
# drift. Keep it under 40 lines.
SHORT_FORM = """\
LEDGER AGENT CONTRACT (short form; the full page is docs/agent-contract.md)
The ledger holds this work's state. Write to it without asking.
Write triggers:
- item opened: ledger new --type TYPE --title TITLE
- item closed: ledger state ITEM --status closed
- decision made: ledger note ITEM --text "decided: ... because ..."
- decision elected: ledger elect, quoting the person's captured words
- brief ratified: an election, not a note; link the brief by path:
  ledger link ITEM --rel url --target PATH
- thread parked: write item.stopped (see the close rule)
- thread stopped: write item.stopped (see the close rule)
Use the verbs note, link and elect. Never hand-write an append script.
Reading order: ledger resume, ledger card ITEM, then ledger export-item ITEM
when the card shows a cut.
Actor stamps: LEDGER_OPERATOR (the person), LEDGER_AGENT (this seat),
LEDGER_HARNESS (the runtime; writes refuse without it), LEDGER_MODEL (the
model), LEDGER_SESSION (the session id), all exported before you write.
Close rule: end a session by appending item.stopped on each item you worked
and printing the exact resume command, never prose. Replace ITEM:
  ledger append --item ITEM --kind item.stopped --payload '{"point": "what is done and what is next", "phase": "build", "skills": ["review"], "refs": ["docs/plan.md"]}'
  ledger resume --item ITEM
phase is one of orient, plan, build, review, close. skills and refs are optional.
"""


def add_arguments(parser) -> None:
    parser.add_argument("--item", dest="item_id", help="show only this item")
    parser.add_argument(
        "--brief",
        action="store_true",
        help="print the agent contract's short form first",
    )


def _commands(db: str, item_id: str) -> tuple[str, str]:
    base = f"ledger --db {shlex.quote(db)}"
    item = shlex.quote(item_id)
    # ``--item=`` and ``card --`` keep an id that starts with a hyphen a value,
    # not an option, so the printed commands run for every valid id.
    return f"{base} resume --item={item}", f"{base} card -- {item}"


def _stop_row(event) -> dict[str, Any]:
    payload = event.payload
    return {
        "event_id": event.id,
        "phase": payload["phase"],
        "point": payload["point"],
        "skills": list(payload.get("skills") or ()),
        "refs": list(payload.get("refs") or ()),
        "recorded_at": event.recorded_at,
    }


def _entry(db: str, item: dict[str, Any], stop) -> dict[str, Any]:
    resume_cmd, card_cmd = _commands(db, item["item_id"])
    return {
        "item_id": item["item_id"],
        "title": item["title"],
        "status": item["status"],
        "stop": _stop_row(stop) if stop is not None else None,
        "resume_command": resume_cmd,
        "card_command": card_cmd,
    }


def _entry_text(entry: dict[str, Any]) -> str:
    lines = [f"{entry['item_id']}\t{entry['status']}\t{entry['title']}"]
    stop = entry["stop"]
    if stop is None:
        lines.append("  no stop recorded; read the card")
    else:
        lines.append(f"  stopped ({stop['phase']}): {stop['point']}")
        if stop["skills"]:
            lines.append(f"  skills: {', '.join(stop['skills'])}")
        if stop["refs"]:
            lines.append(f"  refs: {', '.join(stop['refs'])}")
        lines.append(f"  resume: {entry['resume_command']}")
    lines.append(f"  card: {entry['card_command']}")
    return "\n".join(lines)


def run(args, ctx: Context) -> Result:
    resolved = resolve_db_path(ctx.db)
    entries: list[dict[str, Any]] = []
    if resolved.exists():
        lg = open_ledger_readonly(resolved)
        try:
            db = str(lg.path)
            # One read transaction: the stops and the items come from the same
            # snapshot, so a writer appending between the two reads cannot make
            # an open item with a stop vanish from the list.
            conn = lg._conn
            conn.execute("BEGIN")
            try:
                if args.item_id:
                    stops = lg.events(item_id=args.item_id, kind="item.stopped")
                    item = require_item(lg, args.item_id)
                    entries.append(_entry(db, item, stops[-1] if stops else None))
                else:
                    newest: dict[str, Any] = {}
                    for event in lg.events(kind="item.stopped"):
                        newest[event.item_id] = event
                    for row in lg.items():
                        if row["status"] == "closed" or row["item_id"] not in newest:
                            continue
                        entries.append(_entry(db, row, newest[row["item_id"]]))
            finally:
                conn.execute("ROLLBACK")
        finally:
            lg.close()
    elif args.item_id:
        # Same refusal an existing database gives for an unknown item.
        raise CliError(
            f"item not found: {args.item_id}", code="not_found", exit_code=EXIT_NOT_FOUND
        )
    body = "\n".join(_entry_text(entry) for entry in entries) if entries else NO_OPEN_ITEMS
    data: dict[str, Any] = {"items": entries}
    text = body
    if args.brief:
        data["contract"] = SHORT_FORM
        text = SHORT_FORM + "\n" + body
    return Result(data=data, text=text)


VERB = Verb(
    name="resume",
    help="print where work stopped and the command that continues it",
    add_arguments=add_arguments,
    run=run,
)
