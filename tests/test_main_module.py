"""``python3 -m ledger`` runs the CLI from a checkout, with nothing installed."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import ledger
from ledger.store import load_migrations

REPO = Path(__file__).resolve().parents[1]


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env.pop("PYTHONPATH", None)
    env.update(extra)
    return env


def _run(*argv: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ledger", *argv],
        cwd=REPO,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_doctor_on_a_fresh_database(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    env = _env(LEDGER_HARNESS="pytest")

    missing = _run("--json", "--db", str(db), "doctor", env=env)
    assert missing.returncode == 1, missing.stderr
    assert json.loads(missing.stdout)["error"]["code"] == "missing_db"

    created = _run(
        "--json", "--db", str(db), "new", "--type", "ticket", "--title", "synthetic", env=env
    )
    assert created.returncode == 0, created.stderr

    doctor = _run("--json", "--db", str(db), "doctor", env=env)
    assert doctor.returncode == 0, doctor.stderr
    data = json.loads(doctor.stdout)["data"]
    assert data["path"] == str(db.resolve())
    assert data["schema_version"] == max(m.version for m in load_migrations())


def test_exit_code_passes_through(tmp_path: Path) -> None:
    result = _run(
        "--db", str(tmp_path / "ledger.db"), "note", "it-1", "--text", "x", env=_env()
    )
    assert result.returncode == 3, result.stderr
    assert result.stderr.strip() == (
        "LEDGER_HARNESS is required for writes; set it, e.g. "
        "LEDGER_HARNESS=claude-code"
    )


def test_version_flag_prints_the_package_version_constant() -> None:
    result = _run("--version", env=_env())
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"ledger {ledger.__version__}\n"
    assert result.stderr == ""


def test_version_constant_equals_the_pyproject_version() -> None:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    project = text.split("\n[project]\n", 1)[1].split("\n[", 1)[0]
    matches = re.findall(r'^version = "([^"]+)"$', project, flags=re.MULTILINE)
    assert len(matches) == 1, f"pyproject.toml [project] version lines: {matches!r}"
    assert ledger.__version__ == matches[0]
