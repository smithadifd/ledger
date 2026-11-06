"""`note`, `link` and `elect` take `--idempotency-key`, with `append`'s retry rule.

The same key and content returns the stored event and writes nothing; the same
key with different content is refused as a conflict. Without the flag every
call still writes a fresh event.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ledger.cli import main
from ledger.store import open_ledger
from ledger.verbs import EXIT_REFUSED


def invoke(monkeypatch, argv):
    for key, value in {
        "LEDGER_HARNESS": "pytest",
        "LEDGER_AGENT": "tester",
        "LEDGER_MODEL": "test-model",
        "LEDGER_SESSION": "sess-1",
    }.items():
        monkeypatch.setenv(key, value)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "ledger.db")
    for item_id in ("it-a", "it-b"):
        code, _out, err = invoke(
            monkeypatch,
            ["--db", path, "new", "--id", item_id, "--type", "ticket", "--title", item_id],
        )
        assert code == 0, err
    code, _out, err = invoke(
        monkeypatch,
        [
            "--db", path, "append", "--kind", "utterance.captured", "--payload",
            json.dumps({"text": "use backoff"}), "--channel", "terminal",
            "--provenance-class", "submitted-prompt",
        ],
    )
    assert code == 0, err
    return path


def _count(db: str) -> int:
    with open_ledger(db, create=False) as lg:
        return len(list(lg.events()))


def _events(db: str, kind: str):
    with open_ledger(db, create=False) as lg:
        return list(lg.events(kind=kind))


def _run(monkeypatch, db: str, *argv: str):
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, *argv])
    return code, (json.loads(out) if out.strip() else None), err


def _elect(db: str, chosen: str = "backoff", *extra: str):
    return [
        "--json", "--db", db, "elect", "it-a", "--question", "Which retry policy?",
        "--chosen", chosen, "--chosen-description", "three tries",
        "--rejected", "fixed", "--scope", "sync", "--quote-ref", str(_utterance_id(db)),
        "--quote", "use backoff", *extra,
    ]


def _utterance_id(db: str) -> int:
    return _events(db, "utterance.captured")[0].id


# note ---------------------------------------------------------------------


def test_note_retry_with_same_key_writes_once(monkeypatch, db: str) -> None:
    argv = ["note", "it-a", "hello", "--idempotency-key", "n-1"]
    code, first, err = _run(monkeypatch, db, *argv)
    assert code == 0, err
    before = _count(db)
    code, second, err = _run(monkeypatch, db, *argv)
    assert code == 0, err
    assert second["data"]["event"]["id"] == first["data"]["event"]["id"]
    assert _count(db) == before


def test_note_same_key_different_text_conflicts(monkeypatch, db: str) -> None:
    code, _first, err = _run(monkeypatch, db, "note", "it-a", "hello", "--idempotency-key", "n-1")
    assert code == 0, err
    before = _count(db)
    code, out, _err = _run(monkeypatch, db, "note", "it-a", "other", "--idempotency-key", "n-1")
    assert code == EXIT_REFUSED
    assert out["error"]["code"] == "conflict"
    assert _count(db) == before


def test_note_without_key_writes_each_time(monkeypatch, db: str) -> None:
    before = _count(db)
    for _ in range(2):
        code, _out, err = _run(monkeypatch, db, "note", "it-a", "same")
        assert code == 0, err
    assert _count(db) == before + 2


# link ---------------------------------------------------------------------


def test_link_retry_with_same_key_writes_once(monkeypatch, db: str) -> None:
    argv = ["link", "it-a", "--rel", "pr", "--target", "pr-1", "--idempotency-key", "l-1"]
    code, first, err = _run(monkeypatch, db, *argv)
    assert code == 0, err
    before = _count(db)
    code, second, err = _run(monkeypatch, db, *argv)
    assert code == 0, err
    assert second["data"]["event"]["id"] == first["data"]["event"]["id"]
    assert _count(db) == before


def test_link_same_key_different_target_conflicts(monkeypatch, db: str) -> None:
    code, _first, err = _run(
        monkeypatch, db, "link", "it-a", "--rel", "pr", "--target", "pr-1",
        "--idempotency-key", "l-1",
    )
    assert code == 0, err
    before = _count(db)
    code, out, _err = _run(
        monkeypatch, db, "link", "it-a", "--rel", "pr", "--target", "pr-2",
        "--idempotency-key", "l-1",
    )
    assert code == EXIT_REFUSED
    assert out["error"]["code"] == "conflict"
    assert _count(db) == before


def test_link_without_key_writes_each_time(monkeypatch, db: str) -> None:
    before = _count(db)
    for _ in range(2):
        code, _out, err = _run(monkeypatch, db, "link", "it-a", "--rel", "pr", "--target", "pr-1")
        assert code == 0, err
    assert _count(db) == before + 2


def _dup(*extra: str, target: str = "it-b"):
    return ["link", "it-a", "--rel", "duplicate-of", "--target", target, *extra]


def test_duplicate_of_retry_returns_both_events_and_writes_none(monkeypatch, db: str) -> None:
    code, first, err = _run(monkeypatch, db, *_dup("--idempotency-key", "d-1"))
    assert code == 0, err
    before = _count(db)
    code, second, err = _run(monkeypatch, db, *_dup("--idempotency-key", "d-1"))
    assert code == 0, err
    assert second["data"]["event"]["id"] == first["data"]["event"]["id"]
    assert second["data"]["closed_event"]["id"] == first["data"]["closed_event"]["id"]
    assert _count(db) == before


def test_duplicate_of_same_key_different_target_conflicts(monkeypatch, db: str) -> None:
    code, _first, err = _run(monkeypatch, db, *_dup("--idempotency-key", "d-1"))
    assert code == 0, err
    code, _out, err = invoke(
        monkeypatch, ["--db", db, "new", "--id", "it-c", "--type", "ticket", "--title", "c"]
    )
    assert code == 0, err
    before = _count(db)
    code, out, _err = _run(monkeypatch, db, *_dup("--idempotency-key", "d-1", target="it-c"))
    assert code == EXIT_REFUSED
    assert out["error"]["code"] == "conflict"
    assert _count(db) == before


def test_duplicate_of_refuses_a_half_written_key_by_name(monkeypatch, db: str) -> None:
    # Only the link event exists under the key, written by another call.
    code, _out, err = _run(
        monkeypatch, db, "append", "--kind", "item.linked", "--item", "it-a",
        "--payload", json.dumps({"rel": "duplicate-of", "target": "it-b"}),
        "--idempotency-key", "d-1",
    )
    assert code == 0, err
    before = _count(db)
    code, out, _err = _run(monkeypatch, db, *_dup("--idempotency-key", "d-1"))
    assert code == EXIT_REFUSED
    assert out["error"]["code"] == "partial_write"
    assert _count(db) == before


def _lone_dup_link(monkeypatch, db: str, item: str, target: str) -> None:
    code, _out, err = invoke(
        monkeypatch, ["--db", db, "new", "--id", "it-c", "--type", "ticket", "--title", "c"]
    )
    assert code == 0, err
    code, _out, err = _run(
        monkeypatch, db, "append", "--kind", "item.linked", "--item", item,
        "--payload", json.dumps({"rel": "duplicate-of", "target": target}),
        "--idempotency-key", "d-1",
    )
    assert code == 0, err


def test_duplicate_of_lone_link_with_other_target_conflicts(monkeypatch, db: str) -> None:
    _lone_dup_link(monkeypatch, db, "it-a", "it-c")
    before = _count(db)
    code, out, _err = _run(monkeypatch, db, *_dup("--idempotency-key", "d-1"))
    assert code == EXIT_REFUSED
    assert out["error"]["code"] == "conflict"
    assert _count(db) == before


def test_duplicate_of_lone_link_on_other_item_conflicts(monkeypatch, db: str) -> None:
    _lone_dup_link(monkeypatch, db, "it-c", "it-b")
    before = _count(db)
    code, out, _err = _run(monkeypatch, db, *_dup("--idempotency-key", "d-1"))
    assert code == EXIT_REFUSED
    assert out["error"]["code"] == "conflict"
    assert _count(db) == before


def test_duplicate_of_refuses_a_lone_close_by_name(monkeypatch, db: str) -> None:
    # Only the close event exists under the key's close slot.
    code, _out, err = _run(
        monkeypatch, db, "append", "--kind", "item.state", "--item", "it-a",
        "--payload", json.dumps({"status": "closed", "reason": "x"}),
        "--idempotency-key", "d-1:close",
    )
    assert code == 0, err
    before = _count(db)
    code, out, _err = _run(monkeypatch, db, *_dup("--idempotency-key", "d-1"))
    assert code == EXIT_REFUSED
    assert out["error"]["code"] == "partial_write"
    assert _count(db) == before


def _status(db: str, item_id: str) -> str:
    with open_ledger(db, create=False) as lg:
        return lg.item(item_id)["status"]


def test_plain_link_then_duplicate_of_same_key_conflicts(monkeypatch, db: str) -> None:
    code, _out, err = _run(
        monkeypatch, db, "link", "it-a", "--rel", "pr", "--target", "pr-1",
        "--idempotency-key", "k-1",
    )
    assert code == 0, err
    before = _count(db)
    code, out, _err = _run(monkeypatch, db, *_dup("--idempotency-key", "k-1"))
    assert code == EXIT_REFUSED
    assert out["error"]["code"] == "conflict"
    assert _count(db) == before
    assert _status(db, "it-a") != "closed"


def test_plain_link_to_same_target_then_duplicate_of_same_key_conflicts(
    monkeypatch, db: str
) -> None:
    # Same item and same target as the duplicate-of; only the rel differs.
    code, _out, err = _run(
        monkeypatch, db, "link", "it-a", "--rel", "blocks", "--target", "it-b",
        "--idempotency-key", "k-1",
    )
    assert code == 0, err
    before = _count(db)
    code, out, _err = _run(monkeypatch, db, *_dup("--idempotency-key", "k-1"))
    assert code == EXIT_REFUSED
    assert out["error"]["code"] == "conflict"
    assert _count(db) == before
    assert _status(db, "it-a") != "closed"


def test_duplicate_of_then_plain_link_same_key_conflicts(monkeypatch, db: str) -> None:
    code, _out, err = _run(monkeypatch, db, *_dup("--idempotency-key", "k-1"))
    assert code == 0, err
    before = _count(db)
    code, out, _err = _run(
        monkeypatch, db, "link", "it-a", "--rel", "pr", "--target", "pr-1",
        "--idempotency-key", "k-1",
    )
    assert code == EXIT_REFUSED
    assert out["error"]["code"] == "conflict"
    assert _count(db) == before


def test_duplicate_of_without_key_still_refuses_a_closed_item(monkeypatch, db: str) -> None:
    code, _out, err = _run(monkeypatch, db, *_dup())
    assert code == 0, err
    code, out, _err = _run(monkeypatch, db, *_dup())
    assert code == EXIT_REFUSED
    assert out["error"]["code"] == "closed"


# elect --------------------------------------------------------------------


def test_elect_retry_with_same_key_writes_once(monkeypatch, db: str) -> None:
    argv = _elect(db, "backoff", "--idempotency-key", "e-1")[1:]
    code, first, err = _run(monkeypatch, db, *argv[2:])
    assert code == 0, err
    before = _count(db)
    code, second, err = _run(monkeypatch, db, *argv[2:])
    assert code == 0, err
    assert second["data"]["event"]["id"] == first["data"]["event"]["id"]
    assert _count(db) == before


def test_elect_same_key_different_choice_conflicts(monkeypatch, db: str) -> None:
    first = _elect(db, "backoff", "--idempotency-key", "e-1")[3:]
    code, _out, err = _run(monkeypatch, db, *first)
    assert code == 0, err
    before = _count(db)
    changed = _elect(db, "fixed", "--idempotency-key", "e-1")[3:]
    code, out, _err = _run(monkeypatch, db, *changed)
    assert code == EXIT_REFUSED
    assert out["error"]["code"] == "conflict"
    assert _count(db) == before


def test_elect_without_key_writes_each_time(monkeypatch, db: str) -> None:
    args = _elect(db)[3:]
    before = _count(db)
    for _ in range(2):
        code, _out, err = _run(monkeypatch, db, *args)
        assert code == 0, err
    assert _count(db) == before + 2
