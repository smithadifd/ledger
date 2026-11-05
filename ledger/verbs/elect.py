"""Record a structured election (decision.elected).

An election carries the five-field provenance from
``standards/delegation-loop.md:275``: the full question, the chosen option
label, the chosen option description, the labels of every rejected
alternative, and the scope the decision covers. It also records a
``quote_ref`` to a previously captured utterance event and at least one
verbatim ``--quote`` span of that utterance's text, so every election can be
checked against what the owner actually submitted.

The referenced utterance must exist and have kind ``utterance.captured``,
and each quote must be a verbatim substring of its text: byte-exact, with
no case folding and no whitespace normalisation. A captured prompt is
audit evidence of submitted text, not proof of authorship: the capture hook
also fires for scheduled tasks, subagent reports and other sessions'
messages.

``--ask`` optionally links the election to the open ``decision.asked``
event it answers. The election then closes exactly that ask and no other;
without ``--ask`` it closes no ask. A chosen label outside the ask's
offered options is allowed: asks accept free-text answers.
"""
from __future__ import annotations

import argparse
import json
from typing import Any

from ledger.verbs import (
    EXIT_REFUSED,
    CliError,
    Context,
    Result,
    Verb,
    actor_kwargs,
    event_dict,
    new_key,
    open_db,
    require_item,
)


EPILOG = """\
Every flag except --ask is required. --rejected and --quote are repeatable:
give --rejected once for each alternative that was not chosen, and --quote once
for each verbatim span of the captured utterance (--quote-ref is its event id).

example:
  ledger elect it-1a2b3c4d5e6f --question "Which retry policy?" --chosen backoff --chosen-description "Exponential backoff, three tries" --rejected fixed --rejected none --scope "the sync job" --quote-ref 2 --quote "use backoff"
"""


def add_arguments(parser) -> None:
    parser.epilog = EPILOG
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.add_argument("item")
    parser.add_argument("--question", required=True)
    parser.add_argument("--chosen", required=True)
    parser.add_argument("--chosen-description", required=True)
    parser.add_argument(
        "--rejected",
        required=True,
        action="append",
        help="rejected option label; required, and repeatable: give it once for each "
        "rejected alternative",
    )
    parser.add_argument("--scope", required=True)
    parser.add_argument("--quote-ref", required=True, type=int)
    parser.add_argument(
        "--quote",
        required=True,
        action="append",
        dest="quotes",
        help="verbatim span of the referenced utterance (repeat for each quote)",
    )
    parser.add_argument(
        "--ask",
        type=int,
        help="event id of the open decision.asked this election answers",
    )


def _require_nonempty(name: str, value: str) -> None:
    if value.strip() == "":
        raise CliError(
            f"missing field: {name}",
            code="missing_field",
            exit_code=EXIT_REFUSED,
        )


def _utterance_text(lg, quote_ref: int) -> str:
    row = lg._conn.execute(
        "SELECT kind, payload FROM events WHERE id = ?", (quote_ref,)
    ).fetchone()
    if row is None:
        raise CliError(
            f"quote_ref not found: {quote_ref}",
            code="unresolvable_ref",
            exit_code=EXIT_REFUSED,
        )
    if row["kind"] != "utterance.captured":
        raise CliError(
            "quote_ref must resolve to an utterance.captured event",
            code="unresolvable_ref",
            exit_code=EXIT_REFUSED,
        )
    payload = json.loads(row["payload"])
    if "omitted_bytes" in payload:
        # Its text is a fixed pointer line, not the person's words.
        raise CliError(
            f"quote_ref {quote_ref} is a pointer to an omitted prompt body,"
            " not the person's words",
            code="pointer_utterance",
            exit_code=EXIT_REFUSED,
        )
    return payload["text"]


def validate_quote_provenance(lg, quote_ref: int, quotes: list[str]) -> None:
    """Require a captured utterance containing every verbatim election quote."""
    if type(quote_ref) is not int or quote_ref < 1:
        raise CliError("missing or invalid quote_ref", code="unresolvable_ref", exit_code=EXIT_REFUSED)
    if not quotes:
        raise CliError("missing field: quotes", code="missing_field", exit_code=EXIT_REFUSED)
    for quote in quotes:
        _require_nonempty("quote", quote)
    text = _utterance_text(lg, quote_ref)
    for quote in quotes:
        if quote not in text:
            raise CliError(
                f"quote is not a verbatim substring of utterance"
                f" {quote_ref}: {quote!r}",
                code="quote_mismatch",
                exit_code=EXIT_REFUSED,
            )


def _resolve_ask(conn, item_id: str, asked_event_id: int) -> dict[str, Any] | None:
    """Return the ask's payload when it is an open ``decision.asked`` on ``item_id``.

    ``v_open_decisions`` is the single definition of an open ask: it admits
    only ``decision.asked`` events, on this item, with no later election
    linked to them.
    """
    open_row = conn.execute(
        "SELECT 1 FROM v_open_decisions WHERE item_id = ? AND asked_event_id = ?",
        (item_id, asked_event_id),
    ).fetchone()
    if open_row is None:
        return None
    row = conn.execute(
        "SELECT payload FROM events WHERE id = ?", (asked_event_id,)
    ).fetchone()
    return json.loads(row["payload"])


def _asked_payload(conn, item_id: str, asked_event_id: int) -> dict[str, Any] | None:
    """The payload of a ``decision.asked`` on ``item_id``, open or not."""
    row = conn.execute(
        "SELECT payload FROM events WHERE id = ? AND kind = 'decision.asked' AND item_id = ?",
        (asked_event_id, item_id),
    ).fetchone()
    return None if row is None else json.loads(row["payload"])


def record_election(
    lg,
    *,
    item_id: str,
    question: str,
    chosen: str,
    chosen_description: str,
    rejected: list[str],
    scope: str,
    quote_ref: int,
    quotes: list[str],
    asked_event_id: int | None,
    idempotency_key: str,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
):
    """Apply the same field, quote, and open-ask checks for every election writer."""
    validate_election_fields(question, chosen, chosen_description, rejected, scope)
    require_item(lg, item_id)
    validate_quote_provenance(lg, quote_ref, quotes)
    ask_payload = None
    precondition = None
    if asked_event_id is not None:
        ask_payload = _resolve_ask(lg._conn, item_id, asked_event_id)
        if ask_payload is None and lg.event_by_key(idempotency_key) is not None:
            # A retry: the stored election under this key closed the ask.
            # append returns it when the retry matches and refuses it otherwise.
            ask_payload = _asked_payload(lg._conn, item_id, asked_event_id)
        if ask_payload is None:
            raise CliError(
                f"ask is not an open decision.asked on item {item_id}:"
                f" {asked_event_id}",
                code="unresolvable_ask",
                exit_code=EXIT_REFUSED,
            )

        def ask_is_open(conn) -> None:
            if _resolve_ask(conn, item_id, asked_event_id) is None:
                raise CliError(
                    f"ask is not an open decision.asked on item {item_id}:"
                    f" {asked_event_id}",
                    code="unresolvable_ask",
                    exit_code=EXIT_REFUSED,
                )

        precondition = ask_is_open

    payload = {
        "question": question,
        "chosen": chosen,
        "chosen_description": chosen_description,
        "rejected": rejected,
        "scope": scope,
        "quote_ref": quote_ref,
        "quotes": quotes,
    }
    if ask_payload is not None:
        payload["asked_event_id"] = asked_event_id
        if ask_payload.get("question_id") is not None:
            payload["question_id"] = ask_payload["question_id"]
    return lg.append(
        kind="decision.elected",
        payload=payload,
        item_id=item_id,
        idempotency_key=idempotency_key,
        actor=actor,
        harness=harness,
        model=model,
        session=session,
        precondition=precondition,
    )


def validate_election_fields(
    question: str, chosen: str, chosen_description: str, rejected: list[str], scope: str
) -> None:
    """Validate the structured answer fields before touching a ledger."""
    for name, value in (
        ("question", question), ("chosen", chosen),
        ("chosen-description", chosen_description), ("scope", scope),
    ):
        _require_nonempty(name, value)
    if not rejected:
        raise CliError("missing field: rejected", code="missing_field", exit_code=EXIT_REFUSED)
    for label in rejected:
        if label.strip() == "":
            raise CliError(
                "rejected labels must not be empty",
                code="missing_field",
                exit_code=EXIT_REFUSED,
            )


def run(args, ctx: Context) -> Result:
    lg = open_db(ctx, write=True)
    try:
        event = record_election(
            lg,
            item_id=args.item,
            question=args.question,
            chosen=args.chosen,
            chosen_description=args.chosen_description,
            rejected=args.rejected,
            scope=args.scope,
            quote_ref=args.quote_ref,
            quotes=args.quotes,
            asked_event_id=args.ask,
            idempotency_key=new_key(),
            **actor_kwargs(ctx),
        )
    finally:
        lg.close()
    return Result(
        data={"item_id": args.item, "event": event_dict(event)},
        text=f"elected\t{args.item}\tevent={event.id}",
    )


VERB = Verb(
    name="elect",
    help="record a structured election with quote provenance",
    add_arguments=add_arguments,
    run=run,
    writes=True,
)
