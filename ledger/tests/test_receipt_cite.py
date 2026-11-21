"""The ``review.grant-cite`` writer: a cite exists only if the past-cap
review-round grant stood in the transaction that wrote it.

The grant is raised and answered as every grant is (an ask carrying the
grant id, the harness's picker capture, ``ledger pick``), through the
election-binding fixtures. The cite's refusals are ``standing()``'s own.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ledger.receipts import write_grant_cite
from ledger.receipts import _RECEIPT_WRITER_CAPABILITY
from ledger.schema import CITED_GRANT_ID
from ledger.store import open_ledger
from ledger.verbs import CliError
from ledger.verbs.grants import REVIEW_ROUND_GRANT_ID, standing

from receipt_helpers import cite_payload, count, invalid, link, new_item, refused
from test_election_binding import (
    captured,
    grant_through_picker,
    new_item as cli_new_item,
    prompt_hook,
    raw_elect,
)

ITEM = "it-pr"
OTHER = "it-other"
URL = "https://github.com/o/r/pull/12"
SCOPE = "round 4 of o/r#12"
GRANT = REVIEW_ROUND_GRANT_ID


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "ledger.db")
    cli_new_item(monkeypatch, path, "it-x")
    with open_ledger(path, create=False) as lg:
        new_item(lg, ITEM)
        new_item(lg, OTHER)
        link(lg, ITEM, "o/r#12")
    return path


def cite(lg, item: str = ITEM, **overrides):
    args = {"repo": "o/r", "pr": 12, "round": 4, "scope": SCOPE,
            "actor": "tester", "harness": "pytest"}
    args.update(overrides)
    return write_grant_cite(lg, item, **args)


def standing_refusal(lg) -> tuple[str, str]:
    with pytest.raises(CliError) as info:
        standing(lg, GRANT)
    return info.value.code, str(info.value)


def test_the_cited_grant_is_the_review_round_grant() -> None:
    assert CITED_GRANT_ID == REVIEW_ROUND_GRANT_ID == "grant:review-round-past-cap"


def test_a_cite_lands_while_the_grant_stands(db, monkeypatch) -> None:
    election = grant_through_picker(monkeypatch, db, GRANT)
    with open_ledger(db, create=False) as lg:
        before = count(lg)
        event = cite(lg)
        assert count(lg) == before + 1
        assert event.kind == "review.grant-cite" and event.item_id == ITEM
        assert event.payload == {
            "grant_id": GRANT, "grant_event_id": election, "repo": "o/r", "pr": 12,
            "round": 4, "scope": SCOPE,
        }
        lg.verify()


def test_an_exact_retry_writes_nothing(db, monkeypatch) -> None:
    grant_through_picker(monkeypatch, db, GRANT)
    with open_ledger(db, create=False) as lg:
        first = cite(lg)
        before = count(lg)
        assert cite(lg) == first
        assert count(lg) == before


def test_a_cite_is_refused_while_the_grant_is_unknown(db) -> None:
    with open_ledger(db, create=False) as lg:
        refused(lg, "grant_unknown",
                f"grant {GRANT!r} is not recorded: no election names it", cite, lg)


def test_a_cite_is_refused_while_the_grant_is_withdrawn(db, monkeypatch) -> None:
    grant_through_picker(monkeypatch, db, GRANT)
    withdrawal = grant_through_picker(monkeypatch, db, GRANT, chosen="withdrawn")
    with open_ledger(db, create=False) as lg:
        refused(lg, "grant_withdrawn",
                f"grant {GRANT!r} is WITHDRAWN: the newest election (event {withdrawal}) "
                "chose 'withdrawn'",
                cite, lg)


def test_a_cite_is_refused_while_the_grant_is_unverified(db, monkeypatch) -> None:
    prompt = captured(monkeypatch, db, prompt_hook("Yes, I grant rounds past the cap."),
                      kind="prompt")
    election = raw_elect(monkeypatch, db, {
        "question": f"Stand {GRANT}?", "chosen": "granted", "chosen_description": "granted",
        "rejected": ["withdrawn"], "scope": "rounds past the cap", "quote_ref": prompt,
        "quotes": ["I grant rounds"], "question_id": GRANT,
    })
    with open_ledger(db, create=False) as lg:
        refused(lg, "grant_unverified",
                f"grant {GRANT!r} is UNVERIFIED: the newest election (event {election}) "
                "does not verify (grant_unasked)",
                cite, lg)


def test_a_cite_on_an_item_that_is_not_the_pull_requests_is_refused(db, monkeypatch) -> None:
    grant_through_picker(monkeypatch, db, GRANT)
    with open_ledger(db, create=False) as lg:
        refused(lg, "receipt_item_mismatch",
                f"pull request {URL} is the item {ITEM}'s, not {OTHER}'s",
                cite, lg, OTHER)
        refused(lg, "pr_item_missing",
                "no item links pull request https://github.com/o/r/pull/13 (rel pr)",
                cite, lg, pr=13)


def test_a_cite_stays_after_a_withdrawal_and_grants_nothing(db, monkeypatch) -> None:
    # The ledger half of design section 10 row 11.
    grant_through_picker(monkeypatch, db, GRANT)
    with open_ledger(db, create=False) as lg:
        written = cite(lg)
    withdrawal = grant_through_picker(monkeypatch, db, GRANT, chosen="withdrawn")
    with open_ledger(db, create=False) as lg:
        assert lg.events(kind="review.grant-cite") == [written]
        assert standing_refusal(lg) == (
            "grant_withdrawn",
            f"grant {GRANT!r} is WITHDRAWN: the newest election (event {withdrawal}) "
            "chose 'withdrawn'",
        )
        refused(lg, "grant_withdrawn",
                f"grant {GRANT!r} is WITHDRAWN: the newest election (event {withdrawal}) "
                "chose 'withdrawn'",
                cite, lg)


def test_a_cite_of_another_grant_id_is_refused_by_the_schema(db, monkeypatch) -> None:
    grant_through_picker(monkeypatch, db, "grant:review-round:o/r#12")
    with open_ledger(db, create=False) as lg:
        for other in ("grant:review-round:o/r#12", "grant:review-round-past-cap ",
                      "grant:x"):
            invalid(lg, "bad-type:grant_id", lg._append_receipt_event,
                    _RECEIPT_WRITER_CAPABILITY, kind="review.grant-cite",
                    payload=cite_payload(grant_id=other), item_id=ITEM,
                    idempotency_key=f"cite:{other}")


@pytest.mark.parametrize("override,reason", [
    ({"scope": "a\nb"}, "not-single-line:scope"),
    ({"scope": ""}, "bad-type:scope"),
    ({"scope": "a‮b"}, "not-display-text:scope"),
    ({"repo": "O/r"}, "bad-type:repo"),
    ({"pr": 0}, "bad-type:pr"),
    ({"round": 0}, "bad-type:round"),
])
def test_a_cite_out_of_shape_is_refused(db, monkeypatch, override, reason) -> None:
    grant_through_picker(monkeypatch, db, GRANT)
    with open_ledger(db, create=False) as lg:
        invalid(lg, reason, cite, lg, **override)


def test_a_scope_over_300_characters_is_refused(db, monkeypatch) -> None:
    with open_ledger(db, create=False) as lg:
        invalid(lg, "too-long:scope", cite, lg, scope="s" * 301,
                message="too-long:scope; max 300, got 301")


def test_the_writer_takes_no_grant_from_the_caller(db) -> None:
    with open_ledger(db, create=False) as lg:
        for name in ("grant_id", "grant_event_id"):
            with pytest.raises(TypeError):
                cite(lg, **{name: 1})
