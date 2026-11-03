"""CLI verbs in JSON and text mode, exit codes, caps, doctor, claim capacity."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ledger.cli import main
from ledger.verbs import (
    DEFAULT_LIMIT,
    EXIT_INTEGRITY,
    EXIT_NOT_FOUND,
    EXIT_REFUSED,
    EXIT_USAGE,
    discover_verbs,
)


def invoke(monkeypatch, argv, *, env=None):
    merged = {
        "LEDGER_HARNESS": "pytest",
        "LEDGER_OPERATOR": None,
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


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "ledger.db")


def seed(monkeypatch, db: str, item_id: str = "it-1", title: str = "synthetic-one") -> None:
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "new", "--id", item_id, "--type", "ticket", "--title", title],
    )
    assert code == 0, err


def test_shipped_verbs_discovered() -> None:
    names = set(discover_verbs())
    assert names >= {
        "new",
        "append",
        "state",
        "link",
        "note",
        "query",
        "ready",
        "claim",
        "verify",
        "doctor",
    }


def test_new_json_and_text(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    code, out, err = invoke(
        monkeypatch,
        ["--db", db, "new", "--id", "it-1", "--type", "ticket", "--title", "synthetic-one"],
    )
    assert code == 0, err
    assert "Traceback" not in err
    assert "it-1" in out
    assert "synthetic-one" in out
    code, out, err = invoke(
        monkeypatch,
        [
            "--json",
            "--db",
            db,
            "new",
            "--id",
            "it-2",
            "--type",
            "ticket",
            "--title",
            "synthetic-two",
        ],
    )
    assert code == 0, err
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["error"] is None
    assert payload["data"]["item_id"] == "it-2"
    assert payload["data"]["event"]["kind"] == "item.created"
    assert payload["data"]["event"]["harness"] == "pytest"
    assert "Traceback" not in err


def test_append_json_and_text(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db)
    payload = json.dumps({"text": "synthetic note"})
    code, out, err = invoke(
        monkeypatch,
        ["--db", db, "append", "--kind", "item.note", "--item", "it-1", "--payload", payload],
    )
    assert code == 0, err
    assert "item.note" in out
    code, out, err = invoke(
        monkeypatch,
        [
            "--json",
            "--db",
            db,
            "append",
            "--kind",
            "session.opened",
            "--payload",
            json.dumps({"purpose": "synthetic"}),
        ],
    )
    assert code == 0, err
    body = json.loads(out)
    assert body["ok"] is True
    assert body["data"]["event"]["kind"] == "session.opened"


def test_state_json_and_text(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db)
    code, out, err = invoke(
        monkeypatch,
        ["--db", db, "state", "it-1", "--status", "blocked", "--blocked-by", "other-1"],
    )
    assert code == 0, err
    assert "blocked" in out
    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", db, "state", "it-1", "--status", "open"],
    )
    assert code == 0, err
    body = json.loads(out)
    assert body["data"]["event"]["payload"]["status"] == "open"


def test_link_json_and_text(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db)
    code, out, err = invoke(
        monkeypatch,
        ["--db", db, "link", "it-1", "--rel", "pr", "--target", "org/repo#1"],
    )
    assert code == 0, err
    assert "org/repo#1" in out
    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", db, "link", "it-1", "--rel", "url", "--target", "https://example.test/x"],
    )
    assert code == 0, err
    body = json.loads(out)
    assert body["data"]["event"]["payload"]["rel"] == "url"


def test_note_json_and_text(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db)
    code, out, err = invoke(
        monkeypatch,
        ["--db", db, "note", "it-1", "--text", "synthetic note"],
    )
    assert code == 0, err
    assert "note" in out
    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", db, "note", "it-1", "--text", "second synthetic note"],
    )
    assert code == 0, err
    body = json.loads(out)
    assert body["data"]["event"]["payload"]["text"] == "second synthetic note"


def test_query_json_and_text(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db)
    code, out, err = invoke(monkeypatch, ["--db", db, "query", "--item", "it-1"])
    assert code == 0, err
    assert "it-1" in out
    assert "synthetic-one" in out
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "query", "--type", "ticket"])
    assert code == 0, err
    body = json.loads(out)
    assert body["ok"] is True
    assert body["data"]["items"][0]["item_id"] == "it-1"
    assert body["data"]["truncated"] is False


def test_ready_json_and_text(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "ready-1", "ready-item")
    seed(monkeypatch, db, "blocked-1", "blocked-item")
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "state", "blocked-1", "--status", "blocked", "--blocked-by", "ready-1"],
    )
    assert code == 0, err
    code, out, err = invoke(monkeypatch, ["--db", db, "ready"])
    assert code == 0, err
    assert "ready-1" in out
    assert "blocked-1" not in out
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "ready"])
    assert code == 0, err
    ids = {row["item_id"] for row in json.loads(out)["data"]["items"]}
    assert ids == {"ready-1"}


def test_claim_json_and_text(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db)
    code, out, err = invoke(monkeypatch, ["--db", db, "claim", "it-1"])
    assert code == 0, err
    assert "claimed" in out
    seed(monkeypatch, db, "it-2", "synthetic-two")
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "claim", "it-2", "--owner", "tester"])
    assert code == 0, err
    body = json.loads(out)
    assert body["data"]["event"]["payload"]["status"] == "claimed"
    assert body["data"]["event"]["payload"]["owner"] == "tester"


def test_verify_json_and_text(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db)
    code, out, err = invoke(monkeypatch, ["--db", db, "verify"])
    assert code == 0, err
    assert "ok" in out
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "verify"])
    assert code == 0, err
    body = json.loads(out)
    assert body["ok"] is True
    assert body["data"]["events"] >= 1


def test_doctor_json_and_text(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db)
    code, out, err = invoke(monkeypatch, ["--db", db, "doctor"])
    assert code == 0, err
    assert db in out or Path(db).resolve().as_posix() in out.replace("\\", "/")
    assert "schema_version:" in out
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "doctor"])
    assert code == 0, err
    body = json.loads(out)
    assert body["ok"] is True
    assert body["data"]["schema_version"] == 9
    assert Path(body["data"]["path"]) == Path(db).resolve()


def test_query_default_row_cap(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    for index in range(DEFAULT_LIMIT + 1):
        seed(monkeypatch, db, f"it-{index:03d}", f"synthetic-{index:03d}")
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "query"])
    assert code == 0, err
    body = json.loads(out)
    assert len(body["data"]["items"]) == DEFAULT_LIMIT
    assert body["data"]["limit"] == DEFAULT_LIMIT
    assert body["data"]["truncated"] is True


def test_query_limit(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    for index in range(5):
        seed(monkeypatch, db, f"it-{index}", f"synthetic-{index}")
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "query", "--limit", "2"])
    assert code == 0, err
    body = json.loads(out)
    assert len(body["data"]["items"]) == 2
    assert body["data"]["limit"] == 2
    assert body["data"]["truncated"] is True


def test_write_refused_without_harness(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    code, out, err = invoke(
        monkeypatch,
        ["--db", db, "new", "--type", "ticket", "--title", "synthetic-one"],
        env={"LEDGER_HARNESS": None},
    )
    assert code == EXIT_REFUSED
    assert "LEDGER_HARNESS" in err
    assert "Traceback" not in err
    assert not Path(db).exists()


def test_json_error_envelope(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", db, "new", "--type", "ticket", "--title", "synthetic-one"],
        env={"LEDGER_HARNESS": None},
    )
    assert code == EXIT_REFUSED
    payload = json.loads(out)
    assert set(payload) == {"ok", "data", "error"}
    assert payload["ok"] is False
    assert payload["data"] is None
    assert set(payload["error"]) == {"code", "message"}
    assert payload["error"]["code"] == "missing_harness"
    assert "LEDGER_HARNESS" in payload["error"]["message"]
    assert "Traceback" not in out
    assert "Traceback" not in err


def test_no_stack_trace_on_user_error(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db)
    try:
        code, out, err = invoke(
            monkeypatch,
            ["--db", db, "append", "--kind", "item.exploded", "--payload", "{}"],
        )
    except Exception:
        import traceback as tb

        code, out, err = -1, "", tb.format_exc()
    assert "Traceback" not in out
    assert "Traceback" not in err
    assert code == EXIT_REFUSED
    assert err.count("\n") <= 1 or err.strip().count("\n") == 0
    assert "unknown-kind" in err
    try:
        code, out, err = invoke(
            monkeypatch,
            ["--json", "--db", db, "append", "--kind", "item.exploded", "--payload", "{}"],
        )
    except Exception:
        import traceback as tb

        code, out, err = -1, tb.format_exc(), ""
    assert "Traceback" not in out
    assert "Traceback" not in err
    assert code == EXIT_REFUSED
    payload = json.loads(out)
    assert payload["ok"] is False


def test_doctor_synced_path(tmp_path: Path, monkeypatch) -> None:
    db = str(tmp_path / "Dropbox" / "ledger.db")
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "doctor"])
    assert code == EXIT_REFUSED
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "synced_path"
    assert "Dropbox" in payload["error"]["message"]


def test_doctor_missing_db(tmp_path: Path, monkeypatch) -> None:
    db = str(tmp_path / "missing" / "ledger.db")
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "doctor"])
    assert code == EXIT_NOT_FOUND
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "missing_db"
    code, out, err = invoke(monkeypatch, ["--db", db, "doctor"])
    assert code == EXIT_NOT_FOUND
    assert "does not exist" in err


def test_claim_capacity(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a", "synthetic-a")
    seed(monkeypatch, db, "it-b", "synthetic-b")
    seed(monkeypatch, db, "it-c", "synthetic-c")
    assert invoke(monkeypatch, ["--db", db, "claim", "it-a", "--capacity", "2"])[0] == 0
    assert invoke(monkeypatch, ["--db", db, "claim", "it-b", "--capacity", "2"])[0] == 0
    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", db, "claim", "it-c", "--capacity", "2"],
    )
    assert code == EXIT_REFUSED
    payload = json.loads(out)
    assert payload["error"]["code"] == "capacity"
    assert "capacity 2" in payload["error"]["message"]


def test_query_empty_exit(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db)
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "query", "--item", "no-such"])
    assert code == EXIT_NOT_FOUND
    payload = json.loads(out)
    assert payload["error"]["code"] == "empty"


def test_missing_item_exit(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db)
    code, _out, err = invoke(monkeypatch, ["--db", db, "note", "no-such", "--text", "x"])
    assert code == EXIT_NOT_FOUND
    assert "not found" in err


def test_usage_exit_without_db(monkeypatch) -> None:
    monkeypatch.delenv("LEDGER_DB", raising=False)
    code, out, err = invoke(monkeypatch, ["doctor"], env={"LEDGER_DB": None})
    assert code == EXIT_USAGE
    assert "LEDGER_DB" in err or "--db" in err


def test_verify_integrity_exit(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db)
    conn = sqlite3.connect(db)
    conn.execute("DROP TRIGGER events_no_update")
    conn.execute("UPDATE events SET hash = ? WHERE id = 1", ("ff" * 32,))
    conn.commit()
    conn.close()
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "verify"])
    assert code == EXIT_INTEGRITY
    payload = json.loads(out)
    assert payload["error"]["code"] == "integrity"


def test_create_update_query_through_cli_alone(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-loop", "synthetic-loop")
    assert invoke(monkeypatch, ["--db", db, "note", "it-loop", "--text", "synthetic progress"])[0] == 0
    assert invoke(monkeypatch, ["--db", db, "state", "it-loop", "--status", "claimed"])[0] == 0
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "query", "--item", "it-loop"])
    assert code == 0, err
    item = json.loads(out)["data"]["items"][0]
    assert item["status"] == "claimed"
    assert item["title"] == "synthetic-loop"


def test_entrypoint_module(tmp_path: Path) -> None:
    # ``python -m ledger.cli`` from the folder that holds the package.
    lib = Path(__file__).resolve().parents[2]
    db = tmp_path / "ledger.db"
    env = os.environ.copy()
    env["LEDGER_HARNESS"] = "pytest"
    env["LEDGER_AGENT"] = "tester"
    env["PYTHONPATH"] = str(lib)
    env.pop("LEDGER_DB", None)
    created = subprocess.run(
        [
            sys.executable,
            "-m",
            "ledger.cli",
            "--json",
            "--db",
            str(db),
            "new",
            "--type",
            "ticket",
            "--title",
            "synthetic-entry",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert created.returncode == 0, created.stderr
    payload = json.loads(created.stdout)
    assert payload["ok"] is True
    doctor = subprocess.run(
        [sys.executable, "-m", "ledger.cli", "--json", "--db", str(db), "doctor"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert doctor.returncode == 0, doctor.stderr
    assert json.loads(doctor.stdout)["data"]["schema_version"] == 9


def _assert_unusable(monkeypatch, argv) -> None:
    try:
        code, out, err = invoke(monkeypatch, argv)
    except Exception:
        import traceback as tb

        code, out, err = -1, tb.format_exc(), tb.format_exc()
    assert "Traceback" not in out
    assert "Traceback" not in err
    assert code == EXIT_REFUSED
    json_mode = "--json" in argv
    if json_mode:
        payload = json.loads(out)
        assert payload["ok"] is False
        assert payload["data"] is None
        assert payload["error"]["code"] == "unusable"
        assert "unusable database" in payload["error"]["message"]
    else:
        assert "unusable database" in err


def test_unusable_db_no_traceback(tmp_path: Path, monkeypatch) -> None:
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_text("not-a-database\n", encoding="utf-8")
    db = str(corrupt)
    verbs = [
        ["query"],
        [
            "append",
            "--kind",
            "item.note",
            "--item",
            "nope",
            "--payload",
            json.dumps({"text": "x"}),
        ],
        ["verify"],
    ]
    for extra in verbs:
        _assert_unusable(monkeypatch, ["--db", db, *extra])
        _assert_unusable(monkeypatch, ["--json", "--db", db, *extra])
    if os.geteuid() == 0:
        return
    locked = tmp_path / "locked.db"
    seed(monkeypatch, str(locked))
    locked.chmod(0)
    try:
        ldb = str(locked)
        for extra in verbs:
            _assert_unusable(monkeypatch, ["--db", ldb, *extra])
            _assert_unusable(monkeypatch, ["--json", "--db", ldb, *extra])
    finally:
        locked.chmod(0o600)


def test_append_missing_item_refused(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db)
    payload = json.dumps({"text": "orphan note"})
    code, out, err = invoke(
        monkeypatch,
        ["--db", db, "append", "--kind", "item.note", "--item", "nope", "--payload", payload],
    )
    assert code == EXIT_NOT_FOUND
    assert "item not found: nope" in err
    assert "Traceback" not in err
    code, out, err = invoke(
        monkeypatch,
        [
            "--json",
            "--db",
            db,
            "append",
            "--kind",
            "item.note",
            "--item",
            "nope",
            "--payload",
            payload,
        ],
    )
    assert code == EXIT_NOT_FOUND
    body = json.loads(out)
    assert body["ok"] is False
    assert body["error"]["code"] == "not_found"
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "query"])
    assert code == 0, err
    items = json.loads(out)["data"]["items"]
    assert [item["item_id"] for item in items] == ["it-1"]


def test_claim_held_by_other_refused(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a", "synthetic-a")
    assert invoke(monkeypatch, ["--db", db, "claim", "it-a"])[0] == 0
    code, out, err = invoke(
        monkeypatch,
        ["--db", db, "claim", "it-a"],
        env={"LEDGER_AGENT": "other"},
    )
    assert code == EXIT_REFUSED
    assert "already claimed by tester" in err
    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", db, "claim", "it-a"],
        env={"LEDGER_AGENT": "other"},
    )
    assert code == EXIT_REFUSED
    body = json.loads(out)
    assert body["ok"] is False
    assert body["error"]["code"] == "claimed"
    assert "already claimed by tester" in body["error"]["message"]


def test_claim_same_actor_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a", "synthetic-a")
    assert invoke(monkeypatch, ["--db", db, "claim", "it-a"])[0] == 0
    code, out, err = invoke(monkeypatch, ["--db", db, "claim", "it-a"])
    assert code == 0, err
    assert "claimed" in out


def test_claim_capacity_self_exclusion(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a", "synthetic-a")
    assert invoke(monkeypatch, ["--db", db, "claim", "it-a", "--capacity", "1"])[0] == 0
    code, out, err = invoke(monkeypatch, ["--db", db, "claim", "it-a", "--capacity", "1"])
    assert code == 0, err


def test_new_duplicate_id_refused(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-dup", "synthetic-dup")
    code, out, err = invoke(
        monkeypatch,
        ["--db", db, "new", "--id", "it-dup", "--type", "ticket", "--title", "again"],
    )
    assert code == EXIT_REFUSED
    assert "item already exists: it-dup" in err
    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", db, "new", "--id", "it-dup", "--type", "ticket", "--title", "again"],
    )
    assert code == EXIT_REFUSED
    body = json.loads(out)
    assert body["ok"] is False
    assert body["error"]["code"] == "conflict"


def test_json_and_db_after_verb(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db)
    code, out, err = invoke(monkeypatch, ["--db", db, "query", "--json"])
    assert code == 0, err
    body = json.loads(out)
    assert body["ok"] is True
    assert body["data"]["items"][0]["item_id"] == "it-1"
    code, out, err = invoke(monkeypatch, ["query", "--db", db, "--json"])
    assert code == 0, err
    body = json.loads(out)
    assert body["ok"] is True
    assert body["error"] is None


def test_append_item_created_does_not_require_existing_item(
    tmp_path: Path, monkeypatch
) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db)
    payload = json.dumps({"type": "ticket", "title": "fresh-via-append"})
    code, out, err = invoke(
        monkeypatch,
        [
            "--db",
            db,
            "append",
            "--kind",
            "item.created",
            "--item",
            "it-fresh",
            "--payload",
            payload,
        ],
    )
    assert code == 0, err


# SL36: a sourced `new` takes its idempotency key from the source.
def _new(monkeypatch, db, title, *extra, env=None):
    return invoke(
        monkeypatch,
        ["--db", db, "--json", "new", "--type", "ticket", "--title", title, *extra],
        env=env,
    )


def _count_items(monkeypatch, db) -> int:
    from ledger import open_ledger

    lg = open_ledger(db)
    try:
        return len(lg.events(kind="item.created"))
    finally:
        lg.close()


def test_new_same_source_yields_one_item(monkeypatch, tmp_path) -> None:
    db = _db(tmp_path)
    code1, out1, err1 = _new(monkeypatch, db, "t", "--source", "roadmap:row-1")
    code2, out2, err2 = _new(monkeypatch, db, "t", "--source", "  roadmap:row-1 ",
                             env={"LEDGER_AGENT": "someone-else"})
    assert code1 == 0, err1
    assert code2 == 0, err2
    assert json.loads(out1)["data"]["item_id"] == json.loads(out2)["data"]["item_id"]
    assert _count_items(monkeypatch, db) == 1


def test_new_same_source_different_title_refuses_and_names_item(monkeypatch, tmp_path) -> None:
    db = _db(tmp_path)
    code1, out1, _ = _new(monkeypatch, db, "t", "--source", "roadmap:row-1")
    item_id = json.loads(out1)["data"]["item_id"]
    code2, out2, err2 = _new(monkeypatch, db, "other", "--source", "roadmap:row-1")
    assert code1 == 0
    assert code2 == EXIT_REFUSED
    assert f"already created from this source as {item_id}" in (out2 + err2)
    assert _count_items(monkeypatch, db) == 1


def test_new_sourceless_calls_yield_two_items(monkeypatch, tmp_path) -> None:
    db = _db(tmp_path)
    assert _new(monkeypatch, db, "t")[0] == 0
    assert _new(monkeypatch, db, "t")[0] == 0
    assert _count_items(monkeypatch, db) == 2


def test_new_same_source_different_id_refuses_naming_first(monkeypatch, tmp_path) -> None:
    db = _db(tmp_path)
    assert _new(monkeypatch, db, "t", "--source", "s-id", "--id", "A")[0] == 0
    code, out, err = _new(monkeypatch, db, "t", "--source", "s-id", "--id", "B")
    assert code == EXIT_REFUSED
    assert "already created from this source as A" in (out + err)
    assert _count_items(monkeypatch, db) == 1


def _hide_first_prior_lookup(monkeypatch) -> None:
    """Make the first item.created lookup see nothing, so the store's
    IdempotencyConflict (not the pre-check) is what `new` has to handle."""
    from ledger.store import Ledger

    real = Ledger.events
    calls = {"n": 0}

    def events(self, *a, **kw):
        if kw.get("kind") == "item.created":
            calls["n"] += 1
            if calls["n"] == 1:
                return []
        return real(self, *a, **kw)

    monkeypatch.setattr(Ledger, "events", events)


def test_new_race_same_payload_other_agent_is_noop(monkeypatch, tmp_path) -> None:
    db = _db(tmp_path)
    code1, out1, _ = _new(monkeypatch, db, "t", "--source", "s-race")
    _hide_first_prior_lookup(monkeypatch)
    code2, out2, err2 = _new(monkeypatch, db, "t", "--source", "s-race",
                             env={"LEDGER_AGENT": "other"})
    assert code1 == 0 and code2 == 0, err2
    assert json.loads(out1)["data"]["item_id"] == json.loads(out2)["data"]["item_id"]
    assert _count_items(monkeypatch, db) == 1


def test_new_race_different_title_refuses_naming_item(monkeypatch, tmp_path) -> None:
    db = _db(tmp_path)
    _code, out1, _ = _new(monkeypatch, db, "t", "--source", "s-race2")
    item_id = json.loads(out1)["data"]["item_id"]
    _hide_first_prior_lookup(monkeypatch)
    code, out, err = _new(monkeypatch, db, "other", "--source", "s-race2")
    assert code == EXIT_REFUSED
    assert f"already created from this source as {item_id}" in (out + err)


def test_new_source_not_utf8_is_usage_error(monkeypatch, tmp_path) -> None:
    code, out, err = _new(monkeypatch, _db(tmp_path), "t", "--source", "bad\udcffbyte")
    assert code == EXIT_USAGE
    assert "UTF-8" in (out + err)


def test_new_blank_source_is_usage_error(monkeypatch, tmp_path) -> None:
    for blank in ("", "   "):
        code, _out, _err = _new(monkeypatch, _db(tmp_path), "t", "--source", blank)
        assert code == EXIT_USAGE
