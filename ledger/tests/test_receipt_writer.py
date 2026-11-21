"""The ``review.receipt`` writer (ledger/receipts.py): its payload and its rules.

Every refusal asserts the refusal's code (or schema reason) and its message,
and that nothing was written. ITEM holds the pr link for o/r pull request 12.
"""
from __future__ import annotations

import base64
import inspect
import json
import threading
from pathlib import Path

import pytest

import ledger.receipts as receipts_module
from ledger.receipts import write_receipt
from ledger.schema import DOC_INLINE_LIMIT, ValidationError
from ledger.store import open_ledger
from ledger.verbs import CliError
from ledger.verbs.doc import attach_inline_in_transaction

from receipt_helpers import (
    BASE,
    DOC,
    DOC2,
    DROP,
    HEAD,
    HEAD2,
    ITEM,
    OTHER,
    STAMPS,
    URL,
    count,
    crashed,
    fields,
    invalid,
    link,
    new_item,
    refused,
    sha,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def lg(tmp_path: Path):
    ledger = open_ledger(tmp_path / "ledger.db")
    new_item(ledger, ITEM)
    new_item(ledger, OTHER)
    link(ledger, ITEM, "o/r#12")
    try:
        yield ledger
    finally:
        ledger.close()


def write(lg, payload=None, document=DOC, item=ITEM, **stamps):
    return write_receipt(lg, item, fields() if payload is None else payload, document,
                         **(stamps or STAMPS))


def docs(lg, item=ITEM):
    return lg.events(item_id=item, kind="doc.attached")


# ---- accept ------------------------------------------------------------------


def test_a_round_one_receipt_lands_with_its_document_in_one_write(lg) -> None:
    before = count(lg)
    event = write(lg)
    assert count(lg) == before + 2
    (document,) = docs(lg)
    assert event.kind == "review.receipt" and event.item_id == ITEM
    assert document.id == event.id - 1
    assert document.payload["role"] == "review-receipt"
    assert document.payload["mime"] == "application/json"
    assert base64.b64decode(document.payload["content_b64"]) == DOC
    # doc_sha256 is the digest the ledger computed from the bytes.
    assert event.payload["doc_sha256"] == document.payload["sha256"] == sha(DOC)
    assert event.payload == {**fields(), "doc_sha256": sha(DOC)}
    assert event.actor == "tester" and event.harness == "pytest"
    lg.verify()


def test_the_writer_takes_no_digest_from_the_caller(lg) -> None:
    assert "doc_sha256" not in inspect.signature(write_receipt).parameters
    refused(
        lg, "receipt_doc_sha256_given",
        "doc_sha256 is computed by the ledger from the document bytes; do not pass it",
        write, lg, fields(doc_sha256=sha(DOC)),
    )


def test_round_two_with_its_prior_lands(lg) -> None:
    first = write(lg)
    second = write(lg, fields(round=2, prior_receipt=first.id), DOC2)
    assert second.payload["prior_receipt"] == first.id
    lg.verify()


def test_a_crashed_receipt_with_no_served_model_lands(lg) -> None:
    event = write(lg, crashed())
    assert "served_model" not in event.payload and "open_bugs" not in event.payload
    assert event.payload["verdict"] == "crashed"


def test_an_unparseable_receipt_with_a_served_model_and_family_lands(lg) -> None:
    event = write(lg, crashed(verdict="unparseable", served_model="gpt-x", family="openai"))
    assert event.payload["family"] == "openai"


def test_a_carried_finding_lands(lg) -> None:
    first = write(lg, fields(open_bugs=1, findings=[{"id": "f.py:3#x", "cell": "block"}]))
    second = write(lg, fields(round=2, prior_receipt=first.id, open_bugs=1, findings=[
        {"id": "f.py:3#x", "cell": "block", "carried_from": first.id},
        {"id": "g.py:9#y", "cell": "nit"},
    ]), DOC2)
    assert second.payload["findings"][0]["carried_from"] == first.id


def test_a_superseding_sibling_lands(lg) -> None:
    first = write(lg)
    second = write(lg, fields(supersedes=first.id), DOC2)
    assert second.payload["supersedes"] == first.id
    third = write(lg, fields(supersedes=second.id), b'{"third": 3}')
    assert third.payload["supersedes"] == second.id


def test_another_head_starts_its_own_chain(lg) -> None:
    write(lg)
    event = write(lg, fields(head=HEAD2), DOC2)
    assert "supersedes" not in event.payload


def test_an_exact_retry_returns_the_stored_event_and_writes_nothing(lg) -> None:
    first = write(lg)
    before = count(lg)
    again = write(lg)
    assert again == first
    assert count(lg) == before


def test_an_exact_retry_is_recognised_before_the_supersedes_rule(lg) -> None:
    # Retrying the superseding sibling: by rule (f) alone it would have to
    # supersede itself, so the retry must be recognised first.
    first = write(lg)
    sibling = write(lg, fields(supersedes=first.id), DOC2)
    before = count(lg)
    assert write(lg, fields(supersedes=first.id), DOC2) == sibling
    assert count(lg) == before


def test_two_writers_racing_one_receipt_write_it_once(lg) -> None:
    # Each writer holds its own connection; the second waits on the first's
    # write lock and then finds the stored receipt under its key.
    path = lg.path
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def writer() -> None:
        try:
            with open_ledger(path, create=False) as own:
                barrier.wait(timeout=10)
                results.append(write(own))
        except BaseException as exc:  # reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    assert len(results) == 2 and results[0] == results[1]
    assert count(lg, "review.receipt") == 1
    assert len(docs(lg)) == 1


def test_two_writers_racing_different_receipts_at_one_head_land_one(lg) -> None:
    # The loser sees the winner's receipt inside its own transaction and is
    # refused receipt_supersedes_required; it never lands a second head.
    path = lg.path
    barrier = threading.Barrier(2)
    outcomes: list = []

    def writer(document: bytes) -> None:
        try:
            with open_ledger(path, create=False) as own:
                barrier.wait(timeout=10)
                outcomes.append(("ok", write(own, document=document).id))
        except CliError as exc:
            outcomes.append((exc.code, None))

    threads = [threading.Thread(target=writer, args=(document,)) for document in (DOC, DOC2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(code for code, _id in outcomes) == ["ok", "receipt_supersedes_required"]
    assert count(lg, "review.receipt") == 1
    assert len(docs(lg)) == 1


# ---- 6(a): the item ----------------------------------------------------------


def test_a_receipt_on_another_item_is_refused(lg) -> None:
    refused(lg, "receipt_item_mismatch",
            f"pull request {URL} is the item {ITEM}'s, not {OTHER}'s",
            write, lg, item=OTHER)


def test_a_receipt_for_an_unlinked_pull_request_is_refused(lg) -> None:
    refused(lg, "pr_item_missing",
            "no item links pull request https://github.com/o/r/pull/13 (rel pr)",
            write, lg, fields(pr=13))


def test_a_receipt_for_an_ambiguous_pull_request_is_refused(lg) -> None:
    link(lg, OTHER, URL)
    refused(lg, "pr_item_ambiguous",
            f"pull request {URL} is linked (rel pr) from more than one item: {OTHER}, {ITEM}",
            write, lg)


# ---- 6(b): the document ------------------------------------------------------

NOT_JSON = "the receipt document is not a UTF-8 JSON object"


@pytest.mark.parametrize("document", [
    b"not json", b"[1, 2]", b'"text"', b"1", b"null", b"\xff\xfe{}", b'{"a": NaN}',
    b'{"a": Infinity}', b"", b'{"a": 1',
])
def test_a_document_that_is_not_a_json_object_is_refused(lg, document) -> None:
    refused(lg, "receipt_doc_not_json", NOT_JSON, write, lg, document=document)


def test_a_document_that_is_not_bytes_is_refused(lg) -> None:
    refused(lg, "receipt_doc_not_json", "the receipt document must be bytes",
            write, lg, document='{"a": 1}')


def test_a_document_over_the_inline_limit_is_refused(lg) -> None:
    document = b'{"a": "' + b"x" * DOC_INLINE_LIMIT + b'"}'
    refused(lg, "receipt_doc_too_large",
            f"the receipt document is {len(document)} bytes, over the "
            f"{DOC_INLINE_LIMIT}-byte inline limit",
            write, lg, document=document)


def test_a_document_at_the_inline_limit_lands(lg) -> None:
    document = b'{"a": "' + b"x" * (DOC_INLINE_LIMIT - 10) + b'"}'
    assert len(document) == DOC_INLINE_LIMIT - 1
    event = write(lg, document=document)
    assert event.payload["doc_sha256"] == sha(document)


def test_an_identical_review_receipt_document_on_the_item_is_reused(lg, monkeypatch, tmp_path) -> None:
    # O9: ledger doc add --role review-receipt is left as released. The
    # document alone is no receipt, and the writer reuses it.
    from test_election_binding import cli

    path = tmp_path / "receipt.json"
    path.write_bytes(DOC)
    code, env = cli(monkeypatch, str(lg.path), "doc", "add", ITEM, str(path),
                    "--role", "review-receipt")
    assert code == 0, env
    assert count(lg, "review.receipt") == 0
    (added,) = docs(lg)
    before = count(lg)
    event = write(lg)
    assert count(lg) == before + 1
    assert docs(lg) == [added]
    assert event.payload["doc_sha256"] == added.payload["sha256"]


# ---- 6(c): one transaction ---------------------------------------------------


def test_a_refusal_after_the_document_step_leaves_no_document(lg) -> None:
    before = count(lg)
    refused(lg, "receipt_prior_mismatch",
            f"prior_receipt 999 is not a review.receipt on {ITEM} for o/r#12, lane sol, round 1",
            write, lg, fields(round=2, prior_receipt=999))
    assert docs(lg) == []
    assert count(lg) == before


# ---- 6(d): prior_receipt -----------------------------------------------------


def _prior(lg, prior: int, **extra) -> None:
    payload = fields(round=2, prior_receipt=prior, **extra)
    refused(lg, "receipt_prior_mismatch",
            f"prior_receipt {prior} is not a review.receipt on {ITEM} for o/r#12, "
            f"lane {payload['lane']}, round 1",
            write, lg, payload, DOC2)


def test_a_prior_on_another_lane_is_refused(lg) -> None:
    first = write(lg, fields(lane="codex"))
    _prior(lg, first.id)


def test_a_prior_that_is_not_one_round_lower_is_refused(lg) -> None:
    first = write(lg)
    second = write(lg, fields(round=2, prior_receipt=first.id), DOC2)
    refused(lg, "receipt_prior_mismatch",
            f"prior_receipt {first.id} is not a review.receipt on {ITEM} for o/r#12, "
            "lane sol, round 2",
            write, lg, fields(round=3, prior_receipt=first.id), b'{"r": 3}')
    assert second.id


def test_a_prior_that_is_not_a_receipt_is_refused(lg) -> None:
    write(lg)
    (document,) = docs(lg)
    _prior(lg, document.id)


def test_a_prior_on_another_item_is_refused(lg) -> None:
    link(lg, OTHER, "o/r#13")
    other = write(lg, fields(pr=13), item=OTHER)
    refused(lg, "receipt_prior_mismatch",
            f"prior_receipt {other.id} is not a review.receipt on {ITEM} for o/r#12, "
            "lane sol, round 1",
            write, lg, fields(round=2, prior_receipt=other.id), DOC2)


# ---- 6(e): carried_from ------------------------------------------------------


def _carried(lg, carried: int) -> None:
    payload = fields(open_bugs=1, findings=[
        {"id": "f.py:3#x", "cell": "block", "carried_from": carried}])
    refused(lg, "receipt_carried_mismatch",
            f"findings[0].carried_from {carried} is not a review.receipt on {ITEM} for o/r#12",
            write, lg, payload, DOC2)


def test_a_carried_finding_from_a_non_receipt_is_refused(lg) -> None:
    _carried(lg, 1)


def test_a_carried_finding_from_another_pull_request_is_refused(lg) -> None:
    link(lg, OTHER, "o/r#13")
    other = write(lg, fields(pr=13), item=OTHER)
    _carried(lg, other.id)


def test_a_carried_finding_from_a_missing_event_is_refused(lg) -> None:
    _carried(lg, 999)


# ---- 6(f): the supersedes chain ----------------------------------------------


def test_a_second_receipt_at_the_same_head_must_supersede(lg) -> None:
    first = write(lg)
    refused(lg, "receipt_supersedes_required",
            f"receipt {first.id} already records o/r#12, lane sol, round 1 at this head; "
            f"pass supersedes {first.id}",
            write, lg, fields(), DOC2)


def test_supersedes_must_name_the_newest(lg) -> None:
    first = write(lg)
    second = write(lg, fields(supersedes=first.id), DOC2)
    refused(lg, "receipt_supersedes_mismatch",
            f"supersedes {first.id} is not the newest receipt for o/r#12, lane sol, "
            f"round 1 at this head ({second.id})",
            write, lg, fields(supersedes=first.id), b'{"third": 3}')


def test_supersedes_with_nothing_to_supersede_is_refused(lg) -> None:
    write(lg, fields(lane="codex"))
    refused(lg, "receipt_supersedes_mismatch",
            f"supersedes 1: no receipt on {ITEM} records o/r#12, lane sol, round 1 at this head",
            write, lg, fields(supersedes=1), DOC2)


# ---- 6(h): credential shapes -------------------------------------------------

TOKEN = "ghp_" + "A" * 36
KEY = "sk-" + "a" * 24


def test_a_credential_shape_in_the_document_is_refused(lg) -> None:
    document = json.dumps({"quote": TOKEN}).encode()
    refused(lg, "doc_credential_shape",
            "the document holds a credential shape (github-token at byte 11); "
            "nothing was written",
            write, lg, document=document)


@pytest.mark.parametrize("override", [
    {"served_model": KEY},
    {"requested_model": KEY},
])
def test_a_credential_shape_in_the_payload_is_refused(lg, override) -> None:
    refused(lg, "receipt_credential_shape",
            "a field of the event holds a credential shape (openai-key); the text is not "
            "shown, and nothing was written",
            write, lg, fields(**override))


def test_a_credential_shape_in_an_attribution_stamp_is_refused(lg) -> None:
    refused(lg, "receipt_credential_shape",
            "a field of the event holds a credential shape (github-token); the text is not "
            "shown, and nothing was written",
            write, lg, actor=TOKEN, harness="pytest")


def test_an_attribution_stamp_that_cannot_be_scanned_is_refused(lg) -> None:
    refused(lg, "receipt_credential_shape",
            "a field of the event is not UTF-8 text, so it cannot be scanned for "
            "credential shapes; nothing was written",
            write, lg, actor="a\ud800b", harness="pytest")


# ---- item 5: the payload -----------------------------------------------------


@pytest.mark.parametrize("key", ["diff", "row", "extra"])
def test_an_unknown_key_is_refused(lg, key) -> None:
    # O4: version 1 carries the pull-request shape only.
    invalid(lg, f"unknown-key:{key}", write, lg, fields(**{key: "x"}))


@pytest.mark.parametrize("override,reason", [
    ({"repo": "O/r"}, "bad-type:repo"),
    ({"repo": "o/R"}, "bad-type:repo"),
    ({"repo": "o"}, "bad-type:repo"),
    ({"repo": "o/r/x"}, "bad-type:repo"),
    ({"repo": "o r/r"}, "bad-type:repo"),
    ({"pr": 0}, "bad-type:pr"),
    ({"pr": "12"}, "bad-type:pr"),
    ({"pr": True}, "bad-type:pr"),
    ({"head": "A" * 40}, "bad-type:head"),
    ({"head": "a" * 39}, "bad-type:head"),
    ({"head": "a" * 41}, "bad-type:head"),
    ({"base": "g" * 40}, "bad-type:base"),
    ({"round": 0}, "bad-type:round"),
    ({"counts_toward_cap": 1}, "bad-type:counts_toward_cap"),
    ({"counts_toward_cap": DROP}, "missing-key:counts_toward_cap"),
    ({"lane": "Sol"}, "bad-type:lane"),
    ({"lane": "-sol"}, "bad-type:lane"),
    ({"lane": "s ol"}, "bad-type:lane"),
    ({"family": "Open"}, "bad-type:family"),
    ({"tier": ""}, "bad-type:tier"),
    ({"requested_model": DROP}, "missing-key:requested_model"),
    ({"requested_model": ""}, "bad-type:requested_model"),
    ({"served_model": "a\nb"}, "not-single-line:served_model"),
    ({"served_model": "a​b"}, "not-display-text:served_model"),
    ({"served_model": "a\x1b[2Jb"}, "not-display-text:served_model"),
    ({"served_model": " gpt"}, "not-display-text:served_model"),
    ({"verdict": "pass"}, "bad-type:verdict"),
    ({"open_bugs": -1}, "bad-type:open_bugs"),
    ({"findings": [{"id": "f.py#x", "cell": "nit"}]}, "bad-type:findings[0].id"),
    ({"findings": [{"id": "f.py:0#x", "cell": "nit"}]}, "bad-type:findings[0].id"),
    ({"findings": [{"id": "f.py:3#", "cell": "nit"}]}, "bad-type:findings[0].id"),
    ({"findings": [{"id": "f .py:3#x", "cell": "nit"}]}, "bad-type:findings[0].id"),
    ({"findings": [{"id": "f.py:3#x\n", "cell": "nit"}]}, "not-single-line:findings[0].id"),
    ({"findings": [{"id": "f.py:3#x", "cell": "bug"}]}, "bad-type:findings[0].cell"),
    ({"findings": [{"id": "f.py:3#x"}]}, "missing-key:findings[0].cell"),
    ({"findings": [{"id": "f.py:3#x", "cell": "nit", "note": "n"}]},
     "unknown-key:findings[0].note"),
    ({"findings": [{"id": "f.py:3#x", "cell": "nit", "carried_from": 0}]},
     "bad-type:findings[0].carried_from"),
    ({"prior_receipt": "1", "round": 2}, "bad-type:prior_receipt"),
    ({"supersedes": 0}, "bad-type:supersedes"),
    ({"changed_files_sha256": "C" * 64}, "bad-type:changed_files_sha256"),
])
def test_a_field_out_of_shape_is_refused(lg, override, reason) -> None:
    invalid(lg, reason, write, lg, fields(**override))


def test_a_lane_over_64_characters_is_refused(lg) -> None:
    invalid(lg, "too-long:lane", write, lg, fields(lane="a" * 65),
            message="too-long:lane; max 64, got 65")


def test_a_served_model_over_200_characters_is_refused(lg) -> None:
    invalid(lg, "too-long:served_model", write, lg, fields(served_model="m" * 201),
            message="too-long:served_model; max 200, got 201")


def test_findings_are_bounded(lg) -> None:
    findings = [{"id": f"f.py:{n}#x", "cell": "nit"} for n in range(1, 202)]
    invalid(lg, "too-long:findings", write, lg, fields(findings=findings),
            message="too-long:findings; max 200, got 201")
    event = write(lg, fields(findings=findings[:200]))
    assert len(event.payload["findings"]) == 200


def test_finding_ids_are_unique(lg) -> None:
    invalid(lg, "receipt-finding-duplicate:findings[1].id", write, lg, fields(findings=[
        {"id": "f.py:3#x", "cell": "nit"}, {"id": "f.py:3#x", "cell": "harden"}]))


def test_round_two_needs_a_prior(lg) -> None:
    invalid(lg, "receipt-prior-required", write, lg, fields(round=2))


def test_round_one_takes_no_prior(lg) -> None:
    first = write(lg)
    invalid(lg, "receipt-prior-forbidden", write, lg,
            fields(prior_receipt=first.id, supersedes=first.id), DOC2)


@pytest.mark.parametrize("verdict", ["approved", "changes_requested"])
@pytest.mark.parametrize("key", ["served_model", "family", "open_bugs", "findings"])
def test_a_counted_verdict_needs_its_counts(lg, verdict, key) -> None:
    # 5(a)
    invalid(lg, f"receipt-counts-required:{key}", write, lg,
            fields(verdict=verdict, **{key: DROP}))


def test_open_bugs_is_the_number_of_block_findings(lg) -> None:
    # 5(a)
    findings = [{"id": "f.py:3#x", "cell": "block"}, {"id": "f.py:4#y", "cell": "harden"},
                {"id": "f.py:5#z", "cell": "nit"}]
    invalid(lg, "receipt-open-bugs-mismatch", write, lg,
            fields(verdict="changes_requested", open_bugs=0, findings=findings))
    invalid(lg, "receipt-open-bugs-mismatch", write, lg,
            fields(verdict="changes_requested", open_bugs=2, findings=findings))
    event = write(lg, fields(verdict="changes_requested", open_bugs=1, findings=findings))
    assert event.payload["open_bugs"] == 1


@pytest.mark.parametrize("verdict", ["unparseable", "crashed"])
@pytest.mark.parametrize("key,value", [("open_bugs", 0), ("findings", [])])
def test_an_uncounted_verdict_carries_no_counts(lg, verdict, key, value) -> None:
    # 5(b): a reader testing open_bugs == 0 cannot read a crash as clean.
    invalid(lg, f"receipt-counts-forbidden:{key}", write, lg,
            crashed(verdict=verdict, **{key: value}))


@pytest.mark.parametrize("verdict", ["unparseable", "crashed"])
def test_an_uncounted_verdict_has_a_family_exactly_with_a_served_model(lg, verdict) -> None:
    # 5(b)
    invalid(lg, "receipt-family-needs-served-model", write, lg,
            crashed(verdict=verdict, family="openai"))
    invalid(lg, "receipt-served-model-needs-family", write, lg,
            crashed(verdict=verdict, served_model="gpt-x"))


# ---- O6: imported_from -------------------------------------------------------

STORE = "/receipts/o-r-12/sol-r1.json"
GOOD_IMPORT = {"store": STORE, "sha256": "e" * 64}


def test_imported_from_is_absent_by_default_and_lands_when_well_formed(lg) -> None:
    assert "imported_from" not in write(lg).payload
    event = write(lg, fields(head=HEAD2, imported_from=GOOD_IMPORT), DOC2)
    assert event.payload["imported_from"] == GOOD_IMPORT


def test_imported_from_store_of_1024_characters_lands(lg) -> None:
    event = write(lg, fields(imported_from={"store": "s" * 1024, "sha256": "e" * 64}))
    assert len(event.payload["imported_from"]["store"]) == 1024


@pytest.mark.parametrize("value,reason,message", [
    ("x", "bad-type:imported_from", None),
    ([GOOD_IMPORT], "bad-type:imported_from", None),
    ({**GOOD_IMPORT, "extra": "x"}, "unknown-key:imported_from.extra", None),
    ({"sha256": "e" * 64}, "missing-key:imported_from.store", None),
    ({"store": STORE}, "missing-key:imported_from.sha256", None),
    ({"store": "", "sha256": "e" * 64}, "bad-type:imported_from.store", None),
    ({"store": "s" * 1025, "sha256": "e" * 64}, "too-long:imported_from.store",
     "too-long:imported_from.store; max 1024, got 1025"),
    ({"store": "a\nb", "sha256": "e" * 64}, "not-single-line:imported_from.store", None),
    ({"store": "a\rb", "sha256": "e" * 64}, "not-single-line:imported_from.store", None),
    ({"store": "a\x00b", "sha256": "e" * 64}, "nul-character:imported_from.store", None),
    ({"store": 7, "sha256": "e" * 64}, "bad-type:imported_from.store", None),
    ({"store": STORE, "sha256": "E" * 64}, "bad-type:imported_from.sha256", None),
    ({"store": STORE, "sha256": "e" * 63}, "bad-type:imported_from.sha256", None),
    ({"store": STORE, "sha256": "e" * 65}, "bad-type:imported_from.sha256", None),
    ({"store": STORE, "sha256": 7}, "bad-type:imported_from.sha256", None),
])
def test_a_malformed_imported_from_is_refused_by_name(lg, value, reason, message) -> None:
    invalid(lg, reason, write, lg, fields(imported_from=value), message=message)


# ---- the document step in verbs/doc.py ----------------------------------------


def test_the_document_step_needs_the_callers_transaction(lg) -> None:
    before = count(lg)
    with pytest.raises(Exception) as info:
        attach_inline_in_transaction(lg, ITEM, DOC, role="review-receipt",
                                     mime="application/json")
    assert type(info.value).__name__ == "LedgerError"
    assert str(info.value) == (
        "attach_inline_in_transaction needs the caller's Ledger.transaction()")
    assert count(lg) == before


@pytest.mark.parametrize("data,size", [
    (b"x" * (DOC_INLINE_LIMIT + 1), DOC_INLINE_LIMIT + 1),
    ("text", "not"),
])
def test_the_document_step_refuses_bytes_it_cannot_store_inline(lg, data, size) -> None:
    def attach():
        with lg.transaction():
            attach_inline_in_transaction(lg, ITEM, data, role="other", mime="text/plain")

    refused(lg, "doc_too_large",
            f"the document is {size} bytes; it must be bytes, at most the "
            f"{DOC_INLINE_LIMIT}-byte inline limit; nothing was written", attach)


def test_the_document_step_refuses_a_missing_item(lg) -> None:
    before = count(lg)

    def attach():
        with lg.transaction():
            attach_inline_in_transaction(lg, "it-none", DOC, role="other", mime="text/plain")

    with pytest.raises(CliError) as info:
        attach()
    assert (info.value.code, str(info.value)) == ("not_found", "item not found: it-none")
    assert count(lg) == before


def test_the_document_step_lands_and_reuses_with_the_callers_appends(lg) -> None:
    with lg.transaction():
        first, created = attach_inline_in_transaction(lg, ITEM, DOC, role="other",
                                                      mime="text/plain", title="t")
        again, created_again = attach_inline_in_transaction(lg, ITEM, DOC, role="other",
                                                            mime="text/plain")
    assert (created, created_again) == (True, False)
    assert again == first
    assert first.idempotency_key == f"doc:{ITEM}:other:{sha(DOC)}"
    assert first.payload["title"] == "t" and first.payload["size"] == len(DOC)


# ---- the module boundary -----------------------------------------------------


def test_the_receipts_module_holds_no_other_writers_capability() -> None:
    source = Path(receipts_module.__file__).read_text(encoding="utf-8")
    for name in ("_DOC_WRITER_CAPABILITY", "_LOOP_WRITER_CAPABILITY",
                 "_SPEC_WRITER_CAPABILITY", "_ENTITY_WRITER_CAPABILITY",
                 "_CAPTURE_WRITER_CAPABILITY"):
        assert name not in source, name


def test_the_receipts_docstring_states_the_boundary() -> None:
    doc = " ".join((receipts_module.__doc__ or "").split())
    assert "In-process callers are trusted; the boundary defended is the CLI/process boundary" in doc
    assert "A ``review-receipt`` document alone is not a receipt" in doc


def test_head_and_base_are_kept_as_given(lg) -> None:
    event = write(lg, fields(base=BASE))
    assert (event.payload["head"], event.payload["base"]) == (HEAD, BASE)


def test_a_schema_refusal_writes_no_document(lg) -> None:
    with pytest.raises(ValidationError):
        write(lg, fields(verdict="pass"))
    assert docs(lg) == []
