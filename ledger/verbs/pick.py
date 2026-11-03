"""Record a question-picker answer as an election.

A picker answer (a question, its offered options, the option chosen, and
any typed text) is a tool result, not a submitted prompt, so no prompt
capture ever sees it. This verb records it in two events:

1. ``utterance.captured`` with channel and provenance class
   ``tool-answer``, whose text holds the question, the chosen label and any
   typed text, verbatim;
2. ``decision.elected`` through the same ``record_election`` path as
   ``ledger elect``, citing that utterance as its ``quote_ref`` and quoting
   the chosen label from it. Every other offered option is recorded as
   rejected.

A chosen label outside the offered options is a typed answer: it is
recorded as chosen and every offered option as rejected. Both events land
in one transaction under idempotency keys derived from one key, so an
identical retry returns the recorded pair and a differing retry is refused.
"""
from __future__ import annotations

from typing import Any

from ledger.card import item_spec_moved
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
from ledger.verbs.elect import _resolve_ask, record_election, validate_election_fields

TOOL_ANSWER = "tool-answer"
_OPTION_SEP = "::"


def picker_text(question: str, chosen: str, free_text: str | None) -> str:
    text = f"{question}\nchosen: {chosen}"
    if free_text:
        text += f"\ntyped: {free_text}"
    return text


def record_picker_answer(
    lg,
    *,
    item_id: str,
    question: str,
    options: list[dict[str, str]],
    chosen: str,
    scope: str,
    idempotency_key: str,
    free_text: str | None = None,
    asked_event_id: int | None = None,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> tuple[Any, Any]:
    """Capture the answer as an utterance, then elect from it. Returns both events."""
    labels = [str(option.get("label") or "").strip() for option in options]
    if len(labels) < 2 or any(not label for label in labels):
        raise CliError(
            "a picker offers at least two labelled options",
            code="missing_field",
            exit_code=EXIT_REFUSED,
        )
    if len(set(labels)) != len(labels):
        raise CliError("picker option labels repeat", code="duplicate", exit_code=EXIT_REFUSED)
    if not chosen.strip():
        raise CliError("missing field: chosen", code="missing_field", exit_code=EXIT_REFUSED)
    require_item(lg, item_id)
    description = ""
    if chosen in labels:
        description = str(options[labels.index(chosen)].get("description") or "")
    rejected = [label for label in labels if label != chosen]
    chosen_description = description.strip() or free_text or chosen
    # Check what the election will check before capturing anything, so a
    # refused answer leaves no orphan utterance behind.
    validate_election_fields(question, chosen, chosen_description, rejected, scope)
    election_key = f"{idempotency_key}:election"
    # Both events land in one transaction, so a refusal anywhere leaves
    # neither behind and a retry finds both or none.
    with lg.transaction():
        # A retry finds its own election already closing the ask; the appends
        # below then return the stored pair, or refuse a retry that differs.
        retry = lg.event_by_key(election_key) is not None
        if (
            not retry
            and asked_event_id is not None
            and _resolve_ask(lg._conn, item_id, asked_event_id) is None
        ):
            raise CliError(
                f"ask is not an open decision.asked on item {item_id}: {asked_event_id}",
                code="unresolvable_ask",
                exit_code=EXIT_REFUSED,
            )
        utterance = lg.append(
            kind="utterance.captured",
            payload={"text": picker_text(question, chosen, free_text)},
            item_id=item_id,
            idempotency_key=f"{idempotency_key}:utterance",
            channel=TOOL_ANSWER,
            provenance_class=TOOL_ANSWER,
            actor=actor,
            harness=harness,
            model=model,
            session=session,
        )
        quotes = [chosen] + ([free_text] if free_text else [])
        election = record_election(
            lg,
            item_id=item_id,
            question=question,
            chosen=chosen,
            chosen_description=chosen_description,
            rejected=rejected,
            scope=scope,
            quote_ref=utterance.id,
            quotes=quotes,
            asked_event_id=asked_event_id,
            idempotency_key=election_key,
            actor=actor,
            harness=harness,
            model=model,
            session=session,
        )
    return utterance, election


def _parse_option(raw: str) -> dict[str, str]:
    label, _sep, description = raw.partition(_OPTION_SEP)
    option = {"label": label.strip()}
    if description.strip():
        option["description"] = description.strip()
    return option


def add_arguments(parser) -> None:
    parser.add_argument("item")
    parser.add_argument("--question", required=True)
    parser.add_argument(
        "--option",
        required=True,
        action="append",
        dest="options",
        help=f"offered option as LABEL or LABEL{_OPTION_SEP}DESCRIPTION (repeat)",
    )
    parser.add_argument("--chosen", required=True, help="the chosen label, verbatim")
    parser.add_argument("--free-text", help="any text typed with the answer, verbatim")
    parser.add_argument("--scope", required=True)
    parser.add_argument("--ask", type=int, help="event id of the open decision.asked it answers")
    parser.add_argument("--idempotency-key")


def run(args, ctx: Context) -> Result:
    lg = open_db(ctx, write=True)
    try:
        utterance, election = record_picker_answer(
            lg,
            item_id=args.item,
            question=args.question,
            options=[_parse_option(raw) for raw in args.options],
            chosen=args.chosen,
            free_text=args.free_text,
            scope=args.scope,
            asked_event_id=args.ask,
            idempotency_key=args.idempotency_key or new_key(),
            **actor_kwargs(ctx),
        )
        # The claimant hears when the spec moved past the version it claimed.
        moved = item_spec_moved(lg, args.item)
    finally:
        lg.close()
    return Result(
        data={
            "item_id": args.item,
            "utterance": event_dict(utterance),
            "election": event_dict(election),
            "spec_moved": moved,
        },
        text=f"picked\t{args.item}\tutterance={utterance.id}\telection={election.id}"
        + (f"\n{moved}" if moved else ""),
    )


VERB = Verb(
    name="pick",
    help="record a question-picker answer as an election",
    add_arguments=add_arguments,
    run=run,
    writes=True,
)
