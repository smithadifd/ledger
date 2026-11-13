"""``export-item --ticket`` prints an item's spec when its source names no file.

An item filed with a spec and a source that names no file (empty, neither a
locator nor a row key, or a row key with no valid ``also at`` note) prints its
current spec and version. A source that names a file reads, and refuses,
exactly as it always did: a spec never stands in for a broken locator. An item
with no spec keeps its named refusal.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from ledger.store import open_ledger
from ledger.verbs.spec import set_spec

from test_cli import invoke

ITEM = "it-1"
SHA = "0123456789abcdef0123456789abcdef01234567"
TICKET = "### Ticket: SL1 \u2014 A ticket\n- Prompt: one.\n\n"
SPEC_BODY = "Build the thing.\nThen test it.\n"
SPEC_TEXT = (
    "spec: v1\n"
    "title: Spec title\n"
    "size: S\n"
    "merge_class: MC-2\n"
    "\n"
    "## Body\n"
    "Build the thing.\nThen test it.\n"
    "\n"
    "## Acceptance\n"
    "It prints.\n"
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, text=True, capture_output=True,
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.md").write_text(TICKET + "## Later\nnot it\n", encoding="utf-8")
    (root / "plain.md").write_text("no ticket heading here\n", encoding="utf-8")
    (root / "roadmap.md").write_text("# Roadmap\n\n" + TICKET, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    monkeypatch.chdir(root)
    return root, _git(root, "rev-parse", "HEAD").strip()


@pytest.fixture()
def db(tmp_path: Path) -> str:
    return str(tmp_path / "ledger.db")


def _create(db: str, source: str | None, notes: tuple[str, ...] = ()) -> None:
    payload = {"type": "ticket", "title": "A ticket"}
    if source is not None:
        payload["source"] = source
    with open_ledger(db) as lg:
        lg.append(kind="item.created", payload=payload, item_id=ITEM,
                  idempotency_key="create", harness="pytest", actor="tester")
        for number, text in enumerate(notes):
            lg.append(kind="item.note", payload={"text": text}, item_id=ITEM,
                      idempotency_key=f"note-{number}", harness="pytest", actor="tester")


def _spec(db: str, **content) -> None:
    content = {"title": "Spec title", "body_md": SPEC_BODY, "acceptance": "It prints.",
               "size": "S", "merge_class": "MC-2", **content}
    with open_ledger(db) as lg:
        set_spec(lg, ITEM, content, harness="pytest", actor="tester")


def _view(monkeypatch, db: str, *flags: str):
    code, out, err = invoke(monkeypatch, [*flags, "--db", db, "export-item", "--ticket", ITEM])
    return code, out, err


def _json(monkeypatch, db: str):
    code, out, err = _view(monkeypatch, db, "--json")
    return code, (json.loads(out) if out.strip() else None), err


def _refused(monkeypatch, db: str) -> str:
    """The view failed cleanly with exit 1 (not_found); return its message."""
    code, payload, err = _json(monkeypatch, db)
    assert code == 1, (code, payload, err)
    assert "Traceback" not in err, err
    assert payload["ok"] is False and payload["error"]["code"] == "not_found", payload
    assert "spec:" not in json.dumps(payload) and "Build the thing" not in json.dumps(payload)
    return payload["error"]["message"]


# --- the three verbatim cases ------------------------------------------------


def test_spec_without_locator_prints_the_spec(db, monkeypatch, repo) -> None:
    _create(db, None)
    _spec(db)
    code, out, err = _view(monkeypatch, db)
    assert code == 0, err
    assert out.rstrip("\n") == SPEC_TEXT.rstrip("\n"), out
    code, payload, err = _json(monkeypatch, db)
    assert code == 0, err
    assert payload["data"] == {
        "item_id": ITEM,
        "ticket": SPEC_TEXT,
        "spec": {
            "version": 1,
            "title": "Spec title",
            "size": "S",
            "merge_class": "MC-2",
            "body_md": SPEC_BODY,
            "acceptance": "It prints.",
        },
    }


def test_the_current_spec_and_its_version_print_after_a_change(db, monkeypatch, repo) -> None:
    _create(db, None)
    _spec(db)
    _spec(db, title="Second title")
    code, payload, err = _json(monkeypatch, db)
    assert code == 0, err
    assert payload["data"]["spec"]["version"] == 2
    assert payload["data"]["ticket"].startswith("spec: v2\ntitle: Second title\n")


def test_unrecorded_spec_fields_are_left_out(db, monkeypatch, repo) -> None:
    _create(db, None)
    _spec(db, acceptance=None, size=None, merge_class=None)
    code, out, err = _view(monkeypatch, db)
    assert code == 0, err
    assert out.rstrip("\n") == (
        "spec: v1\ntitle: Spec title\n\n## Body\nBuild the thing.\nThen test it."
    )
    code, payload, _err = _json(monkeypatch, db)
    spec = payload["data"]["spec"]
    assert (spec["size"], spec["merge_class"], spec["acceptance"]) == (None, None, None)


def test_a_locator_item_is_unchanged_even_with_a_spec(db, monkeypatch, repo) -> None:
    _root, head = repo
    _create(db, f"a.md:1@{head}")
    _spec(db)
    code, payload, err = _json(monkeypatch, db)
    assert code == 0, err
    assert payload["data"] == {"item_id": ITEM, "ticket": TICKET}
    assert "spec" not in payload["data"]
    code, out, err = _view(monkeypatch, db)
    assert code == 0, err
    assert out.rstrip("\n") == TICKET.rstrip("\n")


def test_an_item_with_neither_keeps_its_named_refusal(db, monkeypatch, repo) -> None:
    _create(db, None)
    assert _refused(monkeypatch, db) == f"no source locator on {ITEM}"


def test_a_row_key_with_neither_keeps_its_named_refusal(db, monkeypatch, repo) -> None:
    _create(db, "row:SL1")
    message = _refused(monkeypatch, db)
    assert message.startswith("row_source_no_also_at: "), message


def test_a_parent_slug_source_with_neither_keeps_its_named_refusal(db, monkeypatch, repo) -> None:
    _create(db, "it-0:follow-up")
    assert _refused(monkeypatch, db) == "invalid source locator: it-0:follow-up"


# --- the spec prints only when the source names no file ----------------------


def test_a_row_key_with_no_also_at_prints_the_spec(db, monkeypatch, repo) -> None:
    _create(db, "row:SL1")
    _spec(db)
    code, out, err = _view(monkeypatch, db)
    assert code == 0, err
    assert out.rstrip("\n") == SPEC_TEXT.rstrip("\n")


def test_a_row_key_whose_only_also_at_notes_are_invalid_prints_the_spec(
    db, monkeypatch, repo
) -> None:
    _create(db, "row:SL1", notes=("also at ", "also at  a.md#SL1", "Also at a.md#SL1"))
    _spec(db)
    code, out, err = _view(monkeypatch, db)
    assert code == 0, err
    assert out.rstrip("\n") == SPEC_TEXT.rstrip("\n")


def test_a_parent_slug_source_prints_the_spec(db, monkeypatch, repo) -> None:
    _create(db, "it-0:follow-up")
    _spec(db)
    code, out, err = _view(monkeypatch, db)
    assert code == 0, err
    assert out.rstrip("\n") == SPEC_TEXT.rstrip("\n")


# --- a source that names a file never falls back to the spec -----------------


def test_a_missing_file_locator_with_a_spec_still_refuses(db, monkeypatch, repo) -> None:
    _create(db, f"gone.md:1@{SHA}")
    _spec(db)
    assert _refused(monkeypatch, db) == f"source not found: gone.md:1@{SHA}"


def test_a_locator_into_a_file_with_no_ticket_with_a_spec_still_refuses(
    db, monkeypatch, repo
) -> None:
    _root, head = repo
    _create(db, f"plain.md:1@{head}")
    _spec(db)
    assert _refused(monkeypatch, db) == "ticket section not found"


def test_a_dirty_locator_with_a_spec_still_refuses(db, monkeypatch, repo) -> None:
    _root, head = repo
    _create(db, f"a.md:1@{head}+dirty")
    _spec(db)
    assert _refused(monkeypatch, db).startswith("source recorded from uncommitted text")


def test_a_row_key_with_a_missing_anchor_and_a_spec_still_refuses(db, monkeypatch, repo) -> None:
    _create(db, "row:SL9", notes=("also at roadmap.md#SL9",))
    _spec(db)
    assert _refused(monkeypatch, db).startswith("row_source_anchor_not_found: ")


def test_a_row_key_with_a_missing_file_and_a_spec_still_refuses(db, monkeypatch, repo) -> None:
    _create(db, "row:SL1", notes=("also at gone.md#SL1",))
    _spec(db)
    assert _refused(monkeypatch, db) == "source not found: gone.md#SL1"


def test_a_row_key_with_a_valid_anchor_reads_it_not_the_spec(db, monkeypatch, repo) -> None:
    _create(db, "row:SL1", notes=("also at roadmap.md#SL1",))
    _spec(db)
    code, payload, err = _json(monkeypatch, db)
    assert code == 0, err
    assert payload["data"] == {"item_id": ITEM, "ticket": TICKET}


# --- a spec event that cannot be read refuses by name ------------------------


def test_an_unreadable_spec_refuses_by_name_and_prints_nothing(db, monkeypatch, repo) -> None:
    _create(db, None)
    conn = sqlite3.connect(db)
    try:
        # A row the writer would never produce: a spec with no title or body.
        conn.execute(
            "INSERT INTO events(kind, payload_version, payload, item_id, idempotency_key,"
            " recorded_at, prev_hash, hash) VALUES ('item.spec', 1, ?, ?, 'forged',"
            " '2026-01-01T00:00:00Z', '0', 'forged-hash')",
            (json.dumps({"version": 1, "content_hash": "0" * 64}), ITEM),
        )
        conn.commit()
    finally:
        conn.close()
    code, payload, err = _json(monkeypatch, db)
    assert code == 3, (code, payload, err)
    assert payload["ok"] is False and payload["error"]["code"] == "spec_unreadable", payload
    assert payload["error"]["message"].startswith("spec_unreadable: ")
    assert "ticket" not in json.dumps(payload.get("data") or {})


def _forge_spec(db: str, **override) -> None:
    """Insert a spec row the writer would never produce: one field is unreadable."""
    payload = {
        "version": 1, "content_hash": "0" * 64, "title": "Spec title",
        "body_md": SPEC_BODY, "acceptance": "It prints.", "size": "S",
        "merge_class": "MC-2", **override,
    }
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO events(kind, payload_version, payload, item_id, idempotency_key,"
            " recorded_at, prev_hash, hash) VALUES ('item.spec', 1, ?, ?, 'forged',"
            " '2026-01-01T00:00:00Z', '0', 'forged-hash')",
            (json.dumps(payload), ITEM),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "override",
    [
        {"version": 0},
        {"version": True},
        {"size": "XL"},
        {"merge_class": "MC-9"},
    ],
    ids=["version-below-one", "version-a-bool", "size-outside-set", "merge-class-outside-set"],
)
def test_a_spec_failing_only_one_readability_condition_refuses_by_name(
    db, monkeypatch, repo, override
) -> None:
    _create(db, None)
    _forge_spec(db, **override)
    code, out, err = _view(monkeypatch, db, "--json")
    payload = json.loads(out)
    assert code == 3, (code, payload, err)
    assert "Traceback" not in err, err
    assert payload["ok"] is False and payload["error"]["code"] == "spec_unreadable", payload
    assert payload["error"]["message"].startswith("spec_unreadable: ")
    assert "ticket" not in json.dumps(payload.get("data") or {})
    assert "Build the thing" not in out and "Spec title" not in out, out
    code, out, err = _view(monkeypatch, db)
    assert code == 3, (code, out, err)
    assert "Build the thing" not in out and "Spec title" not in out, out
