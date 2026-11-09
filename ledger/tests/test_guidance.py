"""Pool guidance: one ledger item type, written once and read from any folder."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

import pytest

from ledger import (
    IdempotencyConflict,
    ValidationError,
    guidance,
    open_ledger,
)
from ledger.cli import main as ledger_cli

TODAY = date(2026, 10, 19)


def _db(tmp_path: Path) -> Path:
    return tmp_path / "home" / "ledger.db"


def _record(lg, **overrides):
    fields = dict(
        pool="xai-grok",
        direction="burn",
        reason="synthetic reset on friday",
        starts="2026-10-19",
        expires="2026-10-25",
    )
    fields.update(overrides)
    return guidance.record_guidance(lg, harness="pytest", actor="tester", **fields)


def test_guidance_written_in_one_folder_is_read_from_another(tmp_path, monkeypatch):
    db = _db(tmp_path)
    db.parent.mkdir()
    writer_dir = tmp_path / "writer"
    reader_dir = tmp_path / "reader"
    writer_dir.mkdir()
    reader_dir.mkdir()
    monkeypatch.chdir(writer_dir)
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    rc = ledger_cli([
        "--db", str(db), "guidance", "set", "--pool", "xai-grok", "--direction", "burn",
        "--reason", "weekly reset soon", "--starts", "2026-10-19", "--expires", "2026-10-25",
    ])
    assert rc == 0
    monkeypatch.chdir(reader_dir)
    resolved = guidance.resolve_db(None, {"LEDGER_DB": str(db)})
    [item] = guidance.read_in_force(resolved, TODAY)
    assert (item.pool, item.direction, item.reason) == ("xai-grok", "burn", "weekly reset soon")


def test_a_relative_ledger_path_is_refused_so_the_folder_never_matters():
    with pytest.raises(guidance.GuidanceError, match="must be absolute"):
        guidance.resolve_db("ledger.db", {})
    assert guidance.resolve_db(None, {}) is None


def test_guidance_expires_on_its_date_and_drops_out(tmp_path):
    lg = open_ledger(_db(tmp_path))
    _record(lg)
    assert [g.pool for g in guidance.in_force(lg, date(2026, 10, 25))] == ["xai-grok"], (
        "guidance is still in force on its expiry date"
    )
    assert guidance.in_force(lg, date(2026, 10, 26)) == [], (
        "guidance must drop out the day after its expiry date"
    )


def test_guidance_not_yet_started_is_not_in_force(tmp_path):
    lg = open_ledger(_db(tmp_path))
    _record(lg, starts="2026-10-22")
    assert guidance.in_force(lg, date(2026, 10, 21)) == []
    assert len(guidance.in_force(lg, date(2026, 10, 22))) == 1


def test_withdrawn_guidance_is_not_in_force(tmp_path, monkeypatch):
    db = _db(tmp_path)
    lg = open_ledger(db)
    _record(lg)
    lg.close()
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    assert ledger_cli([
        "--db", str(db), "guidance", "withdraw", "--pool", "xai-grok", "--direction", "burn",
    ]) == 0
    assert guidance.read_in_force(db, TODAY) == []


def test_setting_guidance_again_after_withdraw_puts_it_back_in_force(tmp_path, monkeypatch, capsys):
    db = _db(tmp_path)
    lg = open_ledger(db)
    _record(lg, starts="2000-01-01", expires="2999-01-01")
    lg.close()
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    pool = ["--pool", "xai-grok", "--direction", "burn"]
    assert ledger_cli(["--db", str(db), "guidance", "withdraw", *pool]) == 0
    assert ledger_cli([
        "--db", str(db), "guidance", "set", *pool,
        "--reason", "back again", "--starts", "2000-01-01", "--expires", "2999-01-01",
    ]) == 0
    capsys.readouterr()
    assert ledger_cli(["--db", str(db), "--json", "guidance", "list"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert [row["reason"] for row in data] == ["back again"], (
        "guidance set after a withdraw must be listed, not silently lost"
    )
    assert [g.reason for g in guidance.read_in_force(db, TODAY)] == ["back again"]


def test_setting_guidance_again_replaces_it(tmp_path):
    lg = open_ledger(_db(tmp_path))
    _record(lg, expires="2026-10-20")
    _record(lg, expires="2026-10-30", reason="reset moved")
    [item] = guidance.in_force(lg, date(2026, 10, 29))
    assert item.reason == "reset moved"
    assert len(lg.items()) == 1


def test_dates_must_be_ordered_and_iso(tmp_path):
    lg = open_ledger(_db(tmp_path))
    with pytest.raises(guidance.GuidanceError, match="before starts"):
        _record(lg, starts="2026-10-25", expires="2026-10-19")
    with pytest.raises(guidance.GuidanceError, match="ISO date"):
        _record(lg, expires="next friday")
    with pytest.raises(guidance.GuidanceError, match="direction"):
        _record(lg, direction="hoard")


def test_the_schema_refuses_a_multiline_reason(tmp_path):
    lg = open_ledger(_db(tmp_path))
    with pytest.raises(ValidationError):
        _record(lg, reason="line one\nline two")


def test_list_verb_prints_json_for_guidance_in_force(tmp_path, monkeypatch, capsys):
    db = _db(tmp_path)
    lg = open_ledger(db)
    _record(lg, starts="2000-01-01", expires="2999-01-01")
    lg.close()
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    assert ledger_cli(["--db", str(db), "--json", "guidance", "list"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert [row["pool"] for row in data] == ["xai-grok"]


def test_going_back_to_earlier_guidance_puts_it_back_in_force(tmp_path):
    lg = open_ledger(_db(tmp_path))
    _record(lg, reason="first text")
    _record(lg, reason="second text")
    _record(lg, reason="first text")
    [item] = guidance.in_force(lg, TODAY)
    assert item.reason == "first text", (
        "setting earlier guidance again must put it back in force, not keep the later text"
    )
    assert len(lg.events(kind="guidance.set")) == 3


def test_a_retry_of_one_set_is_recorded_once(tmp_path):
    lg = open_ledger(_db(tmp_path))
    _record(lg, operation_id="op-1")
    _record(lg, operation_id="op-1")
    assert len(lg.events(kind="guidance.set")) == 1


def _withdraw(lg, item_id):
    lg.append(kind="item.state", payload={"status": "closed"}, item_id=item_id,
              idempotency_key=f"withdraw-{item_id}", harness="pytest", actor="tester")


def test_a_retry_of_a_set_after_withdraw_leaves_the_item_withdrawn(tmp_path):
    lg = open_ledger(_db(tmp_path))
    first = _record(lg, operation_id="op-1")
    _withdraw(lg, first.item_id)
    events_before = len(lg.events(item_id=first.item_id))
    again = _record(lg, operation_id="op-1")
    assert again == first, "a retried set must return the result the first call recorded"
    assert lg.item(first.item_id)["status"] == "closed", (
        "a retry of an earlier set must not reopen a withdrawn item"
    )
    assert len(lg.events(item_id=first.item_id)) == events_before, (
        "a retry of an earlier set must write nothing"
    )
    assert guidance.in_force(lg, TODAY) == []


def test_a_retry_from_another_session_returns_the_first_record(tmp_path):
    lg = open_ledger(_db(tmp_path))
    first = _record(lg, operation_id="op-1", session="first-session")
    _withdraw(lg, first.item_id)
    events_before = len(lg.events(item_id=first.item_id))
    again = guidance.record_guidance(
        lg, pool="xai-grok", direction="burn", reason="synthetic reset on friday",
        starts="2026-10-19", expires="2026-10-25", operation_id="op-1",
        harness="other-harness", actor="someone-else", session="later-session",
    )
    assert again == first, "a retry from another session must return the first record"
    assert lg.item(first.item_id)["status"] == "closed"
    assert len(lg.events(item_id=first.item_id)) == events_before


def test_a_reused_operation_id_with_changed_fields_is_refused_and_changes_nothing(tmp_path):
    lg = open_ledger(_db(tmp_path))
    first = _record(lg, operation_id="op-1")
    _withdraw(lg, first.item_id)
    events_before = len(lg.events(item_id=first.item_id))
    with pytest.raises(IdempotencyConflict):
        _record(lg, operation_id="op-1", reason="different text")
    assert lg.item(first.item_id)["status"] == "closed", (
        "a refused retry must leave the item withdrawn"
    )
    assert len(lg.events(item_id=first.item_id)) == events_before


def test_a_new_operation_after_withdraw_reopens_the_item(tmp_path):
    lg = open_ledger(_db(tmp_path))
    first = _record(lg, operation_id="op-1")
    _withdraw(lg, first.item_id)
    _record(lg, operation_id="op-2", reason="a later set")
    assert lg.item(first.item_id)["status"] == "open"
    assert [g.reason for g in guidance.in_force(lg, TODAY)] == ["a later set"]
    assert len(lg.events(kind="guidance.set")) == 2


def test_reinstating_identical_guidance_after_withdraw_from_a_later_session(
    tmp_path, monkeypatch, capsys
):
    db = _db(tmp_path)
    db.parent.mkdir()
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    pool = ["--pool", "xai-grok", "--direction", "burn"]
    window = ["--starts", "2000-01-01", "--expires", "2999-01-01"]
    monkeypatch.setenv("LEDGER_SESSION", "first-session")
    assert ledger_cli(["--db", str(db), "guidance", "set", *pool, "--reason", "text a", *window]) == 0
    assert ledger_cli(["--db", str(db), "guidance", "set", *pool, "--reason", "text b", *window]) == 0
    assert ledger_cli(["--db", str(db), "guidance", "withdraw", *pool]) == 0
    monkeypatch.setenv("LEDGER_SESSION", "later-session")
    rc = ledger_cli(["--db", str(db), "guidance", "set", *pool, "--reason", "text a", *window])
    assert rc == 0, "reinstating earlier guidance from a later session must succeed"
    capsys.readouterr()
    assert ledger_cli(["--db", str(db), "--json", "guidance", "list"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert [row["reason"] for row in data] == ["text a"], (
        "reinstated guidance must be open and in force"
    )
    lg = open_ledger(db)
    assert lg.item(guidance.item_id_for("xai-grok", "burn"))["status"] == "open"


def test_a_withdraw_racing_a_set_never_leaves_the_set_closed(tmp_path):
    """A withdraw that commits while a set is waiting for the write lock.

    A second connection opens a real write transaction and appends a
    withdraw, then holds it. The set starts on its own connection, waits
    for the lock, and must decide on the item's status only once it holds
    the lock, so it sees the withdraw and reopens the item.
    """
    db = _db(tmp_path)
    lg = open_ledger(db)
    _record(lg, reason="before the race", starts="2000-01-01", expires="2999-01-01")
    lg.close()
    item_id = guidance.item_id_for("xai-grok", "burn")

    setter = """
import sys
from ledger import guidance, open_ledger
lg = open_ledger(sys.argv[1], create=False)
print("opened", flush=True)
try:
    guidance.record_guidance(
        lg,
        harness="pytest",
        actor="tester",
        pool="xai-grok",
        direction="burn",
        reason="after the race",
        starts="2000-01-01",
        expires="2999-01-01",
    )
finally:
    lg.close()
"""

    withdrawer = open_ledger(db)
    with withdrawer.transaction():
        withdrawer.append(kind="item.state", payload={"status": "closed"}, item_id=item_id,
                          idempotency_key="withdraw-during-set", harness="pytest", actor="tester")
        proc = subprocess.Popen(
            [sys.executable, "-c", setter, str(db)],
            cwd=Path(__file__).resolve().parents[2],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "opened"
        # Let the setter reach the lock (and, if it read the status outside
        # the transaction, read it as still open) before the withdraw commits.
        time.sleep(0.5)
    _out, err = proc.communicate(timeout=30)
    assert proc.returncode == 0, err
    item = withdrawer.item(item_id)
    reasons = [g.reason for g in guidance.in_force(withdrawer, TODAY)]
    withdrawer.close()
    assert (item["status"], reasons) == ("open", ["after the race"])
