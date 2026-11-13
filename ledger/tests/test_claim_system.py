"""``ledger claim-system NAME``: claim a database for its system.

The library (``Ledger.claim_system``) decides; the verb resolves the path the
way every write verb does and reports. Each test reads the database file
directly afterwards, so "writes nothing" is an event count, not a message.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ledger import open_ledger
from ledger.verbs import EXIT_REFUSED, EXIT_USAGE, discover_verbs

from test_cli import invoke, seed

_LIB = Path(__file__).resolve().parents[2]


def claim_events(db: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, payload, hash FROM events WHERE kind = 'ledger.system' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "system": json.loads(r[1])["system"], "hash": r[2]} for r in rows]


def event_count(db: str) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()


def claim(monkeypatch, db, name, *, env=None):
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "claim-system", "--", name], env=env)
    return code, (json.loads(out) if out.strip() else None), err


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "ledger.db")


def test_verb_is_registered_and_writes() -> None:
    verb = discover_verbs()["claim-system"]
    assert verb.writes is True


def test_claims_an_unclaimed_database(db, monkeypatch) -> None:
    code, payload, err = claim(monkeypatch, db, "work")
    assert code == 0, err
    assert payload["ok"] is True
    assert payload["data"]["system"] == "work"
    assert payload["data"]["claimed"] is True
    assert payload["data"]["path"] == str(Path(db).resolve())
    assert [e["system"] for e in claim_events(db)] == ["work"]
    with open_ledger(db) as lg:
        assert lg.system() == "work"
        lg.verify(system="work")


def test_claims_a_database_that_already_holds_events(db, monkeypatch) -> None:
    seed(monkeypatch, db, "it-1")
    before = event_count(db)
    code, payload, err = claim(monkeypatch, db, "home")
    assert code == 0, err
    assert payload["data"]["claimed"] is True
    assert event_count(db) == before + 1
    assert [e["system"] for e in claim_events(db)] == ["home"]


def test_text_output_names_the_system_and_path(db, monkeypatch) -> None:
    code, out, err = invoke(monkeypatch, ["--db", db, "claim-system", "work"])
    assert code == 0, err
    assert out.strip() == f"claimed for work: {Path(db).resolve()}"
    code, out, err = invoke(monkeypatch, ["--db", db, "claim-system", "work"])
    assert code == 0, err
    assert out.strip() == f"already claimed for work: {Path(db).resolve()}"


def test_same_name_is_a_no_op(db, monkeypatch) -> None:
    assert claim(monkeypatch, db, "work")[0] == 0
    before_events = event_count(db)
    before_claims = claim_events(db)
    code, payload, err = claim(monkeypatch, db, "work")
    assert code == 0, err
    assert payload["ok"] is True
    assert payload["data"]["claimed"] is False
    assert payload["data"]["system"] == "work"
    assert event_count(db) == before_events
    assert claim_events(db) == before_claims


def test_another_name_is_refused_and_writes_nothing(db, monkeypatch) -> None:
    assert claim(monkeypatch, db, "work")[0] == 0
    before_events = event_count(db)
    before_claims = claim_events(db)
    code, payload, err = claim(monkeypatch, db, "home")
    assert code == EXIT_REFUSED
    assert payload["ok"] is False
    assert payload["error"]["code"] == "shared_database"
    assert "'work'" in payload["error"]["message"] and "'home'" in payload["error"]["message"]
    assert event_count(db) == before_events
    assert claim_events(db) == before_claims
    with open_ledger(db) as lg:
        assert lg.system() == "work"


def test_synced_path_is_refused_and_creates_nothing(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "Dropbox" / "ledger.db"
    code, payload, err = claim(monkeypatch, str(db), "work")
    assert code == EXIT_REFUSED
    assert payload["ok"] is False
    assert payload["error"]["code"] == "synced_path"
    assert not db.exists()
    assert not db.parent.exists()


def test_linked_path_is_refused_and_leaves_the_target_alone(tmp_path: Path, monkeypatch) -> None:
    real = str(tmp_path / "real.db")
    assert claim(monkeypatch, real, "work")[0] == 0
    before = event_count(real)
    link = tmp_path / "alias.db"
    link.symlink_to(real)
    code, payload, err = claim(monkeypatch, str(link), "home")
    assert code == EXIT_REFUSED
    assert payload["error"]["code"] == "db_symlink"
    assert event_count(real) == before
    assert [e["system"] for e in claim_events(real)] == ["work"]


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "   ",
        "\t",
        "Work",
        "work ",
        " work",
        "wo rk",
        "work\n",
        "9work",
        "-work",
        "_work",
        "work.db",
        "w\u00f6rk",
        "x" * 65,
    ],
)
def test_an_invalid_name_is_a_usage_error_and_creates_nothing(
    tmp_path: Path, monkeypatch, name: str
) -> None:
    db = tmp_path / "sub" / "ledger.db"
    code, payload, err = claim(monkeypatch, str(db), name)
    assert code == EXIT_USAGE
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage"
    assert "lowercase letter" in payload["error"]["message"]
    assert not db.exists()
    assert not db.parent.exists()


@pytest.mark.parametrize("name", ["w", "work", "home-2", "a_b-c9", "x" * 64])
def test_a_valid_name_is_accepted(db, monkeypatch, name: str) -> None:
    code, payload, err = claim(monkeypatch, db, name)
    assert code == 0, err
    assert claim_events(db)[0]["system"] == name


def test_an_invalid_name_on_a_claimed_database_writes_nothing(db, monkeypatch) -> None:
    assert claim(monkeypatch, db, "work")[0] == 0
    before = event_count(db)
    code, _payload, _err = claim(monkeypatch, db, "Work")
    assert code == EXIT_USAGE
    assert event_count(db) == before


def test_a_write_needs_the_harness_and_creates_nothing_without_it(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    code, payload, err = claim(monkeypatch, str(db), "work", env={"LEDGER_HARNESS": None})
    assert code == EXIT_REFUSED
    assert payload["error"]["code"] == "missing_harness"
    assert not db.exists()


def test_a_missing_name_is_a_usage_error(db, monkeypatch) -> None:
    code, _out, _err = invoke(monkeypatch, ["--db", db, "claim-system"])
    assert code == EXIT_USAGE
    assert not Path(db).exists()


def test_two_systems_claiming_at_once_leave_one_claim(tmp_path: Path) -> None:
    db = str(tmp_path / "ledger.db")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    env["LEDGER_HARNESS"] = "pytest"
    procs = {
        name: subprocess.Popen(
            [sys.executable, "-m", "ledger", "--json", "--db", db, "claim-system", name],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for name in ("work", "home")
    }
    codes = {}
    for name, proc in procs.items():
        proc.communicate(timeout=60)
        codes[name] = proc.returncode
    assert sorted(codes.values()) == [0, EXIT_REFUSED], codes
    winner = next(name for name, code in codes.items() if code == 0)
    assert [e["system"] for e in claim_events(db)] == [winner]


def claim_row(db: str) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT actor, harness, model, session FROM events WHERE kind = 'ledger.system'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    return dict(rows[0])


def test_the_claim_event_carries_the_callers_attribution(db, monkeypatch) -> None:
    env = {
        "LEDGER_HARNESS": "claude-code",
        "LEDGER_OPERATOR": "alice",
        "LEDGER_AGENT": "builder",
        "LEDGER_MODEL": "model-x",
        "LEDGER_SESSION": "sess-42",
    }
    code, payload, err = claim(monkeypatch, db, "work", env=env)
    assert code == 0, err
    assert claim_row(db) == {
        "actor": "alice/builder",
        "harness": "claude-code",
        "model": "model-x",
        "session": "sess-42",
    }


def test_the_library_claim_returns_whether_it_wrote(db) -> None:
    with open_ledger(db) as lg:
        assert lg.claim_system("work") is True
        assert lg.claim_system("work") is False
    assert [e["system"] for e in claim_events(db)] == ["work"]


def test_the_library_claim_stamps_only_what_it_is_given(db) -> None:
    with open_ledger(db) as lg:
        lg.claim_system("work")
    assert claim_row(db) == {"actor": None, "harness": None, "model": None, "session": None}


def test_the_report_comes_from_the_claim_not_from_a_read_before_it(
    db, monkeypatch
) -> None:
    # A caller whose read of the claim ran before another caller's commit sees
    # no claim. Play that interleaving: the database is claimed, and
    # Ledger.system() still says it is not. The verb must report the library's
    # answer ("already claimed"), not a stale read ("claimed").
    from ledger.store import Ledger

    with open_ledger(db) as lg:
        lg.claim_system("work")
    monkeypatch.setattr(Ledger, "system", lambda self: None)
    code, payload, err = claim(monkeypatch, db, "work")
    assert code == 0, err
    assert payload["data"]["claimed"] is False
    assert [e["system"] for e in claim_events(db)] == ["work"]


def test_two_callers_claiming_the_same_name_at_once_report_one_claim(tmp_path: Path) -> None:
    db = str(tmp_path / "ledger.db")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    env["LEDGER_HARNESS"] = "pytest"
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "ledger", "--json", "--db", db, "claim-system", "work"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    reports = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        reports.append(json.loads(out)["data"]["claimed"])
    assert sorted(reports) == [False, True], reports
    assert [e["system"] for e in claim_events(db)] == ["work"]


def test_verify_system_reads_the_claim_the_verb_wrote(db, monkeypatch) -> None:
    assert claim(monkeypatch, db, "work")[0] == 0
    code, out, err = invoke(monkeypatch, ["--db", db, "verify", "--system", "work"])
    assert code == 0, err
    code, out, err = invoke(monkeypatch, ["--db", db, "verify", "--system", "home"])
    assert code == EXIT_REFUSED

