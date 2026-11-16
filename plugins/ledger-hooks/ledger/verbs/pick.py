"""Record the owner's picker answer to an ask: ``ledger pick ITEM --ask K``.

The seat raised ask K's picker with the input ``ledger ask-display K``
printed, the owner chose an option, and the harness hook wrote that answer as
a ``hook:picker`` capture (``ledger capture``). ``pick`` takes no question,
option, label, scope or capture id from the seat:

1. K must be an open ``decision.asked`` on ITEM (``unresolvable_ask``).
2. The capture is the earliest ``hook:picker`` capture whose
   ``picker.asked_event_id`` is K (``capture_missing`` when there is none).
   Captures for K that disagree refuse ``capture_conflict``: the seat never
   chooses among answers.
3. The election is derived from K at the captured option position: chosen
   label and description, the other labels as rejected, K's question, scope
   and question id, the chosen label as its one quote, and the capture's
   ``display_sha256`` and ``chosen_position``.
4. ``check_answer`` (``ledger.answers``) runs on it before anything is
   written, so a refused pick writes nothing; then the election is written
   through ``record_election``, closing K.

The retired form (``--question``, ``--option``, ``--chosen``, ``--free-text``,
``--scope`` without ``--ask``) is refused with ``pick_needs_ask``; any of
those flags beside ``--ask`` is a usage error. ``--idempotency-key K`` makes a
retry return the stored election.
"""
from __future__ import annotations

import argparse
from typing import Any

from ledger.answers import check_answer, derive_election, picker_captures
from ledger.card import item_spec_moved
from ledger.verbs import (
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
from ledger.verbs.elect import _asked_payload, _resolve_ask, record_election

_MAX_EVENT_ID = 2**63 - 1  # SQLite integer ceiling
# The retired form's flags. They stay parseable only so that a call in the
# old shape is refused by name instead of by argparse.
RETIRED_FLAGS = (
    ("question", "--question"),
    ("options", "--option"),
    ("chosen", "--chosen"),
    ("free_text", "--free-text"),
    ("scope", "--scope"),
)


def _unresolvable(item_id: str, asked_event_id: int) -> CliError:
    return CliError(
        f"ask is not an open decision.asked on item {item_id}: {asked_event_id}",
        code="unresolvable_ask",
        exit_code=EXIT_REFUSED,
    )


def record_pick(
    lg,
    *,
    item_id: str,
    asked_event_id: int,
    idempotency_key: str,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> tuple[Any, Any]:
    """Elect from the harness's capture for ask K. Returns (capture, election)."""
    if not 1 <= asked_event_id <= _MAX_EVENT_ID:
        raise _unresolvable(item_id, asked_event_id)
    require_item(lg, item_id)
    conn = lg._conn
    # One transaction: the capture read, the check and the write see one state.
    with lg.transaction():
        retry = lg.event_by_key(idempotency_key) is not None
        ask = _resolve_ask(conn, item_id, asked_event_id)
        if ask is None and retry:
            # A retry finds its own election already closing the ask; the
            # append below returns it, or refuses a retry that differs.
            ask = _asked_payload(conn, item_id, asked_event_id)
        if ask is None:
            raise _unresolvable(item_id, asked_event_id)
        captures = picker_captures(conn, asked_event_id)
        if not captures:
            raise CliError(
                f"no hook:picker capture answers ask {asked_event_id}: raise the picker"
                f" `ledger ask-display {asked_event_id}` prints, then pick",
                code="capture_missing",
                exit_code=EXIT_REFUSED,
            )
        capture = captures[0]
        fields = derive_election(asked_event_id, ask, capture) or {}
        reason = check_answer(lg, asked_event_id, capture.id, fields, item_id)
        if reason is not None:
            raise CliError(
                f"the capture (event {capture.id}) does not answer ask {asked_event_id}: {reason}",
                code=reason,
                exit_code=EXIT_REFUSED,
            )
        election = record_election(
            lg,
            item_id=item_id,
            question=fields["question"],
            chosen=fields["chosen"],
            chosen_description=fields["chosen_description"],
            rejected=fields["rejected"],
            scope=fields["scope"],
            quote_ref=capture.id,
            quotes=fields["quotes"],
            asked_event_id=asked_event_id,
            idempotency_key=idempotency_key,
            answer={
                "display_sha256": fields["display_sha256"],
                "chosen_position": fields["chosen_position"],
            },
            actor=actor,
            harness=harness,
            model=model,
            session=session,
        )
    return capture, election


def add_arguments(parser) -> None:
    parser.add_argument("item")
    parser.add_argument(
        "--ask", type=int, help="event id of the open decision.asked the owner answered"
    )
    parser.add_argument("--idempotency-key")
    for flag in ("--question", "--chosen", "--free-text", "--scope"):
        parser.add_argument(flag, help=argparse.SUPPRESS)
    parser.add_argument("--option", action="append", dest="options", help=argparse.SUPPRESS)


def run(args, ctx: Context) -> Result:
    retired = [flag for name, flag in RETIRED_FLAGS if getattr(args, name, None) is not None]
    if args.ask is None:
        raise CliError(
            "pick needs --ask EVENT: it records the harness's capture of the owner's answer to"
            " that ask (the --question/--option/--chosen form is retired)",
            code="pick_needs_ask",
            exit_code=EXIT_REFUSED,
        )
    if retired:
        raise UsageError(
            f"pick --ask takes no {', '.join(retired)}: every field comes from the ask and"
            " the harness's capture"
        )
    lg = open_db(ctx, write=True)
    try:
        capture, election = record_pick(
            lg,
            item_id=args.item,
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
            "utterance": event_dict(capture),
            "election": event_dict(election),
            "spec_moved": moved,
        },
        text=f"picked\t{args.item}\tutterance={capture.id}\telection={election.id}"
        + (f"\n{moved}" if moved else ""),
    )


VERB = Verb(
    name="pick",
    help="record the owner's picker answer to an ask (pick ITEM --ask EVENT) as an election",
    add_arguments=add_arguments,
    run=run,
    writes=True,
)
