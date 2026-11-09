"""Print where work stopped, and the command that continues it.

``ledger resume`` lists, for each open item that has an ``item.stopped``
event, the newest stop (its point, the skills the next session invokes, the
files it reads) and the literal commands that continue: ``resume --item`` and
``card``. ``--item ID`` shows that one item. ``--brief`` prepends the short
form of the agent contract (the full page is in the ledger repository's
docs folder) so a session-start hook can print both into context.

The verb never writes: the database is opened read-only. A database that does
not exist yet is not an error: the output says there are no open items and the
exit status is 0 (an unknown ``--item`` is ``not_found``, exit 1). Every
command printed carries the database's resolved path, so it runs from any
directory.
"""
from __future__ import annotations

import unicodedata
from typing import Any

from ledger.paths import resolve_db_path
from ledger.store import open_ledger_readonly
from ledger.verbs import (
    EXIT_NOT_FOUND,
    CliError,
    Context,
    Result,
    Verb,
    invocation_prefix,
    quote_command_arg,
    require_item,
)

NO_OPEN_ITEMS = "no open items"

# The contract's short form. Every write trigger on the contract page (in the
# ledger repository's docs folder) appears in it by the page's own words and
# with the page's own commands, one line per trigger (a second line only for a
# second command); a repository test reads both so the two cannot drift. Keep
# it under 40 lines.
SHORT_FORM = """\
LEDGER AGENT CONTRACT (short form; the full page is the agent contract in the ledger repo's docs folder)
The ledger holds this work's state. Write to it without asking.
Write triggers:
- item opened: ledger new --type TYPE --title TITLE
- item picked up: ledger claim ITEM
- item closed: ledger state ITEM --status closed
- decision made: ledger note ITEM --text "decided: ... because ..."
- decision elected: ask through the question picker, then ledger pick ITEM --ask K
- brief ratified: an election, not a note: ask the same way, then ledger pick ITEM --ask K;
  link the brief by path: ledger link ITEM --rel url --target PATH
- follow-up filed: SRC is PARENT:SLUG (the parent's id, a colon, a short slug of the follow-up);
  ledger new --type TYPE --title TITLE --source SRC, then link it to its source:
  ledger link NEW --rel child-of --target PARENT
  (interrupted between the two? run both again unchanged: one child, one link)
- thread parked: write item.stopped (see the close rule)
- thread stopped: write item.stopped (see the close rule)
Use the verbs note, link, ask-display, pick and elect. Never hand-write an append script.
Two events have no verb of their own, decision.asked (the question you put to the person)
and item.stopped; write each with one plain ledger append. The harness captures the
person's words; never write them yourself.
Reading order: ledger resume, ledger card ITEM, then ledger export-item ITEM
when the card shows a cut.
Actor stamps: LEDGER_OPERATOR (the person), LEDGER_AGENT (this seat),
LEDGER_HARNESS (the runtime; writes refuse without it), LEDGER_MODEL (the
model), LEDGER_SESSION (the session id), all exported before you write.
Close rule: end a session by appending item.stopped on each item you worked
and printing the exact resume command, never prose. Replace ITEM:
  ledger append --item ITEM --kind item.stopped --payload '{"point": "what is done and what is next", "phase": "build", "skills": ["review"], "refs": ["plan.md"]}'
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


_NAMED = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _is_control(ch: str) -> bool:
    # C0, DEL and C1 are category Cc; U+2028 and U+2029 are Zl and Zp.
    return unicodedata.category(ch) in ("Cc", "Zl", "Zp")


def _escape(text: str) -> str:
    """Text for one printed line: line breaks and control characters become
    visible escapes, so user text can never start or end a line of its own."""
    out = []
    for ch in text:
        if not _is_control(ch):
            out.append(ch)
        elif ch in _NAMED:
            out.append(_NAMED[ch])
        elif ord(ch) < 0x100:
            out.append(f"\\x{ord(ch):02x}")
        else:
            out.append(f"\\u{ord(ch):04x}")
    return "".join(out)


def _commands(item_id: str, prefix: str = "ledger") -> tuple[str, str]:
    item = quote_command_arg(item_id)
    # ``--item=`` and ``card --`` keep an id that starts with a hyphen a value,
    # not an option, so the printed commands run for every valid id.
    return f"{prefix} resume --item={item}", f"{prefix} card -- {item}"


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


def _rollup(rows: list[dict[str, Any]], item_id: str) -> dict[str, int] | None:
    """closed/total over the item's children, or None for an item with none."""
    kids = [row for row in rows if row.get("parent_id") == item_id]
    if not kids:
        return None
    return {
        "closed": sum(1 for row in kids if row["status"] == "closed"),
        "total": len(kids),
    }


def _entry(
    item: dict[str, Any],
    stop,
    rollup: dict[str, int] | None = None,
    *,
    prefix: str = "ledger",
) -> dict[str, Any]:
    resume_cmd, card_cmd = _commands(item["item_id"], prefix)
    extra = {} if rollup is None else {"children": rollup}
    return {
        **extra,
        "item_id": item["item_id"],
        "title": item["title"],
        "status": item["status"],
        "stop": _stop_row(stop) if stop is not None else None,
        "resume_command": resume_cmd,
        "card_command": card_cmd,
    }


def _entry_text(entry: dict[str, Any]) -> str:
    lines = [
        f"{_escape(entry['item_id'])}\t{_escape(entry['status'])}\t{_escape(entry['title'])}"
    ]
    if "children" in entry:
        lines.append(
            f"  children: {entry['children']['closed']}/{entry['children']['total']} closed"
        )
    stop = entry["stop"]
    if stop is None:
        lines.append("  no stop recorded; read the card")
    else:
        lines.append(f"  stopped ({_escape(stop['phase'])}): {_escape(stop['point'])}")
        if stop["skills"]:
            lines.append(f"  skills: {_escape(', '.join(stop['skills']))}")
        if stop["refs"]:
            lines.append(f"  refs: {_escape(', '.join(stop['refs']))}")
        lines.append(f"  resume: {entry['resume_command']}")
    lines.append(f"  card: {entry['card_command']}")
    return "\n".join(lines)


def run(args, ctx: Context) -> Result:
    resolved = resolve_db_path(ctx.db)
    entries: list[dict[str, Any]] = []
    open_without_stop = 0
    if resolved.exists():
        lg = open_ledger_readonly(resolved)
        try:
            prefix = invocation_prefix(ctx.program, str(lg.path), ctx.db_explicit)
            # One read transaction: the stops and the items come from the same
            # snapshot, so a writer appending between the two reads cannot make
            # an open item with a stop vanish from the list.
            conn = lg._conn
            conn.execute("BEGIN")
            try:
                rows = lg.items()
                if args.item_id:
                    stops = lg.events(item_id=args.item_id, kind="item.stopped")
                    item = require_item(lg, args.item_id)
                    entries.append(
                        _entry(
                            item,
                            stops[-1] if stops else None,
                            _rollup(rows, item["item_id"]),
                            prefix=prefix,
                        )
                    )
                else:
                    newest: dict[str, Any] = {}
                    for event in lg.events(kind="item.stopped"):
                        newest[event.item_id] = event
                    for row in rows:
                        if row["status"] == "closed":
                            continue
                        if row["item_id"] not in newest:
                            open_without_stop += 1
                            continue
                        entries.append(
                            _entry(
                                row,
                                newest[row["item_id"]],
                                _rollup(rows, row["item_id"]),
                                prefix=prefix,
                            )
                        )
            finally:
                conn.execute("ROLLBACK")
        finally:
            lg.close()
    elif args.item_id:
        # Same refusal an existing database gives for an unknown item.
        raise CliError(
            f"item not found: {args.item_id}", code="not_found", exit_code=EXIT_NOT_FOUND
        )
    if entries:
        body = "\n".join(_entry_text(entry) for entry in entries)
    elif open_without_stop:
        body = (
            f"{open_without_stop} open item(s) with no stop recorded; "
            "ledger query lists them"
        )
    else:
        body = NO_OPEN_ITEMS
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
