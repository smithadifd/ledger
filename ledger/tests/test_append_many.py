"""``append-many``: several events for one item in ONE transaction."""
from __future__ import annotations

import io
import json
import sqlite3
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ledger import store
from ledger.cli import main
from ledger.verbs import EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_STALE, EXIT_USAGE


def invoke(monkeypatch, argv, stdin, *, actor="writer-a", session="sess-a"):
    for key, value in {
        "LEDGER_HARNESS": "pytest",
        "LEDGER_AGENT": actor,
        "LEDGER_MODEL": "test-model",
        "LEDGER_SESSION": session,
    }.items():
        monkeypatch.setenv(key, value)
    text = stdin if isinstance(stdin, str) else json.dumps(stdin)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _event_count(db: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()


def _revision(monkeypatch, db: str, item: str = "it-1") -> int:
    code, out, err = invoke(
        monkeypatch, ["--json", "--db", db, "query", "--item", item], ""
    )
    assert code == 0, err
    return json.loads(out)["data"]["items"][0]["revision"]


def _batch_argv(db: str, *extra: str, item: str = "it-1") -> list[str]:
    return ["--json", "--db", db, "append-many", item, *extra]


def _note(text: str, **extra) -> dict:
    return {"kind": "item.note", "payload": {"text": text}, **extra}


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "ledger.db")
    code, _out, err = invoke(
        monkeypatch,
        ["--db", path, "new", "--id", "it-1", "--type", "ticket", "--title", "synthetic"],
        "",
    )
    assert code == 0, err
    return path


# (1) The batch lands atomically, in order, in one transaction.


def test_two_event_batch_lands_in_one_transaction(monkeypatch, db: str) -> None:
    statements: list[str] = []
    real_connect = store.connect

    def traced(path):
        conn, proof = real_connect(path)
        conn.set_trace_callback(statements.append)
        return conn, proof

    monkeypatch.setattr(store, "connect", traced)
    batch = [
        _note("pending close", idempotency_key="k-pending"),
        {
            "kind": "item.state",
            "payload": {"status": "closed"},
            "idempotency_key": "k-close",
        },
    ]
    code, out, err = invoke(monkeypatch, _batch_argv(db), batch)
    assert code == 0, err
    body = json.loads(out)
    assert body["ok"] is True
    events = body["data"]["events"]
    assert [e["kind"] for e in events] == ["item.note", "item.state"]
    assert [e["idempotency_key"] for e in events] == ["k-pending", "k-close"]
    assert events[1]["id"] == events[0]["id"] + 1
    assert all(e["item_id"] == "it-1" for e in events)
    assert all(e["actor"] == "writer-a" for e in events)
    # One BEGIN and one COMMIT cover both events.
    assert sum(s.startswith("BEGIN") for s in statements) == 1
    assert statements.count("COMMIT") == 1
    # Both are readable and chained.
    code, out, err = invoke(
        monkeypatch, ["--json", "--db", db, "query", "--item", "it-1"], ""
    )
    assert code == 0, err
    item = json.loads(out)["data"]["items"][0]
    assert item["status"] == "closed"
    assert item["revision"] == events[1]["id"]
    code, _out, err = invoke(monkeypatch, ["--db", db, "verify"], "")
    assert code == 0, err


def test_text_mode_prints_one_line_per_event(monkeypatch, db: str) -> None:
    code, out, err = invoke(
        monkeypatch,
        ["--db", db, "append-many", "it-1"],
        [_note("one"), _note("two")],
    )
    assert code == 0, err
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("appended\titem.note\tevent=2\t")
    assert lines[1].startswith("appended\titem.note\tevent=3\t")


# (2) The revision is checked once, before the first write.


def test_stale_revision_refuses_with_exit_5_and_writes_nothing(
    monkeypatch, db: str
) -> None:
    code, _out, err = invoke(
        monkeypatch, ["--db", db, "note", "it-1", "--text", "bump"], "", actor="writer-b"
    )
    assert code == 0, err
    before = _event_count(db)
    code, out, _err = invoke(
        monkeypatch,
        _batch_argv(db, "--expect-revision", "1"),
        [_note("one"), _note("two")],
    )
    assert code == EXIT_STALE
    body = json.loads(out)
    assert body["ok"] is False
    assert body["error"]["code"] == "stale_revision"
    message = body["error"]["message"]
    assert "it-1" in message
    assert "expected 1" in message
    assert "current is 2" in message
    assert "actor=writer-b" in message
    assert _event_count(db) == before


def test_fresh_revision_is_checked_once_and_both_events_land(
    monkeypatch, db: str
) -> None:
    seen = _revision(monkeypatch, db)
    code, out, err = invoke(
        monkeypatch,
        _batch_argv(db, "--expect-revision", str(seen)),
        [_note("one"), _note("two"), _note("three")],
    )
    assert code == 0, err
    ids = [e["id"] for e in json.loads(out)["data"]["events"]]
    assert ids == [seen + 1, seen + 2, seen + 3]
    assert _revision(monkeypatch, db) == seen + 3


def test_expect_none_on_item_with_events_is_refused(monkeypatch, db: str) -> None:
    before = _event_count(db)
    code, out, _err = invoke(
        monkeypatch, _batch_argv(db, "--expect-revision", "none"), [_note("x")]
    )
    assert code == EXIT_STALE
    assert "expected none" in json.loads(out)["error"]["message"]
    assert _event_count(db) == before


def test_unparseable_revision_is_a_usage_error(monkeypatch, db: str) -> None:
    before = _event_count(db)
    code, out, _err = invoke(
        monkeypatch, _batch_argv(db, "--expect-revision", "abc"), [_note("x")]
    )
    assert code == EXIT_USAGE
    assert json.loads(out)["error"]["code"] == "usage"
    assert _event_count(db) == before


# (3) A failure on a later event rolls back the earlier ones.


def test_failure_on_second_event_rolls_back_the_first(monkeypatch, db: str) -> None:
    before = _event_count(db)
    code, out, _err = invoke(
        monkeypatch,
        _batch_argv(db),
        [
            _note("fine", idempotency_key="k-first"),
            {"kind": "item.state", "payload": {"status": "no-such-status"}},
        ],
    )
    assert code == EXIT_REFUSED
    assert json.loads(out)["error"]["code"] == "validation"
    assert _event_count(db) == before
    # The first key was rolled back too: it is free to use again.
    code, out, err = invoke(
        monkeypatch, _batch_argv(db), [_note("fine", idempotency_key="k-first")]
    )
    assert code == 0, err
    assert json.loads(out)["data"]["events"][0]["id"] == before + 1


# (4) Idempotency keys survive: whole replays write nothing.


def test_whole_batch_replay_returns_stored_events_and_writes_nothing(
    monkeypatch, db: str
) -> None:
    batch = [_note("one", idempotency_key="k-1"), _note("two", idempotency_key="k-2")]
    code, out, err = invoke(monkeypatch, _batch_argv(db), batch)
    assert code == 0, err
    stored = json.loads(out)["data"]["events"]
    # Someone else moves the item on, so a revision taken earlier is stale.
    code, _out, err = invoke(
        monkeypatch, ["--db", db, "note", "it-1", "--text", "other"], ""
    )
    assert code == 0, err
    before = _event_count(db)
    code, out, err = invoke(
        monkeypatch, _batch_argv(db, "--expect-revision", "1"), batch
    )
    assert code == 0, err
    assert json.loads(out)["data"]["events"] == stored
    assert _event_count(db) == before


def test_partial_replay_writes_only_the_new_events(monkeypatch, db: str) -> None:
    code, out, err = invoke(
        monkeypatch, _batch_argv(db), [_note("one", idempotency_key="k-1")]
    )
    assert code == 0, err
    first = json.loads(out)["data"]["events"][0]
    before = _event_count(db)
    code, out, err = invoke(
        monkeypatch,
        _batch_argv(db),
        [_note("one", idempotency_key="k-1"), _note("two", idempotency_key="k-2")],
    )
    assert code == 0, err
    events = json.loads(out)["data"]["events"]
    assert events[0] == first
    assert events[1]["id"] == first["id"] + 1
    assert _event_count(db) == before + 1


def test_partial_replay_checks_the_revision_on_the_first_new_event(
    monkeypatch, db: str
) -> None:
    code, _out, err = invoke(
        monkeypatch, _batch_argv(db), [_note("one", idempotency_key="k-1")]
    )
    assert code == 0, err
    before = _event_count(db)
    # Revision 1 is stale (the stored event moved the item on), and the
    # stored first key must not let the new second event skip the check.
    code, out, _err = invoke(
        monkeypatch,
        _batch_argv(db, "--expect-revision", "1"),
        [_note("one", idempotency_key="k-1"), _note("two", idempotency_key="k-2")],
    )
    assert code == EXIT_STALE
    assert json.loads(out)["error"]["code"] == "stale_revision"
    assert _event_count(db) == before


def test_reused_key_with_different_content_refuses_and_rolls_back(
    monkeypatch, db: str
) -> None:
    code, _out, err = invoke(
        monkeypatch, _batch_argv(db), [_note("one", idempotency_key="k-1")]
    )
    assert code == 0, err
    before = _event_count(db)
    code, out, _err = invoke(
        monkeypatch,
        _batch_argv(db),
        [_note("fresh", idempotency_key="k-new"), _note("changed", idempotency_key="k-1")],
    )
    assert code == EXIT_REFUSED
    assert json.loads(out)["error"]["code"] == "conflict"
    assert _event_count(db) == before


# (5) Malformed input refuses by name and writes nothing.


@pytest.mark.parametrize(
    ("stdin", "named"),
    [
        ("[]", "holds no events"),
        ('{"kind": "item.note"}', "JSON array"),
        ('"item.note"', "JSON array"),
        ("null", "JSON array"),
        ("", "not JSON"),
        ("[{", "not JSON"),
        ('[{"kind": "item.note", "payload": {"text": NaN}}]', "not JSON"),
        ('["item.note"]', "event 0 must be a JSON object"),
        (
            [_note("ok"), {"kind": "item.note", "payload": {"text": "x"}, "bogus": 1}],
            "event 1 has unknown field: bogus",
        ),
        (
            [{"kind": "item.note", "payload": {"text": "x"}, "item": "it-1"}],
            "unknown field: item",
        ),
        ([{"payload": {"text": "x"}}], "missing field: kind"),
        ([{"kind": "item.note"}], "missing field: payload"),
        ([{"kind": "item.note", "payload": "text"}], "payload must be a JSON object"),
        ([{"kind": 7, "payload": {}}], "kind must be a non-empty string"),
        (
            [_note("x", idempotency_key=5)],
            "idempotency_key must be a non-empty string",
        ),
        ([_note("x", payload_version="1")], "payload_version must be an integer"),
        ([_note("x", payload_version=True)], "payload_version must be an integer"),
        (
            [_note("ok"), _note("other", item_id="it-2")],
            "event 1 is for item 'it-2', not 'it-1'",
        ),
        (
            [_note("a", idempotency_key="k"), _note("b", idempotency_key="k")],
            "repeats idempotency_key 'k'",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) and len(value) < 24 else None,
)
def test_malformed_input_is_a_usage_error_and_writes_nothing(
    monkeypatch, db: str, stdin, named: str
) -> None:
    before = _event_count(db)
    code, out, err = invoke(monkeypatch, _batch_argv(db), stdin)
    assert code == EXIT_USAGE, (out, err)
    body = json.loads(out)
    assert body["error"]["code"] == "usage"
    assert named in body["error"]["message"]
    assert _event_count(db) == before


# A JSON escape can spell a lone surrogate, which no UTF-8 text can hold. Every
# string field is refused up front as a usage error, before the database opens.
SURROGATE_FIELDS = ("kind", "item_id", "idempotency_key", "channel", "provenance_class")


@pytest.mark.parametrize("field", SURROGATE_FIELDS)
def test_unencodable_string_field_is_a_usage_error_and_writes_nothing(
    monkeypatch, db: str, field: str
) -> None:
    fields = {"kind": "item.note", "payload": {"text": "x"}}
    fields.update(
        {
            "item_id": "it-1",
            "idempotency_key": "k-ok",
            "channel": "ch",
            "provenance_class": "pc",
        }
    )
    fields[field] = "\ud800"
    # json.dumps writes the lone surrogate as a JSON escape, as a caller's own
    # JSON encoder would.
    text = json.dumps([_note("fine"), fields])
    assert "\\ud800" in text
    before = _event_count(db)
    code, out, err = invoke(monkeypatch, _batch_argv(db), text)
    assert code == EXIT_USAGE, (out, err)
    assert "Traceback" not in err
    assert "Traceback" not in out
    body = json.loads(out)
    assert body["error"]["code"] == "usage"
    assert f"event 1 {field} must be UTF-8 text" in body["error"]["message"]
    assert _event_count(db) == before


@pytest.mark.parametrize(
    "payload_json",
    ['{"text": "\\ud800"}', '{"text": "ok", "\\ud800": 1}', '{"text": ["\\ud800"]}'],
)
def test_unencodable_payload_text_is_refused_cleanly_and_writes_nothing(
    monkeypatch, db: str, payload_json: str
) -> None:
    text = '[{"kind": "item.note", "payload": %s}]' % payload_json
    before = _event_count(db)
    code, out, err = invoke(monkeypatch, _batch_argv(db), text)
    assert code == EXIT_REFUSED, (out, err)
    assert "Traceback" not in err
    body = json.loads(out)
    assert body["error"]["code"] == "validation"
    assert _event_count(db) == before


def test_event_for_a_different_item_is_refused_even_when_it_exists(
    monkeypatch, db: str
) -> None:
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "new", "--id", "it-2", "--type", "ticket", "--title", "other"],
        "",
    )
    assert code == 0, err
    before = _event_count(db)
    code, out, _err = invoke(
        monkeypatch,
        _batch_argv(db),
        [_note("mine", item_id="it-1"), _note("theirs", item_id="it-2")],
    )
    assert code == EXIT_USAGE
    assert "it-2" in json.loads(out)["error"]["message"]
    assert _event_count(db) == before


def test_missing_item_is_not_found_and_writes_nothing(monkeypatch, db: str) -> None:
    before = _event_count(db)
    code, out, _err = invoke(
        monkeypatch, _batch_argv(db, item="ghost"), [_note("one"), _note("two")]
    )
    assert code == EXIT_NOT_FOUND
    assert json.loads(out)["error"]["code"] == "not_found"
    assert _event_count(db) == before


def test_usage_error_does_not_create_a_database(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "absent.db"
    code, _out, _err = invoke(
        monkeypatch, ["--db", str(path), "append-many", "it-1"], "[]"
    )
    assert code == EXIT_USAGE
    assert not path.exists()


def test_reserved_kind_is_refused_and_rolls_back(monkeypatch, db: str) -> None:
    before = _event_count(db)
    code, out, _err = invoke(
        monkeypatch,
        _batch_argv(db),
        [_note("fine"), {"kind": "loop.closed", "payload": {}}],
    )
    assert code == EXIT_REFUSED
    assert json.loads(out)["error"]["code"] == "validation"
    assert _event_count(db) == before
