"""``doctor --json`` store summary: snapshot counts, export, unknown parts."""
from __future__ import annotations

import io
import json
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ledger.cli import main
from ledger.export import write_watermark
from ledger.store import open_ledger
from ledger.verbs import EXIT_OK


def invoke(monkeypatch, argv, *, env=None):
    merged = {
        "LEDGER_HARNESS": "pytest",
        "LEDGER_AGENT": "tester",
        "LEDGER_EXPORT_DIR": None,
    }
    if env:
        merged.update(env)
    for key, value in merged.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def run_json(monkeypatch, db, *argv, env=None):
    code, out, err = invoke(
        monkeypatch, ["--json", "--db", str(db), *argv], env=env
    )
    assert "Traceback" not in err
    return code, json.loads(out) if out.strip() else None, err


def _ask(lg, item_id: str, question: str, key: str):
    return lg.append(
        kind="decision.asked",
        item_id=item_id,
        idempotency_key=key,
        harness="pytest",
        payload={
            "question": question,
            "options": [{"label": "yes"}, {"label": "no"}],
        },
    )


def seed(path: Path, monkeypatch) -> str:
    db = str(path)
    with open_ledger(db) as lg:
        lg.append(
            kind="item.created",
            item_id="it-open",
            idempotency_key="k-open",
            harness="pytest",
            payload={"type": "ticket", "title": "Open"},
        )
        lg.append(
            kind="item.created",
            item_id="it-done",
            idempotency_key="k-done",
            harness="pytest",
            payload={"type": "ticket", "title": "Done"},
        )
        lg.append(
            kind="item.created",
            item_id="it-claim",
            idempotency_key="k-claim",
            harness="pytest",
            payload={"type": "initiative", "title": "Claimed"},
        )
        _ask(lg, "it-open", "open?", "ask-open")
        answered = _ask(lg, "it-done", "answered?", "ask-done")
        utt = lg.append(
            kind="utterance.captured",
            item_id="it-done",
            idempotency_key="utt-done",
            harness="pytest",
            payload={"text": "yes", "origin": "person"},
        )
        lg.append(
            kind="decision.elected",
            item_id="it-done",
            idempotency_key="elect-done",
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
        lg.append(
            kind="item.state",
            item_id="it-done",
            idempotency_key="close-done",
            harness="pytest",
            payload={"status": "closed"},
        )
    code, _out, err = invoke(monkeypatch, ["--db", db, "claim", "it-claim"])
    assert code == 0, err
    return db


def _counts(db: str) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        by_type = {
            row[0]: row[1]
            for row in conn.execute("SELECT type, COUNT(*) FROM v_items GROUP BY type")
        }
        by_status = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT status, COUNT(*) FROM v_items GROUP BY status"
            )
        }
        events_total, last_event_at = conn.execute(
            "SELECT COUNT(*), MAX(recorded_at) FROM events"
        ).fetchone()
        open_questions = conn.execute(
            "SELECT COUNT(*) FROM v_open_decisions"
        ).fetchone()[0]
        leases = conn.execute("SELECT COUNT(*) FROM v_leases").fetchone()[0]
        asked_events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE kind = 'decision.asked'"
        ).fetchone()[0]
    finally:
        conn.close()
    return {
        "items_by_type": by_type,
        "items_by_status": by_status,
        "events_total": events_total,
        "last_event_at": last_event_at,
        "open_questions": open_questions,
        "leases": leases,
        "asked_events": asked_events,
    }


def test_summary_fields_match_independent_counts(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db", monkeypatch)
    expected = _counts(db)
    assert expected["items_by_type"]["ticket"] >= 1
    assert expected["items_by_type"]["initiative"] >= 1
    assert len(expected["items_by_status"]) >= 2
    assert expected["open_questions"] >= 1
    assert expected["asked_events"] > expected["open_questions"]
    assert expected["leases"] >= 1
    code, body, err = run_json(monkeypatch, db, "doctor")
    assert code == EXIT_OK, err
    summary = body["data"]["summary"]
    assert summary["items_by_type"] == expected["items_by_type"]
    assert summary["items_by_status"] == expected["items_by_status"]
    assert summary["events_total"] == expected["events_total"]
    assert summary["last_event_at"] == expected["last_event_at"]
    assert summary["open_questions"] == expected["open_questions"]
    assert summary["leases"] == expected["leases"]
    assert "export" not in summary
    assert set(body["data"]) >= {
        "path",
        "schema_version",
        "search",
        "expired_claims",
        "summary",
    }


def test_export_absent_without_export_dir(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db", monkeypatch)
    code, body, err = run_json(monkeypatch, db, "doctor")
    assert code == EXIT_OK, err
    assert "export" not in body["data"]["summary"]


def test_export_behind_counts_events_past_the_watermark(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db", monkeypatch)
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    write_watermark(export_dir, 2)
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        behind = conn.execute(
            "SELECT COUNT(*) FROM events WHERE id > 2"
        ).fetchone()[0]
    finally:
        conn.close()
    code, body, err = run_json(
        monkeypatch,
        db,
        "doctor",
        env={"LEDGER_EXPORT_DIR": str(export_dir)},
    )
    assert code == EXIT_OK, err
    export = body["data"]["summary"]["export"]
    assert export == {"watermark_event_id": 2, "behind": behind}
    assert behind >= 1


def test_summary_read_failure_is_unknown_and_exit_0(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db", monkeypatch)
    conn = sqlite3.connect(db)
    try:
        conn.execute("DROP VIEW v_items")
        conn.commit()
    finally:
        conn.close()
    code, body, err = run_json(monkeypatch, db, "doctor")
    assert code == EXIT_OK, err
    summary = body["data"]["summary"]
    assert summary["status"] == "unknown"
    assert "reason" in summary
    assert body["data"]["schema_version"] == 10


def test_watermark_failure_marks_export_unknown(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db", monkeypatch)
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "watermark.json").write_text("{not json", encoding="utf-8")
    code, body, err = run_json(
        monkeypatch,
        db,
        "doctor",
        env={"LEDGER_EXPORT_DIR": str(export_dir)},
    )
    assert code == EXIT_OK, err
    export = body["data"]["summary"]["export"]
    assert export["status"] == "unknown"
    assert "reason" in export
    assert body["data"]["summary"]["events_total"] >= 1


def test_export_dir_tilde_resolves_like_export(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db", monkeypatch)
    home = tmp_path / "home"
    export_dir = home / "export"
    export_dir.mkdir(parents=True)
    write_watermark(export_dir, 2)
    monkeypatch.setenv("HOME", str(home))
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        behind = conn.execute(
            "SELECT COUNT(*) FROM events WHERE id > 2"
        ).fetchone()[0]
    finally:
        conn.close()
    code, body, err = run_json(
        monkeypatch,
        db,
        "doctor",
        env={"LEDGER_EXPORT_DIR": "~/export"},
    )
    assert code == EXIT_OK, err
    export = body["data"]["summary"]["export"]
    assert export == {"watermark_event_id": 2, "behind": behind}
    assert behind >= 1


def test_oversized_watermark_marks_only_export_unknown(tmp_path, monkeypatch):
    db = seed(tmp_path / "ledger.db", monkeypatch)
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    write_watermark(export_dir, 2**100)
    code, body, err = run_json(
        monkeypatch,
        db,
        "doctor",
        env={"LEDGER_EXPORT_DIR": str(export_dir)},
    )
    assert code == EXIT_OK, err
    summary = body["data"]["summary"]
    export = summary["export"]
    assert export["status"] == "unknown"
    assert "reason" in export
    assert "items_by_type" in summary
    assert "items_by_status" in summary
    assert summary["events_total"] >= 1
    assert "last_event_at" in summary
    assert "open_questions" in summary
    assert "leases" in summary


def test_existing_doctor_text_lines_are_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "ledger.db")
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "new", "--id", "it-1", "--type", "ticket", "--title", "one"],
    )
    assert code == 0, err
    code, out, err = invoke(monkeypatch, ["--db", db, "doctor"])
    assert code == EXIT_OK, err
    resolved = str(Path(db).resolve())
    assert out == (
        f"path: {resolved}\n"
        f"schema_version: 10\n"
        f"search: missing (ledger index rebuilds it)\n"
    )
