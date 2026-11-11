"""`query --json` rows carry `parent_id` and `children_open` from v_items.

Every ledger here is a temp DB built through the CLI.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout

from ledger.cli import main


def invoke(monkeypatch, argv):
    for key, value in {
        "LEDGER_HARNESS": "pytest",
        "LEDGER_AGENT": "tester",
        "LEDGER_MODEL": "test-model",
        "LEDGER_SESSION": "sess-1",
    }.items():
        monkeypatch.setenv(key, value)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _new(monkeypatch, db, item_id):
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "new", "--id", item_id, "--type", "ticket", "--title", f"title of {item_id}"],
    )
    assert code == 0, err


def _rows(monkeypatch, db):
    code, out, err = invoke(monkeypatch, ["--db", db, "--json", "query"])
    assert code == 0, err
    return {row["item_id"]: row for row in json.loads(out)["data"]["items"]}


def test_query_json_rows_carry_parent_id_and_children_open(tmp_path, monkeypatch):
    db = str(tmp_path / "ledger.db")
    for item_id in ("it-anchor", "it-a", "it-b", "it-loner"):
        _new(monkeypatch, db, item_id)
    for child in ("it-a", "it-b"):
        code, _out, err = invoke(
            monkeypatch,
            ["--db", db, "link", child, "--rel", "child-of", "--target", "it-anchor"],
        )
        assert code == 0, err
    code, _out, err = invoke(monkeypatch, ["--db", db, "state", "it-b", "--status", "closed"])
    assert code == 0, err

    rows = _rows(monkeypatch, db)
    assert {k: (v["parent_id"], v["children_open"]) for k, v in rows.items()} == {
        "it-anchor": (None, 1),
        "it-a": ("it-anchor", 0),
        "it-b": ("it-anchor", 0),
        "it-loner": (None, 0),
    }
    for row in rows.values():
        assert row["parent_id"] is None or isinstance(row["parent_id"], str)
        assert type(row["children_open"]) is int and row["children_open"] >= 0


def _ids(monkeypatch, db, *flags):
    code, out, err = invoke(monkeypatch, ["--db", db, "--json", "query", *flags])
    assert code == 0, err
    return {row["item_id"] for row in json.loads(out)["data"]["items"]}


def test_query_duplicate_of_and_blocks_keep_the_linking_items(tmp_path, monkeypatch):
    db = str(tmp_path / "ledger.db")
    for item_id in ("it-kept", "it-a", "it-b", "it-c", "it-d", "it-e"):
        _new(monkeypatch, db, item_id)
    code, _out, err = invoke(monkeypatch, ["--db", db, "new", "--id=-dash", "--type", "ticket",
                                           "--title", "hyphen"])
    assert code == 0, err
    links = [
        ("it-a", "duplicate-of", "it-kept"),
        ("it-b", "blocks", "it-kept"),
        ("it-c", "blocks", "it-kept"),
        ("it-d", "blocks", "it-a"),
        ("it-e", "blocks", "-dash"),
    ]
    for item, rel, target in links:
        code, _out, err = invoke(
            monkeypatch, ["--db", db, "link", item, "--rel", rel, f"--target={target}"]
        )
        assert code == 0, err
    code, _out, err = invoke(monkeypatch, ["--db", db, "state", "it-c", "--status", "closed"])
    assert code == 0, err

    assert _ids(monkeypatch, db, "--duplicate-of=it-kept", "--all") == {"it-a"}
    # Every item with a blocks link to the target, whatever its status.
    assert _ids(monkeypatch, db, "--blocks=it-kept", "--all") == {"it-b", "it-c"}
    assert _ids(monkeypatch, db, "--blocks=it-kept", "--status", "open") == {"it-b"}
    assert _ids(monkeypatch, db, "--blocks=-dash") == {"it-e"}
    code, _out, _err = invoke(monkeypatch, ["--db", db, "query", "--duplicate-of=it-b"])
    assert code == 1


def test_query_text_output_is_unchanged(tmp_path, monkeypatch):
    db = str(tmp_path / "ledger.db")
    for item_id in ("it-anchor", "it-a"):
        _new(monkeypatch, db, item_id)
    code, _out, err = invoke(
        monkeypatch, ["--db", db, "link", "it-a", "--rel", "child-of", "--target", "it-anchor"]
    )
    assert code == 0, err
    code, out, err = invoke(monkeypatch, ["--db", db, "query"])
    assert code == 0, err
    lines = sorted(out.splitlines())
    assert lines == [
        "it-a\tticket\topen\ttitle of it-a",
        "it-anchor\tticket\topen\ttitle of it-anchor",
    ]
    assert out.endswith("\n") and "parent_id" not in out and "children_open" not in out
