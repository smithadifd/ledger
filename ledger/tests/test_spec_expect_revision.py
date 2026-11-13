"""``spec set --expect-revision``: compare-and-set on the item's newest event.

The check is the one ``append``, ``state``, ``note`` and ``append-many`` run,
parsed by the same code and read inside ``set_spec``'s write transaction,
after the not-found check and before the unchanged compare. A stale value
exits 5 (``stale_revision``) and writes nothing, even when the content and
source are unchanged; ``none`` never matches, since every item has its
creation event. The two races a reader of the spec meets, a close landing
after its read and an import of the same content from another source, both
refuse instead of writing.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest

from ledger import ValidationError
from ledger.store import StaleRevision, open_ledger
from ledger.verbs import EXIT_NOT_FOUND, EXIT_STALE, EXIT_USAGE
from ledger.verbs.spec import set_spec

from test_cli import invoke, seed

ITEM = "it-1"
TITLE = "Spec title"
BODY = "Build the thing.\n"
OTHER_BODY = "Build the thing.\nThen test it.\n"


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "ledger.db")
    seed(monkeypatch, path, ITEM)
    return path


def cli_json(monkeypatch, db: str, *argv: str):
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, *argv])
    return code, (json.loads(out) if out.strip() else None), err


def spec_set(monkeypatch, db: str, body: str, *extra: str, item: str = ITEM):
    return cli_json(
        monkeypatch, db, "spec", "set", item, "--title", TITLE, "--body", body, *extra
    )


def revision(monkeypatch, db: str) -> int:
    code, body, err = cli_json(monkeypatch, db, "query", "--item", ITEM)
    assert code == 0, err
    return body["data"]["items"][0]["revision"]


def event_count(db: str) -> int:
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()


def spec_versions(db: str) -> list[tuple[int, str | None, str]]:
    with open_ledger(db, create=False) as lg:
        return [
            (e.payload["version"], e.payload.get("source"), e.payload["body_md"])
            for e in lg.events(item_id=ITEM, kind="item.spec")
        ]


def note(monkeypatch, db: str, text: str) -> None:
    code, _out, err = invoke(monkeypatch, ["--db", db, "note", ITEM, "--text", text])
    assert code == 0, err


def assert_stale(code: int, body: dict | None) -> None:
    assert code == EXIT_STALE, body
    assert body is not None and body["ok"] is False
    assert body["error"]["code"] == "stale_revision"


# (a) A stale revision exits 5 and the item's event count is unchanged.
def test_stale_revision_exits_5_and_writes_nothing(db, monkeypatch) -> None:
    seen = revision(monkeypatch, db)
    note(monkeypatch, db, "the item moved")
    before = event_count(db)
    code, body, _err = spec_set(monkeypatch, db, BODY, "--expect-revision", str(seen))
    assert_stale(code, body)
    assert f"expected {seen}, current is {seen + 1}" in body["error"]["message"]
    assert event_count(db) == before
    assert spec_versions(db) == []


# (b) The matching revision writes the next spec version.
def test_matching_revision_writes_the_next_version(db, monkeypatch) -> None:
    seen = revision(monkeypatch, db)
    code, v1, err = spec_set(monkeypatch, db, BODY, "--expect-revision", str(seen))
    assert code == 0, err
    assert v1["data"]["unchanged"] is False and v1["data"]["version"] == 1
    seen = revision(monkeypatch, db)
    assert seen == v1["data"]["event"]["id"]
    code, v2, err = spec_set(monkeypatch, db, OTHER_BODY, "--expect-revision", str(seen))
    assert code == 0, err
    assert v2["data"]["unchanged"] is False and v2["data"]["version"] == 2, v2
    assert v2["data"]["event"]["id"] == seen + 1
    assert [v for v, _s, _b in spec_versions(db)] == [1, 2]


# (c) Without the flag, today's behaviour holds: a moved item takes the spec.
def test_without_the_flag_a_moved_item_still_takes_the_spec(db, monkeypatch) -> None:
    code, v1, err = spec_set(monkeypatch, db, BODY, "--source", "A")
    assert code == 0, err
    note(monkeypatch, db, "the item moved")
    code, v2, err = spec_set(monkeypatch, db, OTHER_BODY, "--source", "A")
    assert code == 0, err
    assert v2["data"]["unchanged"] is False and v2["data"]["version"] == 2
    before = event_count(db)
    code, again, err = spec_set(monkeypatch, db, OTHER_BODY, "--source", "A")
    assert code == 0, err
    assert again["data"]["unchanged"] is True and again["data"]["version"] == 2
    assert event_count(db) == before


# (d) A stale revision over unchanged content and source exits 5, not the
# exit-0 no-op: the check runs before the unchanged compare.
@pytest.mark.parametrize("source", [(), ("--source", "A")])
def test_stale_revision_with_unchanged_content_is_not_a_no_op(
    db, monkeypatch, source
) -> None:
    code, _v1, err = spec_set(monkeypatch, db, BODY, "--source", "A")
    assert code == 0, err
    seen = revision(monkeypatch, db)
    note(monkeypatch, db, "the item moved")
    before = event_count(db)
    code, body, _err = spec_set(
        monkeypatch, db, BODY, *source, "--expect-revision", str(seen)
    )
    assert_stale(code, body)
    assert event_count(db) == before
    assert spec_versions(db) == [(1, "A", BODY)]


# (e) The matching revision with unchanged content is today's no-op.
def test_matching_revision_with_unchanged_content_is_the_no_op(db, monkeypatch) -> None:
    code, v1, err = spec_set(monkeypatch, db, BODY, "--source", "A")
    assert code == 0, err
    seen = revision(monkeypatch, db)
    before = event_count(db)
    code, again, err = spec_set(
        monkeypatch, db, BODY, "--source", "A", "--expect-revision", str(seen)
    )
    assert code == 0, err
    assert again["data"]["unchanged"] is True and again["data"]["version"] == 1
    assert again["data"]["event"]["id"] == v1["data"]["event"]["id"]
    assert event_count(db) == before


# (f) A malformed value is refused exactly as note refuses it.
@pytest.mark.parametrize(
    "bad",
    ["", "0", "-1", "abc", "1.5", "+2", " 2", "None", "NONE", "9" * 5000, str(2**63)],
)
def test_malformed_revision_is_refused_as_note_refuses_it(db, monkeypatch, bad) -> None:
    before = event_count(db)
    code, body, _err = spec_set(monkeypatch, db, BODY, "--expect-revision", bad)
    note_code, note_body, _err = cli_json(
        monkeypatch, db, "note", ITEM, "--text", "x", "--expect-revision", bad
    )
    assert code == note_code == EXIT_USAGE
    assert body["error"] == note_body["error"]
    assert body["error"]["code"] == "usage"
    assert event_count(db) == before


# (g) Close-after-read: a close lands after the reader took its revision.
def test_a_close_after_the_read_refuses_the_spec(db, monkeypatch) -> None:
    seen = revision(monkeypatch, db)
    code, _out, err = invoke(monkeypatch, ["--db", db, "state", ITEM, "--status", "closed"])
    assert code == 0, err
    before = event_count(db)
    code, body, _err = spec_set(
        monkeypatch, db, BODY, "--source", "file", "--expect-revision", str(seen)
    )
    assert_stale(code, body)
    assert event_count(db) == before
    assert spec_versions(db) == []


# (h) Cross-source import: the same content from another source lands after
# the read; the reader's write refuses, so exactly one new version exists.
def test_an_import_after_the_read_refuses_the_duplicate_version(db, monkeypatch) -> None:
    code, _v1, err = spec_set(monkeypatch, db, BODY, "--source", "roadmap")
    assert code == 0, err
    seen = revision(monkeypatch, db)
    code, imported, err = spec_set(monkeypatch, db, OTHER_BODY, "--source", "import")
    assert code == 0, err
    assert imported["data"]["version"] == 2
    before = event_count(db)
    code, body, _err = spec_set(
        monkeypatch, db, OTHER_BODY, "--source", "file", "--expect-revision", str(seen)
    )
    assert_stale(code, body)
    assert event_count(db) == before
    assert spec_versions(db) == [(1, "roadmap", BODY), (2, "import", OTHER_BODY)]


# The revision is read inside the write transaction: a writer landing after
# set_spec is called but before its transaction opens is still caught.
def test_a_write_before_the_transaction_opens_is_caught(db, monkeypatch) -> None:
    with open_ledger(db, create=False) as lg:
        seen = lg.events(item_id=ITEM)[-1].id
        real_transaction = lg.transaction

        @contextmanager
        def racing_transaction():
            with open_ledger(db, create=False) as other:
                other.append(
                    kind="item.state",
                    payload={"status": "closed"},
                    item_id=ITEM,
                    idempotency_key="race-close",
                )
            with real_transaction():
                yield

        monkeypatch.setattr(lg, "transaction", racing_transaction)
        with pytest.raises(StaleRevision):
            set_spec(lg, ITEM, {"title": TITLE, "body_md": BODY}, expect_revision=seen)
    assert spec_versions(db) == []


def test_none_never_matches_an_item(db, monkeypatch) -> None:
    before = event_count(db)
    code, body, _err = spec_set(monkeypatch, db, BODY, "--expect-revision", "none")
    assert_stale(code, body)
    assert "expected none, current is 1" in body["error"]["message"]
    assert event_count(db) == before


@pytest.mark.parametrize("value", ["1", "none"])
def test_a_missing_item_keeps_its_not_found_refusal(db, monkeypatch, value) -> None:
    before = event_count(db)
    code, body, _err = spec_set(
        monkeypatch, db, BODY, "--expect-revision", value, item="it-missing"
    )
    assert code == EXIT_NOT_FOUND, body
    assert body["error"]["code"] == "not_found"
    assert event_count(db) == before


@pytest.mark.parametrize("action", [("show",), ("diff",)])
def test_show_and_diff_refuse_the_flag(db, monkeypatch, action) -> None:
    code, _v1, err = spec_set(monkeypatch, db, BODY)
    assert code == 0, err
    code, body, _err = cli_json(
        monkeypatch, db, "spec", *action, ITEM, "--expect-revision", "1"
    )
    assert code == EXIT_USAGE, body
    assert body["error"]["message"] == f"spec {action[0]} does not take --expect-revision"


def test_library_stale_revision_raises_and_writes_nothing(db, monkeypatch) -> None:
    note(monkeypatch, db, "the item moved")
    before = event_count(db)
    with open_ledger(db, create=False) as lg:
        with pytest.raises(StaleRevision):
            set_spec(lg, ITEM, {"title": TITLE, "body_md": BODY}, expect_revision=1)
    assert event_count(db) == before


@pytest.mark.parametrize("bad", [True, 0, -1, "1", 1.0, "None", [1]])
def test_library_refuses_a_value_of_another_type(db, bad) -> None:
    before = event_count(db)
    with open_ledger(db, create=False) as lg:
        with pytest.raises(ValidationError, match="bad-type:expect_revision"):
            set_spec(lg, ITEM, {"title": TITLE, "body_md": BODY}, expect_revision=bad)
    assert event_count(db) == before
