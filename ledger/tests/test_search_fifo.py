"""A non-regular file at the search index path is never opened.

A read-only SQLite open blocks on a FIFO instead of failing, so every check
here runs the command line in a subprocess with a timeout: a hang is a
failure, never a stuck suite. Uses the fixtures of ``test_search.py``; every
file is synthetic and built under tmp_path.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ledger import search as search_mod
from ledger.verbs import EXIT_OK, EXIT_REFUSED

from test_search import db, root  # noqa: F401  (fixtures)

PKG_PARENT = Path(__file__).resolve().parent.parent.parent  # makes ``python -m ledger`` importable
TIMEOUT = 20

# Runs the command line with build_index wrapped: after the real build, a
# FIFO is renamed over the fresh index, so the search's own open meets a FIFO.
PLANT_AFTER_BUILD = """
import os, sys
from ledger import search
from ledger.cli import main
real_build = search.build_index
def build_then_plant(db, *args, **kwargs):
    summary = real_build(db, *args, **kwargs)
    index = search.search_db_path(db)
    planted = index.with_name(".planted.fifo")
    os.mkfifo(planted)
    os.replace(planted, index)
    return summary
search.build_index = build_then_plant
sys.exit(main(sys.argv[1:]))
"""


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env.pop("HARNESS_ROOT", None)
    env.update(LEDGER_HARNESS="pytest", PYTHONPATH=str(PKG_PARENT))
    return env


def _cli(db: Path, *argv: str, driver: str | None = None) -> tuple[int, dict]:
    """Run the command line with ``--json``; a hang past TIMEOUT fails the test."""
    head = ["-c", driver] if driver is not None else ["-m", "ledger"]
    try:
        done = subprocess.run(
            [sys.executable, *head, "--json", "--db", str(db), *argv],
            cwd=PKG_PARENT, env=_env(), capture_output=True, text=True,
            timeout=TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"ledger {' '.join(argv)} blocked for {TIMEOUT}s on the search index path")
    assert "Traceback" not in done.stderr, done.stderr
    return done.returncode, json.loads(done.stdout)


def _index(db: Path) -> Path:
    return db.parent / "search.db"


def _mode(path: Path) -> int:
    return os.lstat(path).st_mode


# --- a FIFO at the index path ------------------------------------------------


def test_search_over_a_fifo_rebuilds_the_index_in_its_place(db):
    os.mkfifo(_index(db))
    code, body = _cli(db, "search", "quokka")
    assert code == EXIT_OK, body
    assert body["data"]["refreshed"] is True
    assert body["data"]["cards"][0]["doc_type"] == "utterance"
    assert stat.S_ISREG(_mode(_index(db)))
    assert list(db.parent.glob(".search.db.*.partial")) == []


def test_doctor_names_the_fifo_as_a_missing_index_without_opening_it(db):
    os.mkfifo(_index(db))
    code, body = _cli(db, "doctor")
    assert code == EXIT_OK, body
    found = body["data"]["search"]
    assert found["status"] == "missing"
    assert found["reasons"] == [f"search.db is a FIFO, not a regular file: {_index(db)}"]
    # doctor writes nothing: the FIFO is still there.
    assert stat.S_ISFIFO(_mode(_index(db)))


def test_index_over_a_fifo_replaces_it(db):
    os.mkfifo(_index(db))
    code, body = _cli(db, "index")
    assert code == EXIT_OK, body
    assert body["data"]["rebuilt"] is True
    assert stat.S_ISREG(_mode(_index(db)))


def test_a_fifo_at_the_index_after_the_build_is_refused_by_name(db):
    # The search's own open checks the path too: a FIFO planted after the
    # rebuild is refused, never opened.
    code, body = _cli(db, "search", "quokka", driver=PLANT_AFTER_BUILD)
    assert code == EXIT_REFUSED, body
    assert body["error"]["code"] == "search"
    assert body["error"]["message"] == (
        f"search.db is a FIFO, not a regular file: {_index(db)}"
    )


# --- a directory at the index path -----------------------------------------


@pytest.mark.parametrize("verb", [["search", "quokka"], ["index"], ["index", "--rebuild"]])
def test_a_directory_at_the_index_path_is_refused_by_name(db, verb):
    _index(db).mkdir()
    (_index(db) / "kept").write_text("kept")
    code, body = _cli(db, *verb)
    assert code == EXIT_REFUSED, body
    assert body["error"]["code"] == "search"
    message = body["error"]["message"]
    assert message.startswith(
        f"cannot replace the directory at the search index path {_index(db)}: "
    ), message
    assert (_index(db) / "kept").read_text() == "kept"
    assert list(db.parent.glob(".search.db.*.partial")) == []


def test_doctor_names_a_directory_as_a_missing_index(db):
    _index(db).mkdir()
    code, body = _cli(db, "doctor")
    assert code == EXIT_OK, body
    assert body["data"]["search"]["status"] == "missing"
    assert body["data"]["search"]["reasons"] == [
        f"search.db is a directory, not a regular file: {_index(db)}"
    ]


# --- a symlink at the index path is not followed ------------------------------


def test_a_symlink_to_a_fifo_is_replaced_and_its_target_left_alone(db, tmp_path):
    target = tmp_path / "elsewhere.fifo"
    os.mkfifo(target)
    _index(db).symlink_to(target)
    code, body = _cli(db, "doctor")
    assert code == EXIT_OK, body
    assert body["data"]["search"]["reasons"] == [
        f"search.db is a symbolic link, not a regular file: {_index(db)}"
    ]
    code, body = _cli(db, "search", "quokka")
    assert code == EXIT_OK, body
    assert stat.S_ISREG(_mode(_index(db)))
    assert stat.S_ISFIFO(_mode(target))


def test_a_symlink_to_a_good_index_is_not_read_through(db, tmp_path):
    code, _body = _cli(db, "index")
    assert code == EXIT_OK
    moved = tmp_path / "moved.db"
    os.replace(_index(db), moved)
    _index(db).symlink_to(moved)
    status = search_mod.index_status(db)
    assert status["status"] == "missing"
    assert status["reasons"] == [f"search.db is a symbolic link, not a regular file: {_index(db)}"]


# --- absent, and an lstat that fails ------------------------------------------


def test_an_absent_index_keeps_its_missing_reason(db):
    assert search_mod.index_status(db)["reasons"] == ["no search.db"]
    with pytest.raises(search_mod.IndexUnavailable, match=r"^no search\.db$"):
        search_mod.stored_manifest(_index(db))


def test_an_lstat_failure_refuses_by_name_and_never_opens(db, monkeypatch):
    code, _body = _cli(db, "index")
    assert code == EXIT_OK
    index = _index(db)
    real_lstat = os.lstat
    opened: list[str] = []
    real_connect = sqlite3.connect

    def denied(path, *args, **kwargs):
        if Path(path) == index:
            raise PermissionError(13, "Permission denied", str(path))
        return real_lstat(path, *args, **kwargs)

    def connect(target, *args, **kwargs):
        opened.append(str(target))
        return real_connect(target, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", denied)
    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(search_mod.SearchError, match=r"^cannot stat the search index ") as raised:
        search_mod.stored_manifest(index)
    assert not isinstance(raised.value, search_mod.IndexUnavailable)
    assert str(index) in str(raised.value)
    status = search_mod.index_status(db)
    assert status["status"] == "stale"
    assert status["reasons"][0].startswith(f"unreadable index: cannot stat the search index {index}")
    assert not any(target.startswith(index.as_uri()) for target in opened), opened
