"""Render the entity registry: ``ledger entities [--type T] [--format md|json]``.

Reads ``v_entities`` (migration 0009) and the ledger revision, the id of the
newest event in the store (0 for an empty store), in one read transaction, so
the rendered view and the revision it names come from the same snapshot.

``--format json`` prints the contract a drift check reads: a list of
``{type, id, title, state, revision}`` objects in that key order, sorted by
type (in ``ENTITY_TYPES`` order) and then id, with ``state`` keys in
``ENTITY_STATE_FIELDS`` order and ``revision`` the id of the entity's newest
event. ``--format md`` (the default) prints a line naming the ledger revision,
then one table per type: every type, or the one ``--type`` names. With
``--json`` the envelope's ``data`` is that same list, and ``--format md`` is
refused. A read verb: it never appends, so it needs no ``LEDGER_HARNESS``.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from ledger.schema import ENTITY_STATE_FIELDS, ENTITY_TYPES
from ledger.store import Ledger
from ledger.verbs import Context, Result, UsageError, Verb, open_db

FORMATS = ("md", "json")
ENTITY_KEYS = ("type", "id", "title", "state", "revision")


def _entity(row: Mapping[str, Any]) -> dict[str, Any]:
    stored = json.loads(row["state"])
    state = {name: stored[name] for name in ENTITY_STATE_FIELDS if name in stored}
    state.update({name: stored[name] for name in sorted(stored) if name not in state})
    return {
        "type": row["type"],
        "id": row["entity_id"],
        "title": row["title"],
        "state": state,
        "revision": row["revision"],
    }


def _order(entity: Mapping[str, Any]) -> tuple[int, str]:
    kind = entity["type"]
    rank = ENTITY_TYPES.index(kind) if kind in ENTITY_TYPES else len(ENTITY_TYPES)
    return rank, entity["id"]


def read_entities(lg: Ledger, entity_type: str | None = None) -> tuple[int, list[dict[str, Any]]]:
    """``(ledger_revision, entities)`` from one read snapshot."""
    conn = lg._conn
    conn.execute("BEGIN")
    try:
        revision = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
        rows = [dict(row) for row in conn.execute("SELECT * FROM v_entities")]
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    entities = sorted((_entity(row) for row in rows), key=_order)
    if entity_type is not None:
        entities = [entity for entity in entities if entity["type"] == entity_type]
    return revision, entities


def render_json(entities: list[Mapping[str, Any]]) -> str:
    return json.dumps(list(entities), indent=2, ensure_ascii=False)


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|")


def render_md(revision: int, entities: list[Mapping[str, Any]], types: tuple[str, ...]) -> str:
    lines = [
        f"Rendered by `ledger entities --format md` from ledger revision {revision} "
        "(the id of the newest event). Change the ledger, not this file."
    ]
    for kind in types:
        rows = [entity for entity in entities if entity["type"] == kind]
        fields = [name for name in ENTITY_STATE_FIELDS if any(name in e["state"] for e in rows)]
        extra = sorted({name for e in rows for name in e["state"]} - set(fields))
        columns = ["id", "title", *fields, *extra, "revision"]
        lines += ["", f"## {kind}", "", "| " + " | ".join(columns) + " |",
                  "|" + "---|" * len(columns)]
        for entity in rows:
            cells = [entity["id"], entity["title"],
                     *(entity["state"].get(name, "") for name in (*fields, *extra)),
                     entity["revision"]]
            lines.append("| " + " | ".join(_cell(cell) for cell in cells) + " |")
    return "\n".join(lines)


def add_arguments(parser) -> None:
    parser.add_argument("--type", dest="entity_type", choices=ENTITY_TYPES,
                        help="only this type")
    parser.add_argument("--format", dest="output_format", choices=FORMATS,
                        help="md (default): one table per type; json: the drift-check list")


def run(args, ctx: Context) -> Result:
    output_format = args.output_format or "md"
    if ctx.json_mode and args.output_format == "md":
        raise UsageError("--json prints the entity list; drop --json for --format md")
    lg = open_db(ctx, write=False)
    try:
        revision, entities = read_entities(lg, args.entity_type)
    finally:
        lg.close()
    if output_format == "json":
        text = render_json(entities)
    else:
        types = ENTITY_TYPES if args.entity_type is None else (args.entity_type,)
        text = render_md(revision, entities, types)
    return Result(data=entities, text=text)


VERB = Verb(
    name="entities",
    help="render the entity registry as markdown tables or the drift-check JSON list",
    add_arguments=add_arguments,
    run=run,
)
