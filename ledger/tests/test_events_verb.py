"""``ledger events``: newest-first listing across items, filters, limit."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ledger.cli import main
from ledger.search import MAX_LIMIT
from ledger.store import open_ledger
from ledger.verbs import EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE

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


def test_empty_result_exits_1_with_empty_code(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, out, err = invoke(
        monkeypatch, ["--db", str(db), "events", "--item", "no-such"]
    )
    assert code == EXIT_NOT_FOUND, err
    assert out == ""
    code, body, err = run_json(monkeypatch, db, "events", "--item", "no-such")
    assert code == EXIT_NOT_FOUND, err
    assert body["error"]["code"] == "empty"


# --- events --id N ------------------------------------------------------------


def event_count(db: Path) -> int:
    with open_ledger(db, create=False) as lg:
        return len(lg.events())


def test_id_prints_exactly_the_events_asked_for_in_id_order(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, body, err = run_json(monkeypatch, db, "events", "--id", "3", "--id", "1")
    assert code == EXIT_OK, err
    assert ids(body) == [1, 3]
    assert kinds_of(body) == ["item.created", "item.created"]
    assert body["data"]["events"][1]["payload"] == {"type": "ticket", "title": "two"}
    assert set(body["data"]) == DATA_KEYS
    assert body["data"]["limit"] is None
    assert body["data"]["truncated"] is False
    for event in body["data"]["events"]:
        assert set(event) == EVENT_KEYS


def test_id_text_form_is_one_line_per_event_in_id_order(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, out, err = invoke(
        monkeypatch, ["--db", str(db), "events", "--id", "3", "--id", "1"]
    )
    assert code == EXIT_OK, err
    lines = out.splitlines()
    assert [line.split("\t")[:3] for line in lines] == [
        ["2026-01-10T09:00:00.000Z", "item.created", "it-1"],
        ["2026-03-10T09:00:00.000Z", "item.created", "it-2"],
    ]


def test_id_given_twice_prints_the_event_once(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, body, err = run_json(monkeypatch, db, "events", "--id", "2", "--id", "2")
    assert code == EXIT_OK, err
    assert ids(body) == [2]


def test_unknown_id_is_refused_by_name_and_prints_nothing(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, body, err = run_json(monkeypatch, db, "events", "--id", "1", "--id", "99")
    assert code == EXIT_NOT_FOUND, err
    assert body["ok"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == "event not found: 99"
    code, out, err = invoke(monkeypatch, ["--db", str(db), "events", "--id", "99"])
    assert code == EXIT_NOT_FOUND
    assert out == ""
    assert "event not found: 99" in err


def test_every_unknown_id_is_named(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    code, body, err = run_json(
        monkeypatch, db, "events", "--id", "98", "--id", "2", "--id", "97"
    )
    assert code == EXIT_NOT_FOUND, err
    assert body["error"]["message"] == "event not found: 97, 98"


def test_id_that_is_not_a_positive_integer_is_a_usage_error(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    for value in ("0", "-1", "abc", "1.5", " 1", "01", "", "9" * 30):
        code, body, err = run_json(monkeypatch, db, "events", f"--id={value}")
        assert code == EXIT_USAGE, (value, err)
        assert body["error"]["code"] == "usage"
        assert body["error"]["message"].startswith("--id "), value


def test_id_with_any_other_filter_is_a_usage_error(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    for extra in (
        ["--since", "2026-01-01"],
        ["--until", "2026-12-31"],
        ["--kind", "item.note"],
        ["--item", "it-1"],
        ["--actor", "alice"],
        ["--limit", "5"],
    ):
        code, body, err = run_json(monkeypatch, db, "events", "--id", "1", *extra)
        assert code == EXIT_USAGE, (extra, err)
        assert body["error"]["message"] == f"--id cannot be combined with {extra[0]}"


def test_id_reads_and_never_writes(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db")
    before = event_count(db)
    run_json(monkeypatch, db, "events", "--id", "1")
    run_json(monkeypatch, db, "events", "--id", "99")
    assert event_count(db) == before


def test_text_cells_keep_an_actor_with_a_tab_or_newline_on_one_line(
    tmp_path, monkeypatch
):
    db = tmp_path / "ledger.db"
    with open_ledger(db) as lg:
        lg.append(
            kind="item.created",
            item_id="it-1",
            idempotency_key="k-tab",
            harness="pytest",
            actor="ali\tce\nbob\r",
            recorded_at="2026-01-10T09:00:00.000Z",
            payload={"type": "ticket", "title": "one"},
        )
    for argv in (["events"], ["events", "--id", "1"]):
        code, out, err = invoke(monkeypatch, ["--db", str(db), *argv])
        assert code == EXIT_OK, err
        lines = out.splitlines()
        assert len(lines) == 1, argv
        parts = lines[0].split("\t")
        assert len(parts) == 5, argv
        assert parts[3] == "ali\\tce\\nbob\\r"
    code, body, err = run_json(monkeypatch, db, "events", "--id", "1")
    assert code == EXIT_OK, err
    assert body["data"]["events"][0]["actor"] == "ali\tce\nbob\r"


# --- line boundaries ----------------------------------------------------------

# Every character str.splitlines splits on, besides newline and carriage return.
BOUNDARIES = ("\u2028", "\u2029", "\x85", "\x0c", "\x0b", "\x1c", "\x1d", "\x1e")


def boundary_ledger(db: Path, ch: str) -> Path:
    """Two events: one whose payload holds ``ch``, one whose actor holds it."""
    with open_ledger(db) as lg:
        lg.append(
            kind="item.note",
            item_id="it-1",
            idempotency_key="k-payload",
            harness="pytest",
            actor="alice",
            recorded_at="2026-01-10T09:00:00.000Z",
            payload={"text": f"one{ch}two"},
        )
        lg.append(
            kind="item.note",
            item_id="it-1",
            idempotency_key="k-actor",
            harness="pytest",
            actor=f"ali{ch}ce",
            recorded_at="2026-01-10T10:00:00.000Z",
            payload={"text": "plain"},
        )
    return db


def payload_escape(ch: str) -> str:
    """How the payload column names ``ch``: JSON's own escape where JSON
    escapes it (the C0 controls), else the ``repr`` escape."""
    json_form = json.dumps(ch, ensure_ascii=False)[1:-1]
    return repr(ch)[1:-1] if json_form == ch else json_form


def test_line_boundaries_are_every_splitlines_boundary():
    from ledger.verbs.events import LINE_BOUNDARIES

    splits = {
        chr(code)
        for code in range(0x110000)
        if len(f"a{chr(code)}b".splitlines()) > 1
    }
    assert LINE_BOUNDARIES == splits
    assert splits == set(BOUNDARIES) | {"\n", "\r"}


@pytest.mark.parametrize("ch", BOUNDARIES, ids=ascii)
def test_every_line_boundary_keeps_one_line_per_event(tmp_path, monkeypatch, ch):
    db = boundary_ledger(tmp_path / "ledger.db", ch)
    for argv in (["events"], ["events", "--id", "1", "--id", "2"]):
        code, out, err = invoke(monkeypatch, ["--db", str(db), *argv])
        assert code == EXIT_OK, err
        lines = out.splitlines()
        assert len(lines) == 2, (argv, ascii(out))
        assert ch not in out, argv
        by_text = {line.split("\t")[4]: line.split("\t") for line in lines}
        payload_line = by_text[f'{{"text":"one{payload_escape(ch)}two"}}']
        assert payload_line[3] == "alice"
        actor_line = by_text['{"text":"plain"}']
        assert actor_line[3] == f"ali{repr(ch)[1:-1]}ce"


@pytest.mark.parametrize("ch", BOUNDARIES, ids=ascii)
def test_json_form_keeps_every_line_boundary_raw(tmp_path, monkeypatch, ch):
    db = boundary_ledger(tmp_path / "ledger.db", ch)
    code, body, err = run_json(monkeypatch, db, "events", "--id", "1", "--id", "2")
    assert code == EXIT_OK, err
    events = body["data"]["events"]
    assert events[0]["payload"] == {"text": f"one{ch}two"}
    assert events[1]["actor"] == f"ali{ch}ce"


def test_ordinary_non_ascii_text_prints_unescaped(tmp_path, monkeypatch):
    db = boundary_ledger(tmp_path / "ledger.db", "\u00e9\U0001f600")
    code, out, err = invoke(monkeypatch, ["--db", str(db), "events"])
    assert code == EXIT_OK, err
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0].split("\t")[3] == "ali\u00e9\U0001f600ce"
    assert lines[1].split("\t")[4] == '{"text":"one\u00e9\U0001f600two"}'
