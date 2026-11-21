"""``search-miss`` logs one line; ``search-audit`` replays the log, reading only.

Every ledger here is synthetic and built under tmp_path; nothing reads a live
database. The expected documents are named the way a seat names them:
``event:N`` for a document built from an event, an item id for the item.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ledger import search as search_mod
from ledger import search_misses
from ledger.cli import main
from ledger.search_misses import (
    DEFAULT_RANK,
    MISSES_NAME,
    MissError,
    append_miss,
    is_candidate,
    parse_line,
    rank_of,
    read_misses,
)
from ledger.store import open_ledger
from ledger.verbs import EXIT_NOT_FOUND, EXIT_OK, EXIT_REFUSED, EXIT_USAGE
from ledger.verbs.search import DEFAULT_SEARCH_LIMIT

PARENT = "parent-item"
CHILD = "child-item"
RULING = (
    "decided: the harness map is the one picture of the harness, owned by "
    f"{CHILD}; its tooltips are attested by machine, never by a reader"
)
QUERY = "harness map"


def invoke(monkeypatch, argv: list[str], harness: str | None = "pytest") -> tuple[int, str, str]:
    if harness is None:
        monkeypatch.delenv("LEDGER_HARNESS", raising=False)
    else:
        monkeypatch.setenv("LEDGER_HARNESS", harness)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _append(lg, kind: str, item_id: str | None, payload: dict, key: str):
    return lg.append(kind=kind, item_id=item_id, payload=payload,
                     idempotency_key=key, harness="fixture")


@pytest.fixture
def ledger_dir(tmp_path: Path, monkeypatch) -> dict:
    """A parent with 40 notes, one of them a ruling, beside topical tickets."""
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    db = tmp_path / "state" / "home.db"
    with open_ledger(db) as lg:
        _append(lg, "item.created", PARENT,
                {"type": "initiative", "title": "Readout of the first round"}, "parent")
        _append(lg, "item.created", CHILD,
                {"type": "ticket", "title": "Harness map tooltips"}, "child")
        for n in range(7):
            _append(lg, "item.created", f"hw-{n}",
                    {"type": "ticket", "title": f"Harness wrapper cleanup {n}"}, f"hw-{n}")
            _append(lg, "item.created", f"mr-{n}",
                    {"type": "ticket", "title": f"Map renderer fix {n}"}, f"mr-{n}")
        ruling = None
        for n in range(40):
            if n == 20:
                ruling = _append(lg, "item.note", PARENT, {"text": RULING}, "ruling").id
            else:
                tail = " with the harness checks green" if n % 10 == 3 else ""
                _append(lg, "item.note", PARENT, {
                    "text": f"round {n} receipt recorded; lane {n} closed on the readout{tail}",
                }, f"note-{n}")
    (db.parent / "config.json").write_text(json.dumps({"search_roots": []}), encoding="utf-8")
    return {"db": db, "dir": db.parent, "ruling": ruling, "misses": db.parent / MISSES_NAME}


def log(monkeypatch, db: Path, *argv: str, harness: str | None = "pytest"):
    return invoke(monkeypatch, ["--db", str(db), "search-miss", *argv], harness=harness)


def audit(monkeypatch, db: Path, *argv: str):
    return invoke(monkeypatch, ["--json", "--db", str(db), "search-audit", *argv])


def current_index(monkeypatch, db: Path) -> None:
    code, _, _ = invoke(monkeypatch, ["--db", str(db), "search", QUERY])
    assert code == EXIT_OK


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(directory: Path) -> dict[str, str]:
    """Every file in the directory with its content hash, except SQLite's
    ``-wal`` and ``-shm`` sidecars, which any read-only open of a WAL database
    may create empty and which hold no ledger or index content."""
    return {
        entry.name: sha(entry)
        for entry in sorted(directory.iterdir())
        if entry.is_file() and not entry.name.endswith(("-wal", "-shm"))
    }


# --- the logging verb: one append, nothing else ----------------------------------


def test_one_call_appends_one_line_and_changes_nothing_else(ledger_dir, monkeypatch):
    current_index(monkeypatch, ledger_dir["db"])
    before = snapshot(ledger_dir["dir"])
    assert MISSES_NAME not in before
    code, out, err = log(monkeypatch, ledger_dir["db"], "--query", QUERY,
                         "--expected", f"event:{ledger_dir['ruling']}", "--note", "found by item id")
    assert (code, err) == (EXIT_OK, "")
    after = snapshot(ledger_dir["dir"])
    # The one new file is the misses file; the ledger, its index and its
    # config are byte for byte as they were.
    assert set(after) - set(before) == {MISSES_NAME}
    assert {name: digest for name, digest in after.items() if name != MISSES_NAME} == before
    lines = ledger_dir["misses"].read_bytes().split(b"\n")
    assert len(lines) == 2 and lines[1] == b""
    entry = json.loads(lines[0])
    assert entry["query"] == QUERY and entry["note"] == "found by item id"
    assert entry["expected"] == f"event:{ledger_dir['ruling']}" and entry["v"] == 1
    assert out == f"logged\tevent:{ledger_dir['ruling']}\t{QUERY}\n"


def test_a_second_call_appends_and_keeps_the_first_line_byte_for_byte(ledger_dir, monkeypatch):
    log(monkeypatch, ledger_dir["db"], "--query", QUERY, "--expected", "event:5")
    first = ledger_dir["misses"].read_bytes()
    code, _, _ = log(monkeypatch, ledger_dir["db"], "--query", "release.notes", "--expected", CHILD)
    assert code == EXIT_OK
    both = ledger_dir["misses"].read_bytes()
    assert both.startswith(first) and both.count(b"\n") == 2


def test_the_misses_file_is_created_private(ledger_dir, monkeypatch):
    log(monkeypatch, ledger_dir["db"], "--query", QUERY, "--expected", CHILD)
    assert stat.S_IMODE(ledger_dir["misses"].stat().st_mode) == 0o600


def test_a_query_with_non_ascii_text_is_stored_as_ascii_and_read_back(ledger_dir, monkeypatch):
    code, _, _ = log(monkeypatch, ledger_dir["db"], "--query", "caf\u00e9 map", "--expected", CHILD,
                     "--note", "d\u00e9j\u00e0 vu")
    assert code == EXIT_OK
    ledger_dir["misses"].read_bytes().decode("ascii")
    [miss] = read_misses(ledger_dir["db"])
    assert (miss.query, miss.note) == ("caf\u00e9 map", "d\u00e9j\u00e0 vu")


def test_a_logged_line_has_no_note_key_unless_a_note_was_given(ledger_dir, monkeypatch):
    log(monkeypatch, ledger_dir["db"], "--query", QUERY, "--expected", CHILD)
    assert "note" not in json.loads(ledger_dir["misses"].read_text(encoding="utf-8"))


def test_the_json_envelope_names_the_file_and_the_entry(ledger_dir, monkeypatch):
    code, out, _ = invoke(monkeypatch, ["--json", "--db", str(ledger_dir["db"]), "search-miss",
                                        "--query", QUERY, "--expected", CHILD])
    assert code == EXIT_OK
    data = json.loads(out)["data"]
    assert Path(data["path"]).name == MISSES_NAME
    assert data["entry"]["expected"] == CHILD


def test_an_append_stamps_the_recorded_time():
    when = datetime(2026, 11, 20, 9, 30, 5, tzinfo=UTC)
    assert search_misses.build_entry(QUERY, CHILD, None, when)["recorded_at"] == "2026-11-20T09:30:05Z"


# Each near miss of the grammar: the argument list, the exit code, the name.
NEAR_MISSES = [
    pytest.param(["--query", "", "--expected", CHILD], "query-empty", id="empty-query"),
    pytest.param(["--query", "   ", "--expected", CHILD], "query-empty", id="blank-query"),
    pytest.param(["--query", "q" * 501, "--expected", CHILD], "query-too-long", id="long-query"),
    pytest.param(["--query", "a\x1bb", "--expected", CHILD], "query-control-character", id="control-query"),
    pytest.param(["--query", "!!! ...", "--expected", CHILD], "query-no-searchable-words", id="no-words"),
    pytest.param(["--query", QUERY, "--expected", ""], "expected-empty", id="empty-expected"),
    pytest.param(["--query", QUERY, "--expected", "x" * 201], "expected-too-long", id="long-expected"),
    pytest.param(["--query", QUERY, "--expected", " parent-item"], "expected-bad-character", id="leading-space"),
    pytest.param(["--query", QUERY, "--expected", "parent item"], "expected-bad-character", id="inner-space"),
    pytest.param(["--query", QUERY, "--expected", "parent\x07item"], "expected-bad-character", id="control-expected"),
    pytest.param(["--query", QUERY, "--expected", "Event:12"], "expected-bad-form", id="event-other-case"),
    pytest.param(["--query", QUERY, "--expected", "event:012"], "expected-bad-form", id="event-leading-zero"),
    pytest.param(["--query", QUERY, "--expected", "event:0"], "expected-bad-form", id="event-zero"),
    pytest.param(["--query", QUERY, "--expected", "event:12x"], "expected-bad-form", id="event-trailing-text"),
    pytest.param(["--query", QUERY, "--expected", "event:"], "expected-bad-form", id="event-no-number"),
    pytest.param(["--query", QUERY, "--expected", "event:9223372036854775808"], "expected-bad-form", id="event-too-large"),
    pytest.param(["--query", QUERY, "--expected", "parent-item:12"], "expected-bad-form", id="doc-id-form"),
    pytest.param(["--query", QUERY, "--expected", CHILD, "--note", "n" * 501], "note-too-long", id="long-note"),
    pytest.param(["--query", QUERY, "--expected", CHILD, "--note", "a\nb"], "note-control-character", id="control-note"),
    pytest.param(["--query", QUERY, "--query", "other", "--expected", CHILD], "repeated-option", id="repeated-query"),
    pytest.param(["--query", QUERY, "--expected", CHILD, "--expected", "x"], "repeated-option", id="repeated-expected"),
    pytest.param(["--query", QUERY, "--expected", CHILD, "--note", "a", "--note", "b"], "repeated-option", id="repeated-note"),
    pytest.param(["--expected", CHILD], "missing-option", id="no-query"),
    pytest.param(["--query", QUERY], "missing-option", id="no-expected"),
]


@pytest.mark.parametrize(("argv", "name"), NEAR_MISSES)
def test_a_near_miss_is_a_usage_error_naming_the_form_and_writes_nothing(
    ledger_dir, monkeypatch, argv, name
):
    before = snapshot(ledger_dir["dir"])
    code, out, err = log(monkeypatch, ledger_dir["db"], *argv)
    assert code == EXIT_USAGE and out == ""
    assert err.startswith(f"{name}: "), err
    assert snapshot(ledger_dir["dir"]) == before


def test_a_refusal_leaves_an_existing_misses_file_as_it_was(ledger_dir, monkeypatch):
    log(monkeypatch, ledger_dir["db"], "--query", QUERY, "--expected", CHILD)
    before = ledger_dir["misses"].read_bytes()
    code, _, _ = log(monkeypatch, ledger_dir["db"], "--query", QUERY, "--expected", "Event:1")
    assert code == EXIT_USAGE
    assert ledger_dir["misses"].read_bytes() == before


def test_the_log_writes_no_event_so_it_needs_no_harness(ledger_dir, monkeypatch):
    # Like export and backup, which write files and not events, the verb is
    # not a writes verb: the loop-writer and session-model checks that cover
    # every writes verb do not apply, and no LEDGER_HARNESS is required.
    from ledger.verbs import discover_verbs

    assert discover_verbs()["search-miss"].writes is False
    code, _, err = log(monkeypatch, ledger_dir["db"], "--query", QUERY, "--expected", CHILD, harness=None)
    assert (code, err) == (EXIT_OK, "")
    assert read_misses(ledger_dir["db"])[0].expected == CHILD


def test_a_database_that_does_not_exist_is_refused_and_creates_no_file(tmp_path, monkeypatch):
    code, _, err = log(monkeypatch, tmp_path / "none.db", "--query", QUERY, "--expected", CHILD)
    assert code == EXIT_NOT_FOUND and "database does not exist" in err
    assert list(tmp_path.iterdir()) == []


def test_a_misses_file_that_is_a_symlink_is_never_followed(ledger_dir, monkeypatch, tmp_path):
    target = tmp_path / "elsewhere.txt"
    target.write_text("keep\n", encoding="utf-8")
    ledger_dir["misses"].symlink_to(target)
    code, _, err = log(monkeypatch, ledger_dir["db"], "--query", QUERY, "--expected", CHILD)
    assert code == EXIT_REFUSED and err.startswith("misses-file-unwritable: ")
    assert target.read_text(encoding="utf-8") == "keep\n"


def test_a_misses_file_that_is_a_fifo_is_refused_and_written_nowhere(ledger_dir, monkeypatch):
    os.mkfifo(ledger_dir["misses"])
    reader = os.open(ledger_dir["misses"], os.O_RDONLY | os.O_NONBLOCK)
    try:
        code, _, err = log(monkeypatch, ledger_dir["db"], "--query", QUERY, "--expected", CHILD)
        assert code == EXIT_REFUSED and err.startswith("misses-file-not-regular: ")
        assert os.read(reader, 100) == b""  # the writer closed and nothing reached the pipe
    finally:
        os.close(reader)


def test_an_append_that_stops_part_way_is_refused_by_name(ledger_dir, monkeypatch):
    real = os.write
    monkeypatch.setattr(search_misses.os, "write", lambda fd, data: real(fd, data[:5]))
    with pytest.raises(MissError) as caught:
        append_miss(ledger_dir["db"], QUERY, CHILD)
    assert caught.value.reason == "misses-file-short-write"


def test_concurrent_writers_each_add_a_whole_line(ledger_dir):
    count = 40
    start = threading.Barrier(8)

    def work(n: int) -> None:
        start.wait()
        for k in range(count // 8):
            append_miss(ledger_dir["db"], f"harness map {n} {k}", CHILD, f"writer {n}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(8)))
    misses = read_misses(ledger_dir["db"])
    assert len(misses) == count
    assert len({miss.query for miss in misses}) == count


# --- the grammar of a logged line ------------------------------------------------

GOOD = {"v": 1, "recorded_at": "2026-11-20T09:30:00Z", "query": QUERY, "expected": CHILD}


def line(**changes) -> str:
    entry = {**GOOD, **changes}
    return json.dumps({k: v for k, v in entry.items() if v is not ...})


LINE_NEAR_MISSES = [
    pytest.param("", "blank-line", id="blank"),
    pytest.param("   ", "blank-line", id="spaces"),
    pytest.param("{not json", "not-json", id="not-json"),
    pytest.param("[1, 2]", "not-an-object", id="array"),
    pytest.param(line(extra="x"), "unknown-key", id="unknown-key"),
    pytest.param(line(expected=...), "missing-key", id="missing-expected"),
    pytest.param(line(v=2), "bad-version", id="version-two"),
    pytest.param(line(v=True), "bad-version", id="version-true"),
    pytest.param(line(query=5), "bad-type", id="query-number"),
    pytest.param(line(note=5), "bad-type", id="note-number"),
    pytest.param(line(recorded_at="2026-11-20 09:30:00"), "bad-recorded-at", id="no-t"),
    pytest.param(line(recorded_at="2026-11-20T09:30:00+00:00"), "bad-recorded-at", id="offset"),
    pytest.param(line(query=""), "query-empty", id="empty-query"),
    pytest.param(line(expected="Event:3"), "expected-bad-form", id="event-case"),
    pytest.param('{"v":1,"v":1,"recorded_at":"2026-11-20T09:30:00Z","query":"q","expected":"x"}',
                 "duplicate-key", id="duplicate-key"),
]


@pytest.mark.parametrize(("text", "name"), LINE_NEAR_MISSES)
def test_a_line_off_the_grammar_is_refused_by_name(text, name):
    with pytest.raises(MissError) as caught:
        parse_line(text)
    assert caught.value.reason == name


def test_a_line_on_the_grammar_parses_with_and_without_a_note():
    assert parse_line(line())["expected"] == CHILD
    assert parse_line(line(note="seen by id"))["note"] == "seen by id"
    assert parse_line(line(expected="event:12"))["expected"] == "event:12"
    assert parse_line(line(query="  harness map  "))["query"] == QUERY


# --- the replay ------------------------------------------------------------------


def _hits(monkeypatch, db: Path) -> list[dict]:
    code, out, _ = invoke(monkeypatch, ["--json", "--db", str(db), "search", "--limit", "1000", QUERY])
    assert code == EXIT_OK
    return json.loads(out)["data"]["cards"]


def test_a_logged_miss_replays_with_its_current_rank(ledger_dir, monkeypatch):
    db = ledger_dir["db"]
    ruling = f"event:{ledger_dir['ruling']}"
    log(monkeypatch, db, "--query", QUERY, "--expected", ruling, "--note", "found by digging")
    log(monkeypatch, db, "--query", QUERY, "--expected", PARENT)
    log(monkeypatch, db, "--query", QUERY, "--expected", "event:999999")
    current_index(monkeypatch, db)
    code, out, err = audit(monkeypatch, db)
    assert (code, err) == (EXIT_OK, "")
    rows = json.loads(out)["data"]["misses"]
    cards = _hits(monkeypatch, db)
    ruling_position = [c["id"] for c in cards].index(f"{PARENT}:{ledger_dir['ruling']}") + 1
    parent_position = [c["id"] for c in cards].index(PARENT) + 1
    assert [row["line"] for row in rows] == [1, 2, 3]
    assert rows[0]["rank"] == ruling_position <= DEFAULT_RANK
    assert rows[0]["candidate"] is False and rows[0]["note"] == "found by digging"
    assert "golden_case" not in rows[0]
    # The parent's own document is where the ruling used to hide.
    assert rows[1]["rank"] == parent_position > DEFAULT_RANK
    assert rows[1]["candidate"] is True
    assert rows[1]["golden_case"] == {"query": QUERY, "expected": PARENT, "max_rank": DEFAULT_RANK}
    assert rows[2]["rank"] is None and rows[2]["candidate"] is True


def test_the_text_report_gives_one_line_per_miss_and_a_count(ledger_dir, monkeypatch):
    db = ledger_dir["db"]
    log(monkeypatch, db, "--query", QUERY, "--expected", f"event:{ledger_dir['ruling']}")
    log(monkeypatch, db, "--query", QUERY, "--expected", "event:999999", "--note", "typo")
    current_index(monkeypatch, db)
    code, out, _ = invoke(monkeypatch, ["--db", str(db), "search-audit"])
    assert code == EXIT_OK
    lines = out.splitlines()
    assert lines[0].startswith("ok\trank=") and lines[0].endswith(f"\tevent:{ledger_dir['ruling']}\t{QUERY}")
    assert lines[1] == f"candidate\trank=none\tevent:999999\t{QUERY}"
    assert lines[2] == "\tnote: typo"
    assert lines[3] == "2 logged misses replayed, 1 candidate golden case (rank past 10 or none)"


def test_the_default_rank_is_the_rank_a_default_search_shows():
    assert DEFAULT_RANK == DEFAULT_SEARCH_LIMIT


def test_a_rank_of_ten_is_not_a_candidate_and_eleven_or_none_is():
    assert is_candidate(DEFAULT_RANK) is False
    assert is_candidate(1) is False
    assert is_candidate(DEFAULT_RANK + 1) is True
    assert is_candidate(None) is True


def test_rank_of_counts_from_one_and_matches_by_document_kind():
    cards = [
        {"doc_type": "item", "id": "event:7"},  # an item named like an event: not event 7
        {"doc_type": "decision", "id": "event:7"},  # a decision is built from an event
        {"doc_type": "utterance", "id": "event:7"},
        {"doc_type": "decision", "id": "some:item:41"},
        {"doc_type": "decision", "id": "some-item"},  # an item's decision is not the item
        {"doc_type": "item", "id": "some-item"},
    ]
    assert rank_of(cards, "event:7") == 2
    assert rank_of(cards, "event:41") == 4
    assert rank_of(cards, "some-item") == 6
    assert rank_of(cards, "event:8") is None
    assert rank_of([], "some-item") is None


def test_the_replay_only_reads(ledger_dir, monkeypatch):
    db = ledger_dir["db"]
    log(monkeypatch, db, "--query", QUERY, "--expected", f"event:{ledger_dir['ruling']}")
    current_index(monkeypatch, db)
    before = snapshot(ledger_dir["dir"])
    code, _, _ = audit(monkeypatch, db)
    assert code == EXIT_OK
    assert snapshot(ledger_dir["dir"]) == before


def test_an_index_that_is_behind_is_unverifiable_and_is_not_refreshed(ledger_dir, monkeypatch):
    db = ledger_dir["db"]
    log(monkeypatch, db, "--query", QUERY, "--expected", f"event:{ledger_dir['ruling']}")
    current_index(monkeypatch, db)
    with open_ledger(db) as lg:
        _append(lg, "item.note", PARENT, {"text": "a later note"}, "later")
    before = snapshot(ledger_dir["dir"])
    code, out, err = invoke(monkeypatch, ["--db", str(db), "search-audit"])
    assert code == EXIT_REFUSED and out == ""
    assert err.startswith("search-audit is unverifiable: the search index is behind")
    assert "run ledger index" in err and "rank=" not in err
    assert snapshot(ledger_dir["dir"]) == before  # search.db was not rebuilt
    # After the index is brought up to date the same replay verifies.
    current_index(monkeypatch, db)
    assert audit(monkeypatch, db)[0] == EXIT_OK


def test_a_missing_index_is_unverifiable(ledger_dir, monkeypatch):
    db = ledger_dir["db"]
    log(monkeypatch, db, "--query", QUERY, "--expected", CHILD)
    code, out, err = audit(monkeypatch, db)
    assert code == EXIT_REFUSED
    envelope = json.loads(out)
    assert envelope["ok"] is False and envelope["error"]["code"] == "unverifiable"
    assert "the search index is missing" in envelope["error"]["message"]
    assert not (ledger_dir["dir"] / search_mod.SEARCH_DB_NAME).exists()


def test_no_misses_file_reads_as_no_logged_misses(ledger_dir, monkeypatch):
    code, out, _ = audit(monkeypatch, ledger_dir["db"])
    assert code == EXIT_OK
    data = json.loads(out)["data"]
    assert data["misses"] == []
    code, out, _ = invoke(monkeypatch, ["--db", str(ledger_dir["db"]), "search-audit"])
    assert out == f"no logged misses ({MISSES_NAME})\n"


def test_a_database_that_does_not_exist_is_refused_by_the_replay(tmp_path, monkeypatch):
    code, _, err = invoke(monkeypatch, ["--db", str(tmp_path / "none.db"), "search-audit"])
    assert code == EXIT_NOT_FOUND and "database does not exist" in err


FILE_NEAR_MISSES = [
    pytest.param(b"{not json\n", "not-json", 1, id="not-json"),
    pytest.param(
        (line() + "\n").encode() + b"\n", "blank-line", 2, id="blank-line"
    ),
    pytest.param(
        (line() + "\n" + line(v=9) + "\n").encode(), "bad-version", 2, id="bad-version-second-line"
    ),
    pytest.param((line()).encode(), "unterminated-line", 1, id="no-final-line-feed"),
    pytest.param(b"\xff\xfe\n", "not-utf8", None, id="not-utf8"),
]


@pytest.mark.parametrize(("content", "name", "number"), FILE_NEAR_MISSES)
def test_a_misses_file_off_its_grammar_makes_the_replay_unverifiable(
    ledger_dir, monkeypatch, content, name, number
):
    db = ledger_dir["db"]
    current_index(monkeypatch, db)
    ledger_dir["misses"].write_bytes(content)
    before = ledger_dir["misses"].read_bytes()
    code, out, err = invoke(monkeypatch, ["--db", str(db), "search-audit"])
    assert code == EXIT_REFUSED and out == ""
    assert err.startswith(f"search-audit is unverifiable: {name}: "), err
    if number is not None:
        assert f"{MISSES_NAME} line {number}" in err
    assert "rank=" not in err
    assert ledger_dir["misses"].read_bytes() == before


def test_a_misses_file_that_is_a_symlink_is_never_read(ledger_dir, monkeypatch, tmp_path):
    current_index(monkeypatch, ledger_dir["db"])
    target = tmp_path / "elsewhere.jsonl"
    target.write_text(line() + "\n", encoding="utf-8")
    ledger_dir["misses"].symlink_to(target)
    code, _, err = invoke(monkeypatch, ["--db", str(ledger_dir["db"]), "search-audit"])
    assert code == EXIT_REFUSED
    assert err.startswith("search-audit is unverifiable: misses-file-unreadable: ")


def test_a_misses_file_that_is_a_fifo_is_unverifiable(ledger_dir, monkeypatch):
    current_index(monkeypatch, ledger_dir["db"])
    os.mkfifo(ledger_dir["misses"])
    code, _, err = invoke(monkeypatch, ["--db", str(ledger_dir["db"]), "search-audit"])
    assert code == EXIT_REFUSED
    assert err.startswith("search-audit is unverifiable: misses-file-not-regular: ")


def test_a_misses_file_that_cannot_be_read_is_unverifiable(ledger_dir, monkeypatch):
    if os.geteuid() == 0:
        pytest.fail("this test needs a user whose file permissions are enforced")
    current_index(monkeypatch, ledger_dir["db"])
    ledger_dir["misses"].write_text(line() + "\n", encoding="utf-8")
    ledger_dir["misses"].chmod(0)
    try:
        code, _, err = invoke(monkeypatch, ["--db", str(ledger_dir["db"]), "search-audit"])
    finally:
        ledger_dir["misses"].chmod(0o600)
    assert code == EXIT_REFUSED
    assert err.startswith("search-audit is unverifiable: misses-file-unreadable: ")


# --- search(..., refresh=False) and the ranking it shares ------------------------


def test_search_without_refresh_raises_on_a_behind_index_and_leaves_it_alone(ledger_dir, monkeypatch):
    db = ledger_dir["db"]
    current_index(monkeypatch, db)
    with open_ledger(db) as lg:
        _append(lg, "item.note", PARENT, {"text": "a later note"}, "later")
    index = ledger_dir["dir"] / search_mod.SEARCH_DB_NAME
    before = sha(index)
    with pytest.raises(search_mod.IndexNotCurrent):
        search_mod.search(db, QUERY, limit=5, refresh=False)
    assert sha(index) == before
    # The default still refreshes, as it always did.
    found = search_mod.search(db, QUERY, limit=5)
    assert found["refreshed"] is True and sha(index) != before


def test_the_replay_ranks_from_the_same_order_search_prints(ledger_dir, monkeypatch):
    db = ledger_dir["db"]
    current_index(monkeypatch, db)
    cards = _hits(monkeypatch, db)
    positions = (1, 5, 12)
    for position in positions:
        card = cards[position - 1]
        expected = card["id"] if card["doc_type"] == "item" else f"event:{card['id'].rsplit(':', 1)[-1]}"
        log(monkeypatch, db, "--query", QUERY, "--expected", expected)
    rows = json.loads(audit(monkeypatch, db)[1])["data"]["misses"]
    assert [row["rank"] for row in rows] == list(positions)
