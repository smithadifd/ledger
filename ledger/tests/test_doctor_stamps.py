"""``doctor`` prints the actor stamps a write from this environment would carry.

The contract tells an agent to set LEDGER_OPERATOR, LEDGER_AGENT, LEDGER_HARNESS,
LEDGER_MODEL and LEDGER_SESSION before it writes. On a machine that denies
``env`` and ``printenv`` the agent cannot see them, so ``ledger doctor`` prints
the four stamps a write would record (actor, harness, model, session), and
``--json`` carries them under ``stamps``. An unset stamp is the word
``missing``. Doctor reads no other environment variable.
"""
from __future__ import annotations

import io
import json
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ledger.cli import main
from ledger.session_model import record_hook_model, stamp
from ledger.verbs import EXIT_OK

STAMP_VARS = (
    "LEDGER_OPERATOR",
    "LEDGER_AGENT",
    "LEDGER_HARNESS",
    "LEDGER_MODEL",
    "LEDGER_SESSION",
)
FULL = {
    "LEDGER_OPERATOR": "op-x",
    "LEDGER_AGENT": "seat-y",
    "LEDGER_HARNESS": "harness-z",
    "LEDGER_MODEL": "model-w",
    "LEDGER_SESSION": "session-v",
}
EXPECTED = {
    "actor": "op-x/seat-y",
    "harness": "harness-z",
    "model": "model-w",
    "session": "session-v",
}
# The variables each stamp is filled from.
FEEDS = {
    "actor": ("LEDGER_OPERATOR", "LEDGER_AGENT"),
    "harness": ("LEDGER_HARNESS",),
    "model": ("LEDGER_MODEL",),
    "session": ("LEDGER_SESSION",),
}
NAMES = tuple(FEEDS)


def invoke(monkeypatch, argv, env):
    for key in (*STAMP_VARS, "LEDGER_EXPORT_DIR", "LEDGER_DB"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    assert "Traceback" not in err.getvalue()
    return code, out.getvalue(), err.getvalue()


@pytest.fixture
def db(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "ledger.db"
    code, _, err = invoke(
        monkeypatch,
        ["--db", str(path), "new", "--type", "ticket", "--title", "stamp probe"],
        FULL,
    )
    assert code == EXIT_OK, err
    return path


def doctor_text(monkeypatch, db, env) -> list[str]:
    code, out, err = invoke(monkeypatch, ["--db", str(db), "doctor"], env)
    assert code == EXIT_OK, err
    return out.splitlines()


def doctor_json(monkeypatch, db, env) -> tuple[dict, str]:
    code, out, err = invoke(monkeypatch, ["--json", "--db", str(db), "doctor"], env)
    assert code == EXIT_OK, err
    return json.loads(out)["data"], out


def last_event_stamps(db: Path) -> dict[str, str | None]:
    conn = sqlite3.connect(str(db))
    try:
        row = conn.execute(
            "SELECT actor, harness, model, session FROM events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return dict(zip(NAMES, row))


@pytest.mark.parametrize("name", NAMES)
def test_a_set_stamp_shows_its_value_in_text_and_json(monkeypatch, db, name) -> None:
    lines = doctor_text(monkeypatch, db, FULL)
    assert f"{name}: {EXPECTED[name]}" in lines
    data, _ = doctor_json(monkeypatch, db, FULL)
    assert data["stamps"][name] == EXPECTED[name]


def test_all_four_stamps_are_one_line_each_and_the_only_stamp_keys(monkeypatch, db) -> None:
    lines = doctor_text(monkeypatch, db, FULL)
    stamp_lines = [line for line in lines if line.split(": ", 1)[0] in NAMES]
    assert stamp_lines == [f"{name}: {EXPECTED[name]}" for name in NAMES]
    data, _ = doctor_json(monkeypatch, db, FULL)
    assert data["stamps"] == EXPECTED


@pytest.mark.parametrize("name", NAMES)
def test_an_unset_stamp_shows_missing_in_text_and_json(monkeypatch, db, name) -> None:
    env = {k: v for k, v in FULL.items() if k not in FEEDS[name]}
    lines = doctor_text(monkeypatch, db, env)
    assert f"{name}: missing" in lines
    data, _ = doctor_json(monkeypatch, db, env)
    assert data["stamps"][name] == "missing"
    # The others still show their own values.
    for other in NAMES:
        if other != name:
            assert data["stamps"][other] == EXPECTED[other]
            assert f"{other}: {EXPECTED[other]}" in lines


@pytest.mark.parametrize("name", NAMES)
def test_an_empty_stamp_variable_shows_missing(monkeypatch, db, name) -> None:
    env = {k: ("" if k in FEEDS[name] else v) for k, v in FULL.items()}
    lines = doctor_text(monkeypatch, db, env)
    assert f"{name}: missing" in lines
    data, _ = doctor_json(monkeypatch, db, env)
    assert data["stamps"][name] == "missing"


def test_nothing_set_shows_four_missing_never_an_empty_value(monkeypatch, db) -> None:
    lines = doctor_text(monkeypatch, db, {})
    assert [line for line in lines if line.split(": ", 1)[0] in NAMES] == [
        f"{name}: missing" for name in NAMES
    ]
    data, _ = doctor_json(monkeypatch, db, {})
    assert data["stamps"] == {name: "missing" for name in NAMES}


def test_no_other_environment_variable_is_printed(monkeypatch, db) -> None:
    other = {
        "LEDGER_TEST_SECRET_TOKEN": "tok-9f3a1c-not-a-real-secret",
        "PLAIN_OTHER_VARIABLE": "plain-other-7d2e41",
    }
    env = {**FULL, **other}
    _, text_out, _ = invoke(monkeypatch, ["--db", str(db), "doctor"], env)
    _, json_out = doctor_json(monkeypatch, db, env)
    for out in (text_out, json_out):
        for key, value in other.items():
            assert key not in out
            assert value not in out
    data = json.loads(json_out)["data"]
    assert set(data["stamps"]) == set(NAMES)


def test_doctor_shows_what_a_write_from_the_same_environment_records(monkeypatch, db) -> None:
    data, _ = doctor_json(monkeypatch, db, FULL)
    code, _, err = invoke(
        monkeypatch,
        ["--db", str(db), "new", "--type", "ticket", "--title", "second write"],
        FULL,
    )
    assert code == EXIT_OK, err
    assert last_event_stamps(db) == data["stamps"]


def test_a_recorded_session_model_wins_as_it_does_for_a_write(monkeypatch, db) -> None:
    env = dict(FULL)
    assert record_hook_model(
        str(db), env["LEDGER_SESSION"], "model", "recorded-m", "SessionStart", stamp()
    )
    lines = doctor_text(monkeypatch, db, env)
    assert "model: recorded-m" in lines
    data, _ = doctor_json(monkeypatch, db, env)
    assert data["stamps"]["model"] == "recorded-m"
    code, _, err = invoke(
        monkeypatch,
        ["--db", str(db), "new", "--type", "ticket", "--title", "third write"],
        env,
    )
    assert code == EXIT_OK, err
    assert last_event_stamps(db)["model"] == "recorded-m"


def test_a_control_character_in_a_value_cannot_forge_a_line(monkeypatch, db) -> None:
    env = {**FULL, "LEDGER_AGENT": "evil\nsession: forged"}
    lines = doctor_text(monkeypatch, db, env)
    assert [line for line in lines if line.startswith("session:")] == ["session: session-v"]
    assert "actor: op-x/evil\\nsession: forged" in lines
    data, _ = doctor_json(monkeypatch, db, env)
    assert data["stamps"]["actor"] == "op-x/evil\nsession: forged"
