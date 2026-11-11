"""`verify` replays the child-of events through the write path's forest guard.

A store written before the guard existed, or edited outside the CLI, can hold
a broken forest. Each broken store here is written with the store's guard
switched off for the write only; `verify` then runs with the guard in place.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ledger import store
from ledger.cli import main
from ledger.store import open_ledger
from ledger.verbs import EXIT_INTEGRITY, EXIT_OK
from ledger.verbs import verify as verify_verb


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
            ["--db", db, "new", "--id", item_id, "--type", "ticket", "--title", item_id],
        )
        assert code == 0, err
    return db


def _write_unguarded(db: str, links) -> list[int]:
    """Append child-of links with the forest guard a no-op; return their ids."""
    ids = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store, "check_child_of_event", lambda *args, **kwargs: None)
        with open_ledger(db, create=False) as lg:
            for key, (child, payload) in enumerate(links):
                event = lg.append(
                    kind="item.linked",
                    payload=payload,
                    item_id=child,
                    idempotency_key=f"unguarded-{key}",
                )
                ids.append(event.id)
    return ids


def _link(monkeypatch, db, child, anchor, *extra):
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "link", child, "--rel", "child-of", "--target", anchor, *extra],
    )
    assert code == 0, err


def _verify(monkeypatch, db, *extra):
    return invoke(monkeypatch, ["--db", db, "verify", *extra])


def test_a_valid_tree_with_a_move_passes_with_the_output_and_exit_unchanged(
    tmp_path, monkeypatch
):
    db = _db(tmp_path, monkeypatch, "it-root", "it-a", "it-b")
    _link(monkeypatch, db, "it-a", "it-root")
    _link(monkeypatch, db, "it-b", "it-a")
    _link(monkeypatch, db, "it-b", "it-root", "--move")
    with open_ledger(db, create=False) as lg:
        count = len(lg.events())

    code, out, err = _verify(monkeypatch, db)
    assert (code, out, err) == (EXIT_OK, f"ok events={count}\n", "")
    code, out, err = _verify(monkeypatch, db, "--json")
    assert code == EXIT_OK, err
    assert json.loads(out) == {"data": {"events": count}, "error": None, "ok": True}


def test_a_store_holding_a_cycle_fails_naming_the_event_and_code(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, "it-x", "it-y")
    first, second = _write_unguarded(
        db,
        [
            ("it-x", {"rel": "child-of", "target": "it-y"}),
            ("it-y", {"rel": "child-of", "target": "it-x"}),
        ],
    )
    code, out, err = _verify(monkeypatch, db)
    assert code == EXIT_INTEGRITY
    assert out == ""
    assert err.startswith(f"event {second} cycle: "), err
    assert str(first) not in err.split(":", 1)[0]

    code, out, _err = _verify(monkeypatch, db, "--json")
    assert code == EXIT_INTEGRITY
    error = json.loads(out)["error"]
    assert error["code"] == "cycle"
    assert error["message"].startswith(f"event {second} cycle: ")


def test_only_the_first_break_is_reported(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, "it-a", "it-b")
    self_link, _cycle_a, _cycle_b = _write_unguarded(
        db,
        [
            ("it-a", {"rel": "child-of", "target": "it-a"}),
            ("it-a", {"rel": "child-of", "target": "it-b", "moved_from": "it-a"}),
            ("it-b", {"rel": "child-of", "target": "it-a"}),
        ],
    )
    code, _out, err = _verify(monkeypatch, db)
    assert code == EXIT_INTEGRITY
    assert err == (
        f"event {self_link} self_link: an item cannot be its own anchor: it-a\n"
    )


@pytest.mark.parametrize(
    "links, code",
    [
        (
            [("it-kid", {"rel": "child-of", "target": "it-one"}),
             ("it-kid", {"rel": "child-of", "target": "it-two"})],
            "second_anchor",
        ),
        ([("it-kid", {"rel": "child-of", "target": "it-ghost"})], "anchor_missing"),
        ([("it-kid", {"rel": "child-of", "target": "it-kid"})], "self_link"),
    ],
)
def test_every_forest_rule_is_replayed(tmp_path, monkeypatch, links, code):
    db = _db(tmp_path, monkeypatch, "it-one", "it-two", "it-kid")
    breaking = _write_unguarded(db, links)[-1]
    rc, _out, err = _verify(monkeypatch, db)
    assert rc == EXIT_INTEGRITY
    assert err.startswith(f"event {breaking} {code}: "), err


def test_a_link_to_an_anchor_created_later_is_refused_in_event_order(tmp_path, monkeypatch):
    # The anchor exists by the end of the log, but not when the link was
    # written: the replay sees only the events before each one.
    db = _db(tmp_path, monkeypatch, "it-kid")
    [link] = _write_unguarded(db, [("it-kid", {"rel": "child-of", "target": "it-late"})])
    assert invoke(
        monkeypatch,
        ["--db", db, "new", "--id", "it-late", "--type", "ticket", "--title", "late"],
    )[0] == 0
    code, _out, err = _verify(monkeypatch, db)
    assert code == EXIT_INTEGRITY
    assert err.startswith(f"event {link} anchor_missing: "), err


def test_an_event_the_guard_cannot_evaluate_is_a_finding_not_a_skip(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, "it-a", "it-b")
    _link(monkeypatch, db, "it-a", "it-b")
    with open_ledger(db, create=False) as lg:
        events = lg.events()
    linked = [event for event in events if event.kind == "item.linked"][-1]

    def cannot_evaluate(conn, kind, payload, item_id):
        if kind == "item.linked":
            raise RuntimeError("guard could not read the event")

    monkeypatch.setattr(verify_verb, "check_child_of_event", cannot_evaluate)
    code, _out, err = _verify(monkeypatch, db)
    assert code == EXIT_INTEGRITY
    assert err == (
        f"event {linked.id} unevaluable: RuntimeError: guard could not read the event\n"
    )


def test_verify_reuses_the_write_paths_guard() -> None:
    assert verify_verb.check_child_of_event is store.check_child_of_event


def test_the_replay_writes_nothing_to_the_store(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, "it-x", "it-y")
    _write_unguarded(
        db,
        [
            ("it-x", {"rel": "child-of", "target": "it-y"}),
            ("it-y", {"rel": "child-of", "target": "it-x"}),
        ],
    )
    with open_ledger(db, create=False) as lg:
        before = [event.hash for event in lg.events()]
    assert _verify(monkeypatch, db)[0] == EXIT_INTEGRITY
    with open_ledger(db, create=False) as lg:
        assert [event.hash for event in lg.events()] == before


def test_a_broken_chain_still_reports_integrity_before_the_forest(tmp_path, monkeypatch):
    db = _db(tmp_path, monkeypatch, "it-x", "it-y")
    _write_unguarded(
        db,
        [
            ("it-x", {"rel": "child-of", "target": "it-y"}),
            ("it-y", {"rel": "child-of", "target": "it-x"}),
        ],
    )

    def broken(self, *, system=None):
        raise store.ChainBroken("event 1: hash mismatch")

    monkeypatch.setattr(store.Ledger, "verify", broken)
    code, out, _err = _verify(monkeypatch, db, "--json")
    assert code == EXIT_INTEGRITY
    assert json.loads(out)["error"] == {"code": "integrity", "message": "event 1: hash mismatch"}
