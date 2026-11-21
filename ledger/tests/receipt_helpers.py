"""Shared fixtures for the review receipt tests (test_receipt_*.py).

ITEM holds the pr link for o/r pull request 12; OTHER is a second item with
no link. ``fields`` builds a valid ``approved`` round-1 payload without
``doc_sha256``; a keyword set to ``DROP`` removes that key.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from ledger.schema import ValidationError
from ledger.store import Ledger
from ledger.verbs import EXIT_REFUSED, CliError

ITEM = "it-pr"
OTHER = "it-other"
REPO = "o/r"
PR = 12
URL = "https://github.com/o/r/pull/12"
HEAD = "a" * 40
HEAD2 = "d" * 40
BASE = "b" * 40
CHANGED = "c" * 64
DOC = b'{"verdict": "approved", "round": 1}'
DOC2 = b'{"verdict": "approved", "round": 1, "rerun": true}'
DROP = object()
STAMPS = {"actor": "tester", "harness": "pytest"}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fields(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "repo": REPO,
        "pr": PR,
        "head": HEAD,
        "base": BASE,
        "round": 1,
        "counts_toward_cap": True,
        "lane": "sol",
        "tier": "full-live",
        "requested_model": "gpt-x",
        "served_model": "gpt-x",
        "family": "openai",
        "verdict": "approved",
        "open_bugs": 0,
        "findings": [],
        "changed_files_sha256": CHANGED,
    }
    for key, value in overrides.items():
        if value is DROP:
            payload.pop(key, None)
        else:
            payload[key] = value
    return payload


def crashed(**overrides: Any) -> dict[str, Any]:
    """A crashed receipt with no served model: no family, no counts."""
    base = {"verdict": "crashed", "served_model": DROP, "family": DROP,
            "open_bugs": DROP, "findings": DROP}
    base.update(overrides)
    return fields(**base)


def new_item(lg: Ledger, item_id: str) -> None:
    lg.append(kind="item.created", payload={"type": "ticket", "title": item_id},
              item_id=item_id, idempotency_key=f"created:{item_id}")


def link(lg: Ledger, item_id: str, target: str, rel: str = "pr") -> None:
    lg.append(kind="item.linked", payload={"rel": rel, "target": target},
              item_id=item_id, idempotency_key=f"link:{item_id}:{rel}:{target}")


def count(lg: Ledger, kind: str | None = None) -> int:
    return len(lg.events(kind=kind))


def refused(lg: Ledger, code: str, message: str, call, *args, **kwargs) -> None:
    """``call`` raises ``CliError`` with exactly ``code`` and ``message``,
    exit 3, writes nothing and leaves no transaction open."""
    before = count(lg)
    with pytest.raises(CliError) as info:
        call(*args, **kwargs)
    got = (info.value.code, str(info.value))
    assert got == (code, message), f"refused {got!r}, expected {(code, message)!r}"
    assert info.value.exit_code == EXIT_REFUSED
    written = count(lg) - before
    assert written == 0, f"the refused call left {written} event(s) written"
    assert not lg._conn.in_transaction


def invalid(lg: Ledger, reason: str, call, *args, message: str | None = None, **kwargs) -> None:
    """``call`` raises ``ValidationError`` whose reason starts with exactly
    ``reason`` (the refusal name and path, before any ``; max ...`` detail)
    and whose message is exactly ``message`` (default: the reason), and
    writes nothing."""
    before = count(lg)
    with pytest.raises(ValidationError) as info:
        call(*args, **kwargs)
    expected = reason if message is None else message
    got = (info.value.reason, str(info.value))
    assert info.value.reason.split(";")[0] == reason, f"refused {got!r}, expected {reason!r}"
    assert got == (expected, expected), f"refused {got!r}, expected {expected!r}"
    written = count(lg) - before
    assert written == 0, f"the refused call left {written} event(s) written"
    assert not lg._conn.in_transaction


def receipt_payload(**overrides: Any) -> dict[str, Any]:
    """A schema-valid stored payload (with doc_sha256)."""
    return {**fields(**overrides), "doc_sha256": sha(DOC)}


def cite_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "grant_id": "grant:review-round-past-cap",
        "grant_event_id": 1,
        "repo": REPO,
        "pr": PR,
        "round": 4,
        "scope": "round 4 of o/r#12",
    }
    payload.update(overrides)
    return payload


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload)
