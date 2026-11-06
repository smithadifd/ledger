"""``ledger events``: newest-first listing across items, filters, limit."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ledger.cli import main
from ledger.search import MAX_LIMIT
from ledger.store import open_ledger
from ledger.verbs import EXIT_OK, EXIT_USAGE

EVENT_KEYS = {
    "id",
    "kind",
    "item_id",
    "actor",
    "harness",
    "model",
    "session",
    "recorded_at",
    "payload",
}
DATA_KEYS = {"events", "limit", "truncated"}


def invoke(monkeypatch, argv):
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def run_json(monkeypatch, db, *argv):
    code, out, err = invoke(monkeypatch, ["--json", "--db", str(db), *argv])
    assert "Traceback" not in err
    return code, json.loads(out) if out.strip() else None, err


def seed(path: Path) -> Path:
    with open_ledger(path) as lg:
        lg.append(
            kind="item.created",
            item_id="it-1",
            idempotency_key="k-it-1",
            harness="pytest",
            actor="alice",
            recorded_at="2026-01-10T09:00:00.000Z",
            payload={"type": "ticket", "title": "one"},
        )
        lg.append(
            kind="item.note",
            item_id="it-1",
            idempotency_key="k-note-1",
            harness="pytest",
            actor="alice",
            recorded_at="2026-02-15T12:00:00.000Z",
            payload={"text": "hello"},
        )
        lg.append(
            kind="item.created",
            item_id="it-2",
            idempotency_key="k-it-2",
            harness="pytest",
            actor="bob",
            recorded_at="2026-03-10T09:00:00.000Z",
            payload={"type": "ticket", "title": "two"},
        )
        lg.append(
            kind="item.note",
            item_id="it-2",
            idempotency_key="k-note-2",
            harness="pytest",
            actor="bob",
            recorded_at="2026-03-10T10:00:00.000Z",
            payload={"text": "later"},
        )
        lg.append(
            kind="session.opened",
            idempotency_key="k-sess",
            harness="pytest",
            recorded_at="2026-04-01T00:00:00.000Z",
            payload={"purpose": "synthetic"},
        )
    return path


def ids(body) -> list[int]:
    return [event["id"] for event in body["data"]["events"]]


def kinds_of(body) -> list[str]:
    return [event["kind"] for event in body["data"]["events"]]


def test_newest_first_by_event_id(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, body, err = run_json(monkeypatch, db, "events")
    assert code == EXIT_OK, err
    assert ids(body) == [5, 4, 3, 2, 1]
    assert kinds_of(body) == [
        "session.opened",
        "item.note",
        "item.created",
        "item.note",
        "item.created",
    ]


def test_since_filter_narrows(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, body, err = run_json(monkeypatch, db, "events", "--since", "2026-03-01")
    assert code == EXIT_OK, err
    assert kinds_of(body) == ["session.opened", "item.note", "item.created"]
    assert {event["item_id"] for event in body["data"]["events"]} == {"it-2", None}


def test_until_filter_keeps_the_whole_of_a_bare_date(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, body, err = run_json(monkeypatch, db, "events", "--until", "2026-02-15")
    assert code == EXIT_OK, err
    assert ids(body) == [2, 1]
    code, body, err = run_json(
        monkeypatch, db, "events", "--until", "2026-02-15T11:59:59Z"
    )
    assert code == EXIT_OK, err
    assert ids(body) == [1]


def test_kind_single_and_repeated(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, body, err = run_json(monkeypatch, db, "events", "--kind", "item.note")
    assert code == EXIT_OK, err
    assert kinds_of(body) == ["item.note", "item.note"]
    assert ids(body) == [4, 2]
    code, body, err = run_json(
        monkeypatch, db, "events", "--kind", "item.note", "--kind", "session.opened"
    )
    assert code == EXIT_OK, err
    assert kinds_of(body) == ["session.opened", "item.note", "item.note"]


def test_item_and_actor_filters(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, body, err = run_json(monkeypatch, db, "events", "--item", "it-1")
    assert code == EXIT_OK, err
    assert {event["item_id"] for event in body["data"]["events"]} == {"it-1"}
    assert ids(body) == [2, 1]
    code, body, err = run_json(monkeypatch, db, "events", "--actor", "alice")
    assert code == EXIT_OK, err
    assert {event["actor"] for event in body["data"]["events"]} == {"alice"}
    code, body, err = run_json(monkeypatch, db, "events", "--actor", "bob")
    assert code == EXIT_OK, err
    assert {event["actor"] for event in body["data"]["events"]} == {"bob"}


def test_filters_combine(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, body, err = run_json(
        monkeypatch,
        db,
        "events",
        "--kind",
        "item.note",
        "--item",
        "it-1",
        "--actor",
        "alice",
        "--since",
        "2026-02-01",
        "--until",
        "2026-02-15",
    )
    assert code == EXIT_OK, err
    events = body["data"]["events"]
    assert len(events) == 1
    assert events[0]["id"] == 2
    assert events[0]["payload"] == {"text": "hello"}


def test_limit_sets_truncated_only_when_more_matched(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, body, err = run_json(monkeypatch, db, "events", "--limit", "2")
    assert code == EXIT_OK, err
    assert len(body["data"]["events"]) == 2
    assert body["data"]["limit"] == 2
    assert body["data"]["truncated"] is True
    code, body, err = run_json(monkeypatch, db, "events", "--limit", "5")
    assert code == EXIT_OK, err
    assert len(body["data"]["events"]) == 5
    assert body["data"]["truncated"] is False
    code, body, err = run_json(monkeypatch, db, "events", "--limit", "50")
    assert code == EXIT_OK, err
    assert body["data"]["truncated"] is False


def test_limit_zero_and_above_max_are_usage_errors(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, body, err = run_json(monkeypatch, db, "events", "--limit", "0")
    assert code == EXIT_USAGE, err
    assert body["error"]["code"] == "usage"
    code, body, err = run_json(
        monkeypatch, db, "events", "--limit", str(MAX_LIMIT + 1)
    )
    assert code == EXIT_USAGE, err
    assert body["error"]["code"] == "usage"


def test_since_after_until_is_a_usage_error(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, body, err = run_json(
        monkeypatch,
        db,
        "events",
        "--since",
        "2026-03-01",
        "--until",
        "2026-02-01",
    )
    assert code == EXIT_USAGE, err
    assert body["error"]["message"] == "--since is after --until"


def test_json_keys_are_exactly_the_listed_set(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, body, err = run_json(monkeypatch, db, "events")
    assert code == EXIT_OK, err
    assert set(body["data"]) == DATA_KEYS
    for event in body["data"]["events"]:
        assert set(event) == EVENT_KEYS
        assert isinstance(event["payload"], dict)


def test_text_form_is_one_line_per_event(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, out, err = invoke(monkeypatch, ["--db", str(db), "events"])
    assert code == EXIT_OK, err
    lines = out.splitlines()
    assert len(lines) == 5
    for line in lines:
        parts = line.split("\t")
        assert len(parts) == 5
    assert lines[0].split("\t")[1] == "session.opened"
    assert lines[0].split("\t")[2] == "-"
    assert lines[0].split("\t")[3] == "-"
    note = [line for line in lines if line.split("\t")[1] == "item.note"][0]
    assert "hello" in note or "later" in note
    assert "\n" not in note.strip()


def test_empty_result_prints_nothing_and_exits_0(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, out, err = invoke(
        monkeypatch, ["--db", str(db), "events", "--item", "no-such"]
    )
    assert code == EXIT_OK, err
    assert out == ""
    code, body, err = run_json(monkeypatch, db, "events", "--item", "no-such")
    assert code == EXIT_OK, err
    assert body["data"]["events"] == []
    assert body["data"]["truncated"] is False
