"""``spec show`` and ``spec diff`` are reads and need no ``LEDGER_HARNESS``;
``spec set`` is the verb's one write and is refused without it."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ledger.store import open_ledger
from ledger.verbs import spec as spec_verb

from test_cli import invoke, seed

ITEM = "it-1"
NO_HARNESS = {"LEDGER_HARNESS": None}
LIB = Path(__file__).resolve().parents[2]


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "ledger.db")
    seed(monkeypatch, path, ITEM)
    for body in ("one\n", "one\ntwo\n"):
        code, _out, err = invoke(
            monkeypatch,
            ["--db", path, "spec", "set", ITEM, "--title", "T", "--body", body],
        )
        assert code == 0, err
    return path


def _spec_event_count(db: str) -> int:
    with open_ledger(db, create=False) as lg:
        return len(spec_verb.spec_events(lg, ITEM))


def test_show_and_diff_run_with_no_harness(db, monkeypatch) -> None:
    assert spec_verb.VERB.writes is False
    for argv in (
        ("spec", "show", ITEM),
        ("spec", "show", ITEM, "--version", "1"),
        ("spec", "diff", ITEM),
        ("spec", "diff", ITEM, "1", "2"),
    ):
        code, out, err = invoke(monkeypatch, ["--json", "--db", db, *argv], env=NO_HARNESS)
        assert code == 0, (argv, err, out)
        assert json.loads(out)["ok"] is True, argv
        code, out, err = invoke(monkeypatch, ["--db", db, *argv], env=NO_HARNESS)
        assert code == 0, (argv, err, out)
        assert out.startswith("spec"), (argv, out)


def test_show_runs_from_a_shell_environment_with_no_harness(db, tmp_path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env["PYTHONPATH"] = str(LIB)
    for argv in (["spec", "show", ITEM], ["spec", "diff", ITEM]):
        done = subprocess.run(
            [sys.executable, "-m", "ledger", "--json", "--db", db, *argv],
            capture_output=True, text=True, env=env, cwd=tmp_path, check=False,
        )
        assert done.returncode == 0, (argv, done.stderr, done.stdout)
        assert json.loads(done.stdout)["ok"] is True, argv


def test_set_still_needs_the_harness_and_writes_nothing(db, monkeypatch) -> None:
    before = _spec_event_count(db)
    for env in (NO_HARNESS, {"LEDGER_HARNESS": ""}):
        code, out, _err = invoke(
            monkeypatch,
            ["--json", "--db", db, "spec", "set", ITEM, "--title", "T2", "--body", "x"],
            env=env,
        )
        assert code == 3, env
        error = json.loads(out)["error"]
        assert error["code"] == "missing_harness", env
        assert error["message"] == (
            "LEDGER_HARNESS is required for writes; set it, e.g. LEDGER_HARNESS=claude-code"
        )
        # The refusal comes before any usage check, as it did for a write verb.
        code, out, _err = invoke(
            monkeypatch, ["--json", "--db", db, "spec", "set", ITEM], env=env
        )
        assert (code, json.loads(out)["error"]["code"]) == (3, "missing_harness"), env
    assert _spec_event_count(db) == before
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "spec", "set", ITEM, "--title", "T2", "--body", "x"],
        env={"LEDGER_HARNESS": "pytest"},
    )
    assert code == 0, err
    assert _spec_event_count(db) == before + 1
