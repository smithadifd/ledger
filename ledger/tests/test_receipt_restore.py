"""Restore, replay, the restore drill and the other readers on a database
holding a review receipt (with its document) and a grant cite.

An export holding either receipt kind restores only with
``--accept-loop-events``, after its stream digest is checked;
``replay_events`` refuses it; the restore drill passes on a database with a
receipt and no loop event. Every other reader keeps working.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ledger.drill import restore_drill
from ledger.export import export_events, verify_export
from ledger.receipts import write_grant_cite, write_receipt
from ledger.restore import replay_events, restore_export
from ledger.store import LedgerError, claim_database, open_ledger
from ledger.verbs.grants import REVIEW_ROUND_GRANT_ID

from receipt_helpers import DOC, ITEM, STAMPS, count, fields, link, new_item
from test_election_binding import cli, grant_through_picker, new_item as cli_new_item


def receipt_db(tmp_path: Path, monkeypatch=None, *, cite: bool = False,
               receipt: bool = True) -> Path:
    path = tmp_path / "source.db"
    if monkeypatch is not None:
        cli_new_item(monkeypatch, str(path), "it-x")
    claim_database(path, "home")
    with open_ledger(path, create=False) as lg:
        new_item(lg, ITEM)
        link(lg, ITEM, "o/r#12")
        if receipt:
            write_receipt(lg, ITEM, fields(), DOC, **STAMPS)
    if cite:
        grant_through_picker(monkeypatch, str(path), REVIEW_ROUND_GRANT_ID)
        with open_ledger(path, create=False) as lg:
            write_grant_cite(lg, ITEM, repo="o/r", pr=12, round=4, scope="round 4",
                             **STAMPS)
    return path


def export(path: Path, export_dir: Path) -> list:
    with open_ledger(path, create=False) as lg:
        export_events(lg, export_dir)
        return lg.events()


def test_restore_without_the_flag_refuses_a_receipt_and_creates_nothing(tmp_path) -> None:
    export_dir = tmp_path / "export"
    export(receipt_db(tmp_path), export_dir)
    dest = tmp_path / "restored.db"
    with pytest.raises(LedgerError) as info:
        restore_export(export_dir, dest, system="home")
    assert str(info.value) == (
        "restore contains loop-owned or review receipt events (kinds: review.receipt); "
        "pass --accept-loop-events to restore them"
    )
    assert not dest.exists()
    assert not dest.with_name(dest.name + "-wal").exists()


def test_restore_without_the_flag_refuses_a_cite(tmp_path, monkeypatch) -> None:
    export_dir = tmp_path / "export"
    export(receipt_db(tmp_path, monkeypatch, cite=True, receipt=False), export_dir)
    dest = tmp_path / "restored.db"
    with pytest.raises(LedgerError) as info:
        restore_export(export_dir, dest, system="home")
    assert str(info.value) == (
        "restore contains loop-owned or review receipt events (kinds: review.grant-cite); "
        "pass --accept-loop-events to restore them"
    )
    assert not dest.exists()


def test_restore_with_the_flag_restores_verifies_and_records_the_restore(tmp_path) -> None:
    export_dir = tmp_path / "export"
    original = export(receipt_db(tmp_path), export_dir)
    dest = tmp_path / "restored.db"
    seen: list = []
    result = restore_export(export_dir, dest, system="home", accept_loop_events=True,
                            on_accept_loop_events=lambda n, kinds: seen.append((n, kinds)))
    assert seen == [(1, ("review.receipt",))]
    assert result.events == len(original) + 1
    with open_ledger(dest, create=False) as restored:
        restored.verify(system="home")
        events = restored.events()
    assert events[:-1] == original
    record = events[-1]
    assert record.kind == "session.opened"
    assert record.provenance_class == "restored-export"
    assert record.payload["source"].startswith("sha256:")


def test_restore_with_the_flag_checks_the_stream_digest(tmp_path) -> None:
    export_dir = tmp_path / "export"
    export(receipt_db(tmp_path), export_dir)
    events = len(verify_export(export_dir))
    (export_dir / "stream-digest.json").write_text(
        json.dumps({"algorithm": "sha256", "digest": "not-the-export", "events": events}) + "\n",
        encoding="utf-8")
    dest = tmp_path / "restored.db"
    with pytest.raises(LedgerError, match="stream digest"):
        restore_export(export_dir, dest, system="home", accept_loop_events=True)
    assert not dest.exists()


def test_replay_events_refuses_a_receipt_by_kind(tmp_path) -> None:
    export_dir = tmp_path / "export"
    export(receipt_db(tmp_path), export_dir)
    with open_ledger(tmp_path / "replay.db") as lg:
        with pytest.raises(LedgerError) as info:
            replay_events(lg, verify_export(export_dir))
        assert str(info.value) == "replay refuses reserved event: review.receipt"
        assert lg.events() == []


def test_a_restore_drill_of_a_database_with_a_receipt_and_no_loop_event_passes(tmp_path) -> None:
    export_dir = tmp_path / "export"
    source = export(receipt_db(tmp_path), export_dir)
    assert not any(event.kind.startswith("loop.") for event in source)
    assert any(event.kind == "review.receipt" for event in source)
    result = restore_drill(source, export_dir, tmp_path / "drill.db", system="home")
    assert result.events == len(source)


def test_the_cli_restore_names_the_receipt_kinds_it_accepts(tmp_path, monkeypatch) -> None:
    export_dir = tmp_path / "export"
    export(receipt_db(tmp_path, monkeypatch, cite=True), export_dir)
    dest = tmp_path / "restored.db"
    from test_election_binding import run_cli

    code, out, err = run_cli(monkeypatch, ["--db", str(dest), "restore", "--dir",
                                           str(export_dir), "--system", "home",
                                           "--accept-loop-events"])
    assert code == 0, err
    assert err == (
        "restore: accepting 2 loop-reserved events (kinds: review.grant-cite, review.receipt) "
        "because --accept-loop-events was given\n"
    )


def test_the_other_readers_keep_working(tmp_path, monkeypatch) -> None:
    # Bar item 10: on a database holding one receipt with its document and
    # one cite, each reader exits 0.
    db = str(receipt_db(tmp_path, monkeypatch, cite=True))
    with open_ledger(db, create=False) as lg:
        kinds = [event.kind for event in lg.events()]
        before = count(lg)
    assert kinds.count("review.receipt") == 1 and kinds.count("review.grant-cite") == 1
    export_dir = tmp_path / "cli-export"
    for argv in (
        ["card", ITEM],
        ["context", ITEM],
        ["events"],
        ["events", "--kind", "review.receipt"],
        ["verify"],
        ["doctor"],
        ["export", "--dir", str(export_dir)],
        ["export-item", ITEM],
        ["backup", "--to", str(tmp_path / "backup.db")],
        ["index"],
        # search exits 1 (empty) when nothing matches, so query the item's
        # title: the index is built over a log holding both new kinds.
        ["search", ITEM],
        ["search", ITEM, "--item", ITEM],
    ):
        code, env = cli(monkeypatch, db, *argv)
        assert code == 0, (argv, env)
    with open_ledger(db, create=False) as lg:
        assert count(lg) == before
    dest = tmp_path / "from-cli-export.db"
    code, env = cli(monkeypatch, str(dest), "restore", "--dir", str(export_dir),
                    "--system", "home", "--accept-loop-events")
    assert code == 0, env
    code, env = cli(monkeypatch, str(dest), "verify")
    assert code == 0, env
    assert json.dumps(env)
