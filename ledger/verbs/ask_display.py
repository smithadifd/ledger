"""Print the picker a seat sends for an ask: ``ledger ask-display K``.

The output is the whole AskUserQuestion input for ask K, a ``questions``
array holding exactly one question rendered from the ask alone, and the
fingerprint of that array (SHA-256 hex of its canonical JSON):

- question: ``[ask K] <question> (scope: <scope>)``, or
  ``[ask K] <question> (scope: <scope>; question id: <question_id>)`` when
  the ask has a question id (a grant id decides which grant the answer
  stands for, so the owner sees it)
- header: ``ask K``
- options: the ask's options in order as ``{label, description}``; an option
  without a description shows its label
- ``multiSelect``: false

The seat pastes this input as its AskUserQuestion call and never composes
the picker itself: a gate counts the owner's answer only when the harness
captured exactly this display (``ledger.answers``). An ask with no scope,
with fewer than two or more than four options, with two options sharing a
label, whose ``ask K`` header would be longer than 12 characters, or whose
displayed text (question id, scope, an option label or description, the
question) breaks ``ledger.schema.display_text_problem`` is refused with
``ask_not_pickable``, naming the field, the class and the code point. That
rule refuses a control character (a newline is allowed in the question
only), a format character (Cf: bidi controls, zero-width characters), a line
or paragraph separator, a surrogate, private-use or unassigned code point, a
default-ignorable code point (U+FE0F, U+034F and the rest), any White_Space
code point but U+0020, a blank (U+2800, the Egyptian hieroglyph blanks,
U+FFFC and the rest of ``BLANK_CODE_POINTS``), text that is not NFC, a
leading combining mark, and leading or trailing whitespace. There is no emoji
exception.

Text output is the input object as indented JSON, then one line
``display_sha256 <hex>``. ``--json`` carries ``{asked_event_id, item_id,
input, display_sha256}``. A read verb.
"""
from __future__ import annotations

import json

from ledger.answers import AskNotPickable, fingerprint, render_ask_display
from ledger.verbs import (
    EXIT_NOT_FOUND,
    EXIT_REFUSED,
    CliError,
    Context,
    Result,
    Verb,
    open_db,
)

_MAX_EVENT_ID = 2**63 - 1


def add_arguments(parser) -> None:
    parser.add_argument("ask", type=int, help="event id of the decision.asked to display")


def run(args, ctx: Context) -> Result:
    if not 1 <= args.ask <= _MAX_EVENT_ID:
        raise CliError(
            f"not a decision.asked event: {args.ask}", code="ask_not_found", exit_code=EXIT_NOT_FOUND
        )
    lg = open_db(ctx, write=False)
    try:
        row = lg._conn.execute(
            "SELECT kind, item_id, payload FROM events WHERE id = ?", (args.ask,)
        ).fetchone()
    finally:
        lg.close()
    if row is None or row["kind"] != "decision.asked":
        raise CliError(
            f"not a decision.asked event: {args.ask}", code="ask_not_found", exit_code=EXIT_NOT_FOUND
        )
    try:
        questions = render_ask_display(args.ask, json.loads(row["payload"]))
    except AskNotPickable as exc:
        raise CliError(str(exc), code="ask_not_pickable", exit_code=EXIT_REFUSED) from exc
    tool_input = {"questions": questions}
    digest = fingerprint(questions)
    return Result(
        data={
            "asked_event_id": args.ask,
            "item_id": row["item_id"],
            "input": tool_input,
            "display_sha256": digest,
        },
        text=json.dumps(tool_input, indent=2, ensure_ascii=False) + f"\ndisplay_sha256 {digest}",
    )


VERB = Verb(
    name="ask-display",
    help="print the AskUserQuestion input (one question) a seat sends for an ask",
    add_arguments=add_arguments,
    run=run,
)
