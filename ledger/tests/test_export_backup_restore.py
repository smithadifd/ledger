"""Export watermark, restore round-trip, backup integrity, commit isolation."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ledger.backup import backup_db, integrity_check
from ledger.cli import main
from ledger.export import (
    ChainBroken,
    LagExceeded,
    canonical_stream,
    check_lag,
    commit_export,
    event_filename,
    export_events,
    load_export,
    read_watermark,
    verify_export,
)
from ledger.restore import replay_events, restore_export
from ledger.store import (
    Event,
    LedgerError,
    canonical_dumps,
    claim_database,
    event_digest,
    open_ledger,
)
from ledger.verbs import EXIT_INTEGRITY, EXIT_REFUSED, EXIT_USAGE, discover_verbs

_LIB = Path(__file__).resolve().parents[2]

WRITER = r"""
import sys
import time
from ledger import open_ledger

db, wid, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
lg = open_ledger(db)
for i in range(n):
    lg.append(
        kind="item.created",
        payload={"type": "ticket", "title": "synthetic-w%s-%s" % (wid, i)},
        item_id="item-w%s-%s" % (wid, i),
        idempotency_key="key-w%s-%s" % (wid, i),
        harness="pytest",
        actor="writer-%s" % wid,
    )
    time.sleep(0.01)
lg.close()
"""


def invoke(monkeypatch, argv, *, env=None):
    merged = {
        "LEDGER_HARNESS": "pytest",
        "LEDGER_AGENT": "tester",
        "LEDGER_MODEL": "test-model",
        "LEDGER_SESSION": "sess-1",
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


def _db(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


def _seed(path: Path, n: int = 3, *, recorded_at: str | None = None) -> list:
    lg = open_ledger(path)
    events = []
    for i in range(n):
        events.append(
            lg.append(
                kind="item.created",
                payload={"type": "ticket", "title": f"synthetic-{i}"},
                item_id=f"item-{i}",
                idempotency_key=f"key-{i}",
                harness="pytest",
                actor="tester",
                recorded_at=recorded_at,
            )
        )
    lg.close()
    return events


def _init_export_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "ledger-test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "ledger-test@example.invalid"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "commit.gpgsign", "false"],
        check=True,
        capture_output=True,
    )
    return path


def test_export_backup_restore_verbs_discovered() -> None:
    names = set(discover_verbs())
    assert {"export", "backup", "restore"} <= names


def test_unexported_commit_survives_and_reexport_is_idempotent(tmp_path: Path) -> None:
    db = _db(tmp_path)
    export_dir = tmp_path / "export"
    original = _seed(db, 2)
    lg = open_ledger(db)
    assert [event.id for event in lg.events()] == [1, 2]
    assert read_watermark(export_dir) == 0
    first = export_events(lg, export_dir)
    assert first.exported == 2
    assert first.watermark == 2
    assert read_watermark(export_dir) == 2
    bodies = {
        path.name: path.read_bytes()
        for path in export_dir.glob("*.json")
        if path.name != "watermark.json"
    }
    second = export_events(lg, export_dir)
    assert second.exported == 0
    assert second.watermark == 2
    assert {
        path.name: path.read_bytes()
        for path in export_dir.glob("*.json")
        if path.name != "watermark.json"
    } == bodies
    assert canonical_stream(load_export(export_dir)) == canonical_stream(original)
    lg.close()


def test_crash_after_files_before_watermark(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    export_dir = tmp_path / "export"
    _seed(db, 1)
    lg = open_ledger(db)
    export_events(lg, export_dir)
    assert read_watermark(export_dir) == 1
    lg.append(
        kind="item.created",
        payload={"type": "ticket", "title": "synthetic-crash"},
        item_id="item-crash",
        idempotency_key="key-crash",
        harness="pytest",
        actor="tester",
    )

    def boom(export_dir: Path, event_id: int) -> None:
        raise RuntimeError("simulated crash before watermark")

    monkeypatch.setattr("ledger.export.write_watermark", boom)
    with pytest.raises(RuntimeError, match="simulated crash before watermark"):
        export_events(lg, export_dir)
    assert read_watermark(export_dir) == 1
    crash_file = export_dir / event_filename(2)
    assert crash_file.is_file()
    crashed_bytes = crash_file.read_bytes()
    monkeypatch.undo()
    result = export_events(lg, export_dir)
    assert result.exported == 1
    assert result.watermark == 2
    assert crash_file.read_bytes() == crashed_bytes
    lg.close()


def test_tampered_export_file_fails_verify(tmp_path: Path) -> None:
    db = _db(tmp_path)
    export_dir = tmp_path / "export"
    _seed(db, 2)
    lg = open_ledger(db)
    export_events(lg, export_dir)
    lg.close()
    path = export_dir / event_filename(1)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["title"] = "tampered-title"
    path.write_text(canonical_dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(ChainBroken, match="hash mismatch"):
        verify_export(export_dir)


def test_restore_round_trip_byte_equal_canonical_stream(tmp_path: Path) -> None:
    db = _db(tmp_path)
    export_dir = tmp_path / "export"
    claim_database(db, "home")
    _seed(db, 3)
    lg = open_ledger(db)
    original = lg.events()
    export_events(lg, export_dir)
    original_stream = canonical_stream(lg.events())
    lg.close()
    db.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    assert not db.exists()
    restored = restore_export(export_dir, db, system="home")
    assert restored.stream == original_stream
    assert restored.stream == canonical_stream(original)
    lg = open_ledger(db)
    try:
        lg.verify(system="home")
        replayed = lg.events()
    finally:
        lg.close()
    assert [(e.id, e.hash, e.recorded_at) for e in replayed] == [
        (e.id, e.hash, e.recorded_at) for e in original
    ]


def test_replay_inserts_exported_hash_without_rehashing(tmp_path: Path) -> None:
    db = _db(tmp_path)
    original = _seed(db, 1)[0]
    bogus = Event(
        id=original.id,
        kind=original.kind,
        payload_version=original.payload_version,
        payload=original.payload,
        item_id=original.item_id,
        idempotency_key="replay-key",
        actor=original.actor,
        harness=original.harness,
        model=original.model,
        session=original.session,
        channel=original.channel,
        provenance_class=original.provenance_class,
        recorded_at=original.recorded_at,
        prev_hash=original.prev_hash,
        hash="ab" * 32,
    )
    dest = tmp_path / "replayed.db"
    lg = open_ledger(dest)
    try:
        replay_events(lg, [bogus])
        stored = lg.events()[0]
        assert stored.hash == "ab" * 32
        assert stored.hash != event_digest(
            {
                "actor": bogus.actor,
                "channel": bogus.channel,
                "harness": bogus.harness,
                "id": bogus.id,
                "idempotency_key": bogus.idempotency_key,
                "item_id": bogus.item_id,
                "kind": bogus.kind,
                "model": bogus.model,
                "payload": bogus.payload,
                "payload_version": bogus.payload_version,
                "prev_hash": bogus.prev_hash,
                "provenance_class": bogus.provenance_class,
                "recorded_at": bogus.recorded_at,
                "session": bogus.session,
            }
        )
        with pytest.raises(Exception, match="hash mismatch"):
            lg.verify()
    finally:
        lg.close()


def test_restore_keeps_update_and_delete_triggers(tmp_path: Path) -> None:
    db = _db(tmp_path)
    export_dir = tmp_path / "export"
    _seed(db, 1)
    lg = open_ledger(db)
    export_events(lg, export_dir)
    lg.close()
    dest = tmp_path / "fresh.db"
    restore_export(export_dir, dest, system="home")
    conn = sqlite3.connect(dest)
    conn.execute("PRAGMA busy_timeout = 5000")
    with pytest.raises(sqlite3.IntegrityError, match="UPDATE refused"):
        conn.execute("UPDATE events SET actor = 'mutated'")
    with pytest.raises(sqlite3.IntegrityError, match="DELETE refused"):
        conn.execute("DELETE FROM events")
    conn.close()


def test_backup_under_concurrent_writes_passes_integrity_check(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    open_ledger(db).close()
    script = tmp_path / "writer.py"
    script.write_text(WRITER, encoding="utf-8")
    workers = 2
    each = 8
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(db), str(wid), str(each)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for wid in range(workers)
    ]
    during: list[Path] = []
    while any(proc.poll() is None for proc in procs):
        dest = tmp_path / f"bak-live-{len(during)}.db"
        backup_db(db, dest)
        assert integrity_check(dest) == "ok"
        during.append(dest)
    failures = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=60)
        if proc.returncode != 0:
            failures.append(f"exit {proc.returncode}\n{stdout}\n{stderr}")
    assert failures == []
    assert during
    final = tmp_path / "bak-final.db"
    result = backup_db(db, final)
    assert result.integrity == "ok"
    assert integrity_check(final) == "ok"
    conn = sqlite3.connect(final)
    count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    conn.close()
    assert count == workers * each


def test_export_check_exceeds_event_lag(tmp_path: Path) -> None:
    db = _db(tmp_path)
    export_dir = tmp_path / "export"
    _seed(db, 5)
    lg = open_ledger(db)
    with pytest.raises(LagExceeded, match="5 events"):
        check_lag(lg, export_dir, max_events=2, max_hours=10**6)
    lg.close()


def test_export_check_exceeds_hour_lag(tmp_path: Path) -> None:
    db = _db(tmp_path)
    export_dir = tmp_path / "export"
    _seed(db, 1, recorded_at="2020-01-01T00:00:00.000Z")
    lg = open_ledger(db)
    with pytest.raises(LagExceeded, match="hours"):
        check_lag(lg, export_dir, max_events=10**6, max_hours=1)
    lg.close()


def test_export_check_within_limits(tmp_path: Path) -> None:
    db = _db(tmp_path)
    export_dir = tmp_path / "export"
    _seed(db, 2)
    lg = open_ledger(db)
    export_events(lg, export_dir)
    lag = check_lag(lg, export_dir, max_events=0, max_hours=0)
    assert lag.events == 0
    lg.close()


def test_export_commit_never_pushes(tmp_path: Path) -> None:
    db = _db(tmp_path)
    export_dir = _init_export_repo(tmp_path / "export")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(export_dir), "remote", "add", "origin", str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(export_dir), "config", "branch.main.remote", "origin"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(export_dir),
            "config",
            "branch.main.merge",
            "refs/heads/main",
        ],
        check=True,
        capture_output=True,
    )
    _seed(db, 1)
    lg = open_ledger(db)
    export_events(lg, export_dir)
    lg.close()
    committed = commit_export(export_dir)
    assert committed.sha is not None
    assert committed.pushed is False
    remote_refs = subprocess.run(
        ["git", "--git-dir", str(remote), "rev-list", "--all"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert remote_refs.stdout.strip() == ""
    local = subprocess.run(
        ["git", "-C", str(export_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert local.stdout.strip() == committed.sha


def test_export_commit_refuses_other_repo(tmp_path: Path) -> None:
    parent = _init_export_repo(tmp_path / "parent")
    head_before = subprocess.run(
        ["git", "-C", str(parent), "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert head_before.returncode == 0
    log_before = subprocess.run(
        ["git", "-C", str(parent), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
    )
    export_dir = parent / "export"
    export_dir.mkdir()
    db = _db(tmp_path)
    _seed(db, 1)
    lg = open_ledger(db)
    export_events(lg, export_dir)
    lg.close()
    with pytest.raises(LedgerError, match="refusing to run git"):
        commit_export(export_dir)
    log_after = subprocess.run(
        ["git", "-C", str(parent), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
    )
    assert log_after.returncode == log_before.returncode
    assert log_after.stdout == log_before.stdout
    status = subprocess.run(
        ["git", "-C", str(parent), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "ledger export" not in status.stdout


def test_cli_restore_round_trip(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    export_dir = tmp_path / "export"
    claim_database(db, "home")
    code, _out, err = invoke(
        monkeypatch,
        [
            "--db",
            str(db),
            "new",
            "--id",
            "it-1",
            "--type",
            "ticket",
            "--title",
            "synthetic-cli",
        ],
    )
    assert code == 0, err
    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", str(db), "export", "--dir", str(export_dir)],
    )
    assert code == 0, err
    body = json.loads(out)
    assert body["ok"] is True
    assert body["data"]["exported"] == 2
    lg = open_ledger(db)
    original_stream = canonical_stream(lg.events())
    lg.close()
    db.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)
    dest = tmp_path / "restored.db"
    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", str(dest), "restore", "--dir", str(export_dir),
         "--system", "home"],
    )
    assert code == 0, err
    payload = json.loads(out)
    assert payload["data"]["events"] == 2
    lg = open_ledger(dest)
    try:
        lg.verify(system="home")
        assert canonical_stream(lg.events()) == original_stream
    finally:
        lg.close()


def test_cli_export_check_and_commit(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    export_dir = _init_export_repo(tmp_path / "export")
    code, _out, err = invoke(
        monkeypatch,
        [
            "--db",
            str(db),
            "new",
            "--id",
            "it-1",
            "--type",
            "ticket",
            "--title",
            "synthetic-check",
        ],
    )
    assert code == 0, err
    code, _out, err = invoke(
        monkeypatch,
        [
            "--json",
            "--db",
            str(db),
            "export",
            "--dir",
            str(export_dir),
            "--check",
            "--max-events",
            "0",
            "--max-hours",
            "1000000",
        ],
    )
    assert code == EXIT_REFUSED
    assert "lag" in err or "lag" in _out
    code, out, err = invoke(
        monkeypatch,
        [
            "--json",
            "--db",
            str(db),
            "export",
            "--dir",
            str(export_dir),
            "--commit",
        ],
    )
    assert code == 0, err
    body = json.loads(out)
    assert body["data"]["exported"] == 1
    assert body["data"]["commit"]
    code, _out, err = invoke(
        monkeypatch,
        [
            "--json",
            "--db",
            str(db),
            "export",
            "--dir",
            str(export_dir),
            "--commit",
            "--push",
        ],
    )
    assert code == EXIT_USAGE


def test_cli_backup(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    dest = tmp_path / "copy.db"
    code, _out, err = invoke(
        monkeypatch,
        [
            "--db",
            str(db),
            "new",
            "--id",
            "it-1",
            "--type",
            "ticket",
            "--title",
            "synthetic-bak",
        ],
    )
    assert code == 0, err
    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", str(db), "backup", "--to", str(dest)],
    )
    assert code == 0, err
    body = json.loads(out)
    assert body["data"]["integrity"] == "ok"
    assert integrity_check(dest) == "ok"


def test_cli_tampered_export_restore_fails(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    export_dir = tmp_path / "export"
    code, _out, err = invoke(
        monkeypatch,
        [
            "--db",
            str(db),
            "new",
            "--id",
            "it-1",
            "--type",
            "ticket",
            "--title",
            "synthetic-tamper",
        ],
    )
    assert code == 0, err
    code, _out, err = invoke(
        monkeypatch,
        ["--db", str(db), "export", "--dir", str(export_dir)],
    )
    assert code == 0, err
    path = export_dir / event_filename(1)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["payload"]["title"] = "tampered-title"
    path.write_text(canonical_dumps(record) + "\n", encoding="utf-8")
    dest = tmp_path / "fresh.db"
    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", str(dest), "restore", "--dir", str(export_dir),
         "--system", "home"],
    )
    assert code == EXIT_INTEGRITY
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "integrity"
    assert not dest.exists()


def test_cli_restore_refuses_short_export(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    export_dir = tmp_path / "export"
    _seed(db, 3)
    lg = open_ledger(db)
    export_events(lg, export_dir)
    lg.close()
    (export_dir / event_filename(3)).unlink()
    dest = tmp_path / "fresh.db"
    code, out, err = invoke(
        monkeypatch,
        ["--db", str(dest), "restore", "--dir", str(export_dir), "--system", "home"],
    )
    assert code == EXIT_REFUSED
    assert "watermark 3" in err
    assert len(err.strip().splitlines()) == 1
    assert not dest.exists()


def test_verify_export_refuses_gap_against_watermark(tmp_path: Path) -> None:
    db = _db(tmp_path)
    export_dir = tmp_path / "export"
    _seed(db, 3)
    lg = open_ledger(db)
    export_events(lg, export_dir)
    lg.close()
    (export_dir / event_filename(2)).unlink()
    with pytest.raises(LedgerError, match="export is short.*watermark 3"):
        verify_export(export_dir)


def test_restore_refuses_existing_target(tmp_path: Path) -> None:
    db = _db(tmp_path)
    export_dir = tmp_path / "export"
    _seed(db, 2)
    lg = open_ledger(db)
    export_events(lg, export_dir)
    lg.close()
    dest = tmp_path / "occupied.db"
    existing = open_ledger(dest)
    existing.close()
    with pytest.raises(LedgerError, match="restore target already exists"):
        restore_export(export_dir, dest, system="home")
    kept = open_ledger(dest)
    try:
        assert kept.events() == []
    finally:
        kept.close()


def test_cli_restore_refuses_export_missing_watermark(
    tmp_path: Path, monkeypatch
) -> None:
    db = _db(tmp_path)
    export_dir = tmp_path / "export"
    _seed(db, 3)
    lg = open_ledger(db)
    export_events(lg, export_dir)
    lg.close()
    (export_dir / "watermark.json").unlink()
    (export_dir / event_filename(3)).unlink()
    dest = tmp_path / "fresh.db"
    code, _out, err = invoke(
        monkeypatch,
        ["--db", str(dest), "restore", "--dir", str(export_dir), "--system", "home"],
    )
    assert code == EXIT_REFUSED
    assert "watermark.json is missing" in err
    assert len(err.strip().splitlines()) == 1
    assert not dest.exists()
    assert not Path(str(dest) + "-wal").exists()
    assert not Path(str(dest) + "-shm").exists()


def test_restore_round_trips_empty_ledger_export(tmp_path: Path) -> None:
    db = _db(tmp_path)
    export_dir = tmp_path / "export"
    lg = open_ledger(db)
    result = export_events(lg, export_dir)
    lg.close()
    assert result.exported == 0
    assert not (export_dir / "watermark.json").is_file()
    dest = tmp_path / "fresh.db"
    restored = restore_export(export_dir, dest, system="home")
    # Nothing to replay; the restore only claims the new database for home.
    assert restored.events == 1
    assert dest.exists()
    lg = open_ledger(dest)
    try:
        lg.verify(system="home")
        assert [event.kind for event in lg.events()] == ["ledger.system"]
    finally:
        lg.close()


@pytest.mark.parametrize(
    "watermark",
    ["true", "2.5", '"3"', "-1", None],
)
def test_restore_refuses_invalid_watermark(tmp_path: Path, watermark) -> None:
    db = _db(tmp_path)
    export_dir = tmp_path / "export"
    _seed(db, 3)
    lg = open_ledger(db)
    export_events(lg, export_dir)
    lg.close()
    if watermark is None:
        body = "{}"
    else:
        body = '{"id": %s}' % watermark
    (export_dir / "watermark.json").write_text(body + "\n", encoding="utf-8")
    dest = tmp_path / "fresh.db"
    with pytest.raises(LedgerError, match="watermark"):
        restore_export(export_dir, dest, system="home")
    assert not dest.exists()


def test_restore_removes_destination_on_replay_failure(
    tmp_path: Path, monkeypatch
) -> None:
    db = _db(tmp_path)
    export_dir = tmp_path / "export"
    _seed(db, 3)
    lg = open_ledger(db)
    export_events(lg, export_dir)
    lg.close()
    dest = tmp_path / "fresh.db"

    def boom(lg, events: list) -> None:
        raise RuntimeError("forced replay failure")

    monkeypatch.setattr("ledger.restore.replay_events", boom)
    with pytest.raises(RuntimeError, match="forced replay failure"):
        restore_export(export_dir, dest, system="home")
    monkeypatch.undo()
    assert not dest.exists()
    assert not Path(str(dest) + "-wal").exists()
    assert not Path(str(dest) + "-shm").exists()
    result = restore_export(export_dir, dest, system="home")
    # The three exported events, then the claim for home on the unclaimed export.
    assert result.events == 4


def test_backup_refuses_live_database_target(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    _seed(db, 1)
    before = db.read_bytes()
    for target in (db, Path(str(db) + "-wal")):
        with pytest.raises(LedgerError, match="live database"):
            backup_db(db, target)
    code, _out, err = invoke(
        monkeypatch,
        ["--db", str(db), "backup", "--to", str(db)],
    )
    assert code == EXIT_REFUSED
    assert len(err.strip().splitlines()) == 1
    assert db.read_bytes() == before
    assert not Path(str(db) + ".partial").exists()


def test_backup_refuses_failed_integrity_check(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    _seed(db, 1)
    dest = tmp_path / "copy.db"
    monkeypatch.setattr("ledger.backup.integrity_check", lambda _path: "bad page 1")
    with pytest.raises(LedgerError, match="integrity_check: bad page 1"):
        backup_db(db, dest)
    assert not dest.exists()
    assert not Path(str(dest) + ".partial").exists()


def test_backup_snapshots_wal_events_not_yet_checkpointed(tmp_path: Path) -> None:
    db = _db(tmp_path)
    lg = open_ledger(db)
    for i in range(2):
        lg.append(
            kind="item.created",
            payload={"type": "ticket", "title": f"synthetic-wal-{i}"},
            item_id=f"item-wal-{i}",
            idempotency_key=f"key-wal-{i}",
            harness="pytest",
            actor="tester",
        )
    dest = tmp_path / "copy.db"
    result = backup_db(db, dest)
    assert result.integrity == "ok"
    copy = open_ledger(dest)
    try:
        assert [event.id for event in copy.events()] == [1, 2]
    finally:
        copy.close()
        lg.close()
