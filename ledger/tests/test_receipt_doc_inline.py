"""The receipt writer reuses an identical ``review-receipt`` document only
when its bytes are stored inline.

A by-path document's file can be missing, now or later, so a receipt that
reused one would name a digest whose bytes the ledger does not hold. Both
the writer and its document step refuse it ``doc_not_inline`` and write
nothing. ITEM holds the pr link for o/r pull request 12.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ledger.receipts import write_receipt
from ledger.store import open_ledger
from ledger.verbs.doc import (
    attach_document,
    attach_inline_in_transaction,
    document_root,
)

from receipt_helpers import DOC, ITEM, OTHER, STAMPS, count, fields, link, new_item, refused


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


def _by_path(lg, data: bytes = DOC, role: str = "review-receipt"):
    root = Path(document_root(lg.path).path)
    root.mkdir(exist_ok=True)
    source = root / "receipt.json"
    source.write_bytes(data)
    event, created = attach_document(lg, ITEM, str(source), role=role, by_path=True)
    assert created and "path" in event.payload and "content_b64" not in event.payload
    return event, source


def _message(event, role: str = "review-receipt") -> str:
    return (f"a {role} document with this sha256 is already on {ITEM} "
            f"(event {event.id}) without its bytes stored inline; this form "
            "reuses inline documents only; nothing was written")


@pytest.mark.parametrize("file_present", [False, True], ids=["file_missing", "file_present"])
def test_the_writer_refuses_an_identical_by_path_document(lg, file_present) -> None:
    by_path, source = _by_path(lg)
    if not file_present:
        source.unlink()
    refused(lg, "doc_not_inline", _message(by_path),
            write_receipt, lg, ITEM, fields(), DOC, **STAMPS)
    assert count(lg, "review.receipt") == 0
    assert lg.events(item_id=ITEM, kind="doc.attached") == [by_path]


def test_other_bytes_beside_a_by_path_document_land_inline(lg) -> None:
    by_path, source = _by_path(lg, b'{"other": true}')
    source.unlink()
    event = write_receipt(lg, ITEM, fields(), DOC, **STAMPS)
    docs = lg.events(item_id=ITEM, kind="doc.attached")
    assert docs[0] == by_path and len(docs) == 2
    assert "content_b64" in docs[1].payload
    assert event.payload["doc_sha256"] == docs[1].payload["sha256"]


def test_the_document_step_refuses_an_identical_by_path_document(lg) -> None:
    by_path, source = _by_path(lg, role="other")
    source.unlink()

    def attach():
        with lg.transaction():
            attach_inline_in_transaction(lg, ITEM, DOC, role="other", mime="text/plain")

    refused(lg, "doc_not_inline", _message(by_path, "other"), attach)


def test_an_inline_document_is_still_reused(lg) -> None:
    with lg.transaction():
        first, created = attach_inline_in_transaction(lg, ITEM, DOC, role="review-receipt",
                                                      mime="application/json")
    before = count(lg)
    event = write_receipt(lg, ITEM, fields(), DOC, **STAMPS)
    assert created and count(lg) == before + 1
    assert lg.events(item_id=ITEM, kind="doc.attached") == [first]
    assert event.payload["doc_sha256"] == first.payload["sha256"]
