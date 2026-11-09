"""Trust grants as election records: ``ledger grants [--check GRANT_ID]``.

A grant record is a ``decision.elected`` event whose ``question_id`` starts
with ``grant:``, ``countersign:`` or ``rung:``. The ``question_id`` is the
grant id, global across items, and the prefix (without its colon) is the
grant's kind. An election with any other ``question_id``, or none, is not a
grant, and this verb never shows it.

Standing. For one grant id the newest election (the highest event id)
decides. The grant is STANDING only when that election's ``chosen`` is
exactly ``granted`` and it is verified. Any other ``chosen`` (the canonical
label is ``withdrawn``) makes it WITHDRAWN. A newest ``granted`` election
that is not verified is UNVERIFIED, never standing. The asymmetry is
deliberate: a withdrawal only removes authority, so it takes effect without
verification and fails closed; a grant adds authority, so it must verify.

Verified. The election answers an ask: its ``asked_event_id`` names a
``decision.asked`` whose ``question_id`` is the same grant id (else the
reason is ``grant_unasked``), and ``ledger.answers.bound_answer`` on that
ask returns this election: the owner's picker answer, captured by the
harness, bound to the ask, with every field derived from it (else the
reason is ``bound_answer``'s, or ``not_first_answer`` when the ask's first
election is another one). A grant is raised as an ask with the grant id as
its ``question_id`` and recorded with ``ledger pick --ask``.
``unverified_reason`` stays as the quote-only audit check: it reports
whether the quotes are verbatim spans of a reply-class utterance, and no
standing reads it.

Grantor and date. ``grantor`` is the operator stamped on the quoted
utterance (the text before the first ``/`` of its actor, as ``v_events``
computes it), ``-`` when it names none. ``date`` is the UTC calendar date
the quoted utterance was recorded (the owner's words), not the date of the
election event; ``-`` when the quote does not resolve to an utterance.

Output. ``ledger grants`` prints one header line naming the ledger revision
(the id of the newest event) and those two definitions, then a markdown table
with one row per grant id, sorted by grant id. ``--json`` prints the same rows.
Nothing of the run itself (time, host, process) is printed, so two runs at one
revision print the same bytes. ``ledger grants --check GRANT_ID`` exits 0 when
the grant is STANDING and otherwise refuses with exit 3 and one of the codes
``grant_withdrawn``, ``grant_unverified`` or ``grant_unknown``.

A read verb: it never appends, so it needs no ``LEDGER_HARNESS``. It reports;
no gate reads it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from ledger.answers import bound_answer
from ledger.decisions import REPLY_PROVENANCE
from ledger.store import Ledger
from ledger.verbs import EXIT_REFUSED, CliError, Context, Result, Verb, open_db
from ledger.verbs.elect import validate_quote_provenance

GRANT_PREFIXES = ("grant:", "countersign:", "rung:")
GRANTED = "granted"
WITHDRAWN_LABEL = "withdrawn"
STANDING = "STANDING"
WITHDRAWN = "WITHDRAWN"
UNVERIFIED = "UNVERIFIED"
# Why a granted election stands on no ask carrying its grant id.
GRANT_UNASKED = "grant_unasked"
# Why a granted election is not the ask's first election.
NOT_FIRST_ANSWER = "not_first_answer"
_MAX_EVENT_ID = 2**63 - 1  # SQLite integer ceiling
# The standing grant for review rounds past the cap. A review-round grant
# line that names a standing grant points at this record.
REVIEW_ROUND_GRANT_ID = "grant:review-round-past-cap"
NONE_CELL = "-"
COLUMNS = (
    ("grant_id", "grant id"),
    ("kind", "kind"),
    ("chosen", "chosen"),
    ("scope", "scope"),
    ("grantor", "grantor"),
    ("date", "date"),
    ("quote_ref", "quote ref"),
    ("status", "status"),
    ("deciding_event_id", "deciding event"),
)
REFUSAL_CODES = {
    WITHDRAWN: "grant_withdrawn",
    UNVERIFIED: "grant_unverified",
}


@dataclass(frozen=True)
class Grant:
    grant_id: str
    kind: str
    chosen: str
    scope: str
    grantor: str
    date: str
    quote_ref: Any
    status: str
    deciding_event_id: int
    # Why a newest ``granted`` election did not verify; None otherwise.
    reason: str | None = None

    def row(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key, _heading in COLUMNS}


def grant_kind(question_id: Any) -> str | None:
    """The grant kind a ``question_id`` names, or None when it is no grant id."""
    if not isinstance(question_id, str):
        return None
    for prefix in GRANT_PREFIXES:
        if question_id.startswith(prefix):
            return prefix[:-1]
    return None


def _utterance(conn, quote_ref: Any) -> Mapping[str, Any] | None:
    """The quoted event when it is an ``utterance.captured``, else None."""
    if type(quote_ref) is not int:
        return None
    row = conn.execute(
        "SELECT kind, provenance_class, recorded_at, operator FROM v_events WHERE id = ?",
        (quote_ref,),
    ).fetchone()
    if row is None or row["kind"] != "utterance.captured":
        return None
    return row


def unverified_reason(lg: Ledger, payload: Mapping[str, Any]) -> str | None:
    """The quote-only audit check: None when the quotes are verbatim spans of a
    reply-class utterance; otherwise why not. No standing reads it."""
    quote_ref = payload.get("quote_ref")
    quotes = payload.get("quotes")
    try:
        validate_quote_provenance(lg, quote_ref, list(quotes) if isinstance(quotes, list) else [])
    except CliError as exc:
        return exc.code
    utterance = _utterance(lg._conn, quote_ref)
    if utterance is None:
        return "unresolvable_ref"
    if utterance["provenance_class"] not in REPLY_PROVENANCE:
        return "not_a_reply"
    return None


def standing_reason(
    lg: Ledger, grant_id: str, event_id: int, payload: Mapping[str, Any]
) -> str | None:
    """None when a ``granted`` election is the bound answer to an ask that
    carries its grant id; otherwise why it is not."""
    asked = payload.get("asked_event_id")
    if type(asked) is not int or not 1 <= asked <= _MAX_EVENT_ID:
        return GRANT_UNASKED
    row = lg._conn.execute(
        "SELECT kind, payload FROM events WHERE id = ?", (asked,)
    ).fetchone()
    if row is None or row["kind"] != "decision.asked":
        return GRANT_UNASKED
    if json.loads(row["payload"]).get("question_id") != grant_id:
        return GRANT_UNASKED
    answer = bound_answer(lg, asked)
    if answer.reason is not None:
        return answer.reason
    if answer.election.id != event_id:
        return NOT_FIRST_ANSWER
    return None


def _grant(lg: Ledger, grant_id: str, event_id: int, payload: Mapping[str, Any]) -> Grant:
    quote_ref = payload.get("quote_ref")
    utterance = _utterance(lg._conn, quote_ref)
    reason = None
    if payload.get("chosen") != GRANTED:
        status = WITHDRAWN
    else:
        reason = standing_reason(lg, grant_id, event_id, payload)
        status = STANDING if reason is None else UNVERIFIED
    grantor = NONE_CELL
    date = NONE_CELL
    if utterance is not None:
        grantor = utterance["operator"] or NONE_CELL
        date = str(utterance["recorded_at"])[:10]
    return Grant(
        grant_id=grant_id,
        kind=grant_kind(grant_id) or NONE_CELL,
        chosen=str(payload.get("chosen", "")),
        scope=str(payload.get("scope", "")),
        grantor=grantor,
        date=date,
        quote_ref=quote_ref,
        status=status,
        deciding_event_id=event_id,
        reason=reason,
    )


def read_grants(lg: Ledger) -> tuple[int, list[Grant]]:
    """``(ledger_revision, grants)`` from one read snapshot, sorted by grant id."""
    conn = lg._conn
    conn.execute("BEGIN")
    try:
        revision = int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0])
        newest: dict[str, tuple[int, dict[str, Any]]] = {}
        for row in conn.execute(
            "SELECT id, payload FROM events WHERE kind = 'decision.elected' ORDER BY id ASC"
        ):
            payload = json.loads(row["payload"])
            grant_id = payload.get("question_id")
            if grant_kind(grant_id) is None:
                continue
            # Ascending ids: a later election replaces an earlier one.
            newest[grant_id] = (int(row["id"]), payload)
        grants = [
            _grant(lg, grant_id, event_id, payload)
            for grant_id, (event_id, payload) in sorted(newest.items())
        ]
    finally:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
    return revision, grants


def find_grant(grants: list[Grant], grant_id: str) -> Grant | None:
    for grant in grants:
        if grant.grant_id == grant_id:
            return grant
    return None


def check_grant(grants: list[Grant], grant_id: str) -> Grant:
    """The grant when it is STANDING; otherwise a refusal naming why."""
    grant = find_grant(grants, grant_id)
    if grant is None:
        raise CliError(
            f"grant {grant_id!r} is not recorded: no election names it",
            code="grant_unknown",
            exit_code=EXIT_REFUSED,
        )
    if grant.status == STANDING:
        return grant
    if grant.status == WITHDRAWN:
        detail = f"the newest election (event {grant.deciding_event_id}) chose {grant.chosen!r}"
    else:
        detail = (
            f"the newest election (event {grant.deciding_event_id}) does not verify"
            f" ({grant.reason})"
        )
    raise CliError(
        f"grant {grant_id!r} is {grant.status}: {detail}",
        code=REFUSAL_CODES[grant.status],
        exit_code=EXIT_REFUSED,
    )


def _cell(value: Any) -> str:
    return " ".join(str(value).split()).replace("|", "\\|")


def render_md(revision: int, grants: list[Grant]) -> str:
    lines = [
        f"Rendered by `ledger grants` from ledger revision {revision} (the id of the newest"
        " event); the newest election per grant id decides; date is the UTC date the quoted"
        " utterance (the owner's words) was recorded, not the election's; grantor is the"
        " operator stamped on that utterance. Change the ledger, not this table.",
        "",
        "| " + " | ".join(heading for _key, heading in COLUMNS) + " |",
        "|" + "---|" * len(COLUMNS),
    ]
    for grant in grants:
        row = grant.row()
        lines.append("| " + " | ".join(_cell(row[key]) for key, _heading in COLUMNS) + " |")
    return "\n".join(lines)


def add_arguments(parser) -> None:
    parser.add_argument(
        "--check",
        metavar="GRANT_ID",
        help="exit 0 when this grant is STANDING; refuse with exit 3 otherwise",
    )


def run(args, ctx: Context) -> Result:
    lg = open_db(ctx, write=False)
    try:
        revision, grants = read_grants(lg)
    finally:
        lg.close()
    if args.check is not None:
        grant = check_grant(grants, args.check)
        return Result(
            data=grant.row(),
            text=(
                f"{STANDING}\t{grant.grant_id}\tevent={grant.deciding_event_id}"
                f"\tdate={grant.date}"
            ),
        )
    return Result(data=[grant.row() for grant in grants], text=render_md(revision, grants))


VERB = Verb(
    name="grants",
    help="render trust grants from their election records, or check one is standing",
    add_arguments=add_arguments,
    run=run,
)
