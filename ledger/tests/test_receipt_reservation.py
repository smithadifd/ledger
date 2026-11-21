"""The receipt writer's review kinds are reserved by exact name.

``ledger append``, ``append-many``, the library's ``append`` and
``append_many`` and every other internal writer refuse these reserved
review kinds; only the receipt writer's capability opens
``_append_receipt_event``. Each payload here is schema-valid, so the
reservation is the only reason a write is refused.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ledger.loop import _LOOP_WRITER_CAPABILITY
from ledger.receipts import _RECEIPT_WRITER_CAPABILITY
from ledger.schema import ValidationError, validate_payload
from ledger.store import RESERVED_KINDS, open_ledger
from ledger.verbs.capture import _CAPTURE_WRITER_CAPABILITY
from ledger.verbs.doc import _DOC_WRITER_CAPABILITY
from ledger.verbs.entity import _ENTITY_WRITER_CAPABILITY
from ledger.verbs.spec import _SPEC_WRITER_CAPABILITY

from receipt_helpers import (
    ITEM,
    cite_payload,
    count,
    invalid,
    link,
    new_item,
    receipt_payload,
)
from test_election_binding import cli

REPO_ROOT = Path(__file__).resolve().parents[2]
KINDS = {
    "review.receipt": receipt_payload,
    "review.grant-cite": cite_payload,
    "review.merge-intent": lambda: {
        "key": f"merge:example/repo#12:{'a' * 40}",
        "pr": "https://github.com/example/repo/pull/12", "head": "a" * 40,
    },
    "review.merge-done": lambda: {
        "key": f"merge:example/repo#12:{'a' * 40}",
        "pr": "https://github.com/example/repo/pull/12", "head": "a" * 40,
    },
}


@pytest.fixture
def db(tmp_path: Path) -> str:
    path = str(tmp_path / "ledger.db")
    with open_ledger(path) as lg:
        new_item(lg, ITEM)
        link(lg, ITEM, "o/r#12")
    return path


def events(db: str) -> int:
    with open_ledger(db, create=False) as lg:
        return count(lg)


def test_review_kinds_are_reserved_by_exact_name() -> None:
    assert set(KINDS) <= set(RESERVED_KINDS)
    assert not any(kind.startswith("review.") for kind in RESERVED_KINDS if kind not in KINDS)


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_ledger_append_refuses_a_valid_receipt_payload(db, monkeypatch, kind) -> None:
    payload = KINDS[kind]()
    validate_payload(kind, payload)  # valid: the reservation is the only reason
    before = events(db)
    code, env = cli(monkeypatch, db, "append", "--kind", kind, "--item", ITEM,
                    "--payload", json.dumps(payload))
    assert code == 3, env
    assert (env["error"]["code"], env["error"]["message"]) == (
        "validation", f"reserved-kind:{kind}")
    assert events(db) == before


def test_review_recorded_stays_a_generic_kind(db, monkeypatch) -> None:
    before = events(db)
    code, env = cli(monkeypatch, db, "append", "--kind", "review.recorded", "--item", ITEM,
                    "--payload", json.dumps({"round": 1, "verdict": "pass"}))
    assert code == 0, env
    assert events(db) == before + 1


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_append_many_refuses_the_batch_and_writes_no_sibling(db, monkeypatch, kind) -> None:
    before = events(db)
    batch = [
        {"kind": "item.note", "payload": {"text": "a valid sibling"}},
        {"kind": kind, "payload": KINDS[kind]()},
    ]
    code, env = cli(monkeypatch, db, "append-many", ITEM, stdin=json.dumps(batch))
    assert code == 3, env
    assert env["error"]["code"] == "validation"
    assert env["error"]["message"].endswith(f"reserved-kind:{kind}"), env
    assert events(db) == before


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_the_library_append_and_append_many_refuse(db, kind) -> None:
    with open_ledger(db, create=False) as lg:
        spec = {"kind": kind, "payload": KINDS[kind](), "item_id": ITEM,
                "idempotency_key": f"lib:{kind}"}
        invalid(lg, f"reserved-kind:{kind}", lg.append, **spec)
        sibling = {"kind": "item.note", "payload": {"text": "sibling"}, "item_id": ITEM,
                   "idempotency_key": "lib:sibling"}
        invalid(lg, f"reserved-kind:{kind}", lg.append_many, [sibling, spec])


@pytest.mark.parametrize("capability", [
    object(), _LOOP_WRITER_CAPABILITY, _DOC_WRITER_CAPABILITY, _SPEC_WRITER_CAPABILITY,
    None,
])
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_the_receipt_entry_point_needs_the_receipt_writers_capability(db, capability,
                                                                       kind) -> None:
    with open_ledger(db, create=False) as lg:
        invalid(lg, "receipt-writer-capability-required", lg._append_receipt_event,
                capability, kind=kind, payload=KINDS[kind](), item_id=ITEM,
                idempotency_key=f"cap:{kind}")


@pytest.mark.parametrize("kind,payload", [
    ("doc.attached", {"role": "other", "sha256": "e" * 64, "size": 1, "mime": "text/plain",
                      "content_b64": "eA=="}),
    ("loop.done", {"operation": "build", "operation_id": "b1", "intent_event_id": 1}),
    ("item.note", {"text": "a note"}),
    ("loop.managed", {}),
    ("ledger.system", {"system": "home"}),
])
def test_the_receipt_entry_point_writes_only_the_reserved_review_kinds(db, kind, payload) -> None:
    with open_ledger(db, create=False) as lg:
        invalid(lg, f"reserved-kind:{kind}", lg._append_receipt_event,
                _RECEIPT_WRITER_CAPABILITY, kind=kind, payload=payload, item_id=ITEM,
                idempotency_key=f"kind:{kind}")


WRITERS = [
    ("_append_loop_event", _LOOP_WRITER_CAPABILITY),
    ("_append_doc_event", _DOC_WRITER_CAPABILITY),
    ("_append_spec_event", _SPEC_WRITER_CAPABILITY),
    ("_append_entity_event", _ENTITY_WRITER_CAPABILITY),
    ("_append_capture_event", _CAPTURE_WRITER_CAPABILITY),
]


@pytest.mark.parametrize("writer,capability", WRITERS)
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_every_other_internal_writer_refuses_reserved_review_kinds(db, writer, capability, kind) -> None:
    with open_ledger(db, create=False) as lg:
        before = lg.events()
        try:
            result = getattr(lg, writer)(
                capability, kind=kind, payload=KINDS[kind](), item_id=ITEM,
                idempotency_key=f"w:{writer}:{kind}",
            )
        except ValidationError as exc:
            result = exc.reason
        assert result == f"reserved-kind:{kind}"
        assert lg.events() == before


def test_no_verb_writes_either_kind_and_the_registry_is_current() -> None:
    registry = (REPO_ROOT / "ledger" / "verbs" / "_registry.py").read_text(encoding="utf-8")
    assert "receipt" not in registry and "cite" not in registry
    result = subprocess.run(
        [sys.executable, "-m", "ledger.gen_verb_registry", "--check"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
