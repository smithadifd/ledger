"""``ledger links ITEM``: outgoing and incoming links, resolved, read-only."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ledger.cli import main
from ledger.store import open_ledger
from ledger.verbs import EXIT_NOT_FOUND, EXIT_OK, discover_verbs

DATA_KEYS = {"item_id", "title", "outgoing", "incoming"}
OUT_KEYS = {"event_id", "rel", "target", "resolution", "title"}
IN_KEYS = {"event_id", "rel", "source", "resolution", "title"}


def invoke(monkeypatch, argv):
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def run_json(monkeypatch, db, *argv):
    code, out, err = invoke(monkeypatch, ["--json", "--db", str(db), *argv])
    assert "Traceback" not in err
    return code, json.loads(out) if out.strip() else None, err


def new_item(monkeypatch, db: Path, item_id: str, title: str) -> None:
    code, _out, err = invoke(
        monkeypatch,
        ["--db", str(db), "new", "--id", item_id, "--type", "ticket", "--title", title],
    )
    assert code == EXIT_OK, err


def link(monkeypatch, db: Path, item_id: str, rel: str, target: str) -> None:
    code, _out, err = invoke(
        monkeypatch, ["--db", str(db), "link", item_id, "--rel", rel, "--target", target]
    )
    assert code == EXIT_OK, err


def seed(tmp_path: Path, monkeypatch) -> Path:
    """A links to B; C links to A; D links to B (names neither A nor C)."""
    db = tmp_path / "ledger.db"
    new_item(monkeypatch, db, "it-a", "alpha")
    new_item(monkeypatch, db, "it-b", "bravo")
    new_item(monkeypatch, db, "it-c", "charlie")
    new_item(monkeypatch, db, "it-d", "delta")
    link(monkeypatch, db, "it-a", "item", "it-b")
    link(monkeypatch, db, "it-c", "blocks", "it-a")
    link(monkeypatch, db, "it-d", "item", "it-b")
    return db


def event_count(db: Path) -> int:
    with open_ledger(db, create=False) as lg:
        return len(lg.events())


def test_links_is_a_registered_read_verb() -> None:
    verb = discover_verbs()["links"]
    assert verb.writes is False


def test_links_json_shows_outgoing_and_incoming_with_titles(tmp_path, monkeypatch):
    db = seed(tmp_path, monkeypatch)
    code, body, err = run_json(monkeypatch, db, "links", "it-a")
    assert code == EXIT_OK, err
    data = body["data"]
    assert set(data) == DATA_KEYS
    assert data["item_id"] == "it-a"
    assert data["title"] == "alpha"
    assert [
        {key: row[key] for key in ("rel", "target", "resolution", "title")}
        for row in data["outgoing"]
    ] == [{"rel": "item", "target": "it-b", "resolution": "item", "title": "bravo"}]
    assert [
        {key: row[key] for key in ("rel", "source", "resolution", "title")}
        for row in data["incoming"]
    ] == [{"rel": "blocks", "source": "it-c", "resolution": "item", "title": "charlie"}]
    for row in data["outgoing"]:
        assert set(row) == OUT_KEYS
        assert isinstance(row["event_id"], int)
    for row in data["incoming"]:
        assert set(row) == IN_KEYS
        assert isinstance(row["event_id"], int)


def test_links_text_shows_both_directions_one_line_each(tmp_path, monkeypatch):
    db = seed(tmp_path, monkeypatch)
    code, out, err = invoke(monkeypatch, ["--db", str(db), "links", "it-a"])
    assert code == EXIT_OK, err
    rows = [line.split("\t") for line in out.splitlines()]
    assert [row[:5] for row in rows] == [
        ["out", "item", "it-b", "item", "bravo"],
        ["in", "blocks", "it-c", "item", "charlie"],
    ]
    assert all(len(row) == 6 and row[5].startswith("event=") for row in rows)


def test_incoming_lists_every_item_that_names_the_target(tmp_path, monkeypatch):
    db = seed(tmp_path, monkeypatch)
    code, body, err = run_json(monkeypatch, db, "links", "it-b")
    assert code == EXIT_OK, err
    assert body["data"]["outgoing"] == []
    assert [row["source"] for row in body["data"]["incoming"]] == ["it-a", "it-d"]
    assert [row["title"] for row in body["data"]["incoming"]] == ["alpha", "delta"]


def test_a_link_to_a_missing_target_is_printed_as_unresolved(tmp_path, monkeypatch):
    db = seed(tmp_path, monkeypatch)
    link(monkeypatch, db, "it-a", "blocks", "it-gone")
    code, body, err = run_json(monkeypatch, db, "links", "it-a")
    assert code == EXIT_OK, err
    gone = [row for row in body["data"]["outgoing"] if row["target"] == "it-gone"]
    assert gone == [
        {
            "event_id": gone[0]["event_id"],
            "rel": "blocks",
            "target": "it-gone",
            "resolution": "unresolved",
            "title": None,
        }
    ]
    code, out, err = invoke(monkeypatch, ["--db", str(db), "links", "it-a"])
    assert code == EXIT_OK, err
    assert [line.split("\t")[:5] for line in out.splitlines()] == [
        ["out", "item", "it-b", "item", "bravo"],
        ["out", "blocks", "it-gone", "unresolved", "-"],
        ["in", "blocks", "it-c", "item", "charlie"],
    ]


def test_an_external_target_is_printed_as_external(tmp_path, monkeypatch):
    db = seed(tmp_path, monkeypatch)
    link(monkeypatch, db, "it-a", "url", "https://example.com/brief")
    code, body, err = run_json(monkeypatch, db, "links", "it-a")
    assert code == EXIT_OK, err
    rows = [row for row in body["data"]["outgoing"] if row["rel"] == "url"]
    assert [(row["target"], row["resolution"], row["title"]) for row in rows] == [
        ("https://example.com/brief", "external", None)
    ]


def test_an_incoming_link_from_a_missing_item_is_unresolved(tmp_path, monkeypatch):
    db = seed(tmp_path, monkeypatch)
    with open_ledger(db) as lg:
        # Written past the CLI: a link held by an id no item.created names.
        lg.append(
            kind="item.linked",
            item_id="it-ghost",
            idempotency_key="k-ghost",
            harness="pytest",
            payload={"rel": "item", "target": "it-a"},
        )
    code, body, err = run_json(monkeypatch, db, "links", "it-a")
    assert code == EXIT_OK, err
    ghost = [row for row in body["data"]["incoming"] if row["source"] == "it-ghost"]
    assert [(row["resolution"], row["title"]) for row in ghost] == [("unresolved", None)]


def test_unknown_id_is_refused_by_name(tmp_path, monkeypatch):
    db = seed(tmp_path, monkeypatch)
    code, body, err = run_json(monkeypatch, db, "links", "it-nope")
    assert code == EXIT_NOT_FOUND, err
    assert body["ok"] is False
    assert body["data"] is None
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == "item not found: it-nope"
    code, out, err = invoke(monkeypatch, ["--db", str(db), "links", "it-nope"])
    assert code == EXIT_NOT_FOUND
    assert out == ""
    assert "item not found: it-nope" in err


def test_an_unknown_id_named_only_as_a_link_target_is_still_refused(
    tmp_path, monkeypatch
):
    db = seed(tmp_path, monkeypatch)
    link(monkeypatch, db, "it-a", "blocks", "it-gone")
    code, body, err = run_json(monkeypatch, db, "links", "it-gone")
    assert code == EXIT_NOT_FOUND, err
    assert body["error"]["code"] == "not_found"


def test_a_known_item_no_link_names_exits_1_empty(tmp_path, monkeypatch):
    db = seed(tmp_path, monkeypatch)
    new_item(monkeypatch, db, "it-e", "echo")
    code, body, err = run_json(monkeypatch, db, "links", "it-e")
    assert code == EXIT_NOT_FOUND, err
    assert body["error"]["code"] == "empty"
    assert body["error"]["message"] == "no links name it-e"


def test_a_self_link_is_in_both_directions(tmp_path, monkeypatch):
    db = seed(tmp_path, monkeypatch)
    link(monkeypatch, db, "it-d", "item", "it-d")
    code, body, err = run_json(monkeypatch, db, "links", "it-d")
    assert code == EXIT_OK, err
    assert [row["target"] for row in body["data"]["outgoing"]] == ["it-b", "it-d"]
    assert [row["source"] for row in body["data"]["incoming"]] == ["it-d"]


def test_text_cells_escape_tabs_and_newlines(tmp_path, monkeypatch):
    db = seed(tmp_path, monkeypatch)
    link(monkeypatch, db, "it-a", "url", "a\tb\nc")
    code, out, err = invoke(monkeypatch, ["--db", str(db), "links", "it-a"])
    assert code == EXIT_OK, err
    lines = out.splitlines()
    assert len(lines) == 3
    assert all(len(line.split("\t")) == 6 for line in lines)
    assert lines[1].split("\t")[2] == "a\\tb\\nc"


def test_links_reads_and_never_writes(tmp_path, monkeypatch):
    db = seed(tmp_path, monkeypatch)
    before = event_count(db)
    run_json(monkeypatch, db, "links", "it-a")
    run_json(monkeypatch, db, "links", "it-nope")
    assert event_count(db) == before
