"""The text a picker displays holds no control character.

An ask's question id, scope, option labels and descriptions, and its question
(a newline alone is allowed there), are printed to the owner by the picker.
A carriage return plus an ANSI erase-line code makes a terminal show only the
text after it while the stored value, which ``ledger grants --check`` and the
display fingerprint match, is longer: the owner could approve what they never
saw. One rule covers every displayed field:

* write time: ``ledger append`` (and every writer behind the store) refuses
  the ask with ``ask_control_character:<field>``;
* read time: an ask stored before the rule reads ``ask_not_pickable`` naming
  the field, so no pick, election or grant binds through it.

The header is ``ask K`` built from the event id; no payload field holds it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ledger.answers import AskNotPickable, render_ask_display
from ledger.schema import control_character
from ledger.store import open_ledger
from test_election_binding import (
    GRANT_OPTIONS,
    ITEM,
    QUESTION,
    SCOPE,
    answer,
    append,
    ask,
    ask_display,
    ask_payload,
    bound,
    captured,
    cli,
    event_count,
    forged,
    grant_ask,
    grant_check,
    new_item,
    pick,
    picker_hook,
    raw_elect,
    reason,
    refusal,
)

# What a seat can type: the visible id, then a carriage return and the ANSI
# erase-line code, then the id a grant check matches.
HIDDEN_ID = "grant:x\r\x1b[2Kgrant:y"
CR = "\r"
ESC_SEQUENCE = "\x1b[2K"
C1 = "\u009b"
CONTROLS = [
    pytest.param(CR, "U+000D", id="carriage-return"),
    pytest.param("a" + ESC_SEQUENCE + "b", "U+001B", id="ansi-escape"),
    pytest.param(C1, "U+009B", id="c1-csi"),
]


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "ledger.db")
    new_item(monkeypatch, path, ITEM)
    return path


def with_field(field: str, value: str) -> dict:
    """A well-formed ask payload with ``value`` in one displayed ``field``."""
    payload = ask_payload(question_id="grant:x")
    if field == "question_id":
        payload["question_id"] = value
    elif field == "question":
        payload["question"] = f"Apply {value} the graduation?"
    elif field == "scope":
        payload["scope"] = value
    elif field == "options[0].label":
        payload["options"][0]["label"] = f"apply{value}"
    elif field == "options[1].description":
        payload["options"][1]["description"] = f"Hold{value} it"
    else:
        raise AssertionError(field)
    return payload


FIELDS = ["question_id", "question", "scope", "options[0].label", "options[1].description"]


# --- write time ---------------------------------------------------------------


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value,codepoint", CONTROLS)
def test_an_ask_with_a_control_character_in_a_displayed_field_is_refused(
        db, monkeypatch, field, value, codepoint) -> None:
    before = event_count(db)
    code, env = append(monkeypatch, db, "decision.asked", with_field(field, value))
    assert code == 3, env
    assert refusal(env) == ("validation", f"ask_control_character:{field}")
    assert event_count(db) == before
    assert event_count(db, "decision.asked") == 0


def test_the_hidden_grant_id_ask_is_refused_whole(db, monkeypatch) -> None:
    code, env = append(monkeypatch, db, "decision.asked", ask_payload(question_id=HIDDEN_ID))
    assert code == 3
    assert refusal(env) == ("validation", "ask_control_character:question_id")
    assert event_count(db, "decision.asked") == 0


@pytest.mark.parametrize("field", ["question_id", "scope", "options[0].label",
                                   "options[1].description"])
def test_a_newline_is_refused_in_every_field_but_the_question(db, monkeypatch, field) -> None:
    code, env = append(monkeypatch, db, "decision.asked", with_field(field, "\n"))
    assert code == 3
    assert refusal(env) == ("validation", f"ask_control_character:{field}")


def test_a_newline_is_allowed_in_the_question_body_and_the_ask_binds(db, monkeypatch) -> None:
    question = "Apply the graduation?\nIt changes the gate.\n\nScope below."
    asked = ask(monkeypatch, db, question=question)
    answer(monkeypatch, db, asked, "apply")
    code, env = pick(monkeypatch, db, asked)
    assert code == 0, env
    assert reason(db, asked) is None
    assert env["data"]["election"]["payload"]["question"] == question


@pytest.mark.parametrize("value", ["\r", "\r\n", "\t", "\x00", "\x7f", "\x85", C1])
def test_every_other_control_character_is_refused_in_the_question(
        db, monkeypatch, value) -> None:
    code, env = append(monkeypatch, db, "decision.asked", with_field("question", value))
    assert code == 3
    assert refusal(env) == ("validation", "ask_control_character:question")


def test_the_header_is_built_from_the_event_id_and_no_payload_field_sets_it(
        db, monkeypatch) -> None:
    code, env = append(monkeypatch, db, "decision.asked",
                       dict(ask_payload(), header="ask 1\r\x1b[2Kask 9"))
    assert code == 3
    assert refusal(env) == ("validation", "unknown-key:header")
    asked = ask(monkeypatch, db)
    header = ask_display(monkeypatch, db, asked)[0]["header"]
    assert header == f"ask {asked}" and control_character(header) is None


@pytest.mark.parametrize("char,expected", [
    ("\x00", "U+0000"), ("\x1f", "U+001F"), ("\x7f", "U+007F"),
    ("\x80", "U+0080"), ("\x9f", "U+009F"),
    (" ", None), ("~", None), ("\xa0", None), ("\xa1", None), ("é", None), (" ", None),
])
def test_the_class_is_c0_del_and_c1_exactly(char, expected) -> None:
    assert control_character(f"a{char}b") == expected


def test_the_newline_exception_is_only_when_asked_for() -> None:
    assert control_character("a\nb") == "U+000A"
    assert control_character("a\nb", allow_newline=True) is None
    assert control_character("a\r\nb", allow_newline=True) == "U+000D"


def test_the_library_write_path_refuses_too(db) -> None:
    with open_ledger(db) as lg:
        with pytest.raises(Exception, match="ask_control_character:question_id"):
            lg.append(kind="decision.asked", item_id=ITEM, idempotency_key="k-lib",
                      harness="pytest", payload=ask_payload(question_id=HIDDEN_ID))
        assert lg.events(kind="decision.asked") == []


@pytest.mark.parametrize("changes,field", [
    ({"question_id": HIDDEN_ID}, "question_id"),
    ({"scope": "the graduation\r only"}, "scope"),
    ({"chosen": "apply\x1b[2K"}, "chosen"),
    ({"chosen_description": "Apply\u009b it"}, "chosen_description"),
    ({"rejected": ["hold\r"]}, "rejected[0]"),
    ({"question": "Apply?\x1b[2K"}, "question"),
])
def test_an_election_carrying_a_control_character_is_refused(
        db, monkeypatch, changes, field) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply")
    code, env = append(monkeypatch, db, "decision.elected",
                       forged(db, asked, capture_id, **changes))
    assert code == 3
    assert refusal(env) == ("validation", f"ask_control_character:{field}")
    assert event_count(db, "decision.elected") == 0


# --- read time ----------------------------------------------------------------


def write_legacy(monkeypatch, db: str, payload: dict) -> int:
    """An ask as it was stored before the write-time rule: the one guard is
    off for this write, so the row, its hash chain and its schema are exactly
    what an older version stored."""
    with monkeypatch.context() as patch:
        patch.setattr("ledger.store.refuse_display_control_characters",
                      lambda kind, payload: None)
        return ask_via(monkeypatch, db, payload)


def ask_via(monkeypatch, db: str, payload: dict) -> int:
    code, env = append(monkeypatch, db, "decision.asked", payload)
    assert code == 0, env
    return env["data"]["event"]["id"]


def seat_picker(asked: int, payload: dict, label: str) -> tuple[list, str]:
    """The picker a seat could compose by hand for a stored ask, as the ask
    would print it if it were pickable."""
    scope = f"scope: {payload['scope']}"
    if payload.get("question_id") is not None:
        scope += f"; question id: {payload['question_id']}"
    question = f"[ask {asked}] {payload['question']} ({scope})"
    options = [{"label": o["label"], "description": o.get("description", o["label"])}
               for o in payload["options"]]
    return ([{"question": question, "header": f"ask {asked}", "multiSelect": False,
              "options": options}], question)


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize("value,codepoint", CONTROLS)
def test_a_stored_ask_with_a_control_character_reads_not_pickable_and_binds_nothing(
        db, monkeypatch, field, value, codepoint) -> None:
    payload = with_field(field, value)
    asked = write_legacy(monkeypatch, db, payload)
    # ask-display refuses by name, and the reason names the field and the character class.
    code, env = cli(monkeypatch, db, "ask-display", str(asked))
    assert code == 3
    code_name, message = refusal(env)
    assert code_name == "ask_not_pickable"
    assert "control character" in message and field in message and codepoint in message
    assert value not in message
    # The seat composes the picker itself and the owner answers it.
    questions, question = seat_picker(asked, payload, payload["options"][0]["label"])
    capture_id = captured(monkeypatch, db, picker_hook(
        questions, {question: payload["options"][0]["label"]}))
    # pick writes nothing.
    code, env = pick(monkeypatch, db, asked)
    assert code == 3
    assert refusal(env)[0] == "ask_not_pickable"
    assert event_count(db, "decision.elected") == 0
    # A raw election through it (the strongest forgery) does not bind.
    with monkeypatch.context() as patch:
        patch.setattr("ledger.store.refuse_display_control_characters",
                      lambda kind, payload: None)
        raw_elect(monkeypatch, db, forged(db, asked, capture_id, position=1))
    assert bound(db, asked).election is None
    assert reason(db, asked) == "ask_not_pickable"


def test_the_hidden_grant_id_does_not_stand_when_the_ask_predates_the_rule(
        db, monkeypatch) -> None:
    payload = ask_payload(question_id=HIDDEN_ID, question=f"Stand {HIDDEN_ID}?",
                          options=GRANT_OPTIONS)
    asked = write_legacy(monkeypatch, db, payload)
    questions, question = seat_picker(asked, payload, "granted")
    capture_id = captured(monkeypatch, db, picker_hook(questions, {question: "granted"}))
    with monkeypatch.context() as patch:
        patch.setattr("ledger.store.refuse_display_control_characters",
                      lambda kind, payload: None)
        raw_elect(monkeypatch, db, forged(db, asked, capture_id, position=1))
    # The id the terminal would show, and the id the election stores: neither stands.
    code, env = grant_check(monkeypatch, db, "grant:y")
    assert code == 3 and refusal(env)[0] == "grant_unknown"
    code, env = grant_check(monkeypatch, db, HIDDEN_ID)
    assert code == 3
    code_name, message = refusal(env)
    assert code_name == "grant_unverified" and "(ask_not_pickable)" in message


def test_an_election_copying_a_stored_ask_s_control_character_is_refused_at_write(
        db, monkeypatch) -> None:
    payload = ask_payload(question_id=HIDDEN_ID)
    asked = write_legacy(monkeypatch, db, payload)
    questions, question = seat_picker(asked, payload, "apply")
    capture_id = captured(monkeypatch, db, picker_hook(questions, {question: "apply"}))
    election = forged(db, asked, capture_id, position=1)
    code, env = append(monkeypatch, db, "decision.elected", election)
    assert code == 3
    assert refusal(env) == ("validation", "ask_control_character:question_id")
    assert event_count(db, "decision.elected") == 0


def test_a_stored_ask_still_verifies_and_exports(db, monkeypatch, tmp_path: Path) -> None:
    """The write rule is not part of payload validation: verify, export and
    restore re-read rows written before it, and they must still read."""
    write_legacy(monkeypatch, db, ask_payload(question_id=HIDDEN_ID))
    with open_ledger(db, create=False) as lg:
        lg.verify()
    code, env = cli(monkeypatch, db, "export-item", ITEM)
    assert code == 0, env


@pytest.mark.parametrize("ask_fields", [
    {"question_id": 7},
    {"question": 7},
    {"options": [{"label": 1}, {"label": "b"}]},
    {"options": [{"label": "a", "description": 3}, {"label": "b"}]},
    {"options": ["a", "b"]},
])
def test_an_ask_whose_displayed_text_is_not_text_is_not_pickable(ask_fields) -> None:
    stored = dict(ask_payload(question_id="grant:x"), **ask_fields)
    with pytest.raises(AskNotPickable):
        render_ask_display(5, stored)


def test_an_exact_retry_of_a_stored_keyed_ask_returns_it_and_a_new_key_is_refused(
        db, monkeypatch) -> None:
    """The rule guards a new append, after the idempotency lookup: a retry of
    an ask stored before the rule returns the stored event."""
    kwargs = dict(kind="decision.asked", item_id=ITEM, harness="pytest",
                  payload=ask_payload(question_id=HIDDEN_ID))
    with open_ledger(db) as lg:
        with monkeypatch.context() as patch:
            patch.setattr("ledger.store.refuse_display_control_characters",
                          lambda kind, payload: None)
            stored = lg.append(idempotency_key="k-legacy", **kwargs)
        before = len(lg.events())
        retried = lg.append(idempotency_key="k-legacy", **kwargs)
        assert retried.id == stored.id and retried.hash == stored.hash
        with pytest.raises(Exception, match="ask_control_character:question_id"):
            lg.append(idempotency_key="k-new", **kwargs)
        assert len(lg.events()) == before


# --- unchanged behaviour ------------------------------------------------------


def test_a_plain_id_binds_end_to_end_and_the_grant_stands(db, monkeypatch) -> None:
    asked = grant_ask(monkeypatch, db, "grant:x")
    questions = ask_display(monkeypatch, db, asked)
    assert "question id: grant:x" in questions[0]["question"]
    answer(monkeypatch, db, asked, "granted")
    code, env = pick(monkeypatch, db, asked)
    assert code == 0, env
    assert reason(db, asked) is None
    code, env = grant_check(monkeypatch, db, "grant:x")
    assert code == 0, env
    assert env["data"]["status"] == "STANDING"


def test_a_plain_ask_without_an_id_is_unaffected(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    assert json.dumps(ask_display(monkeypatch, db, asked)).count("\\u00") == 0
    answer(monkeypatch, db, asked, "apply")
    code, env = pick(monkeypatch, db, asked)
    assert code == 0, env
    assert reason(db, asked) is None
    assert QUESTION in ask_display(monkeypatch, db, asked)[0]["question"]
    assert SCOPE in ask_display(monkeypatch, db, asked)[0]["question"]
