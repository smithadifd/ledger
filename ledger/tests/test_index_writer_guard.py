"""The search index writer never replaces a ledger.

``ledger/search.py`` renames a finished index onto the index path
(``search.db`` beside the ledger) in one place. It replaces only an absent
path, a path that is not a regular file, or a SQLite file with a ``manifest``
table and no ``events`` table. A ledger named ``search.db``, a second ledger
named ``search.db`` beside ``ledger.db``, and a ledger named ``SEARCH.DB`` on a
case-insensitive file system are refused by ``index``, ``index --rebuild`` and
``search``, and doctor reports them as a warning with a remedy.

Every ledger here is synthetic and built under tmp_path.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from ledger import search, store
from ledger.paths import resolve_db_path
from ledger.verbs import EXIT_OK, EXIT_REFUSED

from test_cli import invoke, seed

# SQLite's own side files beside a ledger; anything else new is a leftover.
SIDE_FILES = ("-wal", "-shm", "-journal")
VERBS = [["doctor"], ["index"], ["index", "--rebuild"], ["search", "synthetic"]]


def _events(path: Path) -> list[tuple[int, str]]:
    """Every event's id and hash, read without changing the database."""
    try:
        with store.open_ledger_readonly(path) as lg:
            return [(event.id, event.hash) for event in lg.events()]
    except Exception as exc:  # a replaced ledger no longer reads as one
        pytest.fail(f"the ledger at {path} no longer reads: {type(exc).__name__}: {exc}")


def _named(tmp_path: Path, monkeypatch):
    """A ledger named search.db: its index path is the ledger itself."""
    ledger = tmp_path / "search.db"
    seed(monkeypatch, str(ledger), "it-1")
    return [ledger], [ledger], True


def _beside(tmp_path: Path, monkeypatch):
    """ledger.db beside a second ledger named search.db, ledger.db's index path."""
    other = tmp_path / "search.db"
    seed(monkeypatch, str(other), "it-2")
    first = tmp_path / "ledger.db"
    seed(monkeypatch, str(first), "it-1")
    return [first, other], [first, other], True


def _upper(tmp_path: Path, monkeypatch):
    """A ledger named SEARCH.DB. On a case-insensitive file system (the macOS
    default) its index path names the ledger itself; on a case-sensitive one
    the index is another file, which the verbs build as usual."""
    ledger = tmp_path / "SEARCH.DB"
    seed(monkeypatch, str(ledger), "it-1")
    return [ledger], [ledger], (tmp_path / "search.db").exists()


SHAPES = [_named, _beside, _upper]


@pytest.mark.parametrize("verb", VERBS, ids=" ".join)
@pytest.mark.parametrize("shape", SHAPES, ids=lambda f: f.__name__.lstrip("_"))
def test_no_verb_replaces_a_ledger_at_the_index_path(tmp_path, monkeypatch, shape, verb):
    ledgers, targets, guarded = shape(tmp_path, monkeypatch)
    before = {path: _events(path) for path in ledgers}
    assert all(before.values())
    names = set(os.listdir(tmp_path))
    runs = []
    for target in targets:
        code, out, err = invoke(monkeypatch, ["--json", "--db", str(target), *verb])
        assert "Traceback" not in err, err
        runs.append((target, code, json.loads(out)))
    # The events first: every other assertion is about how the verb said so.
    for path in ledgers:
        assert _events(path) == before[path], f"{' '.join(verb)} changed the events of {path}"
    new = set(os.listdir(tmp_path)) - names
    if not guarded:
        new.discard(search.SEARCH_DB_NAME)  # the index a case-sensitive system builds
    assert sorted(n for n in new if not n.endswith(SIDE_FILES)) == []
    for target, code, body in runs:
        if guarded and verb[0] != "doctor":
            assert code == EXIT_REFUSED, body
            message = body["error"]["message"]
            assert message.startswith("refusing to replace "), message
            resolved = resolve_db_path(target)
            assert str(search.search_db_path(resolved)) in message, message
            assert str(resolved) in message, message
        else:
            assert code == EXIT_OK, body
        if guarded and verb[0] == "doctor":
            assert body["data"]["search"]["severity"] == "warn", body


@pytest.mark.parametrize("shape", SHAPES, ids=lambda f: f.__name__.lstrip("_"))
def test_doctor_line_for_a_ledger_at_the_index_path_is_a_warning_with_a_remedy(
    tmp_path, monkeypatch, shape
):
    ledgers, targets, guarded = shape(tmp_path, monkeypatch)
    before = {path: _events(path) for path in ledgers}
    lines = []
    for target in targets:
        code, out, err = invoke(monkeypatch, ["--db", str(target), "doctor"])
        assert code == EXIT_OK, err
        found = [line for line in out.splitlines() if line.startswith("search: ")]
        assert len(found) == 1, out
        lines.append((target, found[0]))
    # The events first, so a wrong line below fails with the events intact.
    for path in ledgers:
        assert _events(path) == before[path], f"doctor changed the events of {path}"
    for target, line in lines:
        if guarded:
            assert "rebuilds it" not in line, line
            assert "(warn: ledger index refuses: refusing to replace " in line, line
            assert "; to clear it, move or rename that file" in line, line
            assert search.index_status(target)["severity"] == "warn"
        else:
            assert line == "search: current", line


def test_an_index_path_that_cannot_be_stated_is_refused_naming_both_paths(
    tmp_path, monkeypatch
):
    ledger = tmp_path / "ledger.db"
    seed(monkeypatch, str(ledger), "it-1")
    code, out, err = invoke(monkeypatch, ["--json", "--db", str(ledger), "index"])
    assert code == EXIT_OK, out + err
    index = search.search_db_path(ledger)
    built = index.read_bytes()
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if Path(path) == index:
            raise PermissionError(13, "Permission denied", str(path))
        return real_lstat(path, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(search.os, "lstat", lstat)
        code, out, err = invoke(
            monkeypatch, ["--json", "--db", str(ledger), "index", "--rebuild"]
        )
    assert "Traceback" not in err, err
    assert code == EXIT_REFUSED, out
    message = json.loads(out)["error"]["message"]
    assert message.startswith(f"cannot stat the search index {index}: "), message
    assert f"(ledger {resolve_db_path(ledger)})" in message, message
    assert index.read_bytes() == built
    assert [n for n in os.listdir(tmp_path) if n.endswith(".partial")] == []


def test_an_older_index_without_doc_meta_is_rebuilt_in_place(tmp_path, monkeypatch):
    # The marker of a search index is its manifest table, which every format
    # has; doc_meta arrived in format 3, so an older index has none.
    ledger = tmp_path / "ledger.db"
    seed(monkeypatch, str(ledger), "it-1")
    code, out, err = invoke(monkeypatch, ["--json", "--db", str(ledger), "index"])
    assert code == EXIT_OK, out + err
    index = tmp_path / search.SEARCH_DB_NAME
    conn = sqlite3.connect(index)
    try:
        conn.execute("DROP TABLE doc_meta")
        stored = json.loads(
            conn.execute("SELECT value FROM manifest WHERE key = 'manifest'").fetchone()[0]
        )
        stored["format"] = 2
        conn.execute(
            "UPDATE manifest SET value = ? WHERE key = 'manifest'",
            (json.dumps(stored, sort_keys=True),),
        )
        conn.commit()
    finally:
        conn.close()
    found = search.index_status(ledger)
    assert (found["status"], found["severity"]) == ("stale", "info"), found
    code, out, err = invoke(monkeypatch, ["--json", "--db", str(ledger), "index"])
    assert code == EXIT_OK, out + err
    assert json.loads(out)["data"]["refresh"] == "rebuild"
    conn = sqlite3.connect(index)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    finally:
        conn.close()
    assert "doc_meta" in tables
    assert search.index_status(ledger)["status"] == "current"


# --- the publish: a path the check saw absent is never replaced -------------


def _race_at_publish(monkeypatch, act):
    """Run ``act`` once inside the index writer, after its check and before
    its publish: another session writing the index path between the two.
    Returns a dict whose ``fired`` counts the runs."""
    real_replace = search._replace_index
    real_verdict = search._replace_verdict
    state = {"armed": False, "fired": 0}

    def replace_index(tmp, index, ledger):
        state["armed"] = True
        try:
            return real_replace(tmp, index, ledger)
        finally:
            state["armed"] = False

    def verdict(index, ledger):
        found = real_verdict(index, ledger)
        if state["armed"] and not state["fired"]:
            state["fired"] += 1
            act()
        return found

    monkeypatch.setattr(search, "_replace_index", replace_index)
    monkeypatch.setattr(search, "_replace_verdict", verdict)
    return state


def _partials(directory: Path) -> list[str]:
    return sorted(n for n in os.listdir(directory) if n.endswith(".partial"))


@pytest.mark.parametrize("verb", VERBS, ids=" ".join)
def test_a_ledger_created_at_the_index_path_during_a_publish_is_not_replaced(
    tmp_path, monkeypatch, verb
):
    ledger = tmp_path / "ledger.db"
    seed(monkeypatch, str(ledger), "it-1")
    other = tmp_path / "elsewhere" / "ledger.db"
    other.parent.mkdir()
    seed(monkeypatch, str(other), "it-2")
    expected = _events(other)
    expected_bytes = other.read_bytes()
    assert expected
    index = search.search_db_path(ledger)
    assert not os.path.lexists(index)
    race = _race_at_publish(monkeypatch, lambda: shutil.copyfile(other, index))
    code, out, err = invoke(monkeypatch, ["--json", "--db", str(ledger), *verb])
    assert "Traceback" not in err, err
    assert race["fired"] == 1, "the publish never ran"
    # The events first: the ledger that appeared at the index path survives.
    assert _events(index) == expected, f"{' '.join(verb)} replaced the ledger at {index}"
    assert index.read_bytes() == expected_bytes
    assert _partials(tmp_path) == []
    body = json.loads(out)
    if verb[0] == "doctor":
        assert code == EXIT_OK, body
        assert body["data"]["search"]["severity"] == "warn", body
    else:
        assert code == EXIT_REFUSED, body
        message = body["error"]["message"]
        assert message.startswith(f"refusing to replace {index}, "), message
        assert "it has an events table" in message, message


def test_a_search_index_created_at_the_index_path_during_a_publish_is_replaced(
    tmp_path, monkeypatch
):
    # Another session's index is still a search index: the writer decides
    # again on what is there and replaces it, as it replaces any index.
    ledger = tmp_path / "ledger.db"
    seed(monkeypatch, str(ledger), "it-1")
    code, out, err = invoke(monkeypatch, ["--json", "--db", str(ledger), "index"])
    assert code == EXIT_OK, out + err
    index = search.search_db_path(ledger)
    other_index = tmp_path / "elsewhere.db"
    os.rename(index, other_index)
    race = _race_at_publish(monkeypatch, lambda: shutil.copyfile(other_index, index))
    code, out, err = invoke(
        monkeypatch, ["--json", "--db", str(ledger), "index", "--rebuild"]
    )
    assert "Traceback" not in err, err
    assert race["fired"] == 1, "the publish never ran"
    assert code == EXIT_OK, out
    assert os.stat(index).st_ino != os.stat(other_index).st_ino
    assert search.index_status(ledger)["status"] == "current"
    assert _partials(tmp_path) == []


def test_a_link_that_finds_a_file_which_is_gone_again_refuses(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.db"
    seed(monkeypatch, str(ledger), "it-1")
    index = search.search_db_path(ledger)

    def link(src, dst, *args, **kwargs):
        raise FileExistsError(17, "File exists", str(dst))

    monkeypatch.setattr(search.os, "link", link)
    code, out, err = invoke(
        monkeypatch, ["--json", "--db", str(ledger), "index", "--rebuild"]
    )
    assert "Traceback" not in err, err
    assert code == EXIT_REFUSED, out
    message = json.loads(out)["error"]["message"]
    assert message == (
        f"refusing to publish the search index at {index}: the path changed "
        f"while it was published (ledger {resolve_db_path(ledger)})"
    ), message
    assert not os.path.lexists(index)
    assert _partials(tmp_path) == []


def test_a_publish_that_cannot_link_refuses_and_never_renames(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.db"
    seed(monkeypatch, str(ledger), "it-1")
    index = search.search_db_path(ledger)

    def link(src, dst, *args, **kwargs):
        raise PermissionError(1, "Operation not permitted", str(dst))

    monkeypatch.setattr(search.os, "link", link)
    code, out, err = invoke(
        monkeypatch, ["--json", "--db", str(ledger), "index", "--rebuild"]
    )
    assert "Traceback" not in err, err
    assert code == EXIT_REFUSED, out
    message = json.loads(out)["error"]["message"]
    assert message.startswith(
        f"cannot publish the search index at {index} without replacing what may be there: "
    ), message
    assert message.endswith(f"(ledger {resolve_db_path(ledger)})"), message
    assert not os.path.lexists(index)
    assert _partials(tmp_path) == []


# --- what the rule refuses: no manifest, or an events table ------------------


def _no_manifest_table(index: Path) -> None:
    conn = sqlite3.connect(index)
    try:
        conn.execute("CREATE TABLE notes (body TEXT)")
        conn.execute("INSERT INTO notes (body) VALUES ('not an index')")
        conn.commit()
    finally:
        conn.close()


def _zero_bytes(index: Path) -> None:
    index.write_bytes(b"")


def _manifest_and_events(index: Path) -> None:
    conn = sqlite3.connect(index)
    try:
        conn.execute("CREATE TABLE manifest (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, hash TEXT)")
        conn.execute("INSERT INTO events (id, hash) VALUES (1, 'not an index')")
        conn.commit()
    finally:
        conn.close()


def _run_refused(tmp_path, monkeypatch, ledger: Path, index: Path, verb, detail: str):
    """Run ``verb``; the file at ``index`` keeps its bytes, nothing is left
    over, and the verb refuses (doctor reports a warning instead)."""
    before = index.read_bytes()
    code, out, err = invoke(monkeypatch, ["--json", "--db", str(ledger), *verb])
    assert "Traceback" not in err, err
    assert index.read_bytes() == before, f"{' '.join(verb)} replaced {index}"
    assert _partials(tmp_path) == []
    body = json.loads(out)
    if verb[0] == "doctor":
        assert code == EXIT_OK, body
        assert body["data"]["search"]["severity"] == "warn", body
        assert any(detail in reason for reason in body["data"]["search"]["reasons"]), body
    else:
        assert code == EXIT_REFUSED, body
        message = body["error"]["message"]
        assert message.startswith(f"refusing to replace {index}, "), message
        assert message.endswith(detail), message


@pytest.mark.parametrize("verb", VERBS, ids=" ".join)
@pytest.mark.parametrize("make", [_no_manifest_table, _zero_bytes], ids=lambda f: f.__name__.lstrip("_"))
def test_a_file_without_a_manifest_table_at_the_index_path_is_never_replaced(
    tmp_path, monkeypatch, make, verb
):
    ledger = tmp_path / "ledger.db"
    seed(monkeypatch, str(ledger), "it-1")
    index = search.search_db_path(ledger)
    make(index)
    _run_refused(tmp_path, monkeypatch, ledger, index, verb, "it has no manifest table")


@pytest.mark.parametrize("verb", [["index", "--rebuild"], ["search", "synthetic"]], ids=" ".join)
def test_a_file_with_a_manifest_and_an_events_table_is_never_replaced(
    tmp_path, monkeypatch, verb
):
    ledger = tmp_path / "ledger.db"
    seed(monkeypatch, str(ledger), "it-1")
    index = search.search_db_path(ledger)
    _manifest_and_events(index)
    _run_refused(tmp_path, monkeypatch, ledger, index, verb, "it has an events table")


def test_a_behind_index_with_an_events_table_is_not_replaced_by_the_append(
    tmp_path, monkeypatch
):
    ledger = tmp_path / "ledger.db"
    seed(monkeypatch, str(ledger), "it-1")
    code, out, err = invoke(monkeypatch, ["--json", "--db", str(ledger), "index"])
    assert code == EXIT_OK, out + err
    index = search.search_db_path(ledger)
    conn = sqlite3.connect(index)
    try:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, hash TEXT)")
        conn.commit()
    finally:
        conn.close()
    seed(monkeypatch, str(ledger), "it-2", "synthetic-two")
    # Behind: everything recorded matches and one event is new, so search
    # takes the append path first.
    assert search.index_status(ledger)["status"] == "behind"
    _run_refused(
        tmp_path, monkeypatch, ledger, index, ["search", "synthetic"], "it has an events table"
    )
