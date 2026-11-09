"""``ledger grants``: trust grants read from their election records.

Every fixture is a temporary ledger. Utterances are appended through the
library so their ``recorded_at`` can sit on a fixed UTC date; elections go
through ``ledger elect`` (answering an ask that carries the grant id as its
``question_id``) or, where a test needs an election the elect verb would
refuse, through a raw ``ledger append``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import ledger.verbs.elect as elect_module
from ledger.store import open_ledger
from ledger.verbs import EXIT_REFUSED, EXIT_USAGE, discover_verbs
from ledger.verbs import grants as grants_module
from ledger.verbs.grants import (
    REVIEW_ROUND_GRANT_ID,
    STANDING,
    UNVERIFIED,
    WITHDRAWN,
    read_grants,
)

from test_elect import invoke

GRANT = "grant:fixture"
ITEM = "it-grants"
# The owner's words land on 2026-10-30 (UTC); every election is recorded later.
SPOKEN_AT = "2026-10-30T23:30:00.000Z"
SPOKEN_DATE = "2026-10-30"
OWNER_TEXT = "Yes: I grant the fixture permission, scope fixture rounds only."
QUOTE = "I grant the fixture permission"
SCOPE = "fixture rounds only"


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "ledger.db")


def new_item(monkeypatch, db: str, item_id: str = ITEM) -> None:
    code, _out, err = invoke(
        monkeypatch, ["--db", db, "new", "--id", item_id, "--type", "gate", "--title", "grants"]
    )
    assert code == 0, err


def utterance(
    db: str,
    text: str = OWNER_TEXT,
    *,
    provenance: str = "submitted-prompt",
    recorded_at: str = SPOKEN_AT,
    key: str = "u-1",
    extra: dict | None = None,
) -> int:
    with open_ledger(db) as lg:
        return lg.append(
            kind="utterance.captured",
            payload={"text": text, **(extra or {})},
            item_id=ITEM,
            idempotency_key=key,
            actor="owner/capture",
            harness="pytest",
            channel="terminal",
            provenance_class=provenance,
            recorded_at=recorded_at,
        ).id


def ask(monkeypatch, db: str, grant_id: str = GRANT) -> int:
    payload = {
        "question": f"Stand {grant_id}?",
        "options": [{"label": "granted"}, {"label": "withdrawn"}],
        "question_id": grant_id,
    }
    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", db, "append", "--kind", "decision.asked", "--item", ITEM,
         "--payload", json.dumps(payload)],
    )
    assert code == 0, err
    return json.loads(out)["data"]["event"]["id"]


def elect(
    monkeypatch,
    db: str,
    quote_ref: int,
    *,
    chosen: str = "granted",
    grant_id: str = GRANT,
    quotes: tuple[str, ...] = (QUOTE,),
    scope: str = SCOPE,
) -> dict:
    """``ledger elect`` answering a fresh ask that names ``grant_id``."""
    asked = ask(monkeypatch, db, grant_id)
    rejected = "withdrawn" if chosen == "granted" else "granted"
    argv = ["--json", "--db", db, "elect", ITEM, "--question", f"Stand {grant_id}?",
            "--chosen", chosen, "--chosen-description", f"{chosen} by the owner",
            "--rejected", rejected, "--scope", scope, "--quote-ref", str(quote_ref),
            "--ask", str(asked)]
    for quote in quotes:
        argv += ["--quote", quote]
    code, out, err = invoke(monkeypatch, argv)
    assert code == 0, err
    return json.loads(out)["data"]["event"]


def raw_elected(monkeypatch, db: str, payload: dict) -> tuple[int, str, str]:
    return invoke(
        monkeypatch,
        ["--json", "--db", db, "append", "--kind", "decision.elected", "--item", ITEM,
         "--payload", json.dumps(payload)],
    )


def raw_payload(quote_ref: int, *, chosen: str = "granted", quotes=(QUOTE,),
                grant_id: str = GRANT) -> dict:
    return {
        "question": f"Stand {grant_id}?",
        "chosen": chosen,
        "chosen_description": f"{chosen} by the owner",
        "rejected": ["withdrawn" if chosen == "granted" else "granted"],
        "scope": SCOPE,
        "quote_ref": quote_ref,
        "quotes": list(quotes),
        "question_id": grant_id,
    }


def grants(monkeypatch, db: str, *argv: str, env=None) -> tuple[int, str, str]:
    return invoke(monkeypatch, ["--db", db, "grants", *argv], env=env)


def check(monkeypatch, db: str, grant_id: str = GRANT) -> tuple[int, dict]:
    code, out, _err = invoke(monkeypatch, ["--json", "--db", db, "grants", "--check", grant_id])
    return code, json.loads(out)


def rows(monkeypatch, db: str) -> dict:
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "grants"])
    assert code == 0, err
    return {row["grant_id"]: row for row in json.loads(out)["data"]}


def event_count(db: str) -> int:
    with open_ledger(db) as lg:
        return len(lg.events())


@pytest.fixture
def ledger_db(tmp_path: Path, monkeypatch) -> str:
    db = _db(tmp_path)
    new_item(monkeypatch, db)
    return db


# ---------------------------------------------------------------------------
# T1: a recorded grant appears with its date and scope
# ---------------------------------------------------------------------------


def test_grants_is_a_discovered_read_verb(ledger_db, monkeypatch) -> None:
    assert "grants" in discover_verbs()
    assert grants_module.VERB.writes is False
    code, out, err = grants(monkeypatch, ledger_db, env={"LEDGER_HARNESS": None})
    assert code == 0, err
    assert out.startswith("Rendered by `ledger grants` from ledger revision ")


def test_a_recorded_grant_appears_with_its_date_and_scope(ledger_db, monkeypatch) -> None:
    spoken = utterance(ledger_db)
    elected = elect(monkeypatch, ledger_db, spoken)
    # The election is recorded on another UTC date than the owner's words.
    assert elected["recorded_at"][:10] != SPOKEN_DATE
    code, out, err = grants(monkeypatch, ledger_db)
    assert code == 0, err
    header = out.splitlines()[0]
    assert "date is the UTC date the quoted utterance (the owner's words) was recorded" in header
    assert "grantor is the operator stamped on that utterance" in header
    assert out.splitlines()[2:] == [
        "| grant id | kind | chosen | scope | grantor | date | quote ref | status | deciding event |",
        "|---|---|---|---|---|---|---|---|---|",
        f"| {GRANT} | grant | granted | {SCOPE} | owner | {SPOKEN_DATE} | {spoken} | STANDING"
        f" | {elected['id']} |",
    ]
    row = rows(monkeypatch, ledger_db)[GRANT]
    assert row["date"] == SPOKEN_DATE and row["scope"] == SCOPE and row["status"] == STANDING


def test_elections_that_are_not_grants_never_show(ledger_db, monkeypatch) -> None:
    spoken = utterance(ledger_db)
    elect(monkeypatch, ledger_db, spoken, grant_id="colour:pick")
    code, out, err = raw_elected(monkeypatch, ledger_db, {
        k: v for k, v in raw_payload(spoken).items() if k != "question_id"})
    assert code == 0, err
    assert rows(monkeypatch, ledger_db) == {}
    code, envelope = check(monkeypatch, ledger_db, "colour:pick")
    assert code == EXIT_REFUSED and envelope["error"]["code"] == "grant_unknown"


def test_a_prefix_counts_only_at_the_start_and_in_its_own_case(ledger_db, monkeypatch) -> None:
    spoken = utterance(ledger_db)
    near_misses = ("x:grant:foo", "pre-rung:y", "x-countersign:w", "Grant:z")
    for question_id in near_misses:
        elect(monkeypatch, ledger_db, spoken, grant_id=question_id)
    assert rows(monkeypatch, ledger_db) == {}
    for question_id in near_misses:
        code, envelope = check(monkeypatch, ledger_db, question_id)
        assert code == EXIT_REFUSED, (question_id, envelope)
        assert envelope["error"]["code"] == "grant_unknown", (question_id, envelope)


def test_the_three_kinds_are_the_three_prefixes(ledger_db, monkeypatch) -> None:
    spoken = utterance(ledger_db)
    for grant_id in ("rung:builder-mc2", "countersign:deploy", "grant:review-round-past-cap"):
        elect(monkeypatch, ledger_db, spoken, grant_id=grant_id)
    found = rows(monkeypatch, ledger_db)
    assert list(found) == ["countersign:deploy", "grant:review-round-past-cap", "rung:builder-mc2"]
    assert [row["kind"] for row in found.values()] == ["countersign", "grant", "rung"]
    assert REVIEW_ROUND_GRANT_ID == "grant:review-round-past-cap"


# ---------------------------------------------------------------------------
# T2: an election with no quote reference is refused
# ---------------------------------------------------------------------------


def test_elect_without_a_quote_ref_is_refused(ledger_db, monkeypatch) -> None:
    asked = ask(monkeypatch, ledger_db)
    before = event_count(ledger_db)
    code, _out, err = invoke(monkeypatch, [
        "--db", ledger_db, "elect", ITEM, "--question", "Stand?", "--chosen", "granted",
        "--chosen-description", "granted", "--rejected", "withdrawn", "--scope", SCOPE,
        "--quote", QUOTE, "--ask", str(asked)])
    assert code == EXIT_USAGE
    assert "--quote-ref" in err
    assert event_count(ledger_db) == before


def test_raw_append_of_an_election_without_a_quote_ref_is_refused(ledger_db, monkeypatch) -> None:
    payload = raw_payload(1)
    del payload["quote_ref"]
    before = event_count(ledger_db)
    code, out, _err = raw_elected(monkeypatch, ledger_db, payload)
    assert code == EXIT_REFUSED
    envelope = json.loads(out)
    assert envelope["error"]["code"] == "validation"
    assert "quote_ref" in envelope["error"]["message"]
    assert event_count(ledger_db) == before


# ---------------------------------------------------------------------------
# T3: --check passes while standing and refuses once withdrawn
# ---------------------------------------------------------------------------


def test_check_passes_then_refuses_once_withdrawn(ledger_db, monkeypatch) -> None:
    spoken = utterance(ledger_db)
    granted = elect(monkeypatch, ledger_db, spoken)
    code, envelope = check(monkeypatch, ledger_db)
    assert code == 0, envelope
    assert envelope["data"]["status"] == STANDING
    assert envelope["data"]["deciding_event_id"] == granted["id"]
    code, out, _err = grants(monkeypatch, ledger_db, "--check", GRANT)
    assert code == 0 and out.startswith(f"STANDING\t{GRANT}\tevent={granted['id']}\t")

    later = utterance(ledger_db, "I withdraw the fixture grant.", key="u-2")
    withdrawn = elect(monkeypatch, ledger_db, later, chosen="withdrawn",
                      quotes=("I withdraw the fixture grant",))
    code, envelope = check(monkeypatch, ledger_db)
    assert code == EXIT_REFUSED
    assert envelope["error"]["code"] == "grant_withdrawn"
    row = rows(monkeypatch, ledger_db)[GRANT]
    assert row["status"] == WITHDRAWN
    assert row["deciding_event_id"] == withdrawn["id"]
    code, _out, err = grants(monkeypatch, ledger_db, "--check", GRANT)
    assert code == EXIT_REFUSED
    assert err.count("\n") == 1 and "WITHDRAWN" in err


def test_a_withdrawal_takes_effect_without_verifying(ledger_db, monkeypatch) -> None:
    # Withdrawing only removes authority: an unverifiable withdrawal still
    # withdraws (the reader fails closed).
    spoken = utterance(ledger_db)
    elect(monkeypatch, ledger_db, spoken)
    code, _out, err = raw_elected(monkeypatch, ledger_db, raw_payload(
        spoken, chosen="withdrawn", quotes=("words nobody said",)))
    assert code == 0, err
    code, envelope = check(monkeypatch, ledger_db)
    assert code == EXIT_REFUSED and envelope["error"]["code"] == "grant_withdrawn"


def test_check_of_an_unknown_grant_is_refused(ledger_db, monkeypatch) -> None:
    code, envelope = check(monkeypatch, ledger_db, "grant:never-recorded")
    assert code == EXIT_REFUSED
    assert envelope["error"]["code"] == "grant_unknown"


# ---------------------------------------------------------------------------
# T4: a granted election that does not verify is UNVERIFIED
# ---------------------------------------------------------------------------


def test_a_grant_quoting_a_generated_utterance_is_unverified(ledger_db, monkeypatch) -> None:
    # ledger elect checks the quote but not who wrote the utterance; the
    # reader requires a person's reply.
    generated = utterance(ledger_db, provenance="generated", key="u-gen")
    elect(monkeypatch, ledger_db, generated)
    assert rows(monkeypatch, ledger_db)[GRANT]["status"] == UNVERIFIED
    code, envelope = check(monkeypatch, ledger_db)
    assert code == EXIT_REFUSED
    assert envelope["error"]["code"] == "grant_unverified"
    assert "not_a_reply" in envelope["error"]["message"]


def test_a_grant_whose_quote_is_not_in_the_utterance_is_unverified(ledger_db, monkeypatch) -> None:
    spoken = utterance(ledger_db)
    # A raw append skips the elect verb's quote check.
    code, _out, err = raw_elected(monkeypatch, ledger_db, raw_payload(
        spoken, quotes=("I grant the fixture permission forever",)))
    assert code == 0, err
    assert rows(monkeypatch, ledger_db)[GRANT]["status"] == UNVERIFIED
    code, envelope = check(monkeypatch, ledger_db)
    assert code == EXIT_REFUSED
    assert envelope["error"]["code"] == "grant_unverified"
    assert "quote_mismatch" in envelope["error"]["message"]


@pytest.mark.parametrize("payload_change", [
    {"quotes": []},
    {"quote_ref": 999},
])
def test_a_grant_with_no_quotes_or_a_dangling_ref_is_unverified(
        ledger_db, monkeypatch, payload_change) -> None:
    spoken = utterance(ledger_db)
    payload = {**raw_payload(spoken), **payload_change}
    code, _out, err = raw_elected(monkeypatch, ledger_db, payload)
    assert code == 0, err
    code, envelope = check(monkeypatch, ledger_db)
    assert code == EXIT_REFUSED and envelope["error"]["code"] == "grant_unverified"


def test_a_grant_quoting_an_omitted_prompt_pointer_is_unverified(ledger_db, monkeypatch) -> None:
    pointer = utterance(ledger_db, f"[omitted] {QUOTE}", key="u-ptr",
                        extra={"omitted_bytes": 9000, "omitted_sha256": "a" * 64})
    code, _out, err = raw_elected(monkeypatch, ledger_db, raw_payload(pointer))
    assert code == 0, err
    code, envelope = check(monkeypatch, ledger_db)
    assert code == EXIT_REFUSED and envelope["error"]["code"] == "grant_unverified"


def test_the_reader_reuses_the_elect_quote_rule() -> None:
    assert grants_module.validate_quote_provenance is elect_module.validate_quote_provenance


# ---------------------------------------------------------------------------
# T5: the newest election wins
# ---------------------------------------------------------------------------


def test_a_regrant_after_a_withdrawal_stands_again(ledger_db, monkeypatch) -> None:
    spoken = utterance(ledger_db)
    elect(monkeypatch, ledger_db, spoken)
    elect(monkeypatch, ledger_db, spoken, chosen="withdrawn", quotes=("Yes",))
    regranted = elect(monkeypatch, ledger_db, spoken)
    code, envelope = check(monkeypatch, ledger_db)
    assert code == 0, envelope
    assert envelope["data"]["status"] == STANDING
    assert envelope["data"]["deciding_event_id"] == regranted["id"]


def test_an_unverified_regrant_never_stands_on_an_older_verified_grant(
        ledger_db, monkeypatch) -> None:
    spoken = utterance(ledger_db)
    elect(monkeypatch, ledger_db, spoken)
    code, _out, err = raw_elected(monkeypatch, ledger_db, raw_payload(
        spoken, quotes=("words nobody said",)))
    assert code == 0, err
    code, envelope = check(monkeypatch, ledger_db)
    assert code == EXIT_REFUSED and envelope["error"]["code"] == "grant_unverified"


# ---------------------------------------------------------------------------
# T6: deterministic output
# ---------------------------------------------------------------------------


def test_two_runs_print_identical_bytes(ledger_db, monkeypatch) -> None:
    spoken = utterance(ledger_db)
    elect(monkeypatch, ledger_db, spoken)
    elect(monkeypatch, ledger_db, spoken, grant_id="rung:fixture")
    generated = utterance(ledger_db, provenance="generated", key="u-gen")
    elect(monkeypatch, ledger_db, generated, grant_id="countersign:fixture")
    for argv in ((), ("--json",)):
        first = invoke(monkeypatch, [*argv, "--db", ledger_db, "grants"])
        second = invoke(monkeypatch, [*argv, "--db", ledger_db, "grants"])
        assert first[0] == 0, first[2]
        assert first == second


class _WriterAfterTheRevisionRead:
    """``lg._conn`` stand-in: right after ``read_grants`` reads the revision,
    ``race`` runs once, before the elections are read."""

    def __init__(self, conn, race):
        self._conn = conn
        self._race = race

    def execute(self, sql, *args):
        cursor = self._conn.execute(sql, *args)
        if "MAX(id)" in sql and self._race is not None:
            race, self._race = self._race, None
            race()
        return cursor

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_an_election_landing_mid_read_never_shows_past_the_named_revision(
        ledger_db, monkeypatch) -> None:
    spoken = utterance(ledger_db)
    granted = elect(monkeypatch, ledger_db, spoken)
    before = event_count(ledger_db)
    raced: list[int] = []

    def race() -> None:
        # Another writer, on its own connection, withdraws the grant and
        # records a new one between the revision read and the elections read.
        with open_ledger(ledger_db, create=False) as writer:
            for n, payload in enumerate((raw_payload(spoken, chosen="withdrawn"),
                                         raw_payload(spoken, grant_id="grant:raced"))):
                raced.append(writer.append(kind="decision.elected", payload=payload,
                                           item_id=ITEM, idempotency_key=f"race-{n}",
                                           harness="pytest").id)

    real_open_db = grants_module.open_db

    def open_db_with_a_racing_writer(ctx, *, write):
        lg = real_open_db(ctx, write=write)
        lg._conn = _WriterAfterTheRevisionRead(lg._conn, race)
        return lg

    monkeypatch.setattr(grants_module, "open_db", open_db_with_a_racing_writer)
    code, out, err = grants(monkeypatch, ledger_db)
    assert code == 0, err
    assert len(raced) == 2 and min(raced) > before
    lines = out.splitlines()
    assert f"from ledger revision {before} " in lines[0]
    assert lines[4:] == [
        f"| {GRANT} | grant | granted | {SCOPE} | owner | {SPOKEN_DATE} | {spoken} | STANDING"
        f" | {granted['id']} |",
    ]
    # The raced writes did land: the next read names them.
    with open_ledger(ledger_db, create=False) as lg:
        revision, found = read_grants(lg)
    assert revision == raced[-1]
    assert [(grant.grant_id, grant.status) for grant in found] == [
        (GRANT, WITHDRAWN), ("grant:raced", STANDING)]


def test_read_grants_names_the_revision_it_read(ledger_db, monkeypatch) -> None:
    spoken = utterance(ledger_db)
    elected = elect(monkeypatch, ledger_db, spoken)
    with open_ledger(ledger_db, create=False) as lg:
        revision, found = read_grants(lg)
    assert revision == elected["id"]
    assert [grant.grant_id for grant in found] == [GRANT]
    code, out, _err = grants(monkeypatch, ledger_db)
    assert f"from ledger revision {revision} " in out.splitlines()[0]
