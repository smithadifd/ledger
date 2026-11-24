"""PR number admission and receipt repository segment boundaries."""
from __future__ import annotations

import json

import pytest

from ledger.loop import link_pull_request, take_item
from ledger.receipts import pr_item, write_merge_done, write_merge_intent, write_receipt
from ledger.schema import ValidationError, validate_payload
from ledger.store import open_ledger
from ledger.verbs import EXIT_REFUSED

from receipt_helpers import (
    DOC, HEAD, cite_payload, fields, link, new_item, receipt_payload, refused,
)
from test_cli import invoke, seed


@pytest.mark.parametrize("stored", ["o/r#{n}", "https://github.com/o/r/pull/{n}"])
@pytest.mark.parametrize("number", [str(2**63), "9" * 4301], ids=["over-cap", "4301-digits"])
def test_invalid_stored_pr_number_does_not_block_other_writes(tmp_path, stored, number):
    with open_ledger(tmp_path / "ledger.db") as lg:
        new_item(lg, "it-bad")
        new_item(lg, "it-good")
        link(lg, "it-bad", stored.format(n=number))
        url = "https://github.com/o/r/pull/12"
        refused(lg, "pr_item_missing", f"no item links pull request {url} (rel pr)",
                pr_item, lg, url)
        refused(lg, "bad_pr_target",
                "a pull request is named by https://github.com/OWNER/REPO/pull/N",
                pr_item, lg, f"https://github.com/o/r/pull/{number}")
        link(lg, "it-good", "o/r#12")
        assert pr_item(lg, url) == "it-good"
        receipt = write_receipt(lg, "it-good", fields(), DOC)
        assert (receipt.kind, receipt.item_id) == ("review.receipt", "it-good")
        merge = {"key": f"merge:o/r#12:{HEAD}", "pr": url, "head": HEAD}
        assert write_merge_intent(lg, "it-good", merge).state == "started"
        assert write_merge_done(lg, "it-good", merge).state == "finished"
        # The loop's duplicate check uses the same stored-link reader.
        new_item(lg, "it-loop")
        take_item(lg, "it-loop", "holder", idempotency_key="managed", claim_key="take")
        assert link_pull_request(lg, "it-loop", "holder", "o/r", 13).created
        lg.verify()


@pytest.mark.parametrize("target", ["#{n}", "o/r#{n}", "https://github.com/o/r/pull/{n}"])
@pytest.mark.parametrize("number", [str(2**63), "9" * 4301], ids=["over-cap", "4301-digits"])
def test_generic_pr_link_refuses_out_of_range_number(tmp_path, monkeypatch, target, number):
    db = str(tmp_path / "ledger.db")
    seed(monkeypatch, db)
    with open_ledger(db) as lg:
        before = lg.events()
    code, out, err = invoke(monkeypatch, [
        "--json", "--db", db, "link", "it-1", "--rel", "pr", "--target", target.format(n=number),
    ])
    assert code == EXIT_REFUSED, (out, err)
    assert json.loads(out)["error"]["code"] == "bad_pr_target"
    with open_ledger(db) as lg:
        assert lg.events() == before, "an invalid PR number must write nothing"


@pytest.mark.parametrize("target", ["#{n}", "o/r#{n}", "https://github.com/o/r/pull/{n}"])
@pytest.mark.parametrize("number", [1, 2**63 - 1])
def test_generic_pr_link_accepts_receipt_number_boundaries(tmp_path, monkeypatch, target, number):
    db = str(tmp_path / "ledger.db")
    seed(monkeypatch, db)
    code, out, err = invoke(monkeypatch, [
        "--json", "--db", db, "link", "it-1", "--rel", "pr", "--target", target.format(n=number),
    ])
    assert code == 0, (out, err)
    assert json.loads(out)["data"]["event"]["payload"]["target"] == target.format(n=number)
    if not target.startswith("#"):
        with open_ledger(db) as lg:
            assert pr_item(lg, f"https://github.com/o/r/pull/{number}") == "it-1"


@pytest.mark.parametrize("repo", [
    "./r", "../r", ".../r", "o/.", "o/..", "o/...", "../..",
])
@pytest.mark.parametrize("consumer", ["review.receipt", "review.grant-cite", "loop"])
def test_dot_only_repo_segments_refuse_in_schema_and_loop(tmp_path, repo, consumer):
    with open_ledger(tmp_path / "ledger.db") as lg:
        new_item(lg, "it-loop")
        take_item(lg, "it-loop", "holder", idempotency_key="managed", claim_key="take")
        before = lg.events()
        with pytest.raises(ValidationError, match="^bad-type:repo$"):
            if consumer == "loop":
                link_pull_request(lg, "it-loop", "holder", repo, 12)
            elif consumer == "review.receipt":
                validate_payload(consumer, receipt_payload(repo=repo))
            else:
                validate_payload(consumer, cite_payload(repo=repo))
        assert lg.events() == before, "an invalid repository must write nothing"


@pytest.mark.parametrize("repo", ["a.b/c.d", "o/my.repo", ".a/b.", "_/--"])
def test_names_containing_non_dot_characters_remain_valid(tmp_path, repo):
    with open_ledger(tmp_path / "ledger.db") as lg:
        new_item(lg, "it-loop")
        take_item(lg, "it-loop", "holder", idempotency_key="managed", claim_key="take")
        assert link_pull_request(lg, "it-loop", "holder", repo, 12).created
        receipt = write_receipt(lg, "it-loop", fields(repo=repo), DOC)
        assert receipt.payload["repo"] == repo
        payload = cite_payload(repo=repo)
        assert validate_payload("review.grant-cite", payload) == payload
