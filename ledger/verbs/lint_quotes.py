"""Lint quoted spans in elections against their referenced utterances.

Every ``decision.elected`` event must carry a non-empty ``quotes`` list, and
every quoted span must be a verbatim substring of the text in the utterance
event its ``quote_ref`` points to. ``quote_ref`` is resolved for every
election, quotes or not: a missing event, an event that is not
``utterance.captured``, an utterance whose body was omitted (it carries
``omitted_bytes``; its text is a pointer, not the person's words), or an
election with zero quotes is a failure. The
comparison is byte-exact: no whitespace stripping, no collapsing, and no
case folding. A captured prompt is audit evidence of submitted text, not
proof of authorship.
"""
from __future__ import annotations

import json
from typing import Any

from ledger.verbs import (
    EXIT_REFUSED,
    CliError,
    Context,
    Result,
    Verb,
    open_db,
)


def add_arguments(parser) -> None:
    parser.add_argument("--item", help="limit lint to elections for this item")


def _fetch_utterance(lg, quote_ref: int) -> tuple[str | None, bool]:
    """Return the utterance's text and whether its body was omitted."""
    row = lg._conn.execute(
        "SELECT kind, payload FROM events WHERE id = ?", (quote_ref,)
    ).fetchone()
    if row is None or row["kind"] != "utterance.captured":
        return None, False
    payload = json.loads(row["payload"])
    return payload.get("text"), "omitted_bytes" in payload


def _lint_elections(lg, item_id: str | None) -> tuple[list[dict[str, Any]], int]:
    sql = "SELECT * FROM events WHERE kind = 'decision.elected'"
    params: list[Any] = []
    if item_id is not None:
        sql += " AND item_id = ?"
        params.append(item_id)
    sql += " ORDER BY id ASC"

    failures: list[dict[str, Any]] = []
    checked = 0
    for row in lg._conn.execute(sql, params):
        checked += 1
        payload = json.loads(row["payload"])
        quotes = payload.get("quotes") or []
        quote_ref = payload.get("quote_ref")
        text, pointer = _fetch_utterance(lg, quote_ref)
        if pointer:
            failures.append(
                {
                    "event_id": row["id"],
                    "item_id": row["item_id"],
                    "quote_ref": quote_ref,
                    "quote": None,
                    "reason": "quote_ref is an omitted-body pointer, not the person's words",
                }
            )
            continue
        if text is None:
            failures.append(
                {
                    "event_id": row["id"],
                    "item_id": row["item_id"],
                    "quote_ref": quote_ref,
                    "quote": None,
                    "reason": "unresolvable quote_ref",
                }
            )
        if not quotes:
            failures.append(
                {
                    "event_id": row["id"],
                    "item_id": row["item_id"],
                    "quote_ref": quote_ref,
                    "quote": None,
                    "reason": "election has no quotes",
                }
            )
        if text is None or not quotes:
            continue
        for quote in quotes:
            if quote not in text:
                failures.append(
                    {
                        "event_id": row["id"],
                        "item_id": row["item_id"],
                        "quote_ref": quote_ref,
                        "quote": quote,
                        "reason": "quote is not a verbatim substring",
                    }
                )
    return failures, checked


def run(args, ctx: Context) -> Result:
    lg = open_db(ctx, write=False)
    try:
        failures, checked = _lint_elections(lg, args.item)
    finally:
        lg.close()
    if failures:
        detail = "; ".join(
            f"event={f['event_id']} quote={f['quote']!r}: {f['reason']}"
            for f in failures
        )
        raise CliError(
            f"quote lint failed: {len(failures)} mismatch(es): {detail}",
            code="quote_mismatch",
            exit_code=EXIT_REFUSED,
        )
    return Result(
        data={"checked": checked},
        text=f"ok quotes checked={checked}",
    )


VERB = Verb(
    name="lint-quotes",
    help="check election quotes against their referenced utterances",
    add_arguments=add_arguments,
    run=run,
)
