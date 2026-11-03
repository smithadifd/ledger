"""Register an entity or change its state: the entity registry's only writer.

An entity is an automation, a project, a machine or a model, keyed by its
type and a stable id (the same id may name entities of two types).
``entity register TYPE ID --title T`` appends one ``entity.registered`` event
and refuses a key that is already registered. ``entity set TYPE ID --FIELD V
...`` appends one ``entity.state`` event that carries only the fields whose
value differs from the current one; when every given field already holds that
value it writes nothing and reports ``unchanged``. The current state is read
inside the write transaction. A state field outside ``ENTITY_STATE_FIELDS``
is refused by the schema, and nothing is written.

The store reserves both kinds for this module (``_ENTITY_WRITER_CAPABILITY``),
so ``ledger append`` and ``append-many`` refuse them. ``ledger entities``
renders the registry from ``v_entities``.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from ledger.schema import (
    ENTITY_REGISTERED_KIND,
    ENTITY_STATE_FIELDS,
    ENTITY_STATE_KIND,
    ENTITY_TYPES,
    validate_payload,
)
from ledger.store import Event, Ledger
from ledger.verbs import (
    EXIT_NOT_FOUND,
    EXIT_REFUSED,
    CliError,
    Context,
    Result,
    UsageError,
    Verb,
    actor_kwargs,
    event_dict,
    open_db,
)

ACTIONS = ("register", "set")
# The store appends entity.registered and entity.state only for a caller
# holding this (register_entity and set_entity_state).
_ENTITY_WRITER_CAPABILITY = object()


def entity_label(entity_type: str, entity_id: str) -> str:
    return f"{entity_type}/{entity_id}"


def current_entity(lg: Ledger, entity_type: str, entity_id: str) -> dict[str, Any] | None:
    """The entity's ``v_entities`` row with ``state`` decoded, or ``None``."""
    row = lg._conn.execute(
        "SELECT * FROM v_entities WHERE type = ? AND entity_id = ?",
        (entity_type, entity_id),
    ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["state"] = json.loads(data["state"])
    return data


def register_entity(
    lg: Ledger,
    entity_type: str,
    entity_id: str,
    title: str,
    *,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> Event:
    """Append the entity's ``entity.registered`` event.

    Refuses (``entity_exists``, exit 3) when the (type, id) key is already
    registered, whatever the title; the check and the append share one write
    transaction.
    """
    payload = {"type": entity_type, "id": entity_id, "title": title}
    validate_payload(ENTITY_REGISTERED_KIND, payload)
    with lg.transaction():
        existing = current_entity(lg, entity_type, entity_id)
        if existing is not None:
            raise CliError(
                f"entity already registered: {entity_label(entity_type, entity_id)} "
                f"(event {existing['registered_event_id']})",
                code="entity_exists",
                exit_code=EXIT_REFUSED,
            )
        return lg._append_entity_event(
            _ENTITY_WRITER_CAPABILITY,
            kind=ENTITY_REGISTERED_KIND,
            payload=payload,
            idempotency_key=f"entity:{entity_type}:{entity_id}:registered",
            actor=actor,
            harness=harness,
            model=model,
            session=session,
        )


def set_entity_state(
    lg: Ledger,
    entity_type: str,
    entity_id: str,
    state: Mapping[str, str],
    *,
    actor: str | None = None,
    harness: str | None = None,
    model: str | None = None,
    session: str | None = None,
) -> tuple[Event | None, dict[str, str], int]:
    """Append an ``entity.state`` event holding the fields that changed.

    Returns ``(event, changed, revision)``. ``changed`` maps each field whose
    value differs from the current one to its new value; the event carries
    exactly those. When nothing changed, nothing is written and the result is
    ``(None, {}, revision)`` with the entity's current revision. The schema
    check (unknown fields, an empty map, a bad value) runs before anything is
    read or written.
    """
    validate_payload(
        ENTITY_STATE_KIND, {"type": entity_type, "id": entity_id, "state": dict(state)}
    )
    with lg.transaction():
        current = current_entity(lg, entity_type, entity_id)
        if current is None:
            raise CliError(
                f"entity not registered: {entity_label(entity_type, entity_id)}",
                code="not_found",
                exit_code=EXIT_NOT_FOUND,
            )
        changed = {
            name: value for name, value in state.items() if current["state"].get(name) != value
        }
        if not changed:
            return None, {}, int(current["revision"])
        event = lg._append_entity_event(
            _ENTITY_WRITER_CAPABILITY,
            kind=ENTITY_STATE_KIND,
            payload={"type": entity_type, "id": entity_id, "state": changed},
            idempotency_key=(
                f"entity:{entity_type}:{entity_id}:state:after:{current['revision']}"
            ),
            actor=actor,
            harness=harness,
            model=model,
            session=session,
        )
    return event, changed, event.id


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def add_arguments(parser) -> None:
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("entity_type", metavar="TYPE", choices=ENTITY_TYPES,
                        help="one of " + ", ".join(ENTITY_TYPES))
    parser.add_argument("entity_id", metavar="ID", help="the entity's stable id")
    parser.add_argument("--title", help="register: the entity's title (one line)")
    for name in ENTITY_STATE_FIELDS:
        parser.add_argument(_flag(name), dest=name, help=f"set: the {name} value")


def _given_state(args) -> dict[str, str]:
    return {
        name: getattr(args, name)
        for name in ENTITY_STATE_FIELDS
        if getattr(args, name) is not None
    }


def _run_register(args, ctx: Context) -> Result:
    given = _given_state(args)
    if given:
        flags = ", ".join(_flag(name) for name in given)
        raise UsageError(f"entity register does not take {flags}; use entity set")
    if args.title is None:
        raise UsageError("entity register needs --title")
    lg = open_db(ctx, write=True)
    try:
        event = register_entity(
            lg, args.entity_type, args.entity_id, args.title, **actor_kwargs(ctx)
        )
    finally:
        lg.close()
    label = entity_label(args.entity_type, args.entity_id)
    return Result(
        data={"type": args.entity_type, "id": args.entity_id, "event": event_dict(event)},
        text=f"entity\t{label}\tregistered\tevent={event.id}",
    )


def _run_set(args, ctx: Context) -> Result:
    if args.title is not None:
        raise UsageError("entity set does not take --title")
    given = _given_state(args)
    if not given:
        flags = ", ".join(_flag(name) for name in ENTITY_STATE_FIELDS)
        raise UsageError(f"entity set needs at least one of {flags}")
    lg = open_db(ctx, write=True)
    try:
        event, changed, revision = set_entity_state(
            lg, args.entity_type, args.entity_id, given, **actor_kwargs(ctx)
        )
    finally:
        lg.close()
    label = entity_label(args.entity_type, args.entity_id)
    if event is None:
        text = f"entity\t{label}\tunchanged"
    else:
        text = f"entity\t{label}\tevent={event.id}\t" + ",".join(changed)
    return Result(
        data={
            "type": args.entity_type,
            "id": args.entity_id,
            "unchanged": event is None,
            "changed": list(changed),
            "revision": revision,
            "event": None if event is None else event_dict(event),
        },
        text=text,
    )


def run(args, ctx: Context) -> Result:
    if args.action == "register":
        return _run_register(args, ctx)
    return _run_set(args, ctx)


VERB = Verb(
    name="entity",
    help="register an entity (automation, project, machine, model) or change its state",
    add_arguments=add_arguments,
    run=run,
    writes=True,
)
