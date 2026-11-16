"""Decision records: the standing guard and the read side.

A decision is one ``decision.recorded`` event. Its standing moves only by a
later ``decision.standing`` event that names it, and what came of it is a
``decision.result`` event; nothing is rewritten.

The guard runs inside the store's append transaction, so every writer (the
library, ``ledger append``, batch appends) meets the same rules:

* lean -> settled needs a source event: an id already in this ledger, of a
  kind that records something outside the decision itself (an outside
  event such as a merge or reply, an election, a captured utterance, or a
  loop merge or close). Elapsed time or silence never settles a decision.
  An utterance counts only when its provenance class says a person wrote it
  or answered a picker; generated or drafted text never settles anything.
* the source event sits on the decision's own item, or on an item the
  decision is tagged with as a ticket. The tag is the declared link that lets
  a decision span tickets; an event on any other item is refused.
* superseded needs ``superseded_by``, the decision that replaces it, and is
  final: a superseded decision takes no further standing.
* a role's position is recorded with ``relayed_by``, the person who relayed
  it, so it is never stored as that person's own words.

The guard reads the database through the connection it is given and never
imports the store.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from .schema import ValidationError

# Kinds that can settle a decision. loop.done counts only for a merge.
SETTLING_KINDS = frozenset({
    "outside.recorded",
    "decision.elected",
    "utterance.captured",
    "loop.closed",
})
_GUARDED = frozenset({"decision.recorded", "decision.standing", "decision.result"})
# Provenance classes of an utterance a person wrote: a submitted prompt or a
# question-picker answer. Anything else (generated, drafted, imported,
# restored, or unmarked) cannot settle a decision.
REPLY_PROVENANCE = frozenset({"submitted-prompt", "tool-answer"})


def _event(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, kind, item_id, payload, provenance_class FROM events WHERE id = ?",
        (event_id,),
    ).fetchone()


def is_settling(row: Mapping[str, Any]) -> bool:
    kind = row["kind"]
    if kind == "utterance.captured":
        return row["provenance_class"] in REPLY_PROVENANCE
    if kind in SETTLING_KINDS:
        return True
    if kind == "loop.done":
        return json.loads(row["payload"]).get("operation") == "merge"
    return False


def _require_source(
    conn: sqlite3.Connection,
    source_event_id: Any,
    item_id: str | None,
    tags: list[Mapping[str, Any]],
) -> None:
    if source_event_id is None:
        raise ValidationError("settled-needs-source-event")
    row = _event(conn, source_event_id)
    if row is None:
        raise ValidationError(f"unknown-source-event:{source_event_id}")
    if not is_settling(row):
        if row["kind"] == "utterance.captured":
            raise ValidationError(
                f"source-utterance-not-a-reply:{row['provenance_class'] or 'unmarked'}"
            )
        raise ValidationError(f"source-event-cannot-settle:{row['kind']}")
    source_item = row["item_id"]
    linked = source_item == item_id or {"type": "ticket", "id": source_item} in tags
    if source_item is None or not linked:
        raise ValidationError(f"source-event-other-item:{source_event_id}")


def _decision(conn: sqlite3.Connection, event_id: int, item_id: str | None) -> sqlite3.Row:
    row = _event(conn, event_id)
    if row is None or row["kind"] != "decision.recorded":
        raise ValidationError(f"unknown-decision:{event_id}")
    if item_id is not None and row["item_id"] != item_id:
        raise ValidationError(f"decision-on-other-item:{event_id}")
    return row


def current_standing(conn: sqlite3.Connection, decision_event_id: int) -> str | None:
    row = conn.execute(
        "SELECT standing FROM v_decision_records WHERE decision_event_id = ?",
        (decision_event_id,),
    ).fetchone()
    return None if row is None else row["standing"]


def check_event(
    conn: sqlite3.Connection,
    kind: str,
    payload: Mapping[str, Any],
    item_id: str | None,
) -> None:
    """Refuse a decision event that breaks the standing rules."""
    if kind not in _GUARDED:
        return
    if kind == "decision.recorded":
        if payload["who_kind"] == "role" and "relayed_by" not in payload:
            raise ValidationError("role-needs-relayed-by")
        if payload["standing"] == "settled":
            _require_source(conn, payload.get("source_event_id"), item_id, payload["tags"])
        elif "source_event_id" in payload:
            if _event(conn, payload["source_event_id"]) is None:
                raise ValidationError(f"unknown-source-event:{payload['source_event_id']}")
        return
    decision_id = payload["decision_event_id"]
    decision = _decision(conn, decision_id, item_id)
    if kind == "decision.result":
        return
    standing = payload["standing"]
    if current_standing(conn, decision_id) == "superseded":
        raise ValidationError(f"decision-superseded:{decision_id}")
    if standing != "superseded" and "superseded_by" in payload:
        raise ValidationError("superseded-by-needs-superseded")
    if standing == "settled":
        _require_source(
            conn,
            payload.get("source_event_id"),
            decision["item_id"],
            json.loads(decision["payload"])["tags"],
        )
    elif standing == "superseded":
        replacement = payload.get("superseded_by")
        if replacement is None:
            raise ValidationError("superseded-needs-link")
        if replacement == decision_id:
            raise ValidationError("superseded-by-itself")
        _decision(conn, replacement, None)
        if "source_event_id" in payload and _event(conn, payload["source_event_id"]) is None:
            raise ValidationError(f"unknown-source-event:{payload['source_event_id']}")
    elif "source_event_id" in payload and _event(conn, payload["source_event_id"]) is None:
        raise ValidationError(f"unknown-source-event:{payload['source_event_id']}")


def _record_row(row: Mapping[str, Any]) -> dict[str, Any]:
    record = dict(row)
    record["tags"] = json.loads(record["tags"]) if record["tags"] else []
    return record


def decision_records(
    conn: sqlite3.Connection,
    *,
    item_id: str | None = None,
    tag: tuple[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Decision records with their current standing, oldest first.

    ``tag`` is ``(type, id)``, for example ``("initiative", "x")``, and finds
    a decision from any item that carries that tag.
    """
    sql = "SELECT * FROM v_decision_records"
    params: list[Any] = []
    if item_id is not None:
        sql += " WHERE item_id = ?"
        params.append(item_id)
    sql += " ORDER BY decision_event_id ASC"
    rows = [_record_row(row) for row in conn.execute(sql, params)]
    if tag is not None:
        wanted = {"type": tag[0], "id": tag[1]}
        rows = [row for row in rows if wanted in row["tags"]]
    return rows


__all__ = [
    "REPLY_PROVENANCE",
    "SETTLING_KINDS",
    "check_event",
    "current_standing",
    "decision_records",
    "is_settling",
]
