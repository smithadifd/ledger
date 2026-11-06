"""``ledger questions``: open asks across items, oldest first, filters."""
from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ledger.cli import main
from ledger.search import MAX_LIMIT
from ledger.store import open_ledger
from ledger.verbs import EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE

QUESTION_KEYS = {
    "item_id",
    "asked_event_id",
    "question",
    "recommendation",
    "default",
    "recorded_at",
    "type",
    "title",
    "status",
}
DATA_KEYS = {"questions", "limit", "truncated"}


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


def _ask(lg, item_id: str, question: str, key: str, at: str, **extra):
    payload = {
        "question": question,
        "options": [{"label": "yes"}, {"label": "no"}],
        **extra,
    }
    return lg.append(
        kind="decision.asked",
        item_id=item_id,
        idempotency_key=key,
        harness="pytest",
        recorded_at=at,
        payload=payload,
    )


def seed(path: Path) -> dict[str, int]:
    ids: dict[str, int] = {}
    with open_ledger(path) as lg:
        lg.append(
            kind="item.created",
            item_id="it-ticket",
            idempotency_key="k-ticket",
            harness="pytest",
            payload={"type": "ticket", "title": "Alpha"},
        )
        lg.append(
            kind="item.created",
            item_id="it-init",
            idempotency_key="k-init",
            harness="pytest",
            payload={"type": "initiative", "title": "Beta"},
        )
        first = _ask(
            lg, "it-ticket", "first?\nmore", "ask-first", "2026-06-01T09:00:00.000Z"
        )
        second = _ask(
            lg, "it-init", "second?", "ask-second", "2026-01-01T09:00:00.000Z"
        )
        answered = _ask(
            lg, "it-ticket", "answered?", "ask-answered", "2026-03-01T09:00:00.000Z"
        )
        utt = lg.append(
            kind="utterance.captured",
            item_id="it-ticket",
            idempotency_key="utt-1",
            harness="pytest",
            payload={"text": "yes", "origin": "person"},
        )
        lg.append(
            kind="decision.elected",
            item_id="it-ticket",
            idempotency_key="elect-1",
            harness="pytest",
            payload={
                "question": "answered?",
                "chosen": "yes",
                "chosen_description": "go",
                "rejected": ["no"],
                "scope": "test",
                "quote_ref": utt.id,
                "asked_event_id": answered.id,
            },
        )
        ids["first"] = first.id
        ids["second"] = second.id
        ids["answered"] = answered.id
    return ids


def questions(body) -> list[dict]:
    return body["data"]["questions"]


def test_answered_ask_drops_out(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    ids = seed(db)
    code, body, err = run_json(monkeypatch, db, "questions")
    assert code == EXIT_OK, err
    asked = [row["asked_event_id"] for row in questions(body)]
    assert ids["answered"] not in asked
    assert set(asked) == {ids["first"], ids["second"]}


def test_oldest_first_by_asked_event_id(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    ids = seed(db)
    code, body, err = run_json(monkeypatch, db, "questions")
    assert code == EXIT_OK, err
    asked = [row["asked_event_id"] for row in questions(body)]
    assert asked == [ids["first"], ids["second"]]
    assert asked == sorted(asked)


def test_item_and_type_filters_narrow(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    seed(db)
    code, body, err = run_json(monkeypatch, db, "questions", "--item", "it-ticket")
    assert code == EXIT_OK, err
    rows = questions(body)
    assert [row["item_id"] for row in rows] == ["it-ticket"]
    assert [row["question"] for row in rows] == ["first?\nmore"]
    code, body, err = run_json(monkeypatch, db, "questions", "--type", "initiative")
    assert code == EXIT_OK, err
    rows = questions(body)
    assert [row["item_id"] for row in rows] == ["it-init"]
    code, body, err = run_json(monkeypatch, db, "questions", "--type", "ticket")
    assert code == EXIT_OK, err
    assert [row["item_id"] for row in questions(body)] == ["it-ticket"]


def test_since_until_and_combined_filters(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    seed(db)
    code, body, err = run_json(monkeypatch, db, "questions", "--since", "2026-05-01")
    assert code == EXIT_OK, err
    assert [row["question"] for row in questions(body)] == ["first?\nmore"]
    code, body, err = run_json(monkeypatch, db, "questions", "--until", "2026-01-01")
    assert code == EXIT_OK, err
    assert [row["question"] for row in questions(body)] == ["second?"]
    code, body, err = run_json(
        monkeypatch,
        db,
        "questions",
        "--type",
        "ticket",
        "--since",
        "2026-01-01",
        "--until",
        "2026-12-31",
    )
    assert code == EXIT_OK, err
    assert [row["item_id"] for row in questions(body)] == ["it-ticket"]


def test_limit_and_truncated(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    seed(db)
    code, body, err = run_json(monkeypatch, db, "questions", "--limit", "1")
    assert code == EXIT_OK, err
    assert len(questions(body)) == 1
    assert body["data"]["truncated"] is True
    assert body["data"]["limit"] == 1
    code, body, err = run_json(monkeypatch, db, "questions", "--limit", "2")
    assert code == EXIT_OK, err
    assert len(questions(body)) == 2
    assert body["data"]["truncated"] is False
    code, body, err = run_json(monkeypatch, db, "questions", "--limit", "0")
    assert code == EXIT_USAGE, err
    code, body, err = run_json(
        monkeypatch, db, "questions", "--limit", str(MAX_LIMIT + 1)
    )
    assert code == EXIT_USAGE, err


def test_joined_type_title_status_are_current(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    seed(db)
    with open_ledger(db) as lg:
        lg.append(
            kind="item.state",
            item_id="it-ticket",
            idempotency_key="close-ticket",
            harness="pytest",
            payload={"status": "closed"},
        )
    code, body, err = run_json(monkeypatch, db, "questions", "--item", "it-ticket")
    assert code == EXIT_OK, err
    row = questions(body)[0]
    assert row["type"] == "ticket"
    assert row["title"] == "Alpha"
    assert row["status"] == "closed"
    assert set(row) == QUESTION_KEYS
    assert set(body["data"]) == DATA_KEYS


def test_text_form(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    seed(db)
    code, out, err = invoke(monkeypatch, ["--db", str(db), "questions"])
    assert code == EXIT_OK, err
    lines = out.splitlines()
    assert len(lines) == 2
    first = lines[0].split("\t")
    assert len(first) == 3
    assert re.fullmatch(r"\d+[dhm]", first[0])
    assert first[1] == "it-ticket"
    assert first[2] == "first? more"


def test_empty_result_exits_1_with_empty_code(tmp_path, monkeypatch):
    db = tmp_path / "ledger.db"
    seed(db)
    code, out, err = invoke(
        monkeypatch, ["--db", str(db), "questions", "--item", "no-such"]
    )
    assert code == EXIT_NOT_FOUND, err
    assert out == ""
    code, body, err = run_json(monkeypatch, db, "questions", "--item", "no-such")
    assert code == EXIT_NOT_FOUND, err
    assert body["error"]["code"] == "empty"
