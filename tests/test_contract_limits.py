"""The contract page and `resume --brief` state what a stop and a new item accept.

A refusal now says its limit and the allowed ref forms; the page and the short
form say the same up front. Every number, ref example and item type below is
read from the code (the schema and the ``new`` verb), so the text cannot drift
from what is enforced.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ledger.schema import ITEM_TYPES, REF_FORMS, SCHEMAS
from ledger.verbs import discover_verbs
from ledger.verbs.resume import SHORT_FORM

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "docs" / "agent-contract.md").read_text(encoding="utf-8")
STOPPED = SCHEMAS[("item.stopped", 1)]
POINT_MAX = STOPPED["required"]["point"]["max_len"]
SKILLS_MAX = STOPPED["optional"]["skills"]["max_len"]
REFS_MAX = STOPPED["optional"]["refs"]["max_len"]
REF_MAX = STOPPED["optional"]["refs"]["items"]["max_len"]
URL_ROUTE = "ledger link ITEM --rel url --target URL"

TEXTS = {"page": PAGE, "short form": SHORT_FORM}


def squash(text: str) -> str:
    return " ".join(text.split())


def test_the_limits_in_the_code_are_the_ones_this_test_expects() -> None:
    # If a limit changes, change it in the page and the short form, then here.
    assert (POINT_MAX, SKILLS_MAX, REFS_MAX, REF_MAX) == (600, 12, 20, 300)


@pytest.mark.parametrize("where", TEXTS)
def test_the_point_limit_is_stated(where: str) -> None:
    text = squash(TEXTS[where])
    assert f"1 to {POINT_MAX} characters" in text
    assert f"at most {SKILLS_MAX}" in text
    assert f"at most {REFS_MAX}" in text
    assert f"at most {REF_MAX} " in text


@pytest.mark.parametrize("where", TEXTS)
def test_every_ref_form_has_its_example(where: str) -> None:
    text = TEXTS[where]
    for form, example in REF_FORMS:
        needle = f"`{example}`" if where == "page" else example
        assert needle in text, f"the {where} lacks the {form!r} example {example!r}"


@pytest.mark.parametrize("where", TEXTS)
def test_the_url_route_is_stated(where: str) -> None:
    assert URL_ROUTE in squash(TEXTS[where])


@pytest.mark.parametrize("where", TEXTS)
def test_every_item_type_new_takes_is_stated(where: str) -> None:
    text = TEXTS[where]
    for item_type in ITEM_TYPES:
        needle = f"`{item_type}`" if where == "page" else item_type
        assert needle in text, f"the {where} lacks the item type {item_type!r}"
    assert "no `task`" in text or "no task" in text


def test_the_new_verb_takes_exactly_the_listed_types() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    discover_verbs()["new"].add_arguments(parser)
    choices = next(a.choices for a in parser._actions if a.dest == "type")
    assert tuple(choices) == ITEM_TYPES


def test_the_short_form_stays_short() -> None:
    assert len(SHORT_FORM.splitlines()) < 40


def test_the_short_form_and_page_name_the_same_point_limit() -> None:
    page = re.search(r"1 to (\d+) characters", PAGE)
    short = re.search(r"point is 1 to (\d+) characters", SHORT_FORM)
    assert page and short
    assert page.group(1) == short.group(1) == str(POINT_MAX)
