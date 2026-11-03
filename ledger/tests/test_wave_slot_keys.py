"""``item.created`` v1 carries two optional keys the unattended wave slot reads.

``queue`` is the SoT's ``## Queue <letter>`` key and ``target_repo`` a plain
repo name. Nothing else changes: no other kind accepts them.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ledger import open_ledger
from ledger.schema import ValidationError


@pytest.fixture
def lg(tmp_path: Path):
    ledger = open_ledger(tmp_path / "ledger.db")
    yield ledger
    ledger.close()


def _created(lg, key: str, **extra):
    payload = {"type": "ticket", "title": "t", **extra}
    return lg.append(kind="item.created", item_id=key, payload=payload,
                     idempotency_key=f"k-{key}", harness="fixture")


def test_both_keys_are_accepted_and_stored(lg):
    _created(lg, "EO1", queue="ZZ", target_repo="hoard")
    (event,) = lg.events(kind="item.created")
    assert event.payload["queue"] == "ZZ"
    assert event.payload["target_repo"] == "hoard"


@pytest.mark.parametrize("bad", ["../evil", "..", ".", "a/b", ""])
def test_target_repo_must_be_a_plain_name(lg, bad):
    with pytest.raises(ValidationError):
        _created(lg, "EO2", queue="ZZ", target_repo=bad)


@pytest.mark.parametrize("bad", ["ZZ1", "", "Z-Z"])
def test_queue_must_be_letters(lg, bad):
    with pytest.raises(ValidationError):
        _created(lg, "EO3", queue=bad, target_repo="hoard")


def test_no_other_kind_accepts_the_keys(lg):
    with pytest.raises(ValidationError):
        lg.append(kind="session.closed", payload={"outcome": "x", "queue": "ZZ"},
                  idempotency_key="k-sc", harness="fixture")


def test_the_keys_stay_optional(lg):
    _created(lg, "EO4")
    (event,) = lg.events(kind="item.created")
    assert "queue" not in event.payload and "target_repo" not in event.payload
