"""Bind an owner's answer to the ask it answers.

A gate opens on an election only through ``bound_answer(lg, K)``. It reads
ask ``K`` (a ``decision.asked`` event), the first election whose
``asked_event_id`` is ``K`` and that election's quoted utterance, and returns
the election, or the reason it does not count. ``check_answer`` is the one
validator: ``ledger pick`` runs it before it writes and ``bound_answer`` runs
it on the stored election, so a refused pick writes nothing and a raw append
that skipped the verb is refused at read time.

The answer that counts is a picker capture written by ``ledger capture
--kind picker`` (channel ``hook:picker``, which only that verb can write):
the harness recorded what it displayed, as ``picker.display`` and its
fingerprint ``picker.display_sha256``, and which option the owner chose, as
``picker.chosen_position``. The display must be exactly the one
``ledger ask-display K`` renders from the ask, and the election must record
that fingerprint and position and take every other field from the ask at that
position.

Refusal reasons, checked in this order (the first failure is the reason):

==========================  =====================================================
``ask_not_found``           K is not a ``decision.asked`` event
``no_election``             no election names K (``bound_answer`` only)
``answer_other_item``       R1: the election is on another item than K
``typed_answer``            R2: the quote is a ``hook:prompt`` capture (typed text)
``pre_binding``             R2: the quote is no picker capture (every utterance
                            written before this rule, or none at all)
``answer_before_ask``       R3: the capture was recorded before K
``answer_unbound``          R4: the capture carries no ``[ask K]`` tag
``answer_other_ask``        R4: the capture's tag names another ask
``capture_conflict``        R4a: another capture for K shows another display,
                            chose another position, or carries a note
``display_fingerprint_bad`` R5: the capture's fingerprint is not its display's
``ask_not_pickable``        R6: K cannot be shown in a picker (see below)
``display_differs``         R6: the capture's display is not ``ask-display K``
``picker_multi_question``   R7: the picker held more than one question
``picker_typed``            R7: the owner typed an answer instead of choosing
``picker_ambiguous``        R7: two options carry the chosen label
``picker_unanswered``       R7: the answers map holds no answer to the question
``picker_annotated``        R7: the answer carries a nonempty owner note
(the quote rule's code)     R8: ``validate_quote_provenance`` refused the quotes
``choice_mismatch``         R9: the election's fingerprint or position is not
                            the capture's (or is missing)
``election_differs``        R9: a field of the election is not the one derived
                            from K at that position
==========================  =====================================================

An ask is pickable when it has a scope, two to four options, no two options
with one label, an ``ask K`` header of at most 12 characters, and no control
character (C0, DEL or C1) in any text the picker displays: the question id,
the scope, an option label or description, or the question (where a newline
alone is allowed). The write path refuses such an ask; one stored before that
rule reads ``ask_not_pickable`` naming the field, so no pick, election or
grant binds through it.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping, NamedTuple

from .schema import PICKER_CHANNEL, PROMPT_CHANNEL, display_control_character
from .store import Event, Ledger, _event_from_row, canonical_dumps
from .verbs import CliError
from .verbs.elect import validate_quote_provenance

# The tag that binds a picker question to its ask: "[ask K]" at the very
# start of the first question's text, K a positive integer with no sign and
# no leading zero. Anything else (another case, a space inside, a tag later
# in the text) binds nothing.
ASK_TAG = re.compile(r"\[ask ([1-9][0-9]*)\]")
_MAX_EVENT_ID = 2**63 - 1  # SQLite integer ceiling
PICKER_MIN_OPTIONS = 2
PICKER_MAX_OPTIONS = 4
PICKER_HEADER_MAX = 12
UNANSWERED = "unanswered"

ASK_NOT_FOUND = "ask_not_found"
NO_ELECTION = "no_election"
ANSWER_OTHER_ITEM = "answer_other_item"
TYPED_ANSWER = "typed_answer"
PRE_BINDING = "pre_binding"
ANSWER_BEFORE_ASK = "answer_before_ask"
ANSWER_UNBOUND = "answer_unbound"
ANSWER_OTHER_ASK = "answer_other_ask"
CAPTURE_CONFLICT = "capture_conflict"
DISPLAY_FINGERPRINT_BAD = "display_fingerprint_bad"
ASK_NOT_PICKABLE = "ask_not_pickable"
DISPLAY_DIFFERS = "display_differs"
PICKER_MULTI_QUESTION = "picker_multi_question"
PICKER_TYPED = "picker_typed"
PICKER_AMBIGUOUS = "picker_ambiguous"
PICKER_UNANSWERED = "picker_unanswered"
PICKER_ANNOTATED = "picker_annotated"
CHOICE_MISMATCH = "choice_mismatch"
ELECTION_DIFFERS = "election_differs"

# The election fields R9 derives from the ask and the capture. The answer
# itself (display_sha256 and chosen_position) is compared first.
DERIVED_FIELDS = (
    "question",
    "chosen",
    "chosen_description",
    "rejected",
    "scope",
    "quotes",
    "question_id",
    "quote_ref",
    "asked_event_id",
)


class AskNotPickable(ValueError):
    """The ask cannot be shown as a one-question picker."""


class BoundAnswer(NamedTuple):
    """``election`` when the ask's answer counts, else ``reason``."""

    election: Event | None
    reason: str | None


def fingerprint(display: Any) -> str:
    """SHA-256 hex of the ledger's canonical JSON of ``display``."""
    return hashlib.sha256(canonical_dumps(display).encode("utf-8")).hexdigest()


def ask_tag(text: Any) -> int | None:
    """The ask id an ``[ask K]`` tag at the start of ``text`` names, or None."""
    if type(text) is not str:
        return None
    match = ASK_TAG.match(text)
    if match is None:
        return None
    try:
        value = int(match.group(1))
    except ValueError:  # more digits than Python converts
        return None
    return value if value <= _MAX_EVENT_ID else None


def picker_text(question: str, answer: str) -> str:
    """One answered question as a picker capture's text records it."""
    return f"{question}\nchosen: {answer}"


def unanswered_text(question: str) -> str:
    """One question the answers map leaves out. Never carries ``chosen: ``."""
    return f"{question}\n{UNANSWERED}"


def option_description(option: Mapping[str, Any]) -> str:
    """An option's description; its label when it has none (or an empty one)."""
    description = option.get("description")
    if isinstance(description, str) and description != "":
        return description
    return option["label"]


def _require_displayable(asked_event_id: int, ask: Mapping[str, Any]) -> None:
    """Fail closed on the text the picker will show: every displayed field
    must be a string, and none may hold a control character (a newline alone
    is allowed in the question). The reason names the field and the code
    point, never the character."""
    question = ask.get("question")
    if not isinstance(question, str) or question == "":
        raise AskNotPickable(f"ask {asked_event_id} has no question text")
    question_id = ask.get("question_id")
    if question_id is not None and not isinstance(question_id, str):
        raise AskNotPickable(f"ask {asked_event_id} has a question id that is not text")
    options = ask.get("options")
    for index, option in enumerate(options if isinstance(options, list) else []):
        label = option.get("label") if isinstance(option, dict) else None
        description = option.get("description") if isinstance(option, dict) else None
        if not isinstance(label, str) or label == "":
            raise AskNotPickable(f"ask {asked_event_id} option {index + 1} has no label")
        if description is not None and not isinstance(description, str):
            raise AskNotPickable(
                f"ask {asked_event_id} option {index + 1} has a description that is not text"
            )
    hit = display_control_character("decision.asked", ask)
    if hit is not None:
        raise AskNotPickable(
            f"ask {asked_event_id} holds a control character ({hit[1]}) in {hit[0]},"
            " which the picker would display: write a new ask without it"
        )


def render_ask_display(asked_event_id: int, ask: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ``questions`` array a seat sends as its AskUserQuestion input for K.

    Exactly one question, built only from the ask; raises ``AskNotPickable``
    when the ask cannot be shown in a picker. The question text names the
    scope and, when the ask has one, its ``question_id``: an election takes
    that id from the ask, and ``ledger grants`` reads it to decide which grant
    the answer stands for, so the owner sees it and the fingerprint covers it.
    """
    header = f"ask {asked_event_id}"
    _require_displayable(asked_event_id, ask)
    scope = ask.get("scope")
    if not isinstance(scope, str) or scope == "":
        raise AskNotPickable(f"ask {asked_event_id} has no scope")
    options = ask.get("options")
    if not isinstance(options, list) or not (
        PICKER_MIN_OPTIONS <= len(options) <= PICKER_MAX_OPTIONS
    ):
        count = len(options) if isinstance(options, list) else 0
        raise AskNotPickable(
            f"ask {asked_event_id} has {count} options; a picker shows"
            f" {PICKER_MIN_OPTIONS} to {PICKER_MAX_OPTIONS}"
        )
    labels = [option["label"] for option in options]
    if len(set(labels)) != len(labels):
        raise AskNotPickable(
            f"ask {asked_event_id} repeats an option label, so no position could be recovered"
        )
    if len(header) > PICKER_HEADER_MAX:
        raise AskNotPickable(
            f"the header {header!r} is longer than {PICKER_HEADER_MAX} characters"
        )
    shown = f"scope: {scope}"
    # The same condition derive_election copies the question id under.
    if ask.get("question_id") is not None:
        shown += f"; question id: {ask['question_id']}"
    return [
        {
            "question": f"[ask {asked_event_id}] {ask['question']} ({shown})",
            "header": header,
            "options": [
                {"label": option["label"], "description": option_description(option)}
                for option in options
            ],
            "multiSelect": False,
        }
    ]


def has_nonempty(value: Any) -> bool:
    """True when any value at any level is content: a nonempty string, a
    number or a boolean. Empty strings, null and empty containers are not."""
    if value is None:
        return False
    if isinstance(value, str):
        return value != ""
    if isinstance(value, dict):
        return any(has_nonempty(item) for item in value.values())
    if isinstance(value, list):
        return any(has_nonempty(item) for item in value)
    return True


def _event(conn, event_id: Any) -> Event | None:
    if type(event_id) is not int or not 1 <= event_id <= _MAX_EVENT_ID:
        return None
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return None if row is None else _event_from_row(row)


def _picker(event: Event) -> dict[str, Any]:
    picker = event.payload.get("picker")
    return picker if isinstance(picker, dict) else {}


def picker_captures(conn, asked_event_id: int) -> list[Event]:
    """Every ``hook:picker`` capture tagged for the ask, oldest first."""
    rows = conn.execute(
        "SELECT * FROM events WHERE kind = 'utterance.captured' AND channel = ?"
        " AND json_extract(payload, '$.picker.asked_event_id') = ? ORDER BY id ASC",
        (PICKER_CHANNEL, asked_event_id),
    )
    captures = []
    for row in rows:
        event = _event_from_row(row)
        tag = _picker(event).get("asked_event_id")
        if type(tag) is int and tag == asked_event_id:
            captures.append(event)
    return captures


def first_election(conn, asked_event_id: int) -> Event | None:
    """The first ``decision.elected`` whose ``asked_event_id`` is the ask."""
    rows = conn.execute(
        "SELECT * FROM events WHERE kind = 'decision.elected'"
        " AND json_extract(payload, '$.asked_event_id') = ? ORDER BY id ASC",
        (asked_event_id,),
    )
    for row in rows:
        event = _event_from_row(row)
        linked = event.payload.get("asked_event_id")
        if type(linked) is int and linked == asked_event_id:
            return event
    return None


def derive_election(
    asked_event_id: int, ask: Mapping[str, Any], capture: Event
) -> dict[str, Any] | None:
    """The election R9 requires: the capture's answer, and every other field
    from the ask at the chosen position. None when the position names no
    option of the ask."""
    picker = _picker(capture)
    position = picker.get("chosen_position")
    options = ask.get("options")
    if not isinstance(options, list):
        return None
    if type(position) is not int or not 1 <= position <= len(options):
        return None
    chosen = options[position - 1]
    label = chosen["label"]
    fields: dict[str, Any] = {
        "question": ask.get("question"),
        "chosen": label,
        "chosen_description": option_description(chosen),
        "rejected": [
            option["label"] for index, option in enumerate(options) if index != position - 1
        ],
        "scope": ask.get("scope"),
        "quote_ref": capture.id,
        "quotes": [label],
        "asked_event_id": asked_event_id,
        "display_sha256": picker.get("display_sha256"),
        "chosen_position": position,
    }
    if ask.get("question_id") is not None:
        fields["question_id"] = ask["question_id"]
    return fields


def check_answer(
    lg: Ledger,
    asked_event_id: int,
    capture_id: Any,
    election_fields: Mapping[str, Any],
    item_id: str | None,
) -> str | None:
    """None when an election with ``election_fields`` on ``item_id``, quoting
    capture ``capture_id``, is a bound answer to ask ``asked_event_id``;
    otherwise the first refusal reason (R1..R9, in order)."""
    conn = lg._conn
    ask = _event(conn, asked_event_id)
    if ask is None or ask.kind != "decision.asked":
        return ASK_NOT_FOUND
    # R1: the election is on the ask's item.
    if item_id != ask.item_id:
        return ANSWER_OTHER_ITEM
    # R2: the quote is a picker capture. The picker object is the cutover
    # marker: no event written before this rule can carry it.
    capture = _event(conn, capture_id)
    if capture is None or capture.kind != "utterance.captured":
        return PRE_BINDING
    if capture.channel == PICKER_CHANNEL and isinstance(capture.payload.get("picker"), dict):
        pass
    elif capture.channel == PROMPT_CHANNEL:
        return TYPED_ANSWER
    else:
        return PRE_BINDING
    picker = _picker(capture)
    # R3: recorded after the ask.
    if capture.id <= ask.id:
        return ANSWER_BEFORE_ASK
    # R4: tagged for this ask.
    tag = picker.get("asked_event_id")
    if tag is None:
        return ANSWER_UNBOUND
    if tag != asked_event_id:
        return ANSWER_OTHER_ASK
    # R4a: every capture tagged for the ask agrees with this one.
    for sibling in picker_captures(conn, asked_event_id):
        other = _picker(sibling)
        if other.get("display_sha256") != picker.get("display_sha256"):
            return CAPTURE_CONFLICT
        if other.get("chosen_position") != picker.get("chosen_position"):
            return CAPTURE_CONFLICT
        if sibling.id != capture.id and has_nonempty(other.get("annotation")):
            return CAPTURE_CONFLICT
    # R5: the fingerprint is the display's own.
    if picker.get("display_sha256") != fingerprint(picker.get("display")):
        return DISPLAY_FINGERPRINT_BAD
    # R6: the owner saw the ask, as ask-display renders it now.
    try:
        shown = render_ask_display(asked_event_id, ask.payload)
    except AskNotPickable:
        return ASK_NOT_PICKABLE
    if picker.get("display_sha256") != fingerprint(shown):
        return DISPLAY_DIFFERS
    # R7: one question, one option, no owner note.
    if picker.get("question_count") != 1:
        return PICKER_MULTI_QUESTION
    position = picker.get("chosen_position")
    if position is None:
        if picker.get("typed") is True:
            return PICKER_TYPED
        if picker.get("ambiguous") is True:
            return PICKER_AMBIGUOUS
        return PICKER_UNANSWERED
    if has_nonempty(picker.get("annotation")):
        return PICKER_ANNOTATED
    # R8: the quote rule every election keeps.
    quotes = election_fields.get("quotes")
    try:
        validate_quote_provenance(
            lg, election_fields.get("quote_ref"), list(quotes) if isinstance(quotes, list) else []
        )
    except CliError as exc:
        return exc.code
    # R9: the election records the capture's answer and derives the rest.
    if election_fields.get("display_sha256") != picker.get("display_sha256"):
        return CHOICE_MISMATCH
    if election_fields.get("chosen_position") != position:
        return CHOICE_MISMATCH
    expected = derive_election(asked_event_id, ask.payload, capture)
    if expected is None:
        return ELECTION_DIFFERS
    for key in DERIVED_FIELDS:
        if election_fields.get(key) != expected.get(key):
            return ELECTION_DIFFERS
    return None


def bound_answer(lg: Ledger, asked_event_id: int) -> BoundAnswer:
    """The election that answers ask ``asked_event_id``, or why none does.

    Reads one snapshot: inside the caller's transaction when there is one,
    else in a read transaction of its own. The first election linked to the
    ask decides; a later one never replaces it.
    """
    conn = lg._conn
    own = not conn.in_transaction
    if own:
        conn.execute("BEGIN")
    try:
        ask = _event(conn, asked_event_id)
        if ask is None or ask.kind != "decision.asked":
            return BoundAnswer(None, ASK_NOT_FOUND)
        election = first_election(conn, asked_event_id)
        if election is None:
            return BoundAnswer(None, NO_ELECTION)
        reason = check_answer(
            lg,
            asked_event_id,
            election.payload.get("quote_ref"),
            election.payload,
            election.item_id,
        )
        if reason is not None:
            return BoundAnswer(None, reason)
        return BoundAnswer(election, None)
    finally:
        if own and conn.in_transaction:
            conn.execute("ROLLBACK")


__all__ = [
    "AskNotPickable",
    "BoundAnswer",
    "bound_answer",
    "check_answer",
    "derive_election",
    "fingerprint",
    "render_ask_display",
]
