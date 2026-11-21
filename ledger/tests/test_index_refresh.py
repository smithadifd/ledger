"""``ledger index`` on a behind index appends the new events; ``severity``.

Uses the fixtures of ``test_search.py``; every ledger and markdown file is
synthetic and built under tmp_path.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from ledger import search as search_mod
from ledger.store import open_ledger, open_ledger_readonly
from ledger.verbs import EXIT_OK, EXIT_REFUSED

from test_search import (  # noqa: F401
    _append,
    _doctor_cannot_refresh,
    _rewrite_stored_manifest,
    db,
    invoke,
    root,
    run_json,
)


def _index(db: Path) -> Path:
    return db.parent / "search.db"


REPO_ROOT = Path(__file__).resolve().parents[2]


def _lock_path(db: Path) -> Path:
    """The index writers' shared lock file beside the index."""
    return db.parent / ".search.db.lock"


def _fresh_lock_granted(db: Path, flags: int) -> bool:
    """Whether a new descriptor on the lock file gets ``flags`` right now.

    flock belongs to the open file, so this descriptor conflicts with any
    other one, in this process or another, that holds an incompatible lock.
    """
    fd = os.open(_lock_path(db), os.O_RDONLY)
    try:
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError:
            return False
        return True
    finally:
        os.close(fd)


# A writer in its own process: a real full rebuild that stops just before
# its publish, holding its shared lock and its temp, until ``go`` appears.
_PAUSED_WRITER = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "from ledger import search as search_mod\n"
    "db = Path(sys.argv[1])\n"
    "ready = Path(sys.argv[2])\n"
    "go = Path(sys.argv[3])\n"
    "real = search_mod._replace_index\n"
    "def paused(tmp, index, ledger):\n"
    "    note = ready.with_name(ready.name + '.tmp')\n"
    "    note.write_text(str(tmp), encoding='utf-8')\n"
    "    note.replace(ready)\n"
    "    deadline = time.time() + 60\n"
    "    while not go.exists():\n"
    "        if time.time() > deadline:\n"
    "            raise SystemExit(3)\n"
    "        time.sleep(0.01)\n"
    "    real(tmp, index, ledger)\n"
    "search_mod._replace_index = paused\n"
    "search_mod.build_index(db)\n"
)


def _start_paused_writer(db: Path) -> tuple[subprocess.Popen, Path, Path]:
    """Start ``_PAUSED_WRITER`` and wait until it holds its temp.

    Returns the child, its temp and the file that lets it publish.
    """
    ready = db.parent / "writer-ready"
    go = db.parent / "writer-go"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    child = subprocess.Popen(
        [sys.executable, "-c", _PAUSED_WRITER, str(db), str(ready), str(go)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.time() + 60
    while not ready.exists():
        if child.poll() is not None:
            _out, err = child.communicate()
            raise AssertionError(f"writer exited early {child.returncode}: {err!r}")
        if time.time() > deadline:
            child.kill()
            child.wait(timeout=10)
            raise AssertionError("writer did not reach its publish")
        time.sleep(0.02)
    return child, Path(ready.read_text(encoding="utf-8")), go


def _stop(child: subprocess.Popen) -> None:
    if child.poll() is None:
        child.kill()
        child.wait(timeout=10)


def _snapshot(db: Path) -> dict:
    """Everything a search reads, without rowids, so two builds compare."""
    conn = sqlite3.connect(_index(db).as_uri() + "?mode=ro", uri=True)
    try:
        docs = sorted(
            tuple("" if v is None else str(v) for v in row)
            for row in conn.execute(
                "SELECT f.doc_type, f.doc_id, f.title, f.content, p.path, m.item_id, "
                "m.status, m.date, m.ts FROM fts_corpus f "
                "LEFT JOIN doc_path p ON p.rowid = f.rowid "
                "LEFT JOIN doc_meta m ON m.rowid = f.rowid"
            )
        )
        links = sorted(conn.execute("SELECT item_id, initiative FROM item_link"))
        manifest = json.loads(
            conn.execute("SELECT value FROM manifest WHERE key = 'manifest'").fetchone()[0])
        orphans = conn.execute(
            "SELECT (SELECT COUNT(*) FROM doc_meta WHERE rowid NOT IN "
            "(SELECT rowid FROM fts_corpus)) + (SELECT COUNT(*) FROM doc_path WHERE "
            "rowid NOT IN (SELECT rowid FROM fts_corpus))"
        ).fetchone()[0]
    finally:
        conn.close()
    return {"docs": docs, "links": links, "manifest": manifest, "orphans": orphans}


def _doctor_search(monkeypatch, db: Path) -> dict:
    code, body = run_json(monkeypatch, db, "doctor")
    assert code == EXIT_OK, body
    return body["data"]["search"]


# --- a behind index is brought up to date by appending ---------------------


def test_one_new_event_reads_behind_info_and_index_makes_it_current(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "a fresh note about the capybara"}, "k9")
    status = search_mod.index_status(db)
    assert status["status"] == "behind"
    assert status["new_events"] == 1
    assert status["severity"] == "info"
    with _doctor_cannot_refresh(monkeypatch):
        assert _doctor_search(monkeypatch, db)["severity"] == "info"
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert body["data"]["refresh"] == "append"
    assert body["data"]["new_events"] == 1
    assert body["data"]["status"] == "current"
    status = search_mod.index_status(db)
    assert status["status"] == "current"
    assert status["severity"] == "ok"
    assert _doctor_search(monkeypatch, db)["severity"] == "ok"
    code, body = run_json(monkeypatch, db, "search", "capybara")
    assert code == EXIT_OK, body
    assert body["data"]["refreshed"] is False
    assert body["data"]["cards"][0]["id"] == "T-feed"


def test_index_text_names_the_appended_events(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "one"}, "k9")
        _append(lg, "item.note", "T-feed", {"text": "two"}, "k10")
    code, out, _err = invoke(monkeypatch, ["--db", str(db), "index"])
    assert code == EXIT_OK
    assert ", appending 2 new events" in out.splitlines()[0]


def test_an_append_holds_what_a_full_rebuild_holds(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        # A note, a status change (the item's question takes the new status),
        # a new item, a question on another item, an initiative link, and a
        # person utterance on no item.
        _append(lg, "item.note", "T-feed", {"text": "the okapi follow-up"}, "k9")
        _append(lg, "item.state", "T-feed", {"status": "closed"}, "k10")
        _append(lg, "item.created", "T-new", {"type": "ticket", "title": "Gecko intake"}, "k11")
        _append(lg, "decision.asked", "T-zephyr", {
            "question": "Which ibis window?", "options": [{"label": "a"}, {"label": "b"}],
        }, "k12")
        _append(lg, "item.linked", "T-new", {"rel": "item", "target": "T-zephyr"}, "k13")
        _append(lg, "utterance.captured", None,
                {"text": "the tapir call is fine", "origin": "person"}, "k14")
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert body["data"]["refresh"] == "append"
    assert body["data"]["new_events"] == 6
    appended = _snapshot(db)
    assert appended["orphans"] == 0
    assert any(doc[0] == "decision" and doc[6] == "closed" for doc in appended["docs"])
    code, body = run_json(monkeypatch, db, "index", "--rebuild")
    assert body["data"]["refresh"] == "rebuild"
    rebuilt = _snapshot(db)
    assert appended == rebuilt
    assert body["data"]["docs"] == len(appended["docs"])


def test_a_search_on_a_behind_index_appends_without_rebuilding(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-zephyr", {"text": "the pangolin schedule"}, "k9")

    def no_rebuild(*_args, **_kwargs):
        raise AssertionError("a behind index was rebuilt from scratch")

    monkeypatch.setattr(search_mod, "build_index", no_rebuild)
    code, body = run_json(monkeypatch, db, "search", "pangolin")
    assert code == EXIT_OK, body
    assert body["data"]["refreshed"] is True
    assert body["data"]["cards"][0]["id"] == "T-zephyr"
    assert search_mod.index_status(db)["status"] == "current"


def test_an_append_leaves_no_partial_file_and_keeps_the_index_private(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "after"}, "k9")
    run_json(monkeypatch, db, "index")
    assert not list(db.parent.glob(".search.db.*.partial"))
    assert stat.S_IMODE(_index(db).stat().st_mode) & 0o077 == 0


# --- anything but behind takes the full rebuild ----------------------------


def test_a_changed_source_with_new_events_takes_the_full_rebuild(db, root, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "after"}, "k9")
    path = root / "standards/routing.md"
    path.write_text(path.read_text() + "\nthe heron lane\n")
    os.utime(path, ns=(1, 1))
    assert search_mod.index_status(db)["status"] == "stale"
    code, body = run_json(monkeypatch, db, "index")
    assert body["data"]["refresh"] == "rebuild"
    assert search_mod.index_status(db)["status"] == "current"
    code, body = run_json(monkeypatch, db, "search", "heron")
    assert body["data"]["cards"][0]["doc_type"] == "standard"


def test_a_rewritten_event_log_takes_the_full_rebuild(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "after"}, "k9")
    _rewrite_stored_manifest(db, last_event_hash="0" * 64)
    code, body = run_json(monkeypatch, db, "index")
    assert body["data"]["refresh"] == "rebuild"
    assert search_mod.index_status(db)["status"] == "current"


def test_an_index_whose_copy_reads_current_is_rebuilt_not_appended(db, monkeypatch):
    # The precheck said behind, but the copy the append works on is already
    # current (another writer caught up): nothing is appended.
    run_json(monkeypatch, db, "index")
    summary = search_mod.refresh_index(db)
    assert summary["refresh"] == "rebuild"


def test_an_error_in_the_append_falls_back_to_the_full_rebuild(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "the quetzal note"}, "k9")

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(search_mod, "_append_into", broken)
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert body["data"]["refresh"] == "rebuild"
    assert not list(db.parent.glob(".search.db.*.partial"))
    code, body = run_json(monkeypatch, db, "search", "quetzal")
    assert body["data"]["cards"][0]["id"] == "T-feed"


# --- severity ---------------------------------------------------------------


@pytest.mark.parametrize("status, severity", [
    ("current", "ok"), ("behind", "info"), ("stale", "info"), ("missing", "info"),
    ("unknown", "warn"), ("other", "warn"),
])
def test_severity_for_each_status(status, severity):
    assert search_mod.severity_for(status) == severity


def test_missing_and_stale_read_info(db, root, monkeypatch):
    assert search_mod.index_status(db)["severity"] == "info"
    run_json(monkeypatch, db, "index")
    (root / "standards/new.md").write_text("# New\n")
    status = search_mod.index_status(db)
    assert (status["status"], status["severity"]) == ("stale", "info")


def test_a_build_error_reads_warn(db, monkeypatch):
    # search_roots that no build can use: the index cannot be built.
    (db.parent / "config.json").write_text(json.dumps({"search_roots": "not-a-list"}))
    status = search_mod.index_status(db)
    assert status["status"] == "missing"
    assert status["severity"] == "warn"
    assert status["reasons"][-1] == "search_roots must be a list of absolute paths"
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_REFUSED
    assert body["error"]["code"] == "search"


def test_unusable_search_roots_with_an_index_reads_warn_not_a_raise(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    (db.parent / "config.json").write_text(json.dumps({"search_roots": "not-a-list"}))
    status = search_mod.index_status(db)
    assert status["status"] == "unknown"
    assert status["severity"] == "warn"
    assert status["reasons"] == ["search_roots must be a list of absolute paths"]
    found = _doctor_search(monkeypatch, db)
    assert (found["status"], found["severity"]) == ("unknown", "warn")
    assert found["reasons"] == ["search_roots must be a list of absolute paths"]
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_REFUSED
    assert body["error"]["code"] == "search"


def test_an_unreadable_config_with_an_index_reads_unknown_warn_in_doctor(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    (db.parent / "config.json").write_text("{not json")
    found = _doctor_search(monkeypatch, db)
    assert (found["status"], found["severity"]) == ("unknown", "warn")


def test_a_directory_at_the_index_path_reads_warn(db):
    _index(db).mkdir()
    status = search_mod.index_status(db)
    assert status["status"] == "missing"
    assert status["severity"] == "warn"
    assert status["reasons"] == [
        f"search.db is a directory, not a regular file: {_index(db)}"]


def test_an_index_inside_a_git_work_tree_reads_warn(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    assert search_mod.index_status(db)["severity"] == "ok"
    (db.parent / ".git").mkdir()
    status = search_mod.index_status(db)
    assert status["status"] == "current"
    assert status["severity"] == "warn"
    assert status["reasons"] == [
        f"refusing to use search.db inside a git work tree: {db.parent}"]


# --- a fresh install's first index ------------------------------------------


def test_an_index_without_roots_says_only_the_ledger_is_indexed_and_exits_0(db, monkeypatch):
    config = db.parent / "config.json"
    config.unlink()
    code, out, err = invoke(monkeypatch, ["--db", str(db), "index"])
    assert code == EXIT_OK, err
    lines = out.splitlines()
    assert lines[1] == (
        "only the ledger is indexed (search_roots is not set in the ledger's "
        "config.json); to index markdown too, set search_roots to a list of "
        f"absolute paths in {config}"
    )
    assert "no markdown roots" not in out


# --- leftover writer temps ---------------------------------------------------


def _write_index_schema(path: Path, statements) -> None:
    """A SQLite file at ``path`` built by running ``statements`` and closing."""
    conn = sqlite3.connect(path)
    try:
        for statement in statements:
            conn.execute(statement)
        conn.commit()
    finally:
        conn.close()


def _stale_index_partial(directory: Path, middle: str) -> Path:
    """What a killed rebuild leaves, under the index writer's temp name.

    A SQLite file holding the tables the writer's own schema statements
    (``search_mod.SCHEMA_SQL``) create, with no side file beside it.
    """
    path = directory / f".search.db.{middle}.partial"
    _write_index_schema(path, search_mod.SCHEMA_SQL)
    os.utime(path, ns=(1, 1))
    return path


def test_a_planted_stale_index_partial_is_removed_on_rebuild(db, monkeypatch):
    leftover = _stale_index_partial(db.parent, "stale3")
    decoy_backup = db.parent / f".{db.name}.stale3.partial"
    decoy_backup.write_bytes(b"backup-temp")
    os.utime(decoy_backup, ns=(1, 1))
    ledger_before = db.read_bytes()
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert not leftover.exists()
    assert decoy_backup.read_bytes() == b"backup-temp"
    assert db.read_bytes() == ledger_before
    assert search_mod.index_status(db)["status"] == "current"
    assert (db.parent / "search.db").is_file()


def test_a_planted_stale_index_partial_is_removed_on_the_next_append_refresh(
    db, monkeypatch
):
    run_json(monkeypatch, db, "index")
    leftover = _stale_index_partial(db.parent, "stale1")
    decoy_backup = db.parent / f".{db.name}.stale1.partial"
    decoy_backup.write_bytes(b"backup-temp")
    os.utime(decoy_backup, ns=(1, 1))
    decoy_contains = db.parent / "note.search.db.stale1.partial"
    decoy_contains.write_bytes(b"contains-pattern")
    os.utime(decoy_contains, ns=(1, 1))
    decoy_no_dot = db.parent / "search.db.stale1.partial"
    decoy_no_dot.write_bytes(b"no-dot")
    os.utime(decoy_no_dot, ns=(1, 1))
    other_dir = db.parent / "other"
    other_dir.mkdir()
    decoy_elsewhere = _stale_index_partial(other_dir, "stale1")
    elsewhere_bytes = decoy_elsewhere.read_bytes()
    link = db.parent / ".search.db.link1.partial"
    link.symlink_to(leftover)
    named_dir = db.parent / ".search.db.dir1.partial"
    named_dir.mkdir()
    empty_middle = db.parent / ".search.db.partial"
    empty_middle.write_bytes(b"empty-middle")
    os.utime(empty_middle, ns=(1, 1))
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "the ibis note"}, "k-partial-1")
    ledger_before = db.read_bytes()
    code, out, err = invoke(monkeypatch, ["--db", str(db), "index"])
    assert code == EXIT_OK, err
    remaining = {p.name for p in db.parent.iterdir()}
    assert leftover.name not in remaining
    assert decoy_backup.is_file(), "a refresh removed another file's partial"
    assert db.is_file(), "a refresh removed the ledger database"
    assert decoy_backup.read_bytes() == b"backup-temp"
    assert decoy_contains.read_bytes() == b"contains-pattern"
    assert decoy_no_dot.read_bytes() == b"no-dot"
    assert decoy_elsewhere.read_bytes() == elsewhere_bytes
    assert link.is_symlink()
    assert named_dir.is_dir()
    assert empty_middle.read_bytes() == b"empty-middle"
    assert db.read_bytes() == ledger_before
    assert search_mod.index_status(db)["status"] == "current"


def test_the_running_rebuild_keeps_its_own_partial_until_it_is_published(
    db, monkeypatch
):
    leftover = _stale_index_partial(db.parent, "stale4")
    seen: dict = {}
    real = search_mod._replace_index

    def wrap(tmp, index, ledger):
        seen["own_name"] = tmp.name
        seen["own_existed"] = tmp.is_file()
        seen["stale_during"] = leftover.is_file()
        assert tmp.name != leftover.name
        real(tmp, index, ledger)

    monkeypatch.setattr(search_mod, "_replace_index", wrap)
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert seen["own_existed"] is True
    assert seen["stale_during"] is True
    assert not leftover.exists()
    assert not (db.parent / seen["own_name"]).exists()


def test_a_partial_removal_error_is_one_stderr_line_and_keeps_the_new_index(
    db, monkeypatch
):
    leftover = _stale_index_partial(db.parent, "stale5")
    planted = leftover.read_bytes()
    real_unlink = os.unlink

    def flaky(path, *args, **kwargs):
        if Path(path) == leftover:
            raise OSError(13, "Permission denied")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", flaky)
    code, out, err = invoke(monkeypatch, ["--db", str(db), "index"])
    assert code == EXIT_OK, err
    assert leftover.is_file()
    assert leftover.read_bytes() == planted
    lines = [ln for ln in err.splitlines() if ln]
    assert len(lines) == 1, err
    assert lines[0].startswith("cannot remove leftover search index partial ")
    assert leftover.name in lines[0]
    assert "\n" not in lines[0]
    assert search_mod.index_status(db)["status"] == "current"
    assert _index(db).is_file()


def test_a_listing_error_during_cleanup_leaves_the_partial_and_the_index(
    db, monkeypatch
):
    leftover = _stale_index_partial(db.parent, "stale8")
    real_replace = search_mod._replace_index
    real_listdir = os.listdir

    def replace_then_break(tmp, index, ledger):
        real_replace(tmp, index, ledger)

        def boom(path):
            if Path(path) == db.parent:
                raise OSError(13, "Permission denied")
            return real_listdir(path)

        monkeypatch.setattr(os, "listdir", boom)

    monkeypatch.setattr(search_mod, "_replace_index", replace_then_break)
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert leftover.is_file()
    assert search_mod.index_status(db)["status"] == "current"


def test_doctor_refresh_removes_a_planted_stale_index_partial(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    leftover = _stale_index_partial(db.parent, "stale6")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "the ibis note"}, "k-partial-6")
    code, body = run_json(monkeypatch, db, "doctor")
    assert code == EXIT_OK, body
    assert not leftover.exists()
    assert body["data"]["search"]["status"] == "current"


def test_search_refresh_removes_a_planted_stale_index_partial(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    leftover = _stale_index_partial(db.parent, "stale7")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "the ibis note"}, "k-partial-7")
    code, body = run_json(monkeypatch, db, "search", "ibis")
    assert code == EXIT_OK, body
    assert not leftover.exists()
    assert body["data"]["cards"][0]["id"] == "T-feed"


def test_cleanup_leaves_a_partial_when_fcntl_is_unavailable(db, monkeypatch):
    leftover = _stale_index_partial(db.parent, "nofcntl")
    planted = leftover.read_bytes()
    monkeypatch.setattr(search_mod, "fcntl", None)
    ledger_before = db.read_bytes()
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert leftover.is_file()
    assert leftover.read_bytes() == planted
    assert db.read_bytes() == ledger_before
    assert search_mod.index_status(db)["status"] == "current"
    assert _index(db).is_file()


def test_a_lock_error_leaves_the_partial_and_the_refresh_still_publishes(
    db, monkeypatch
):
    leftover = _stale_index_partial(db.parent, "lockerr")
    planted = leftover.read_bytes()

    def no_locks(_fd, _flags):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(search_mod.fcntl, "flock", no_locks)
    ledger_before = db.read_bytes()
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert leftover.read_bytes() == planted
    assert db.read_bytes() == ledger_before
    assert search_mod.index_status(db)["status"] == "current"


def test_a_symlinked_lock_file_leaves_the_partial_and_the_refresh_still_publishes(
    db, monkeypatch
):
    target = db.parent / "lock-target"
    target.write_bytes(b"not-a-lock")
    _lock_path(db).symlink_to(target)
    leftover = _stale_index_partial(db.parent, "symlock")
    planted = leftover.read_bytes()
    ledger_before = db.read_bytes()
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert leftover.is_file(), "a refresh removed a leftover through a symlinked lock file"
    assert leftover.read_bytes() == planted
    assert _lock_path(db).is_symlink()
    assert target.read_bytes() == b"not-a-lock"
    assert db.read_bytes() == ledger_before
    assert search_mod.index_status(db)["status"] == "current"


def test_a_lock_path_that_is_not_a_regular_file_leaves_the_partial(db, monkeypatch):
    _lock_path(db).mkdir()
    leftover = _stale_index_partial(db.parent, "dirlock")
    planted = leftover.read_bytes()
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert leftover.is_file(), "a refresh removed a leftover with a directory at the lock path"
    assert leftover.read_bytes() == planted
    assert _lock_path(db).is_dir()
    assert search_mod.index_status(db)["status"] == "current"


@pytest.mark.parametrize("path", ["rebuild", "append"])
def test_the_writer_holds_its_shared_lock_before_its_temp_is_created(
    db, monkeypatch, path
):
    if path == "append":
        run_json(monkeypatch, db, "index")
        with open_ledger(db) as lg:
            _append(lg, "item.note", "T-feed", {"text": "the ibis note"}, "k-lock-order")
        args = ("index",)
    else:
        args = ("index", "--rebuild")
    real = tempfile.mkstemp
    seen: list[str] = []

    def watch(*a, **kw):
        if kw.get("prefix") == ".search.db." and kw.get("suffix") == ".partial":
            if not _lock_path(db).is_file():
                seen.append("no lock file")
            elif _fresh_lock_granted(db, fcntl.LOCK_EX | fcntl.LOCK_NB):
                seen.append("exclusive lock granted")
            else:
                seen.append("held")
        return real(*a, **kw)

    monkeypatch.setattr(tempfile, "mkstemp", watch)
    code, body = run_json(monkeypatch, db, *args)
    assert code == EXIT_OK, body
    assert seen == ["held"], seen
    assert search_mod.index_status(db)["status"] == "current"
    assert stat.S_IMODE(_index(db).stat().st_mode) & 0o077 == 0


def test_the_lock_file_keeps_its_inode_and_mode_across_refreshes(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    lock = _lock_path(db)
    first = os.lstat(lock)
    assert stat.S_ISREG(first.st_mode)
    assert stat.S_IMODE(first.st_mode) & 0o077 == 0
    leftover = _stale_index_partial(db.parent, "perm1")
    code, body = run_json(monkeypatch, db, "index", "--rebuild")
    assert code == EXIT_OK, body
    assert not leftover.exists()
    assert lock.exists(), "a refresh removed the lock file"
    after = os.lstat(lock)
    assert (after.st_dev, after.st_ino) == (first.st_dev, first.st_ino)
    assert stat.S_ISREG(after.st_mode)


def test_the_cleanup_lists_the_directory_only_while_it_holds_the_exclusive_lock(
    db, monkeypatch
):
    run_json(monkeypatch, db, "index")
    leftover = _stale_index_partial(db.parent, "list1")
    real_listdir = os.listdir
    seen: list[bool] = []

    def watch(path=None):
        if path is not None and Path(path) == db.parent:
            seen.append(not _fresh_lock_granted(db, fcntl.LOCK_SH | fcntl.LOCK_NB))
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", watch)
    code, body = run_json(monkeypatch, db, "index", "--rebuild")
    assert code == EXIT_OK, body
    assert seen, "the cleanup never listed the directory"
    assert all(seen), seen
    assert not leftover.exists()


def test_a_live_writer_keeps_its_partial_until_it_is_killed(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    child, tmp, _go = _start_paused_writer(db)
    try:
        assert tmp.is_file()
        code, body = run_json(monkeypatch, db, "index", "--rebuild")
        assert code == EXIT_OK, body
        assert tmp.is_file(), "a refresh removed a live writer's partial"
    finally:
        if child.poll() is None:
            os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)
    assert child.returncode == -signal.SIGKILL
    assert tmp.is_file()
    code, body = run_json(monkeypatch, db, "index", "--rebuild")
    assert code == EXIT_OK, body
    assert not tmp.exists()
    assert search_mod.index_status(db)["status"] == "current"


def test_two_overlapping_rebuilds_both_publish(db, monkeypatch):
    run_json(monkeypatch, db, "index")
    child, tmp, go = _start_paused_writer(db)
    try:
        code, body = run_json(monkeypatch, db, "index", "--rebuild")
        assert code == EXIT_OK, body
        assert tmp.is_file(), "rebuild B removed writer A's live temp"
        go.write_text("go", encoding="utf-8")
        _out, err = child.communicate(timeout=60)
        assert child.returncode == 0, err
    finally:
        _stop(child)
    assert not tmp.exists()
    assert search_mod.index_status(db)["status"] == "current"
    assert stat.S_IMODE(_index(db).stat().st_mode) & 0o077 == 0


# --- the ledger database is never a leftover ---------------------------------


def _event_keys(path: Path) -> list[str]:
    """The idempotency keys of every event in the ledger at ``path``."""
    with open_ledger_readonly(path) as lg:
        return [event.idempotency_key for event in lg.events()]


def test_a_ledger_database_named_like_a_partial_survives_rebuild_and_refresh(
    db, monkeypatch
):
    live = db.parent / ".search.db.livedata.partial"
    with open_ledger(live) as lg:
        _append(lg, "item.created", "T-live",
                {"type": "ticket", "title": "Live ledger item"}, "k-live-1")
        _append(lg, "item.note", "T-live", {"text": "the heron note"}, "k-live-2")
    keys = _event_keys(live)
    leftover = _stale_index_partial(db.parent, "stale9")
    code, body = run_json(monkeypatch, live, "index", "--rebuild")
    assert code == EXIT_OK, body
    assert live.is_file(), "index --rebuild removed the ledger database"
    assert _event_keys(live) == keys
    assert not leftover.exists()
    with open_ledger(live) as lg:
        _append(lg, "item.note", "T-live", {"text": "the egret note"}, "k-live-3")
    leftover = _stale_index_partial(db.parent, "stale10")
    code, body = run_json(monkeypatch, live, "index")
    assert code == EXIT_OK, body
    assert live.is_file(), "an append refresh removed the ledger database"
    assert _event_keys(live) == [*keys, "k-live-3"]
    assert not leftover.exists()
    assert search_mod.index_status(live)["status"] == "current"


def test_a_hard_link_to_the_ledger_database_under_a_partial_name_survives(
    db, monkeypatch
):
    # The ledger refuses to open a database with more than one link, so the
    # link appears after the refresh has read the ledger and published its
    # index, just before the cleanup lists the directory: another name for
    # the same file that the name rule alone cannot recognise.
    alias = db.parent / ".search.db.alias1.partial"
    keys = _event_keys(db)
    leftover = _stale_index_partial(db.parent, "stale11")
    real_replace = search_mod._replace_index

    def replace_then_link(tmp, index, ledger):
        real_replace(tmp, index, ledger)
        os.link(db, alias)

    monkeypatch.setattr(search_mod, "_replace_index", replace_then_link)
    code, body = run_json(monkeypatch, db, "index", "--rebuild")
    assert code == EXIT_OK, body
    assert alias.is_file(), "index --rebuild removed a hard link to the ledger database"
    assert os.path.samefile(alias, db)
    assert not leftover.exists()
    alias.unlink()
    assert _event_keys(db) == keys


def test_a_hard_link_to_an_index_shaped_db_survives_rebuild_and_refresh(
    db, monkeypatch
):
    # A ledger has an events table, so the schema check would keep a hard
    # link to it even without the identity skip. After the writer has read
    # the ledger and published, rewrite --db in place (same inode) as an
    # index-shaped file with no side files, then plant a hard link under a
    # partial name. Only the device-and-inode skip can keep that link: the
    # schema check alone would accept it.
    alias = db.parent / ".search.db.alias-shape.partial"
    ledger_bytes = db.read_bytes()
    planted: dict[str, bytes] = {}
    real_replace = search_mod._replace_index

    def replace_then_shape(tmp, index, ledger):
        real_replace(tmp, index, ledger)
        scratch = Path(ledger).parent / "index-shape-src"
        _write_index_schema(scratch, search_mod.SCHEMA_SQL)
        payload = scratch.read_bytes()
        scratch.unlink()
        for suffix in _SIDE_SUFFIXES:
            side = scratch.with_name(scratch.name + suffix)
            if os.path.lexists(side):
                side.unlink()
        target = Path(ledger)
        target.write_bytes(payload)
        for suffix in _SIDE_SUFFIXES:
            side = target.with_name(target.name + suffix)
            if os.path.lexists(side):
                side.unlink()
        if not alias.exists():
            os.link(target, alias)
        planted["bytes"] = target.read_bytes()
        found = os.lstat(alias)
        assert search_mod._why_not_index_temp(alias, found) is None
        _no_side_files(alias)
        _no_side_files(target)

    monkeypatch.setattr(search_mod, "_replace_index", replace_then_shape)

    leftover = _stale_index_partial(db.parent, "shap1")
    code, body = run_json(monkeypatch, db, "index", "--rebuild")
    assert code == EXIT_OK, body
    assert alias.is_file(), (
        "index --rebuild removed a hard link to an index-shaped --db"
    )
    assert db.is_file(), "index --rebuild removed the --db file"
    assert os.path.samefile(alias, db)
    assert alias.read_bytes() == planted["bytes"]
    assert db.read_bytes() == planted["bytes"]
    assert not leftover.exists()
    alias.unlink()
    db.write_bytes(ledger_bytes)

    leftover = _stale_index_partial(db.parent, "shap2")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "the ibis note"}, "k-shape-1")
    ledger_bytes = db.read_bytes()
    code, body = run_json(monkeypatch, db, "index")
    assert code == EXIT_OK, body
    assert alias.is_file(), (
        "an append refresh removed a hard link to an index-shaped --db"
    )
    assert db.is_file(), "an append refresh removed the --db file"
    assert os.path.samefile(alias, db)
    assert alias.read_bytes() == planted["bytes"]
    assert db.read_bytes() == planted["bytes"]
    assert not leftover.exists()
    alias.unlink()
    db.write_bytes(ledger_bytes)


def test_a_partial_named_after_the_ledger_database_is_kept(db, monkeypatch):
    live = db.parent / ".search.db.livedata"
    with open_ledger(live) as lg:
        _append(lg, "item.created", "T-live",
                {"type": "ticket", "title": "Live ledger item"}, "k-live-4")
    copy = db.parent / ".search.db.livedata.copy.partial"
    copy.write_bytes(b"hand-copy")
    os.utime(copy, ns=(1, 1))
    leftover = _stale_index_partial(db.parent, "stale12")
    code, body = run_json(monkeypatch, live, "index", "--rebuild")
    assert code == EXIT_OK, body
    assert copy.is_file(), "index --rebuild removed a file named after the ledger database"
    assert copy.read_bytes() == b"hand-copy"
    assert live.is_file()
    assert not leftover.exists()


def test_a_ledger_database_that_cannot_be_compared_leaves_every_partial(
    db, monkeypatch
):
    leftover = _stale_index_partial(db.parent, "stale13")
    planted = leftover.read_bytes()
    real_replace = search_mod._replace_index
    real_lstat = os.lstat
    broken = {"on": False}

    def boom(path, *args, **kwargs):
        if broken["on"] and os.path.basename(os.fspath(path)) == db.name:
            raise OSError(13, "Permission denied")
        return real_lstat(path, *args, **kwargs)

    def replace_then_break(tmp, index, ledger):
        real_replace(tmp, index, ledger)
        broken["on"] = True

    monkeypatch.setattr(os, "lstat", boom)
    monkeypatch.setattr(search_mod, "_replace_index", replace_then_break)
    code, body = run_json(monkeypatch, db, "index")
    broken["on"] = False
    assert code == EXIT_OK, body
    assert leftover.is_file(), "a refresh removed a partial it could not compare with the ledger"
    assert leftover.read_bytes() == planted
    assert search_mod.index_status(db)["status"] == "current"


# --- only a positively identified index temp is removed ----------------------


_SIDE_SUFFIXES = ("-journal", "-wal", "-shm")


def _no_side_files(path: Path) -> None:
    for suffix in _SIDE_SUFFIXES:
        side = path.with_name(path.name + suffix)
        assert not os.path.lexists(side), f"the refresh left {side.name} beside {path.name}"


def _skip_lines(err: str, path: Path) -> list[str]:
    """The cleanup's skip lines on stderr that name ``path``."""
    return [
        line
        for line in err.splitlines()
        if line.startswith("search index cleanup skipped ") and path.name in line
    ]


def _refresh_by_append(db: Path, monkeypatch, key: str) -> list[str]:
    """Build the index, append one event, and return the argv of the refresh."""
    run_json(monkeypatch, db, "index")
    with open_ledger(db) as lg:
        _append(lg, "item.note", "T-feed", {"text": "the ibis note"}, key)
    return ["--db", str(db), "index"]


def test_a_cli_backup_named_like_a_partial_survives_rebuild_and_refresh(db, monkeypatch):
    snapshot = db.parent / ".search.db.snapshot.partial"
    code, _out, err = invoke(monkeypatch, ["--db", str(db), "backup", "--to", str(snapshot)])
    assert code == EXIT_OK, err
    before = snapshot.read_bytes()
    leftover = _stale_index_partial(db.parent, "stale14")
    code, _out, err = invoke(monkeypatch, ["--db", str(db), "index", "--rebuild"])
    assert code == EXIT_OK, err
    assert snapshot.is_file(), "index --rebuild removed a ledger backup"
    assert snapshot.read_bytes() == before, "index --rebuild changed a ledger backup"
    _no_side_files(snapshot)
    lines = [line for line in err.splitlines() if line]
    assert len(lines) == 1, err
    assert _skip_lines(err, snapshot) == lines, err
    assert not leftover.exists()
    argv = _refresh_by_append(db, monkeypatch, "k-backup-1")
    leftover = _stale_index_partial(db.parent, "stale15")
    code, _out, err = invoke(monkeypatch, argv)
    assert code == EXIT_OK, err
    assert snapshot.is_file(), "an append refresh removed a ledger backup"
    assert snapshot.read_bytes() == before, "an append refresh changed a ledger backup"
    _no_side_files(snapshot)
    assert len(_skip_lines(err, snapshot)) == 1, err
    assert not leftover.exists()
    assert search_mod.index_status(db)["status"] == "current"


def test_another_ledger_named_like_a_partial_survives_and_keeps_its_events(db, monkeypatch):
    other = db.parent / ".search.db.other.partial"
    with open_ledger(other) as lg:
        _append(lg, "item.created", "T-other",
                {"type": "ticket", "title": "Other ledger item"}, "k-other-1")
        _append(lg, "item.note", "T-other", {"text": "the plover note"}, "k-other-2")
    _no_side_files(other)
    before = other.read_bytes()
    code, _out, err = invoke(monkeypatch, ["--db", str(db), "index", "--rebuild"])
    assert code == EXIT_OK, err
    assert other.is_file(), "index --rebuild removed another ledger"
    assert other.read_bytes() == before, "index --rebuild changed another ledger"
    _no_side_files(other)
    assert len(_skip_lines(err, other)) == 1, err
    argv = _refresh_by_append(db, monkeypatch, "k-other-refresh")
    code, _out, err = invoke(monkeypatch, argv)
    assert code == EXIT_OK, err
    assert other.is_file(), "an append refresh removed another ledger"
    assert other.read_bytes() == before, "an append refresh changed another ledger"
    _no_side_files(other)
    assert len(_skip_lines(err, other)) == 1, err
    assert search_mod.index_status(db)["status"] == "current"
    assert _event_keys(other) == ["k-other-1", "k-other-2"]


def test_a_new_ledger_still_open_under_a_partial_name_is_kept(db, monkeypatch):
    # A WAL ledger just created and still open keeps its tables in its -wal
    # file; its main file reads as an empty schema, so only the side file
    # beside it shows that it is not an index temp.
    live = db.parent / ".search.db.fresh.partial"
    with open_ledger(live) as lg:
        _append(lg, "item.created", "T-fresh",
                {"type": "ticket", "title": "Fresh ledger item"}, "k-fresh-1")
        code, _out, err = invoke(monkeypatch, ["--db", str(db), "index", "--rebuild"])
        assert code == EXIT_OK, err
        assert live.is_file(), "index --rebuild removed a ledger that is open"
        lines = _skip_lines(err, live)
        assert len(lines) == 1, err
        assert "-wal is beside it" in lines[0] or "-journal is beside it" in lines[0], lines
    assert _event_keys(live) == ["k-fresh-1"]


def _schema_only(*statements: str):
    def build(path: Path) -> None:
        _write_index_schema(path, statements)
    return build


_GENUINE_TEMPS = {
    "zero-length": lambda path: path.write_bytes(b""),
    "empty-schema": _schema_only("CREATE TABLE scratch (a)", "DROP TABLE scratch"),
    "some-tables": _schema_only(*search_mod.SCHEMA_SQL[:2]),
    "all-tables": _schema_only(
        *search_mod.SCHEMA_SQL,
        "INSERT INTO manifest (key, value) VALUES ('manifest', '{}')",
    ),
    "index-and-trigger": _schema_only(
        *search_mod.SCHEMA_SQL,
        "CREATE INDEX extra_meta ON doc_meta (item_id)",
        "CREATE TRIGGER extra_trigger AFTER INSERT ON manifest BEGIN SELECT 1; END",
    ),
}


@pytest.mark.parametrize("refresh", ["rebuild", "append"])
@pytest.mark.parametrize("shape", sorted(_GENUINE_TEMPS))
def test_a_genuine_index_temp_is_removed(db, monkeypatch, shape, refresh):
    if refresh == "append":
        argv = _refresh_by_append(db, monkeypatch, f"k-genuine-{shape}")
    else:
        argv = ["--db", str(db), "index", "--rebuild"]
    temp = db.parent / f".search.db.{shape}.partial"
    _GENUINE_TEMPS[shape](temp)
    _no_side_files(temp)
    code, _out, err = invoke(monkeypatch, argv)
    assert code == EXIT_OK, err
    assert not temp.exists(), f"a refresh kept a genuine index temp ({shape}): {err}"
    assert temp.name not in err
    assert search_mod.index_status(db)["status"] == "current"


_FOREIGN_OBJECTS = {
    "events-table": (
        "CREATE TABLE events (id INTEGER PRIMARY KEY, kind TEXT NOT NULL)",
        "INSERT INTO events (kind) VALUES ('item.created')",
    ),
    "view": ("CREATE VIEW v_items AS SELECT item_id FROM doc_meta",),
    "other-table": ("CREATE TABLE notes (body TEXT)",),
}


@pytest.mark.parametrize("shape", sorted(_FOREIGN_OBJECTS))
def test_an_index_shaped_file_with_a_foreign_object_is_kept(db, monkeypatch, shape):
    path = db.parent / f".search.db.{shape}.partial"
    _write_index_schema(path, (*search_mod.SCHEMA_SQL, *_FOREIGN_OBJECTS[shape]))
    before = path.read_bytes()
    code, _out, err = invoke(monkeypatch, ["--db", str(db), "index", "--rebuild"])
    assert code == EXIT_OK, err
    assert path.is_file(), f"a refresh removed an index-shaped file holding a foreign {shape}"
    assert path.read_bytes() == before
    _no_side_files(path)
    lines = _skip_lines(err, path)
    assert len(lines) == 1, err
    assert "which the index writer never creates" in lines[0], lines
    assert search_mod.index_status(db)["status"] == "current"


_NOT_INDEX_FILES = {
    "one-byte": (b"x", "short read: 1 of 16 header bytes"),
    "short-text": (b"stale-index", "short read: 11 of 16 header bytes"),
    "text": (b"operator notes, not a database at all\n", "not a SQLite file"),
    "bad-body": (b"SQLite format 3\x00" + b"\xff" * 200, "cannot read its SQLite schema"),
}


@pytest.mark.parametrize("shape", sorted(_NOT_INDEX_FILES))
def test_a_file_that_is_not_an_index_temp_is_kept_with_one_line(db, monkeypatch, shape):
    payload, reason = _NOT_INDEX_FILES[shape]
    path = db.parent / f".search.db.{shape}.partial"
    path.write_bytes(payload)
    code, _out, err = invoke(monkeypatch, ["--db", str(db), "index", "--rebuild"])
    assert code == EXIT_OK, err
    assert path.is_file(), f"a refresh removed a {shape} file"
    assert path.read_bytes() == payload
    _no_side_files(path)
    lines = _skip_lines(err, path)
    assert len(lines) == 1, err
    assert reason in lines[0], lines
    assert search_mod.index_status(db)["status"] == "current"


def test_a_symlink_or_directory_named_like_a_partial_is_kept_with_one_line(db, monkeypatch):
    target = _stale_index_partial(db.parent.parent, "target")
    planted = target.read_bytes()
    link = db.parent / ".search.db.link2.partial"
    link.symlink_to(target)
    named_dir = db.parent / ".search.db.dir2.partial"
    named_dir.mkdir()
    code, _out, err = invoke(monkeypatch, ["--db", str(db), "index", "--rebuild"])
    assert code == EXIT_OK, err
    assert link.is_symlink()
    assert named_dir.is_dir()
    assert target.read_bytes() == planted
    for path in (link, named_dir):
        lines = _skip_lines(err, path)
        assert len(lines) == 1, err
        assert lines[0].endswith(": not a regular file"), lines


def test_a_candidate_that_cannot_be_stat_ed_is_kept_with_one_line(db, monkeypatch):
    leftover = _stale_index_partial(db.parent, "nostat")
    planted = leftover.read_bytes()
    real_lstat = os.lstat

    def boom(path, *args, **kwargs):
        if os.path.basename(os.fspath(path)) == leftover.name:
            raise OSError(errno.EACCES, "Permission denied")
        return real_lstat(path, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(os, "lstat", boom)
        code, _out, err = invoke(monkeypatch, ["--db", str(db), "index", "--rebuild"])
    assert code == EXIT_OK, err
    assert leftover.read_bytes() == planted
    lines = _skip_lines(err, leftover)
    assert len(lines) == 1, err
    assert ": cannot stat it: " in lines[0], lines


@pytest.mark.parametrize("change", ["grown", "side-file"])
def test_a_candidate_that_changes_after_identification_is_kept(db, monkeypatch, change):
    leftover = _stale_index_partial(db.parent, f"change-{change}")
    real = search_mod._why_not_index_temp

    def then_change(path, found):
        reason = real(path, found)
        if Path(path).name == leftover.name:
            if change == "grown":
                with open(path, "ab") as handle:
                    handle.write(b"appended after it was identified")
            else:
                Path(f"{path}-journal").write_bytes(b"")
        return reason

    monkeypatch.setattr(search_mod, "_why_not_index_temp", then_change)
    code, _out, err = invoke(monkeypatch, ["--db", str(db), "index", "--rebuild"])
    assert code == EXIT_OK, err
    assert leftover.is_file(), f"a refresh removed a candidate that changed ({change})"
    lines = _skip_lines(err, leftover)
    assert len(lines) == 1, err
    if change == "grown":
        assert lines[0].endswith(": it changed after it was identified"), lines
    else:
        assert "-journal is beside it" in lines[0], lines
    assert search_mod.index_status(db)["status"] == "current"


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"], ids=["journal", "wal", "shm"])
@pytest.mark.parametrize("empty", [True, False], ids=["zero-length", "index-schema"])
def test_identification_refuses_a_candidate_with_a_side_file(tmp_path, suffix, empty):
    # The check before the unlink would also catch a side file that stays;
    # this pins the identification's own check, which keeps a candidate
    # whose side file is there while it is read and gone by the unlink.
    path = tmp_path / ".search.db.side.partial"
    if empty:
        path.write_bytes(b"")
    else:
        _write_index_schema(path, search_mod.SCHEMA_SQL)
    assert search_mod._why_not_index_temp(path, os.lstat(path)) is None
    Path(f"{path}{suffix}").write_bytes(b"")
    reason = search_mod._why_not_index_temp(path, os.lstat(path))
    assert reason is not None, f"a candidate with {suffix} beside it was identified"
    assert f"{path.name}{suffix} is beside it" in reason, reason


def test_the_allow_list_is_the_writer_schema():
    conn = sqlite3.connect(":memory:")
    try:
        for statement in search_mod.SCHEMA_SQL:
            conn.execute(statement)
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    finally:
        conn.close()
    allowed = search_mod._index_table_names()
    assert allowed == tables
    assert {"fts_corpus", "fts_corpus_data", "fts_corpus_idx", "fts_corpus_content",
            "fts_corpus_docsize", "fts_corpus_config", "doc_path", "doc_meta",
            "item_link", "manifest"} == set(allowed)
    assert "events" not in allowed
