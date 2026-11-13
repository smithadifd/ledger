"""The text a picker displays is the text that is stored.

A right-to-left override, a zero-width space, a line separator, a variation
selector or a braille blank in an ask's question id, scope, option text or
question makes the id or text a person sees differ from the one stored, and
``ledger grants`` compares the stored question id code point for code
point. ``ledger.schema.display_text_problem`` is the one rule for every
displayed field:

* write time: ``ledger append`` (and every writer behind the store) refuses
  the ask or election with ``ask_control_character:<field>``;
* read time: an ask stored before the rule reads ``ask_not_pickable`` naming
  the field, the class and the code point, so no pick, election or grant
  binds through it.

There is no emoji exception: a joined emoji (U+200D) and U+FE0F are refused
in the question too; a single emoji code point still passes.
"""
from __future__ import annotations

import types
import unicodedata
from pathlib import Path

import pytest

from ledger import schema
from ledger.schema import (
    BLANK_CODE_POINTS,
    DEFAULT_IGNORABLE_RANGES,
    DEFAULT_IGNORABLE_UNICODE_VERSIONS,
    display_text_problem,
)
from test_ask_control_characters import seat_picker, write_legacy
from test_election_binding import (
    GRANT_OPTIONS,
    ITEM,
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

ZWJ = "‍"
FE0F = "️"
BRAILLE_BLANK = "⠀"
# man ZWJ woman ZWJ girl
FAMILY = "\U0001F468‍\U0001F469‍\U0001F467"
RED_HEART = "❤️"
# Round 3 design probe: a category-keyed emoji exception let this through.
ROUND3_PROBE = "¦‍¦"
GRINNING = "\U0001F600"

FORMAT = "a format character"
SEPARATOR = "a line or paragraph separator"
INVISIBLE = "an invisible character"
BLANK = "a blank character"
SPACE = "a space other than U+0020"
LEADING = "leading whitespace"
TRAILING = "trailing whitespace"
LEADING_MARK = "a leading combining mark"
NOT_NFC = "text that is not NFC"


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "ledger.db")
    new_item(monkeypatch, path, ITEM)
    return path


def grant_payload(field: str, value: str) -> dict:
    """A well-formed grant ask for ``grant:x`` with ``value`` as the whole of
    one displayed ``field``."""
    payload = ask_payload(question_id="grant:x", question="Stand grant:x?",
                          options=GRANT_OPTIONS)
    if field in ("question_id", "question", "scope"):
        payload[field] = value
    elif field == "options[0].label":
        payload["options"][0]["label"] = value
    elif field == "options[1].description":
        payload["options"][1]["description"] = value
    else:
        raise AssertionError(field)
    return payload


# (field, whole value, class, code point): every case the design names.
CASES = [
    pytest.param("question_id", "grant:‮x", FORMAT, "U+202E", id="id-rlo"),
    pytest.param("question_id", "grant:x​", FORMAT, "U+200B", id="id-zwsp"),
    pytest.param("question_id", "grant: x", SEPARATOR, "U+2028", id="id-line-sep"),
    pytest.param("question_id", "grant:x️", INVISIBLE, "U+FE0F", id="id-fe0f"),
    pytest.param("question_id", "grant:x͏", INVISIBLE, "U+034F", id="id-cgj"),
    pytest.param("question_id", "grant:­x", FORMAT, "U+00AD", id="id-soft-hyphen"),
    pytest.param("question_id", "grant:xㅤ", INVISIBLE, "U+3164", id="id-hangul-filler"),
    pytest.param("question_id", "grant:x￹", FORMAT, "U+FFF9", id="id-cf-not-ignorable"),
    pytest.param("question_id", "grant: x", SPACE, "U+00A0", id="id-nbsp-inside"),
    pytest.param("question_id", "grant:　x", SPACE, "U+3000", id="id-ideographic-space"),
    pytest.param("question_id", " grant:x", LEADING, "U+0020", id="id-leading-space"),
    pytest.param("question_id", "grant:x⠀", BLANK, "U+2800", id="id-braille-blank"),
    pytest.param("question_id", "grant:é", NOT_NFC, "U+0301", id="id-not-nfc"),
    pytest.param("question_id", "grant:Å", NOT_NFC, "U+212B", id="id-not-nfc-singleton"),
    pytest.param("scope", "the grant only‍", FORMAT, "U+200D", id="scope-zwj"),
    pytest.param("scope", "the grant only⠀", BLANK, "U+2800", id="scope-braille-blank"),
    pytest.param("scope", "the grant only ", TRAILING, "U+0020", id="scope-trailing-space"),
    pytest.param("scope", "the grant only ", SPACE, "U+00A0", id="scope-trailing-nbsp"),
    pytest.param("options[0].label", "gran‮ted", FORMAT, "U+202E", id="label-rlo"),
    pytest.param("options[0].label", "́granted", LEADING_MARK, "U+0301",
                 id="label-leading-mark"),
    pytest.param("options[1].description", "The grant is withdrawn ", SEPARATOR,
                 "U+2029", id="description-para-sep"),
    pytest.param("question", "Stand grant:x?‍", FORMAT, "U+200D", id="question-lone-zwj"),
    pytest.param("question", f"Stand grant:x? {FAMILY}", FORMAT, "U+200D",
                 id="question-family-emoji"),
    pytest.param("question", f"Stand grant:x? {RED_HEART}", INVISIBLE, "U+FE0F",
                 id="question-red-heart-fe0f"),
    pytest.param("question", f"Stand grant:x? {ROUND3_PROBE}", FORMAT, "U+200D",
                 id="question-round3-probe"),
    pytest.param("question", "⠀Stand grant:x?", BLANK, "U+2800",
                 id="question-braille-blank"),
    pytest.param("question", "Stand grant:x?\n", TRAILING, "U+000A",
                 id="question-trailing-newline"),
]


# --- write time ---------------------------------------------------------------


@pytest.mark.parametrize("field,value,problem,code_point", CASES)
def test_an_ask_breaking_the_displayed_text_rule_is_refused_by_name(
        db, monkeypatch, field, value, problem, code_point) -> None:
    before = event_count(db)
    code, env = append(monkeypatch, db, "decision.asked", grant_payload(field, value))
    assert code == 3, env
    assert refusal(env) == ("validation", f"ask_control_character:{field}")
    assert event_count(db) == before
    assert event_count(db, "decision.asked") == 0


@pytest.mark.parametrize("field,value,problem,code_point", CASES)
def test_the_rule_names_the_class_and_the_code_point(field, value, problem, code_point) -> None:
    hit = schema.display_control_character("decision.asked", grant_payload(field, value))
    assert hit == (field, problem, code_point)


@pytest.mark.parametrize("changes,field", [
    ({"question_id": "grant:x͏"}, "question_id"),
    ({"scope": "the graduation⠀"}, "scope"),
    ({"chosen": "apply​"}, "chosen"),
    ({"chosen_description": "Apply‮ it"}, "chosen_description"),
    ({"rejected": ["hold "]}, "rejected[0]"),
    ({"question": "Apply? "}, "question"),
    ({"question": f"Apply? {FAMILY}"}, "question"),
])
def test_an_election_breaking_the_displayed_text_rule_is_refused(
        db, monkeypatch, changes, field) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply")
    code, env = append(monkeypatch, db, "decision.elected",
                       forged(db, asked, capture_id, **changes))
    assert code == 3
    assert refusal(env) == ("validation", f"ask_control_character:{field}")
    assert event_count(db, "decision.elected") == 0


# --- read time ----------------------------------------------------------------


@pytest.mark.parametrize("field,value,problem,code_point", CASES)
def test_a_stored_ask_breaking_the_rule_reads_not_pickable_and_no_grant_stands(
        db, monkeypatch, field, value, problem, code_point) -> None:
    payload = grant_payload(field, value)
    asked = write_legacy(monkeypatch, db, payload)
    code, env = cli(monkeypatch, db, "ask-display", str(asked))
    assert code == 3
    code_name, message = refusal(env)
    assert code_name == "ask_not_pickable"
    assert f"holds {problem} ({code_point}) in {field}," in message
    character = chr(int(code_point[2:], 16))
    if character != " ":
        assert character not in message
    # The seat composes the picker itself and the owner answers it.
    label = payload["options"][0]["label"]
    questions, question = seat_picker(asked, payload, label)
    capture_id = captured(monkeypatch, db, picker_hook(questions, {question: label}))
    code, env = pick(monkeypatch, db, asked)
    assert code == 3
    assert refusal(env)[0] == "ask_not_pickable"
    assert event_count(db, "decision.elected") == 0
    # A raw election through it (the strongest forgery) binds nothing.
    with monkeypatch.context() as patch:
        patch.setattr("ledger.store.refuse_display_control_characters",
                      lambda kind, payload: None)
        raw_elect(monkeypatch, db, forged(db, asked, capture_id, position=1))
    assert bound(db, asked).election is None
    assert reason(db, asked) == "ask_not_pickable"
    code, env = grant_check(monkeypatch, db, payload["question_id"])
    assert code == 3
    code_name, message = refusal(env)
    if not payload["question_id"].startswith("grant:"):
        # A leading space: no grant prefix, so no grant at all.
        assert code_name == "grant_unknown"
    elif label != "granted":
        # The chosen label is not the plain "granted": never STANDING.
        assert code_name == "grant_withdrawn"
    else:
        assert code_name == "grant_unverified" and "(ask_not_pickable)" in message


def standing_attempt(monkeypatch, db: str, payload: dict) -> tuple[int, dict]:
    """A design-round probe: the ask is refused at write; the same ask stored
    before the rule, answered in a seat-composed picker and elected raw, is
    then checked as a grant."""
    code, env = append(monkeypatch, db, "decision.asked", payload)
    assert code == 3 and refusal(env)[1].startswith("ask_control_character:")
    asked = write_legacy(monkeypatch, db, payload)
    questions, question = seat_picker(asked, payload, "granted")
    capture_id = captured(monkeypatch, db, picker_hook(questions, {question: "granted"}))
    with monkeypatch.context() as patch:
        patch.setattr("ledger.store.refuse_display_control_characters",
                      lambda kind, payload: None)
        raw_elect(monkeypatch, db, forged(db, asked, capture_id, position=1))
    return grant_check(monkeypatch, db, payload["question_id"])


@pytest.mark.parametrize("field,value", [
    pytest.param("question_id", "grant:x️", id="round1-id-fe0f"),
    pytest.param("scope", "the grant only‍", id="round1-scope-zwj"),
    pytest.param("question_id", "grant:x⠀", id="round2-id-braille-blank"),
])
def test_the_design_round_probes_no_longer_reach_standing(db, monkeypatch, field, value) -> None:
    code, env = standing_attempt(monkeypatch, db, grant_payload(field, value))
    assert code == 3
    code_name, message = refusal(env)
    assert code_name == "grant_unverified" and "(ask_not_pickable)" in message


# --- what still passes --------------------------------------------------------


@pytest.mark.parametrize("question", [
    pytest.param(f"Stand grant:x? {GRINNING}", id="single-emoji"),
    pytest.param("Stand grant:x? ❤", id="bare-heart"),
    pytest.param("Stand grant:x?\nIt changes　the gate.", id="inner-spaces-newline"),
])
def test_a_question_with_a_single_emoji_or_inner_spaces_binds_and_the_grant_stands(
        db, monkeypatch, question) -> None:
    asked = grant_ask(monkeypatch, db, "grant:x", question=question)
    assert question in ask_display(monkeypatch, db, asked)[0]["question"]
    answer(monkeypatch, db, asked, "granted")
    code, env = pick(monkeypatch, db, asked)
    assert code == 0, env
    assert reason(db, asked) is None
    code, env = grant_check(monkeypatch, db, "grant:x")
    assert code == 0, env
    assert env["data"]["status"] == "STANDING"


def test_an_nfc_id_with_letters_beyond_ascii_binds(db, monkeypatch) -> None:
    grant_id = "grant:café-日本"
    asked = grant_ask(monkeypatch, db, grant_id)
    answer(monkeypatch, db, asked, "granted")
    code, env = pick(monkeypatch, db, asked)
    assert code == 0, env
    code, env = grant_check(monkeypatch, db, grant_id)
    assert code == 0, env
    assert env["data"]["status"] == "STANDING"


# --- the rule itself ----------------------------------------------------------

# The design's Unicode 16.0 Default_Ignorable_Code_Point list.
DESIGN_TABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
# Format characters Unicode leaves out of Default_Ignorable_Code_Point: the
# prepended concatenation marks, the interlinear annotation characters and
# the Egyptian hieroglyph format controls. The rule refuses them as Cf.
NOT_IGNORABLE_FORMAT = (
    set(range(0x0600, 0x0606)) | {0x06DD, 0x070F, 0x0890, 0x0891, 0x08E2}
    | set(range(0xFFF9, 0xFFFC)) | {0x110BD, 0x110CD} | set(range(0x13430, 0x13440))
)


def table_code_points() -> list[int]:
    return [code for low, high in DEFAULT_IGNORABLE_RANGES for code in range(low, high + 1)]


def test_the_default_ignorable_table_is_pinned_to_the_unicode_database() -> None:
    assert unicodedata.unidata_version in DEFAULT_IGNORABLE_UNICODE_VERSIONS
    assert DEFAULT_IGNORABLE_RANGES == DESIGN_TABLE
    for (_low, high), (next_low, _next_high) in zip(DEFAULT_IGNORABLE_RANGES,
                                                     DEFAULT_IGNORABLE_RANGES[1:]):
        assert high < next_low


def test_the_table_holds_every_format_character_and_variation_selector_it_should() -> None:
    table = set(table_code_points())
    for code in range(0x110000):
        char = chr(code)
        if unicodedata.category(char) == "Cf" and code not in NOT_IGNORABLE_FORMAT:
            assert code in table, f"U+{code:04X}"
        if "VARIATION SELECTOR" in unicodedata.name(char, ""):
            assert code in table, f"U+{code:04X}"


@pytest.mark.parametrize("question", [False, True])
def test_every_table_code_point_is_refused_in_every_field(question) -> None:
    for code in table_code_points():
        found = display_text_problem(f"a{chr(code)}b", question=question)
        assert found is not None and found[1] == f"U+{code:04X}", f"U+{code:04X}"


def test_every_blank_is_refused_and_the_braille_blank_is_named() -> None:
    assert BLANK_CODE_POINTS == {0x115F, 0x1160, 0x2800, 0x3164, 0xFFA0}
    for code in BLANK_CODE_POINTS:
        assert display_text_problem(f"a{chr(code)}b", question=True) is not None
    assert display_text_problem(f"a{BRAILLE_BLANK}b", question=True) == (BLANK, "U+2800")


def test_there_is_no_emoji_exception() -> None:
    assert display_text_problem(f"go {FAMILY}", question=True) == (FORMAT, "U+200D")
    assert display_text_problem(f"go {RED_HEART}", question=True) == (INVISIBLE, "U+FE0F")
    assert display_text_problem(f"go {ROUND3_PROBE}", question=True) == (FORMAT, "U+200D")
    assert display_text_problem(f"go {GRINNING}", question=True) is None
    assert display_text_problem("go ❤", question=True) is None


@pytest.mark.parametrize("text,expected", [
    pytest.param("a\ud800b", ("a surrogate", "U+D800"), id="surrogate"),
    pytest.param("ab", ("a private-use character", "U+E000"), id="private-use"),
    pytest.param("a͸b", ("an unassigned character", "U+0378"), id="unassigned"),
    pytest.param("a b", (SEPARATOR, "U+2029"), id="paragraph-separator"),
    pytest.param("a⁦b", (FORMAT, "U+2066"), id="isolate"),
    pytest.param("a\x1bb", ("a control character", "U+001B"), id="control"),
])
def test_each_refused_category_is_named(text, expected) -> None:
    assert display_text_problem(text) == expected
    assert display_text_problem(text, question=True) == expected


def test_a_code_point_of_an_unknown_category_is_refused(monkeypatch) -> None:
    real = unicodedata

    def category(char: str) -> str:
        return "Xx" if char == "q" else real.category(char)

    monkeypatch.setattr(schema, "unicodedata", types.SimpleNamespace(
        category=category, normalize=real.normalize, is_normalized=real.is_normalized,
        combining=real.combining))
    assert display_text_problem("aqb") == ("a character of unknown category", "U+0071")
    assert display_text_problem("aqb", question=True) == (
        "a character of unknown category", "U+0071")
    assert display_text_problem("abc") is None


def test_only_the_question_holds_a_space_other_than_u0020_and_only_inside() -> None:
    spaces = [code for code in range(0x110000)
              if unicodedata.category(chr(code)) == "Zs" and code != 0x20]
    assert 0xA0 in spaces and 0x3000 in spaces
    for code in spaces:
        char = chr(code)
        assert display_text_problem(f"a{char}b") == (SPACE, f"U+{code:04X}")
        if unicodedata.normalize("NFC", char) == char:
            assert display_text_problem(f"a{char}b", question=True) is None
        else:  # U+2000 and U+2001 are canonical copies of U+2002 and U+2003
            assert display_text_problem(f"a{char}b", question=True) == (
                NOT_NFC, f"U+{code:04X}")
        assert display_text_problem(f"{char}a", question=True) == (LEADING, f"U+{code:04X}")
        assert display_text_problem(f"a{char}", question=True) == (TRAILING, f"U+{code:04X}")
    assert display_text_problem("a b") is None
    assert display_text_problem("a\nb", question=True) is None
    assert display_text_problem("\na", question=True) == (LEADING, "U+000A")
    assert display_text_problem("a\n", question=True) == (TRAILING, "U+000A")
    assert display_text_problem("a\nb") == ("a control character", "U+000A")


def test_text_must_be_nfc_and_must_not_start_with_a_mark() -> None:
    assert display_text_problem("é") == (NOT_NFC, "U+0301")
    assert display_text_problem("Å") == (NOT_NFC, "U+212B")
    assert display_text_problem("가")[0] == NOT_NFC
    assert display_text_problem("é") is None
    assert display_text_problem("́a") == (LEADING_MARK, "U+0301")
    assert display_text_problem("ःa") == (LEADING_MARK, "U+0903")
    assert display_text_problem("q́") is None
    assert display_text_problem("") is None
