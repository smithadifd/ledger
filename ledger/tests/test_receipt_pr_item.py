"""The one-item rule: ``pr_item(lg, url)`` in ledger/receipts.py.

A pull request is named only by its full GitHub URL. A stored ``pr`` link
names it in the URL form or the ``owner/repo#N`` form, owner and repo
matched casefolded; a bare ``#N`` and a link of another rel match nothing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ledger.receipts import pr_item
from ledger.store import open_ledger

from receipt_helpers import count, link, new_item, refused

URL = "https://github.com/o/r/pull/12"


@pytest.fixture
def lg(tmp_path: Path):
    ledger = open_ledger(tmp_path / "ledger.db")
    for item_id in ("it-a", "it-b"):
        new_item(ledger, item_id)
    try:
        yield ledger
    finally:
        ledger.close()


def missing(lg, url: str = URL) -> None:
    refused(lg, "pr_item_missing", f"no item links pull request {url} (rel pr)",
            pr_item, lg, url)


def ambiguous(lg, *items: str, url: str = URL) -> None:
    refused(lg, "pr_item_ambiguous",
            f"pull request {url} is linked (rel pr) from more than one item: "
            + ", ".join(sorted(items)),
            pr_item, lg, url)


@pytest.mark.parametrize("target", [
    "o/r#12",
    URL,
    "O/R#12",
    "https://github.com/O/r/pull/12",
])
def test_a_link_in_either_form_and_any_letter_case_resolves(lg, target) -> None:
    link(lg, "it-a", target)
    before = count(lg)
    assert pr_item(lg, URL) == "it-a"
    assert count(lg) == before


def test_a_url_in_another_letter_case_resolves_a_lowercase_link(lg) -> None:
    link(lg, "it-a", "o/r#12")
    assert pr_item(lg, "https://github.com/O/R/pull/12") == "it-a"


def test_two_links_on_one_item_are_one_item(lg) -> None:
    link(lg, "it-a", "o/r#12")
    link(lg, "it-a", URL)
    assert pr_item(lg, URL) == "it-a"


def test_no_link_is_missing(lg) -> None:
    missing(lg)


def test_a_bare_number_matches_nothing(lg) -> None:
    link(lg, "it-a", "#12")
    missing(lg)


def test_a_link_to_another_pull_request_is_missing(lg) -> None:
    link(lg, "it-a", "o/r#13")
    missing(lg)


def test_a_url_link_of_another_rel_is_missing(lg) -> None:
    link(lg, "it-a", URL, rel="url")
    missing(lg)


@pytest.mark.parametrize("stored,url", [
    ("o/r#1", URL),
    ("o/r#12", "https://github.com/o/r/pull/1"),
    ("o/r2#12", URL),
    ("o/r#12", "https://github.com/o/r2/pull/12"),
    ("oo/r#12", URL),
    ("o/r#120", URL),
])
def test_near_numbers_and_names_do_not_match(lg, stored, url) -> None:
    link(lg, "it-a", stored)
    missing(lg, url)


@pytest.mark.parametrize("stored", [
    "o/r#12 ", " o/r#12", "o/r #12", "https://github.com/o/r/pull/12/", "o/r#012",
    "http://github.com/o/r/pull/12", "https://www.github.com/o/r/pull/12",
])
def test_a_stored_target_in_neither_form_matches_nothing(lg, stored) -> None:
    lg.append(kind="item.linked", payload={"rel": "pr", "target": stored},
              item_id="it-a", idempotency_key=f"raw:{stored}")
    missing(lg)


def test_two_items_are_ambiguous(lg) -> None:
    link(lg, "it-a", "o/r#12")
    link(lg, "it-b", URL)
    ambiguous(lg, "it-a", "it-b")


def test_a_closed_item_still_counts(lg) -> None:
    link(lg, "it-a", "o/r#12")
    link(lg, "it-b", "o/r#12")
    lg.append(kind="item.state", payload={"status": "closed"}, item_id="it-b",
              idempotency_key="close-b")
    ambiguous(lg, "it-a", "it-b")


def test_an_item_closed_as_a_duplicate_still_counts(lg) -> None:
    link(lg, "it-a", "o/r#12")
    link(lg, "it-b", "o/r#12")
    link(lg, "it-b", "it-a", rel="duplicate-of")
    lg.append(kind="item.state", payload={"status": "closed"}, item_id="it-b",
              idempotency_key="close-b")
    ambiguous(lg, "it-a", "it-b")


def test_two_items_whose_links_differ_only_in_letter_case_are_ambiguous(lg) -> None:
    link(lg, "it-a", "o/r#12")
    link(lg, "it-b", "O/R#12")
    ambiguous(lg, "it-a", "it-b")


def test_the_ambiguous_message_sorts_the_items(lg) -> None:
    for item_id in ("it-z", "it-c"):
        new_item(lg, item_id)
    for item_id in ("it-z", "it-a", "it-c"):
        link(lg, item_id, "o/r#12")
    ambiguous(lg, "it-a", "it-c", "it-z")


@pytest.mark.parametrize("url", [
    "o/r#12", "#12", "http://github.com/o/r/pull/12", "https://github.com/o/r/pull/12/",
    "https://github.com/o/r/pull/012", "https://github.com/o/r/issues/12",
    " https://github.com/o/r/pull/12", "https://github.com/o/r/pull/12\n", "", 12, None,
])
def test_a_url_out_of_form_is_refused(lg, url) -> None:
    link(lg, "it-a", "o/r#12")
    refused(lg, "bad_pr_target", "a pull request is named by https://github.com/OWNER/REPO/pull/N",
            pr_item, lg, url)
