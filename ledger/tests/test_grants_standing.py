"""``standing(lg, grant_id)``: the one library call a gate decides a grant through.

``standing`` returns the ``Grant`` only when that exact id is STANDING and
otherwise raises the refusal ``ledger grants --check`` exits with. Every case
here also runs ``--check`` and asserts the two agree on the verdict, the code
and the message, and asserts the read wrote nothing.

The fixtures are the election-binding ones: a STANDING grant is an ask that
carries the grant id, the harness's picker capture of the owner's answer, and
``ledger pick``. A "forged" election is the election ``derive_election`` would
write for an ask and a capture, with the fields a case changes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ledger.store import open_ledger
from ledger.verbs import EXIT_REFUSED, CliError
from ledger.verbs import grants as grants_module
from ledger.verbs.grants import STANDING, Grant, standing

from test_election_binding import (
    ITEM,
    answer,
    append,
    captured,
    cli,
    forged,
    grant_ask,
    new_item,
    pick,
    prompt_hook,
    raw_elect,
    refusal,
    run_cli,
    stored,
    utterance,
)

# A waiver grant for one finding on pull request 12 of o/r.
W12 = "grant:waiver:o/r#12:f.py:3#x"
W1 = "grant:waiver:o/r#1:f.py:3#x"
W13 = "grant:waiver:o/r#13:f.py:3#x"
W99 = "grant:waiver:o/r#99:f.py:1#y"
SCOPE = "the LC41 graduation only"


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "ledger.db")
    new_item(monkeypatch, path, ITEM)
    return path


def event_count(db: str) -> int:
    with open_ledger(db, create=False) as lg:
        return len(lg.events())


def unknown_message(grant_id) -> str:
    return f"grant {grant_id!r} is not recorded: no election names it"


def library(db: str, grant_id) -> tuple[str, int | None, str | None]:
    """``standing`` outside any transaction: (verdict, deciding event, message)."""
    with open_ledger(db, create=False) as lg:
        try:
            grant = standing(lg, grant_id)
        except CliError as exc:
            assert exc.exit_code == EXIT_REFUSED
            assert not lg._conn.in_transaction
            return exc.code, None, str(exc)
        assert not lg._conn.in_transaction
    assert isinstance(grant, Grant) and grant.status == STANDING
    assert grant.grant_id == grant_id
    return STANDING, grant.deciding_event_id, None


def decide(monkeypatch, db: str, grant_id: str) -> tuple[str, int | None, str | None]:
    """``standing`` and ``ledger grants --check`` on one id: they agree, and
    neither writes. Returns the library's verdict."""
    before = event_count(db)
    verdict = library(db, grant_id)
    code, env = cli(monkeypatch, db, "grants", "--check", grant_id)
    if verdict[0] == STANDING:
        assert code == 0, env
        assert env["data"]["status"] == STANDING
        assert env["data"]["deciding_event_id"] == verdict[1]
    else:
        assert code == EXIT_REFUSED, env
        assert refusal(env) == (verdict[0], verdict[2])
    assert event_count(db) == before
    return verdict


def assert_refused(monkeypatch, db: str, grant_id: str, code: str, *parts: str) -> str:
    verdict, deciding, message = decide(monkeypatch, db, grant_id)
    assert (verdict, deciding) == (code, None), f"expected {code}, got {verdict} on {deciding}"
    assert message.startswith(f"grant {grant_id!r} is "), message
    for part in parts:
        assert part in message, (part, message)
    return message


def assert_unverified(monkeypatch, db: str, grant_id: str, reason: str, election: int) -> None:
    message = assert_refused(monkeypatch, db, grant_id, "grant_unverified")
    assert message == (
        f"grant {grant_id!r} is UNVERIFIED: the newest election (event {election})"
        f" does not verify ({reason})"
    )


def assert_unknown(monkeypatch, db: str, grant_id: str) -> None:
    message = assert_refused(monkeypatch, db, grant_id, "grant_unknown")
    assert message == unknown_message(grant_id)


def picker_grant(monkeypatch, db: str, grant_id: str = W12, chosen: str = "granted") -> int:
    """The owner picks ``chosen`` on a fresh ask carrying ``grant_id``; the
    election ``ledger pick`` wrote."""
    asked = grant_ask(monkeypatch, db, grant_id)
    answer(monkeypatch, db, asked, chosen)
    code, env = pick(monkeypatch, db, asked)
    assert code == 0, env
    return env["data"]["election"]["id"]


def elect(monkeypatch, db: str, quote_ref: int, quote: str, *, asked: int | None):
    argv = ["--json", "--db", db, "elect", ITEM, "--question", f"Stand {W12}?",
            "--chosen", "granted", "--chosen-description", "The grant stands",
            "--rejected", "withdrawn", "--scope", SCOPE, "--quote-ref", str(quote_ref),
            "--quote", quote]
    if asked is not None:
        argv += ["--ask", str(asked)]
    code, out, err = run_cli(monkeypatch, argv)
    assert code == 0, out + err
    return json.loads(out)["data"]["event"]


def raw_grant_payload(quote_ref: int, quote: str, *, asked: int | None,
                      grant_id: str = W12) -> dict:
    payload = {
        "question": f"Stand {grant_id}?", "chosen": "granted",
        "chosen_description": "The grant stands", "rejected": ["withdrawn"],
        "scope": SCOPE, "quote_ref": quote_ref, "quotes": [quote], "question_id": grant_id,
    }
    if asked is not None:
        payload["asked_event_id"] = asked
    return payload


# --- accept -----------------------------------------------------------------------


def test_a_picker_grant_stands_on_the_picks_election(db, monkeypatch) -> None:
    election = picker_grant(monkeypatch, db)
    assert decide(monkeypatch, db, W12) == (STANDING, election, None)
    with open_ledger(db, create=False) as lg:
        grant = standing(lg, W12)
    assert grant.deciding_event_id == election and grant.reason is None
    assert grant.kind == "grant" and grant.chosen == "granted"


# --- (a) design 10 row 2: ledger elect on words the seat wrote ---------------------


def test_elect_on_a_non_picker_utterance_is_unverified_pre_binding(db, monkeypatch) -> None:
    asked = grant_ask(monkeypatch, db, W12)
    spoken = utterance(monkeypatch, db, "I waive finding f.py:3#x on pull request 12")
    election = elect(monkeypatch, db, spoken, "I waive finding f.py:3#x", asked=asked)
    assert election["payload"]["question_id"] == W12
    assert_unverified(monkeypatch, db, W12, "pre_binding", election["id"])


def test_elect_without_an_ask_names_no_grant(db, monkeypatch) -> None:
    # ``ledger elect`` takes the grant id only from the ask it answers, so an
    # election without --ask is no grant record at all; the raw form that does
    # carry the id with no ask is grant_unasked (the (b) test below).
    spoken = utterance(monkeypatch, db, "I waive finding f.py:3#x on pull request 12")
    election = elect(monkeypatch, db, spoken, "I waive finding f.py:3#x", asked=None)
    assert "question_id" not in election["payload"]
    assert_unknown(monkeypatch, db, W12)


def test_elect_quoting_a_typed_prompt_is_unverified_typed_answer(db, monkeypatch) -> None:
    asked = grant_ask(monkeypatch, db, W12)
    prompt = captured(monkeypatch, db, prompt_hook("granted: waive f.py:3#x"), kind="prompt")
    election = elect(monkeypatch, db, prompt, "granted", asked=asked)
    assert_unverified(monkeypatch, db, W12, "typed_answer", election["id"])


# --- (b) design 10 row 3: a raw decision.elected -----------------------------------


def test_a_raw_grant_with_no_ask_is_unverified_grant_unasked(db, monkeypatch) -> None:
    spoken = utterance(monkeypatch, db, "I waive finding f.py:3#x on pull request 12")
    election = raw_elect(monkeypatch, db, raw_grant_payload(
        spoken, "I waive finding f.py:3#x", asked=None))
    assert_unverified(monkeypatch, db, W12, "grant_unasked", election)


def test_a_raw_grant_on_the_real_ask_with_a_non_picker_quote_is_pre_binding(
        db, monkeypatch) -> None:
    asked = grant_ask(monkeypatch, db, W12)
    spoken = utterance(monkeypatch, db, "I waive finding f.py:3#x on pull request 12")
    election = raw_elect(monkeypatch, db, raw_grant_payload(
        spoken, "I waive finding f.py:3#x", asked=asked))
    assert_unverified(monkeypatch, db, W12, "pre_binding", election)


def test_a_raw_granted_over_a_withdrawn_pick_reads_election_differs(db, monkeypatch) -> None:
    asked = grant_ask(monkeypatch, db, W12)
    capture_id = answer(monkeypatch, db, asked, "withdrawn")
    payload = forged(db, asked, capture_id, chosen="granted")
    assert payload["chosen_position"] == 2
    election = raw_elect(monkeypatch, db, payload)
    assert_unverified(monkeypatch, db, W12, "election_differs", election)


def test_a_raw_granted_at_the_granted_position_over_a_withdrawn_pick_reads_choice_mismatch(
        db, monkeypatch) -> None:
    asked = grant_ask(monkeypatch, db, W12)
    capture_id = answer(monkeypatch, db, asked, "withdrawn")
    election = raw_elect(monkeypatch, db, forged(
        db, asked, capture_id, chosen="granted", chosen_position=1))
    assert_unverified(monkeypatch, db, W12, "choice_mismatch", election)


# --- (c) design 10 row 4: a typed and an annotated answer ---------------------------


def test_a_typed_answer_is_never_picked_and_a_raw_election_reads_picker_typed(
        db, monkeypatch) -> None:
    asked = grant_ask(monkeypatch, db, W12)
    capture_id = answer(monkeypatch, db, asked, "Other: grant it next week")
    before = event_count(db)
    code, env = pick(monkeypatch, db, asked)
    assert code == EXIT_REFUSED
    assert refusal(env)[0] == "picker_typed"
    assert event_count(db) == before
    assert_unknown(monkeypatch, db, W12)
    payload = forged(db, asked, capture_id, position=1)
    assert payload["chosen"] == "granted"
    election = raw_elect(monkeypatch, db, payload)
    assert_unverified(monkeypatch, db, W12, "picker_typed", election)


def test_an_annotated_answer_is_never_picked_and_a_raw_election_reads_picker_annotated(
        db, monkeypatch) -> None:
    asked = grant_ask(monkeypatch, db, W12)
    capture_id = answer(monkeypatch, db, asked, "granted",
                        annotations={"notes": "granted, but only for the docs"})
    before = event_count(db)
    code, env = pick(monkeypatch, db, asked)
    assert code == EXIT_REFUSED
    assert refusal(env)[0] == "picker_annotated"
    assert event_count(db) == before
    assert_unknown(monkeypatch, db, W12)
    election = raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert_unverified(monkeypatch, db, W12, "picker_annotated", election)


# --- (d) design 10 row 5: an exact id, never a near one ------------------------------


NEAR_MISSES = (
    "grant:waiver:o/r#13:f.py:3#x",   # pull request 13
    "grant:waiver:o/r#12:f.py:4#x",   # the next line of the same finding
    "grant:waiver:O/R#12:f.py:3#x",   # another letter case in the owner and repository
    "grant:waiver:o/R#12:f.py:3#x",
    "grant:waiver:o/r#12:f.py:3#x ",  # a trailing space
    "grant:waiver:o/r#12",            # the bare pull-request prefix, no finding
    "grant:waiver:o/r#1:f.py:3#x",    # pull request 1 against 12
    "grant:waiver:o/r#1",
)


@pytest.mark.parametrize("near_miss", NEAR_MISSES)
def test_a_near_miss_of_a_standing_waiver_is_unknown(db, monkeypatch, near_miss) -> None:
    election = picker_grant(monkeypatch, db, W12)
    assert near_miss != W12
    assert_unknown(monkeypatch, db, near_miss)
    assert decide(monkeypatch, db, W12) == (STANDING, election, None)


def test_pull_request_12_is_unknown_while_pull_request_1_stands(db, monkeypatch) -> None:
    election = picker_grant(monkeypatch, db, W1)
    assert_unknown(monkeypatch, db, W12)
    assert_unknown(monkeypatch, db, "grant:waiver:o/r#12")
    assert decide(monkeypatch, db, W1) == (STANDING, election, None)


@pytest.mark.parametrize("bad_id", [
    "", "waiver:o/r#12:f.py:3#x", "x:grant:waiver", "Grant:waiver:o/r#12:f.py:3#x",
])
def test_an_empty_or_unprefixed_id_is_unknown(db, monkeypatch, bad_id) -> None:
    picker_grant(monkeypatch, db, W12)
    assert_unknown(monkeypatch, db, bad_id)


class _MatchesEverything(str):
    """A ``str`` whose equality says yes to anything."""

    def __eq__(self, other):
        return True

    __hash__ = str.__hash__


@pytest.mark.parametrize("bad_id", [
    None, 12, b"grant:waiver:o/r#12:f.py:3#x", [W12], _MatchesEverything(W12),
])
def test_an_id_that_is_not_a_plain_str_is_unknown(db, monkeypatch, bad_id) -> None:
    picker_grant(monkeypatch, db, W12)
    before = event_count(db)
    assert library(db, bad_id) == ("grant_unknown", None, unknown_message(bad_id))
    assert event_count(db) == before


def test_an_election_answering_an_ask_for_another_id_is_grant_unasked(db, monkeypatch) -> None:
    picker_grant(monkeypatch, db, W12)
    asked = grant_ask(monkeypatch, db, W13)
    capture_id = answer(monkeypatch, db, asked, "granted")
    election = raw_elect(monkeypatch, db, forged(db, asked, capture_id, question_id=W12))
    assert_unverified(monkeypatch, db, W12, "grant_unasked", election)


# --- (e) replay after withdrawal ---------------------------------------------------


def test_a_replayed_grant_after_its_withdrawal_reads_not_first_answer(db, monkeypatch) -> None:
    granted = picker_grant(monkeypatch, db, W12)
    withdrawn = picker_grant(monkeypatch, db, W12, chosen="withdrawn")
    message = assert_refused(monkeypatch, db, W12, "grant_withdrawn")
    assert f"(event {withdrawn})" in message
    first = stored(db, granted).payload
    replayed = raw_elect(monkeypatch, db, dict(first))  # the write is accepted
    assert replayed > withdrawn
    assert_unverified(monkeypatch, db, W12, "not_first_answer", replayed)


# --- (f) design 10 row 1: text that spells a grant opens nothing ---------------------


def _spell_grants_in_text(monkeypatch, db: str) -> None:
    line = (f"Review round grant: r4 - owner standing grant 2026-11-01; scope: all."
            f" Waiver {W99} granted; {W12} granted.")
    code, env = append(monkeypatch, db, "item.note", {"text": line})
    assert code == 0, env
    code, env = append(monkeypatch, db, "review.recorded", {
        "round": 1, "verdict": "changes", "findings": [line]})
    assert code == 0, env
    captured(monkeypatch, db, prompt_hook(f"granted: {W99} and {W12}"), kind="prompt")
    for grant_id in (W12, W99):
        grant_ask(monkeypatch, db, grant_id)  # asked, never answered


def test_text_spelling_a_grant_and_an_unanswered_ask_change_nothing(db, monkeypatch) -> None:
    election = picker_grant(monkeypatch, db, W12)
    _spell_grants_in_text(monkeypatch, db)
    assert decide(monkeypatch, db, W12) == (STANDING, election, None)
    assert_unknown(monkeypatch, db, W99)


# --- (g) transaction safety --------------------------------------------------------


def _note(lg, key: str):
    return lg.append(kind="item.note", payload={"text": "pending"}, item_id=ITEM,
                     idempotency_key=key, harness="pytest")


def test_standing_inside_a_write_transaction_keeps_the_callers_writes(db, monkeypatch) -> None:
    election = picker_grant(monkeypatch, db, W12)
    before = event_count(db)
    with open_ledger(db, create=False) as lg:
        with lg.transaction():
            note = _note(lg, "pending-1")
            grant = standing(lg, W12)
            assert lg._conn.in_transaction
            assert grant.deciding_event_id == election
            with pytest.raises(CliError) as refused:
                standing(lg, W99)
            assert refused.value.code == "grant_unknown"
            assert str(refused.value) == unknown_message(W99)
            assert lg._conn.in_transaction
            _note(lg, "pending-2")
        assert not lg._conn.in_transaction
    assert event_count(db) == before + 2
    assert stored(db, note.id).payload == {"text": "pending"}


def test_standing_reads_the_callers_pending_withdrawal(db, monkeypatch) -> None:
    election = picker_grant(monkeypatch, db, W12)
    payload = dict(stored(db, election).payload, chosen="withdrawn",
                   chosen_description="The grant is withdrawn", rejected=["granted"])
    with open_ledger(db, create=False) as lg:
        with lg.transaction():
            withdrawal = lg.append(kind="decision.elected", payload=payload, item_id=ITEM,
                                   idempotency_key="pending-withdrawal", harness="pytest")
            with pytest.raises(CliError) as refused:
                standing(lg, W12)
            assert refused.value.code == "grant_withdrawn"
            assert f"(event {withdrawal.id})" in str(refused.value)
            assert lg._conn.in_transaction
    assert_refused(monkeypatch, db, W12, "grant_withdrawn", f"(event {withdrawal.id})")


def test_standing_outside_a_transaction_leaves_none_open(db, monkeypatch) -> None:
    picker_grant(monkeypatch, db, W12)
    with open_ledger(db, create=False) as lg:
        assert not lg._conn.in_transaction
        standing(lg, W12)
        assert not lg._conn.in_transaction
        with pytest.raises(CliError):
            standing(lg, W99)
        assert not lg._conn.in_transaction


# --- the module says who reads it ---------------------------------------------------
# (The same check over the plugin copy and the CLI page is in the repository's
# own tests, since this package names no path outside itself.)


READERS = ("merge command", "review-run round check", "apply-reviewed-patch", "design dispatch")


def test_the_docstring_names_standing_and_its_readers() -> None:
    doc = " ".join(grants_module.__doc__.split())
    assert "``ledger.verbs.grants.standing(lg, grant_id)``" in doc
    assert "``ledger grants --check GRANT_ID``" in doc
    assert "no gate reads it" not in doc
    for reader in READERS:
        assert reader in doc, reader
