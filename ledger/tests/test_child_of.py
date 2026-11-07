"""`link --rel child-of`: a child under one anchor, with a rollup on the anchor's card.

Every ledger here is a temp DB built through the CLI.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from ledger.cli import main
from ledger.schema import LINK_RELS
from ledger.store import open_ledger
from ledger.verbs import EXIT_REFUSED, EXIT_USAGE


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


def _db(tmp_path: Path, monkeypatch, *ids: str) -> str:
    db = str(tmp_path / "ledger.db")
    for item_id in ids:
        code, _out, err = invoke(
            monkeypatch,
            ["--db", db, "new", "--id", item_id, "--type", "ticket", "--title", f"title of {item_id}"],
        )
        assert code == 0, err
    return db


def _child_of(monkeypatch, db, child, anchor, *extra):
    return invoke(
        monkeypatch,
        ["--db", db, "link", child, "--rel", "child-of", "--target", anchor, *extra],
    )


def _event_count(db: str) -> int:
    with open_ledger(db, create=False) as lg:
        return len(lg.events())


def _item(db: str, item_id: str) -> dict:
    with open_ledger(db, create=False) as lg:
        row = lg.item(item_id)
    assert row is not None
    return row


def _card(monkeypatch, db: str, item_id: str) -> str:
    code, out, err = invoke(monkeypatch, ["--db", db, "card", item_id])
    assert code == 0, err
    return out


def _close(monkeypatch, db: str, item_id: str) -> None:
    code, _out, err = invoke(monkeypatch, ["--db", db, "state", item_id, "--status", "closed"])
    assert code == 0, err


def _children_lines(card: str) -> list[str]:
    lines = card.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("## Children"))
    body = []
    for line in lines[start + 1 :]:
        if line.startswith("## ") or not line:
            break
        body.append(line)
    return body


def test_child_of_is_a_link_rel() -> None:
    assert "child-of" in LINK_RELS


def test_a_child_shows_on_the_anchor_card_with_a_zero_of_one_rollup(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, "it-anchor", "it-kid")

    code, out, err = _child_of(monkeypatch, db, "it-kid", "it-anchor")

    assert code == 0, err
    assert out.startswith("linked\tit-kid\tchild-of\tit-anchor\t")
    card = _card(monkeypatch, db, "it-anchor")
    assert "## Children (0/1)" in card
    assert _children_lines(card) == ["- it-kid: title of it-kid [open] holder: -"]
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "card", "it-anchor"])
    assert code == 0, err
    data = json.loads(out)["data"]
    assert data["children_rollup"] == {"closed": 0, "total": 1}
    assert [row["item_id"] for row in data["children"]] == ["it-kid"]
    # The child's own card has no Children section.
    assert "## Children" not in _card(monkeypatch, db, "it-kid")


def test_closing_the_child_moves_the_rollup_and_children_open(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, "it-anchor", "it-kid")
    assert _child_of(monkeypatch, db, "it-kid", "it-anchor")[0] == 0
    assert _item(db, "it-anchor")["children_open"] == 1

    _close(monkeypatch, db, "it-kid")

    assert "## Children (1/1)" in _card(monkeypatch, db, "it-anchor")
    assert _item(db, "it-anchor")["children_open"] == 0
    assert _item(db, "it-kid")["parent_id"] == "it-anchor"


def test_a_second_anchor_refuses_without_move_and_moves_with_it(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, "it-one", "it-two", "it-kid")
    assert _child_of(monkeypatch, db, "it-kid", "it-one")[0] == 0
    before = _event_count(db)

    code, _out, err = _child_of(monkeypatch, db, "it-kid", "it-two")

    assert code == EXIT_REFUSED
    assert "it-one" in err and "--move" in err
    assert _event_count(db) == before
    assert _item(db, "it-kid")["parent_id"] == "it-one"

    code, _out, err = _child_of(monkeypatch, db, "it-kid", "it-two", "--move")

    assert code == 0, err
    assert _event_count(db) == before + 1
    assert _item(db, "it-kid")["parent_id"] == "it-two"
    assert _item(db, "it-one")["children_open"] == 0
    assert _item(db, "it-two")["children_open"] == 1
    assert "## Children" not in _card(monkeypatch, db, "it-one")
    assert "## Children (0/1)" in _card(monkeypatch, db, "it-two")
    with open_ledger(db, create=False) as lg:
        moves = lg.events(item_id="it-kid", kind="item.linked")
    # The first link stays as written; the move is a new event naming it.
    assert [e.payload for e in moves] == [
        {"rel": "child-of", "target": "it-one"},
        {"rel": "child-of", "target": "it-two", "moved_from": "it-one"},
    ]


def test_linking_the_same_anchor_again_writes_nothing(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, "it-anchor", "it-kid")
    assert _child_of(monkeypatch, db, "it-kid", "it-anchor")[0] == 0
    before = _event_count(db)

    for extra in ((), ("--move",)):
        code, out, err = _child_of(monkeypatch, db, "it-kid", "it-anchor", *extra)
        assert code == 0, err
        assert out.startswith("unchanged\tit-kid\tchild-of\tit-anchor\t")

    assert _event_count(db) == before


def test_an_idempotency_key_retry_of_a_move_returns_the_stored_event(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, "it-one", "it-two", "it-kid")
    assert _child_of(monkeypatch, db, "it-kid", "it-one")[0] == 0
    first = _child_of(monkeypatch, db, "it-kid", "it-two", "--move", "--idempotency-key", "k1")
    count = _event_count(db)

    again = _child_of(monkeypatch, db, "it-kid", "it-two", "--move", "--idempotency-key", "k1")

    assert first[0] == 0 and again == first
    assert _event_count(db) == count


def test_self_link_and_cycles_refuse_and_write_nothing(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, "it-a", "it-b", "it-c")
    before = _event_count(db)

    code, _out, err = _child_of(monkeypatch, db, "it-a", "it-a")
    assert code == EXIT_REFUSED and "own anchor" in err
    assert _event_count(db) == before

    assert _child_of(monkeypatch, db, "it-a", "it-b")[0] == 0
    before = _event_count(db)
    code, _out, err = _child_of(monkeypatch, db, "it-b", "it-a")
    assert code == EXIT_REFUSED and "cycle" in err
    assert _event_count(db) == before
    assert _item(db, "it-b")["parent_id"] is None

    # A deeper cycle: a under b, b under c, then c under a.
    assert _child_of(monkeypatch, db, "it-b", "it-c")[0] == 0
    before = _event_count(db)
    code, _out, err = _child_of(monkeypatch, db, "it-c", "it-a")
    assert code == EXIT_REFUSED and "cycle" in err
    assert _event_count(db) == before
    # The same cycle through --move is refused too.
    code, _out, err = _child_of(monkeypatch, db, "it-c", "it-a", "--move")
    assert code == EXIT_REFUSED and "cycle" in err
    assert _event_count(db) == before


def test_a_missing_anchor_is_not_found_and_writes_nothing(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, "it-kid")
    before = _event_count(db)

    code, _out, err = _child_of(monkeypatch, db, "it-kid", "it-nowhere")

    assert code == 1 and "it-nowhere" in err
    assert _event_count(db) == before


def test_view_columns_match_across_a_small_tree(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, "it-root", "it-a", "it-b", "it-c", "it-lone")
    for child, anchor in (("it-a", "it-root"), ("it-b", "it-root"), ("it-c", "it-a")):
        assert _child_of(monkeypatch, db, child, anchor)[0] == 0
    _close(monkeypatch, db, "it-b")

    with open_ledger(db, create=False) as lg:
        rows = {row["item_id"]: row for row in lg.items()}
    assert {k: (v["parent_id"], v["children_open"]) for k, v in rows.items()} == {
        "it-root": (None, 1),
        "it-a": ("it-root", 1),
        "it-b": ("it-root", 0),
        "it-c": ("it-a", 0),
        "it-lone": (None, 0),
    }
    assert "## Children (1/2)" in _card(monkeypatch, db, "it-root")
    assert "## Children (0/1)" in _card(monkeypatch, db, "it-a")


def test_blocks_order_is_honoured_in_the_children_section(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, "it-anchor", "it-k1", "it-k2", "it-k3")
    for kid in ("it-k1", "it-k2", "it-k3"):
        assert _child_of(monkeypatch, db, kid, "it-anchor")[0] == 0

    def order() -> list[str]:
        lines = _children_lines(_card(monkeypatch, db, "it-anchor"))
        return [line.split(":")[0][2:] for line in lines]

    # Without blocks links the fallback is creation order.
    assert order() == ["it-k1", "it-k2", "it-k3"]

    # k3 blocks k1 (the link lives on the blocker and names what it blocks).
    code, _out, err = invoke(
        monkeypatch, ["--db", db, "link", "it-k3", "--rel", "blocks", "--target", "it-k1"]
    )
    assert code == 0, err
    names = order()
    assert names.index("it-k3") < names.index("it-k1")
    assert sorted(names) == ["it-k1", "it-k2", "it-k3"]


def test_resume_brief_prints_the_rollup_for_an_anchor(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, "it-anchor", "it-kid", "it-other")
    assert _child_of(monkeypatch, db, "it-kid", "it-anchor")[0] == 0

    code, out, err = invoke(
        monkeypatch, ["--db", db, "resume", "--brief", "--item=it-anchor"]
    )
    assert code == 0, err
    assert "  children: 0/1 closed" in out.splitlines()

    _close(monkeypatch, db, "it-kid")
    code, out, err = invoke(monkeypatch, ["--db", db, "resume", "--item=it-anchor"])
    assert code == 0, err
    assert "  children: 1/1 closed" in out.splitlines()

    # An item with no children prints no rollup line.
    code, out, err = invoke(monkeypatch, ["--db", db, "resume", "--item=it-other"])
    assert code == 0, err
    assert "children:" not in out


def test_move_with_another_rel_refuses_and_writes_nothing(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, "it-a", "it-b")
    before = _event_count(db)

    for rel in ("blocks", "item", "duplicate-of"):
        code, _out, err = invoke(
            monkeypatch,
            ["--db", db, "link", "it-a", "--rel", rel, "--target", "it-b", "--move"],
        )
        assert code == EXIT_USAGE, rel
        assert "--move" in err
    assert _event_count(db) == before
