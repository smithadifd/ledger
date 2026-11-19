"""Print where work stopped, and the command that continues it.

``ledger resume`` lists, for each open item that has an ``item.stopped``
event, the newest stop (its point, the skills the next session invokes, the
files it reads) and the literal commands that continue: ``resume --item`` and
``card``. ``--item ID`` shows that one item. ``--brief`` prepends the ledger
version line (exactly what ``ledger --version`` prints) and then the short
form of the agent contract (the full page is in the ledger repository's
docs folder), so a session-start hook can print the version and the contract
into context. The JSON data then also carries ``version`` and ``contract``.

The JSON data always carries ``open_without_stop``: how many open items have
no stop recorded (0 when there are none, and 0 with ``--item``). Those items
are not listed in ``items``, so the count is what tells an empty ledger from one
whose open items were never stopped.

The verb never writes: the database is opened read-only. A database that does
not exist yet is not an error: the output says there are no open items and the
exit status is 0 (an unknown ``--item`` is ``not_found``, exit 1). Every
command printed carries the database's resolved path, so it runs from any
directory.

``--hook-env`` is for a SessionStart hook. It reads the hook's JSON input on
stdin and prints the lines the hook appends to the session's environment file,
each ``export NAME=<value>`` with the value shell-quoted, in this order:
``LEDGER_SESSION`` from ``session_id`` (even when the variable is already set);
then ``LEDGER_AGENT`` as ``claude-code`` when the name is absent from this
process's environment, and ``LEDGER_HARNESS`` as ``claude-code`` when it is
absent or exactly ``cli`` (the value a shell profile sets; this hook runs only
inside Claude Code, where ``cli`` is never true); any other value, an empty
one included, is kept and gets no line. A ``session_id``
that is missing, not a string, empty, or holds a control character leaves
its line out. It prints no ``LEDGER_MODEL`` line, whatever the input's
``model``: only a few hook events may write the session's environment file
and a model switch is not one of them, so a stamped model would go stale
while later writes kept recording it (``--hook-model`` records the model
instead). Stdin that is not one UTF-8 JSON object
is refused with ``hook_env_bad_input``, and an input for another event with
``hook_env_not_session_start`` (exit 3, nothing on stdout). The flag never
resolves, opens or creates the database, and it cannot be combined with
``--brief`` or ``--item``.

``--hook-model`` is for the SessionStart and PostModelSwitch hooks. It reads
the hook's JSON input on stdin and records the session's model in the
session's file beside the database (``ledger/session_model.py``), which
every later write of that session reads. The model is ``to_model`` for
PostModelSwitch and ``model`` for SessionStart, a usable name of at most 256
UTF-8 bytes. A SessionStart with no model records that writes stamp
``LEDGER_MODEL``. Any other model value records that the model is unknown
(writes stamp none) and is then refused with ``hook_model_bad_model``. A
PostModelSwitch older than the stored record writes nothing and exits 0
with ``stale`` true. Refusals (exit 3, nothing on stdout):
``hook_model_bad_input`` (stdin not one UTF-8 JSON object, or no usable
``session_id``), ``hook_model_wrong_event``, ``hook_model_no_folder``,
``hook_model_unsafe_folder``, ``hook_model_write_failed``,
``hook_model_timeout`` (not done within two seconds of its start) and
``hook_model_unsupported`` (a platform without ``flock``, such as Windows).
The record changes what later writes stamp, so like a write it needs
``LEDGER_HARNESS`` and is refused with ``missing_harness`` without it. It
never opens or creates the database and never reads ``LEDGER_SESSION``; it
cannot be combined with ``--hook-env``, ``--brief`` or ``--item``.
"""
from __future__ import annotations

import os
import shlex
import sys
import unicodedata
from typing import Any, Mapping

from ledger import __version__, session_model
from ledger.paths import resolve_db_path
from ledger.store import open_ledger_readonly
from ledger.verbs import (
    EXIT_NOT_FOUND,
    EXIT_REFUSED,
    VERSION_LINE,
    CliError,
    Context,
    Result,
    UsageError,
    Verb,
    invocation_prefix,
    quote_command_arg,
    require_item,
)
from ledger.verbs.capture import read_hook_input

NO_OPEN_ITEMS = "no open items"

# The contract's short form. Every write trigger on the contract page (in the
# ledger repository's docs folder) appears in it by the page's own words and
# with the page's own commands, one line per trigger (a second line only for a
# second command); a repository test reads both so the two cannot drift. Keep
# it under 40 lines.
SHORT_FORM = """\
LEDGER AGENT CONTRACT (short form; run ledger contract for the full page)
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
Finding things: ledger search WORD... (a decision or a topic, by words); ledger context ITEM (an item with what it links to); ledger links ITEM (the links to and from an item); ledger questions (what waits on the person); ledger decisions (what was settled, with each decision's standing).
Actor stamps: LEDGER_OPERATOR (the person), LEDGER_AGENT (this seat),
LEDGER_HARNESS (the runtime; writes refuse without it), LEDGER_MODEL (the
model), LEDGER_SESSION (the session id), all exported before you write.
Close rule: end a session by appending item.stopped on each item you worked
and printing the exact resume command, never prose. Replace ITEM:
  ledger append --item ITEM --kind item.stopped --payload '{"point": "what is done and what is next", "phase": "build", "skills": ["review"], "refs": ["plan.md"]}'
  ledger resume --item ITEM
phase is one of orient, plan, build, review, close. skills and refs are optional.
Item types for ledger new --type: topic, ticket, initiative, decision, gate, guidance (there is no task: use ticket).
Limits: point is 1 to 600 characters; skills at most 12; refs at most 20, each at most 300 characters, one line, and one of:
a file path (src/app.py:12), a pull request or issue (#12 or owner/repo#12), a branch (branch:feat/x), a commit sha (3f2a9c1), an event (event:42).
A URL is not a ref: ledger link ITEM --rel url --target URL. A refusal names the limit and the length you gave.
"""


def add_arguments(parser) -> None:
    parser.add_argument("--item", dest="item_id", help="show only this item")
    parser.add_argument(
        "--brief",
        action="store_true",
        help="print the ledger version and the agent contract's short form first",
    )
    parser.add_argument(
        "--hook-env",
        action="store_true",
        help="read a SessionStart hook input on stdin and print the export lines"
        " for LEDGER_SESSION, LEDGER_AGENT and LEDGER_HARNESS (never LEDGER_MODEL);"
        " LEDGER_HARNESS is printed when it is unset or exactly cli; opens no database",
    )
    parser.add_argument(
        "--hook-model",
        action="store_true",
        help="read a SessionStart or PostModelSwitch hook input on stdin and record"
        " the session's model beside the database; opens no database",
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


# What --hook-env stamps for a variable the environment does not set.
HOOK_AGENT = "claude-code"
HOOK_HARNESS = "claude-code"
# The one inherited LEDGER_HARNESS value --hook-env replaces: a shell profile
# sets it for the commands a person types, and a session started from that
# shell inherits it, but the hook runs only inside Claude Code.
SHELL_HARNESS = "cli"


def _hook_env_refusal(code: str, message: str) -> CliError:
    return CliError(message, code=code, exit_code=EXIT_REFUSED)


def _usable(value: object) -> str | None:
    """A non-empty UTF-8 string with no control character, else None."""
    if type(value) is not str or value == "" or any(_is_control(ch) for ch in value):
        return None
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate from a JSON escape
        return None
    return value


def hook_env(hook: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, str]:
    """The variables ``--hook-env`` prints for a SessionStart ``hook`` input,
    in order, given the process environment ``environ``."""
    env: dict[str, str] = {}
    session = _usable(hook.get("session_id"))
    if session is not None:
        env["LEDGER_SESSION"] = session
    if "LEDGER_AGENT" not in environ:
        env["LEDGER_AGENT"] = HOOK_AGENT
    if "LEDGER_HARNESS" not in environ or environ["LEDGER_HARNESS"] == SHELL_HARNESS:
        env["LEDGER_HARNESS"] = HOOK_HARNESS
    # No LEDGER_MODEL: the input's model is the one the session started on, and
    # a model switch later in the session cannot re-stamp the environment file.
    return env


def _read_stdin(code: str = "hook_env_bad_input") -> str:
    buffer = getattr(sys.stdin, "buffer", None)
    try:
        if buffer is None:
            return sys.stdin.read()
        return buffer.read().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _hook_env_refusal(code, f"stdin is not UTF-8 text: {exc}") from exc


def _run_hook_env(args) -> Result:
    if args.brief or args.item_id is not None:
        raise UsageError("--hook-env cannot be combined with --brief or --item")
    raw = _read_stdin()
    try:
        hook = read_hook_input(raw)
    except CliError as exc:
        raise _hook_env_refusal("hook_env_bad_input", str(exc)) from exc
    if hook.get("hook_event_name") != "SessionStart":
        raise _hook_env_refusal(
            "hook_env_not_session_start",
            "resume --hook-env takes a SessionStart hook input",
        )
    env = hook_env(hook, os.environ)
    lines = [f"export {name}={shlex.quote(value)}" for name, value in env.items()]
    return Result(data={"env": env}, text="\n".join(lines))


HOOK_MODEL_EVENTS = {"PostModelSwitch": "to_model", "SessionStart": "model"}


def _hook_model_intent(event: str, hook: Mapping[str, Any]) -> tuple[str, str | None, bool]:
    """The record the input asks for: (state, model, bad model)."""
    field = HOOK_MODEL_EVENTS[event]
    value = hook.get(field)
    if event == "SessionStart" and value is None:
        # A start that names no model: writes stamp LEDGER_MODEL again.
        return "env", None, False
    model = session_model.usable_model(value)
    if model is None:
        # A model the hook cannot name: neither an older record nor
        # LEDGER_MODEL may be stamped until a later valid event.
        return "unknown", None, True
    return "model", model, False


def _run_hook_model(args, ctx: Context) -> Result:
    # The ordering key and the start of the two-second budget.
    seq = session_model.stamp()
    # The record changes what this session's later writes stamp, so it needs
    # the writer identity every write needs. resume is not a write verb (the
    # CLI's gate does not run for it), so this action checks for itself, as
    # ``doc add`` does.
    if not ctx.harness:
        raise CliError(
            "LEDGER_HARNESS is required for writes; set it, e.g. "
            "LEDGER_HARNESS=claude-code",
            code="missing_harness",
            exit_code=EXIT_REFUSED,
        )
    if args.hook_env or args.brief or args.item_id is not None:
        raise UsageError("--hook-model cannot be combined with --hook-env, --brief or --item")
    raw = _read_stdin("hook_model_bad_input")
    try:
        hook = read_hook_input(raw)
    except CliError as exc:
        raise _hook_env_refusal("hook_model_bad_input", str(exc)) from exc
    event = hook.get("hook_event_name")
    if type(event) is not str or event not in HOOK_MODEL_EVENTS:
        raise _hook_env_refusal(
            "hook_model_wrong_event",
            "resume --hook-model takes a SessionStart or PostModelSwitch hook input",
        )
    session = _usable(hook.get("session_id"))
    if session is None:
        raise _hook_env_refusal("hook_model_bad_input", "the input has no usable session_id")
    state, model, bad = _hook_model_intent(event, hook)
    try:
        written = session_model.record_hook_model(ctx.db, session, state, model, event, seq)
    except session_model.HookModelRefusal as exc:
        raise _hook_env_refusal(exc.code, str(exc)) from exc
    if not written:
        # Superseded by a newer record: not an error, whatever its model.
        return Result(data={"recorded": None, "state": None, "stale": True}, text="")
    if bad:
        raise _hook_env_refusal(
            "hook_model_bad_model",
            f"the input's {HOOK_MODEL_EVENTS[event]} is not a usable model name;"
            " the session's model is recorded as unknown",
        )
    return Result(data={"recorded": model, "state": state, "stale": False}, text="")


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
    if args.hook_model:
        # Never resolves the database for opening: it only names the folder
        # the session's model record sits in.
        return _run_hook_model(args, ctx)
    if args.hook_env:
        # Before resolve_db_path: the flag never resolves, opens or creates
        # the database.
        return _run_hook_env(args)
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
    data: dict[str, Any] = {"items": entries, "open_without_stop": open_without_stop}
    text = body
    if args.brief:
        data["version"] = __version__
        data["contract"] = SHORT_FORM
        text = VERSION_LINE + "\n" + SHORT_FORM + "\n" + body
    return Result(data=data, text=text)


VERB = Verb(
    name="resume",
    help="print where work stopped and the command that continues it",
    add_arguments=add_arguments,
    run=run,
)
