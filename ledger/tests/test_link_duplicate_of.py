"""`link --rel duplicate-of` records the link and closes the duplicate in one step.

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
from ledger.verbs import EXIT_NOT_FOUND, EXIT_REFUSED


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


def _pair(tmp_path: Path, monkeypatch) -> str:
    db = str(tmp_path / "ledger.db")
    for item_id, title in (("it-loser", "the later copy"), ("it-kept", "the item kept")):
        code, _out, err = invoke(
            monkeypatch,
            ["--db", db, "new", "--id", item_id, "--type", "ticket", "--title", title],
        )
        assert code == 0, err
    return db


def _events(db: str, item_id: str):
    with open_ledger(db, create=False) as lg:
        return [(e.kind, e.payload) for e in lg.events(item_id=item_id)]


def test_duplicate_of_is_a_link_rel() -> None:
    assert "duplicate-of" in LINK_RELS


def test_duplicate_of_records_the_link_and_closes_the_duplicate(tmp_path, monkeypatch):
    db = _pair(tmp_path, monkeypatch)

    code, out, err = invoke(
        monkeypatch,
        ["--db", db, "link", "it-loser", "--rel", "duplicate-of", "--target", "it-kept"],
    )

    assert code == 0, err
    assert out.splitlines()[0].startswith("linked\tit-loser\tduplicate-of\tit-kept\tevent=")
    assert out.splitlines()[1].startswith("closed\tit-loser\tevent=")
    assert _events(db, "it-loser")[1:] == [
        ("item.linked", {"rel": "duplicate-of", "target": "it-kept"}),
        ("item.state", {"status": "closed", "reason": "duplicate of it-kept"}),
    ]
    with open_ledger(db, create=False) as lg:
        assert lg.item("it-loser")["status"] == "closed"
        assert lg.item("it-kept")["status"] == "open"
        assert [row["item_id"] for row in lg.ready()] == ["it-kept"]


def test_duplicate_of_json_names_both_events(tmp_path, monkeypatch):
    db = _pair(tmp_path, monkeypatch)

    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", db, "link", "it-loser", "--rel", "duplicate-of", "--target", "it-kept"],
    )

    assert code == 0, err
    data = json.loads(out)
    data = data.get("data", data)
    assert data["event"]["kind"] == "item.linked"
    assert data["closed_event"]["kind"] == "item.state"
    assert data["closed_event"]["payload"]["status"] == "closed"


def test_both_cards_render_the_duplicate_relation(tmp_path, monkeypatch):
    db = _pair(tmp_path, monkeypatch)
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "link", "it-loser", "--rel", "duplicate-of", "--target", "it-kept"],
    )
    assert code == 0, err
    with open_ledger(db, create=False) as lg:
        link_id = lg.events(item_id="it-loser", kind="item.linked")[0].id

    code, loser_card, err = invoke(monkeypatch, ["--db", db, "card", "it-loser"])
    assert code == 0, err
    code, kept_card, err = invoke(monkeypatch, ["--db", db, "card", "it-kept"])
    assert code == 0, err

    assert f"- duplicate-of: it-kept (event={link_id})" in loser_card
    assert f"- duplicated-by: it-loser (event={link_id})" in kept_card
    assert "status: closed" in loser_card
    assert "status: open" in kept_card


def test_an_already_closed_item_is_refused_and_nothing_is_recorded(tmp_path, monkeypatch):
    db = _pair(tmp_path, monkeypatch)
    code, _out, err = invoke(monkeypatch, ["--db", db, "state", "it-loser", "--status", "closed"])
    assert code == 0, err
    before = _events(db, "it-loser")

    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "link", "it-loser", "--rel", "duplicate-of", "--target", "it-kept"],
    )

    assert code == EXIT_REFUSED
    assert "it-loser is already closed" in err
    assert _events(db, "it-loser") == before


def test_a_missing_or_self_target_is_refused(tmp_path, monkeypatch):
    db = _pair(tmp_path, monkeypatch)

    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "link", "it-loser", "--rel", "duplicate-of", "--target", "it-absent"],
    )
    assert code == EXIT_NOT_FOUND
    assert "item not found: it-absent" in err

    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "link", "it-loser", "--rel", "duplicate-of", "--target", "it-loser"],
    )
    assert code == EXIT_REFUSED
    assert "cannot be a duplicate of itself" in err
    assert [kind for kind, _payload in _events(db, "it-loser")] == ["item.created"]


def test_a_target_closed_as_a_duplicate_is_refused(tmp_path, monkeypatch):
    """Closing both halves of a pair as each other's duplicate would lose the work."""
    db = _pair(tmp_path, monkeypatch)
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "link", "it-kept", "--rel", "duplicate-of", "--target", "it-loser"],
    )
    assert code == 0, err

    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "link", "it-loser", "--rel", "duplicate-of", "--target", "it-kept"],
    )

    assert code == EXIT_REFUSED
    assert "it-kept is itself closed as a duplicate" in err
    with open_ledger(db, create=False) as lg:
        assert lg.item("it-loser")["status"] == "open"


def test_other_rels_still_close_nothing(tmp_path, monkeypatch):
    db = _pair(tmp_path, monkeypatch)

    code, _out, err = invoke(
        monkeypatch, ["--db", db, "link", "it-loser", "--rel", "item", "--target", "it-kept"]
    )

    assert code == 0, err
    with open_ledger(db, create=False) as lg:
        assert lg.item("it-loser")["status"] == "open"
