"""Verify the hash chain, the payloads and the child-of forest.

The chain and payload checks run first (``Ledger.verify``). Then every event
is replayed, in id order, into an empty in-memory database with the ledger's
schema, and each one first passes the same child-of guard the write path and
restore use (``check_child_of_event``), seeing only the events before it. A
store written before that guard existed, or edited outside the CLI, can hold
a second anchor without ``moved_from``, a self-link, a link to an anchor that
is not an item, or a cycle; the first event the guard refuses is reported by
event id and code, and the verb exits 4 (integrity). An event the guard cannot
evaluate is reported the same way with the code ``unevaluable``, never
skipped. A store that passes prints what it always printed and exits 0.
"""
from __future__ import annotations

import sqlite3

from ledger.restore import _INSERT
from ledger.store import (
    Event,
    ForestRefusal,
    canonical_dumps,
    check_child_of_event,
    migrate,
)
from ledger.verbs import EXIT_INTEGRITY, CliError, Context, Result, Verb, open_db

UNEVALUABLE = "unevaluable"


def add_arguments(parser) -> None:
    parser.add_argument(
        "--system",
        help="also require the database to be claimed for this system",
    )


def first_forest_break(events: list[Event]) -> tuple[int, str, str] | None:
    """Replay ``events`` in id order through the write path's child-of guard.

    Returns ``(event id, code, message)`` for the first event the guard
    refuses, or that it cannot evaluate (code ``unevaluable``), else None.
    The replay runs in a private in-memory database and writes nothing to
    the store.
    """
    scratch = sqlite3.connect(":memory:", isolation_level=None)
    scratch.row_factory = sqlite3.Row
    try:
        migrate(scratch)
        scratch.execute("BEGIN")
        for event in sorted(events, key=lambda e: e.id):
            try:
                check_child_of_event(scratch, event.kind, event.payload, event.item_id)
                scratch.execute(
                    _INSERT,
                    (
                        event.id,
                        event.kind,
                        event.payload_version,
                        canonical_dumps(event.payload),
                        event.item_id,
                        event.idempotency_key,
                        event.actor,
                        event.harness,
                        event.model,
                        event.session,
                        event.channel,
                        event.provenance_class,
                        event.recorded_at,
                        event.prev_hash,
                        event.hash,
                    ),
                )
            except ForestRefusal as exc:
                return event.id, exc.code, str(exc)
            except Exception as exc:  # fail closed: never skip an event
                return event.id, UNEVALUABLE, f"{type(exc).__name__}: {exc}"
        return None
    finally:
        scratch.close()


def run(args, ctx: Context) -> Result:
    lg = open_db(ctx, write=False)
    try:
        lg.verify(system=args.system)
        events = lg.events()
    finally:
        lg.close()
    found = first_forest_break(events)
    if found is not None:
        event_id, code, message = found
        raise CliError(
            f"event {event_id} {code}: {message}",
            code=code,
            exit_code=EXIT_INTEGRITY,
        )
    count = len(events)
    return Result(
        data={"events": count},
        text=f"ok events={count}",
    )


VERB = Verb(
    name="verify",
    help="verify the hash chain, payloads and child-of forest",
    add_arguments=add_arguments,
    run=run,
)
