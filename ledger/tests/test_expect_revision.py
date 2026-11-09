"""``--expect-revision``: compare-and-set on an item's newest event id."""
from __future__ import annotations

import io
import json
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ledger.cli import main
from ledger.verbs import EXIT_REFUSED, EXIT_STALE, EXIT_USAGE


def invoke(monkeypatch, argv, *, actor="writer-a", session="sess-a"):
    for key, value in {
        "LEDGER_HARNESS": "pytest",
        "LEDGER_AGENT": actor,
        "LEDGER_MODEL": "test-model",
        "LEDGER_SESSION": session,
    }.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _event_count(db: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()


def _revision(monkeypatch, db: str, item: str = "it-1"):
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "query", "--item", item])
    assert code == 0, err
    return json.loads(out)["data"]["items"][0]["revision"]


def _note(db: str, text: str, *extra: str, item: str = "it-1") -> list[str]:
    return [
        "--json", "--db", db, "append", "--kind", "item.note", "--item", item,
        "--payload", json.dumps({"text": text}), *extra,
    ]


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "ledger.db")
    code, _out, err = invoke(
        monkeypatch,
        ["--db", path, "new", "--id", "it-1", "--type", "ticket", "--title", "synthetic"],
    )
    assert code == 0, err
    return path


def test_query_exposes_item_revision(monkeypatch, db: str) -> None:
    created = _revision(monkeypatch, db)
    assert created == 1
    code, out, err = invoke(monkeypatch, _note(db, "one"))
    assert code == 0, err
    assert json.loads(out)["data"]["event"]["id"] == 2
    assert _revision(monkeypatch, db) == 2


def test_query_revision_is_per_item(monkeypatch, db: str) -> None:
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "new", "--id", "it-2", "--type", "ticket", "--title", "other"],
    )
    assert code == 0, err
    code, _out, err = invoke(monkeypatch, _note(db, "on one"))
    assert code == 0, err
    assert _revision(monkeypatch, db, "it-1") == 3
    assert _revision(monkeypatch, db, "it-2") == 2


def test_second_writer_with_stale_revision_is_refused(monkeypatch, db: str) -> None:
    seen_a = _revision(monkeypatch, db)
    seen_b = _revision(monkeypatch, db)
    assert seen_a == seen_b
    code, _out, err = invoke(
        monkeypatch,
        _note(db, "from a", "--expect-revision", str(seen_a)),
        actor="writer-a",
        session="sess-a",
    )
    assert code == 0, err
    before = _event_count(db)
    code, out, err = invoke(
        monkeypatch,
        _note(db, "from b", "--expect-revision", str(seen_b)),
        actor="writer-b",
        session="sess-b",
    )
    assert code == EXIT_STALE
    assert EXIT_STALE not in (EXIT_REFUSED, EXIT_USAGE, 0, 1, 4)
    body = json.loads(out)
    assert body["ok"] is False
    assert body["error"]["code"] == "stale_revision"
    message = body["error"]["message"]
    assert "it-1" in message
    assert f"current is {seen_a + 1}" in message
    assert "actor=writer-a" in message
    assert "session=sess-a" in message
    assert "writer-b" not in message
    assert _event_count(db) == before
    assert _revision(monkeypatch, db) == seen_a + 1


def test_stale_refusal_text_mode_names_writer(monkeypatch, db: str) -> None:
    code, _out, err = invoke(monkeypatch, _note(db, "first"), actor="writer-a")
    assert code == 0, err
    code, out, err = invoke(
        monkeypatch,
        ["--db", db, "note", "it-1", "--text", "late", "--expect-revision", "1"],
        actor="writer-b",
    )
    assert code == EXIT_STALE
    assert out == ""
    assert "actor=writer-a" in err
    assert "Traceback" not in err


def test_stale_refusal_plain_writer_message_is_unchanged(monkeypatch, db: str) -> None:
    code, _out, err = invoke(monkeypatch, _note(db, "first"))
    assert code == 0, err
    code, out, _err = invoke(monkeypatch, _note(db, "late", "--expect-revision", "1"))
    assert code == EXIT_STALE
    assert json.loads(out)["error"]["message"] == (
        "stale revision for item it-1: expected 1, current is 2 "
        "(event 2 written by actor=writer-a session=sess-a)"
    )


def test_stale_refusal_keeps_non_ascii_writer_readable(monkeypatch, db: str) -> None:
    code, _out, err = invoke(
        monkeypatch, _note(db, "first"), actor="écrivain-ü", session="séance ü"
    )
    assert code == 0, err
    code, out, _err = invoke(monkeypatch, _note(db, "late", "--expect-revision", "1"))
    assert code == EXIT_STALE
    message = json.loads(out)["error"]["message"]
    assert "actor=écrivain-ü session=séance ü)" in message


def test_stale_refusal_escapes_control_characters_in_writer(
    monkeypatch, db: str
) -> None:
    # A stored actor or session holding a newline or a terminal control
    # sequence must not split the one-line refusal or forge a second line.
    actor = "a\nstale revision for item X: expected 1, current is 1 (ok)"
    session = "s\r\x1b[2K\x7fend\tz"
    code, _out, err = invoke(monkeypatch, _note(db, "first"), actor=actor, session=session)
    assert code == 0, err
    code, out, _err = invoke(monkeypatch, _note(db, "late", "--expect-revision", "1"))
    assert code == EXIT_STALE
    message = json.loads(out)["error"]["message"]
    assert "\n" not in message
    assert "\r" not in message
    assert "\x1b" not in message
    assert "\x7f" not in message
    raw = [ch for ch in message if ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F]
    assert raw == []
    assert (
        "actor=a\\nstale revision for item X: expected 1, current is 1 (ok) "
        in message
    )
    assert "session=s\\r\\x1b[2K\\x7fend\\tz)" in message
    # Text mode prints the same refusal as exactly one line.
    code, out, err = invoke(
        monkeypatch, ["--db", db, "note", "it-1", "--text", "late", "--expect-revision", "1"]
    )
    assert code == EXIT_STALE
    assert out == ""
    assert err.endswith("\n")
    assert "\n" not in err[:-1]
    assert "\r" not in err
    assert "\x1b" not in err


def test_stale_refusal_names_unset_writer_as_dash(monkeypatch, db: str) -> None:
    # A write with LEDGER_AGENT and LEDGER_SESSION unset stores NULL; the
    # refusal still names that writer as "-". An empty value is normalised
    # to NULL before it is stored, so this one case covers both.
    code, out, err = invoke(monkeypatch, _note(db, "first"), actor=None, session=None)
    assert code == 0, err
    written = json.loads(out)["data"]["event"]["id"]
    conn = sqlite3.connect(db)
    try:
        stored = conn.execute(
            "SELECT actor, session FROM events WHERE id = ?", (written,)
        ).fetchone()
    finally:
        conn.close()
    assert stored == (None, None)
    code, out, _err = invoke(monkeypatch, _note(db, "late", "--expect-revision", "1"))
    assert code == EXIT_STALE
    assert json.loads(out)["error"]["message"].endswith(
        f"(event {written} written by actor=- session=-)"
    )


def test_fresh_revision_lands_and_new_revision_chains(monkeypatch, db: str) -> None:
    code, out, err = invoke(monkeypatch, _note(db, "one", "--expect-revision", "1"))
    assert code == 0, err
    code, out, err = invoke(monkeypatch, _note(db, "two", "--expect-revision", "2"))
    assert code == 0, err
    assert json.loads(out)["data"]["event"]["id"] == 3


def test_state_and_note_verbs_take_the_flag(monkeypatch, db: str) -> None:
    code, _out, err = invoke(monkeypatch, _note(db, "bump"), actor="writer-a")
    assert code == 0, err
    before = _event_count(db)
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "state", "it-1", "--status", "blocked", "--expect-revision", "1"],
    )
    assert code == EXIT_STALE, err
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "note", "it-1", "--text", "x", "--expect-revision", "1"],
    )
    assert code == EXIT_STALE, err
    assert _event_count(db) == before
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "state", "it-1", "--status", "blocked", "--expect-revision", "2"],
    )
    assert code == 0, err
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "note", "it-1", "--text", "x", "--expect-revision", "3"],
    )
    assert code == 0, err


def test_expect_none_on_item_with_events_is_refused(monkeypatch, db: str) -> None:
    before = _event_count(db)
    code, out, _err = invoke(monkeypatch, _note(db, "x", "--expect-revision", "none"))
    assert code == EXIT_STALE
    message = json.loads(out)["error"]["message"]
    assert "expected none" in message
    assert "current is 1" in message
    assert _event_count(db) == before


def test_expect_none_on_new_item_lands(monkeypatch, db: str) -> None:
    payload = json.dumps({"type": "ticket", "title": "fresh"})
    argv = [
        "--json", "--db", db, "append", "--kind", "item.created", "--item", "it-new",
        "--payload", payload, "--expect-revision", "none",
    ]
    code, out, err = invoke(monkeypatch, argv)
    assert code == 0, err
    assert json.loads(out)["data"]["event"]["item_id"] == "it-new"
    # Now it has an event, so the same expectation no longer holds.
    code, _out, _err = invoke(
        monkeypatch,
        [*argv[:-3], json.dumps({"type": "ticket", "title": "again"}), "--expect-revision", "none"],
    )
    assert code == EXIT_STALE


def test_missing_item_with_numeric_revision_refuses(monkeypatch, db: str) -> None:
    payload = json.dumps({"type": "ticket", "title": "fresh"})
    before = _event_count(db)
    code, out, _err = invoke(
        monkeypatch,
        [
            "--json", "--db", db, "append", "--kind", "item.created", "--item", "ghost",
            "--payload", payload, "--expect-revision", "7",
        ],
    )
    assert code == EXIT_STALE
    assert "item has no events" in json.loads(out)["error"]["message"]
    assert _event_count(db) == before


def test_replay_of_stored_key_ignores_stale_revision(monkeypatch, db: str) -> None:
    argv = _note(db, "once", "--idempotency-key", "key-1", "--expect-revision", "1")
    code, out, err = invoke(monkeypatch, argv)
    assert code == 0, err
    stored = json.loads(out)["data"]["event"]
    code, _out, err = invoke(monkeypatch, _note(db, "other"), actor="writer-b")
    assert code == 0, err
    before = _event_count(db)
    # Revision 1 is stale now, but the key is already stored: same event back.
    code, out, err = invoke(monkeypatch, argv)
    assert code == 0, err
    assert json.loads(out)["data"]["event"] == stored
    assert _event_count(db) == before
    # A new key under the same stale revision is still refused.
    code, _out, _err = invoke(
        monkeypatch,
        _note(db, "once", "--idempotency-key", "key-2", "--expect-revision", "1"),
    )
    assert code == EXIT_STALE


@pytest.mark.parametrize(
    "bad",
    ["", "0", "-1", "abc", "1.5", "+2", " 2", "None", "NONE", "9" * 5000, str(2**63)],
)
def test_unparseable_revision_is_a_usage_error(monkeypatch, db: str, bad: str) -> None:
    before = _event_count(db)
    code, out, _err = invoke(monkeypatch, _note(db, "x", "--expect-revision", bad))
    assert code == EXIT_USAGE
    assert json.loads(out)["error"]["code"] == "usage"
    assert _event_count(db) == before


def test_query_revision_matches_state_under_concurrent_append(
    monkeypatch, db: str
) -> None:
    from ledger.store import Ledger, open_ledger

    fired = []

    def wrap(name):
        original = getattr(Ledger, name)

        def wrapper(self, *args, **kwargs):
            result = original(self, *args, **kwargs)
            if not fired:
                fired.append(name)
                other = open_ledger(db)
                try:
                    other.append(
                        kind="item.state",
                        payload={"status": "blocked"},
                        item_id="it-1",
                        idempotency_key="interleaved",
                        actor="writer-b",
                        harness="pytest",
                    )
                finally:
                    other.close()
            return result

        monkeypatch.setattr(Ledger, name, wrapper)

    # Whichever read query uses, an append lands right after it returns.
    for name in ("items", "item_revisions", "items_with_revision"):
        if hasattr(Ledger, name):
            wrap(name)
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "query", "--item", "it-1"])
    assert code == EXIT_REFUSED, err
    assert fired
    assert json.loads(out)["error"]["code"] == "db_fd_unknown"
    assert _event_count(db) == 1


def test_revision_without_item_is_refused(monkeypatch, db: str) -> None:
    before = _event_count(db)
    code, _out, err = invoke(
        monkeypatch,
        [
            "--db", db, "append", "--kind", "session.opened",
            "--payload", json.dumps({"purpose": "synthetic"}),
            "--expect-revision", "1",
        ],
    )
    assert code == EXIT_REFUSED
    assert "expect-revision-needs-item" in err
    assert _event_count(db) == before


def test_no_flag_behaviour_is_unchanged(monkeypatch, db: str) -> None:
    # Two blind writers both land, as before the flag existed. The
    # unflagged append/state/note paths are also covered by
    # test_cli.py::test_append_json_and_text, test_state_json_and_text and
    # test_note_json_and_text.
    for text in ("a", "b"):
        code, _out, err = invoke(monkeypatch, _note(db, text))
        assert code == 0, err
    assert _event_count(db) == 3
