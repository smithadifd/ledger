"""Resume cards: revision, types, truncation, JSON, and --md path guard."""
from __future__ import annotations

import dataclasses
import io
import json
import os
import random
import re
import shlex
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ledger.card import (
    Card,
    _blocker_source_ids,
    _bound_ids,
    _key_step_ids,
    DROP_ORDER,
    LINK_LIMIT,
    WORD_LIMIT,
    footer_line,
    format_card,
    item_spec_moved,
    project_card,
    project_item,
    word_count,
)
from ledger.cli import main
from ledger.schema import ITEM_TYPES
from ledger.store import open_ledger
from ledger.verbs import EXIT_NOT_FOUND, EXIT_REFUSED, discover_verbs
from ledger.verbs.spec import set_spec

PAD = " ".join(["alpha"] * 40)


def invoke(monkeypatch, argv, *, env=None):
    merged = {
        "LEDGER_HARNESS": "pytest",
        "LEDGER_AGENT": "tester",
        "LEDGER_MODEL": "test-model",
        "LEDGER_SESSION": "sess-1",
    }
    if env:
        merged.update(env)
    for key, value in merged.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _db(tmp_path: Path) -> Path:
    return tmp_path / "ledger.db"


def _append(lg, kind, payload, item_id, key):
    return lg.append(
        kind=kind,
        payload=payload,
        item_id=item_id,
        idempotency_key=key,
        harness="pytest",
    )


def _create(lg, item_id, item_type, title="synthetic-item"):
    return _append(
        lg,
        "item.created",
        {"type": item_type, "title": title},
        item_id,
        f"c-{item_id}",
    )


def test_card_verb_is_discovered() -> None:
    assert "card" in discover_verbs()


def test_card_from_revision_n_names_n(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    created = _create(lg, "it-1", "ticket", "synthetic-one")
    card = project_card(lg, "it-1")
    text = format_card(card)
    assert card.revision_id == created.id
    assert card.revision_hash == created.hash
    assert f"revision: {created.id} {created.hash}" in text
    note = _append(
        lg, "item.note", {"text": "synthetic note"}, "it-1", "n-1"
    )
    card = project_card(lg, "it-1")
    text = format_card(card)
    assert card.revision_id == note.id
    assert f"revision: {note.id} {note.hash}" in text
    other = _create(lg, "it-2", "topic", "synthetic-two")
    card = project_card(lg, "it-1")
    text = format_card(card)
    assert card.revision_id == other.id
    assert f"revision: {other.id} {other.hash}" in text
    lg.close()


@pytest.mark.parametrize("item_type", ITEM_TYPES)
def test_all_item_types_render(tmp_path: Path, item_type: str) -> None:
    lg = open_ledger(_db(tmp_path))
    item_id = f"it-{item_type}"
    _create(lg, item_id, item_type, f"synthetic-{item_type}")
    card = project_card(lg, item_id)
    text = format_card(card)
    assert card.type == item_type
    assert f"type: {item_type}" in text
    assert f"title: synthetic-{item_type}" in text
    assert f"# {item_id}" in text
    lg.close()


def _bulky_item(lg, item_id="it-1"):
    _create(lg, item_id, "ticket", "synthetic-one")
    _append(
        lg,
        "item.state",
        {"status": "claimed", "owner": "tester", "claim": "tester"},
        item_id,
        "s-1",
    )
    _append(
        lg,
        "decision.asked",
        {
            "question": "pick a colour?",
            "options": [{"label": "red"}, {"label": "blue"}],
            "recommendation": "blue",
            "default": "red",
        },
        item_id,
        "ask-1",
    )
    elected = _append(
        lg,
        "decision.elected",
        {
            "question": "pick a colour?",
            "chosen": "blue",
            "chosen_description": "cool",
            "rejected": ["red"],
            "scope": "palette",
            "quote_ref": 1,
        },
        item_id,
        "elect-1",
    )
    asked = _append(
        lg,
        "decision.asked",
        {
            "question": "ship the change?",
            "options": [{"label": "yes"}, {"label": "no"}],
        },
        item_id,
        "ask-2",
    )
    for index in range(8):
        _append(
            lg,
            "item.linked",
            {"rel": "url", "target": f"https://example.invalid/n/{index}/{PAD}"},
            item_id,
            f"l-{index}",
        )
    for index in range(12):
        _append(
            lg,
            "item.note",
            {"text": f"note-{index} {PAD}"},
            item_id,
            f"n-{index}",
        )
    return elected, asked


def test_truncation_keeps_decisions_and_next_step(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    elected, asked = _bulky_item(lg)
    card = project_card(lg, "it-1")
    text = format_card(card)
    assert "## Decisions" in text
    assert f"event={elected.id} quote_ref=1:" in text
    assert "pick a colour? -> blue" in text
    assert "## Open questions" in text
    assert f"event={asked.id}: ship the change?" in text
    assert "## Next step" in text
    assert card.next_step in text
    assert card.next_step == "Answer open questions before continuing."
    assert DROP_ORDER == ("links", "notes")
    lg.close()


def test_truncated_card_fits_word_limit(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _bulky_item(lg)
    card = project_card(lg, "it-1")
    text = format_card(card)
    assert word_count(text) <= 400
    assert word_count(text) <= WORD_LIMIT
    assert "links" in card.truncated
    assert "notes" in card.truncated
    assert "## Truncated" in text
    assert "dropped: links, notes" in text
    assert "https://example.invalid" not in text
    assert "note-0 " not in text
    assert "## Notes\n(+12 cut; ledger export-item --json -- it-1)\n" in text
    assert "ledger query" not in text
    lg.close()


def test_decisions_include_elected_id_and_quote_ref(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "it-1", "ticket")
    elected = _append(
        lg,
        "decision.elected",
        {
            "question": "pick a colour?",
            "chosen": "blue",
            "chosen_description": "cool",
            "rejected": ["red"],
            "scope": "palette",
            "quote_ref": 1,
        },
        "it-1",
        "elect-1",
    )
    card = project_card(lg, "it-1")
    text = format_card(card)
    assert card.decisions[0]["event_id"] == elected.id
    assert card.decisions[0]["quote_ref"] == 1
    assert f"event={elected.id} quote_ref=1: pick a colour? -> blue" in text
    body = card.as_dict()
    assert body["decisions"][0]["event_id"] == elected.id
    assert body["decisions"][0]["quote_ref"] == 1
    lg.close()


def test_footer_is_projection_not_authority(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "it-1", "ticket")
    text = format_card(project_card(lg, "it-1"))
    assert text.rstrip().endswith(footer_line("it-1"))
    assert "This card is a projection; the events are the authority" in text
    assert "(ledger export-item -- it-1)" in text
    lg.close()


def test_link_cap_drops_the_oldest_link_across_own_and_inbound(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "it-kept", "ticket")
    _create(lg, "it-dup", "ticket")
    inbound = _append(
        lg, "item.linked", {"rel": "duplicate-of", "target": "it-kept"}, "it-dup", "dup-link"
    )
    own = [
        _append(
            lg, "item.linked", {"rel": "pr", "target": f"org/repo#{n}"}, "it-kept", f"pr-{n}"
        ).id
        for n in range(1, 46)
    ]
    card = project_card(lg, "it-kept")
    kept = [row["event_id"] for row in card.links]
    assert inbound.id not in kept
    assert 0 < len(kept) < len(own)
    assert kept == own[-len(kept):]
    lg.close()


def test_source_event_ids_in_header(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    created = _create(lg, "it-1", "ticket")
    state = _append(
        lg, "item.state", {"status": "open"}, "it-1", "s-1"
    )
    card = project_card(lg, "it-1")
    text = format_card(card)
    assert created.id in card.source_event_ids
    assert state.id in card.source_event_ids
    assert f"source_event_ids: {created.id}, {state.id}" in text
    lg.close()


def test_section_order_is_stable(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "it-1", "ticket")
    text = format_card(project_card(lg, "it-1"))
    headings = [
        "## Decisions",
        "## Open questions",
        "## Next step",
        "## Links",
        "## Blockers",
        "## Notes",
    ]
    positions = [text.index(name) for name in headings]
    assert positions == sorted(positions)
    lg.close()


def test_status_decisions_and_next_step_readable(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _create(lg, "it-1", "ticket", "synthetic-one")
    _append(
        lg,
        "item.state",
        {"status": "claimed", "owner": "tester", "claim": "tester"},
        "it-1",
        "s-1",
    )
    _append(
        lg,
        "decision.elected",
        {
            "question": "pick a colour?",
            "chosen": "blue",
            "chosen_description": "cool",
            "rejected": ["red"],
            "scope": "palette",
            "quote_ref": 1,
        },
        "it-1",
        "elect-1",
    )
    lg.close()
    code, out, err = invoke(monkeypatch, ["--db", db, "card", "it-1"])
    assert code == 0, err
    assert "status: claimed" in out
    assert "quote_ref=1" in out
    assert "Continue claimed work (tester)." in out
    assert word_count(out) <= 400


def test_json_structured_same_content(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _create(lg, "it-1", "ticket", "synthetic-one")
    _append(
        lg,
        "item.state",
        {"status": "claimed", "owner": "tester", "claim": "tester"},
        "it-1",
        "s-1",
    )
    elected = _append(
        lg,
        "decision.elected",
        {
            "question": "pick a colour?",
            "chosen": "blue",
            "chosen_description": "cool",
            "rejected": ["red"],
            "scope": "palette",
            "quote_ref": 1,
        },
        "it-1",
        "elect-1",
    )
    lg.close()
    code, text_out, err = invoke(monkeypatch, ["--db", db, "card", "it-1"])
    assert code == 0, err
    code, json_out, err = invoke(
        monkeypatch, ["--json", "--db", db, "card", "it-1"]
    )
    assert code == 0, err
    payload = json.loads(json_out)
    data = payload["data"]
    assert payload["ok"] is True
    assert data["item_id"] == "it-1"
    assert data["type"] == "ticket"
    assert data["status"] == "claimed"
    assert data["next_step"] == "Continue claimed work (tester)."
    assert data["revision"]["id"] >= elected.id
    assert data["revision"]["hash"]
    assert data["decisions"][0]["event_id"] == elected.id
    assert data["decisions"][0]["quote_ref"] == 1
    assert data["footer"] == footer_line(data["item_id"], f"ledger --db {db}")
    assert f"revision: {data['revision']['id']} {data['revision']['hash']}" in text_out
    assert data["next_step"] in text_out
    assert f"event={elected.id} quote_ref=1" in text_out
    assert data["status"] in text_out


def test_md_writes_human_copy(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _create(lg, "it-1", "ticket", "synthetic-one")
    lg.close()
    dest = tmp_path / "copy.md"
    code, out, err = invoke(
        monkeypatch, ["--db", db, "card", "it-1", "--md", str(dest)]
    )
    assert code == 0, err
    assert dest.exists()
    written = dest.read_text(encoding="utf-8")
    assert written == out
    assert footer_line("it-1", f"ledger --db {db}") in written


def test_md_refuses_synced_root(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _create(lg, "it-1", "ticket", "synthetic-one")
    lg.close()
    dest = tmp_path / "Dropbox" / "copy.md"
    code, out, err = invoke(
        monkeypatch, ["--db", db, "card", "it-1", "--md", str(dest)]
    )
    assert code == EXIT_REFUSED
    assert "Dropbox" in err
    assert not dest.exists()
    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", db, "card", "it-1", "--md", str(dest)],
    )
    assert code == EXIT_REFUSED
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "synced_path"


def test_missing_item_not_found(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _create(lg, "it-1", "ticket")
    lg.close()
    code, _out, err = invoke(monkeypatch, ["--db", db, "card", "no-such"])
    assert code == EXIT_NOT_FOUND
    assert "not found" in err


def _elect(lg, item_id: str, index: int, key: str, *, pad: str = ""):
    question = f"choose-{index:02d}?"
    if pad:
        question = f"{question} {pad}"
    return _append(
        lg,
        "decision.elected",
        {
            "question": question,
            "chosen": f"yes-{index:02d}",
            "chosen_description": "synthetic",
            "rejected": ["no"],
            "scope": "probe",
            "quote_ref": 1,
        },
        item_id,
        key,
    )


def test_decision_heavy_card_fits_and_keeps_latest(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "it-1", "ticket", "synthetic-one")
    _append(
        lg,
        "item.state",
        {"status": "claimed", "owner": "tester", "claim": "tester"},
        "it-1",
        "s-1",
    )
    last = None
    for index in range(30):
        last = _elect(lg, "it-1", index, f"e-{index}", pad=PAD)
    for index in range(200):
        _append(
            lg,
            "item.linked",
            {"rel": "url", "target": f"https://example.invalid/n/{index}/{PAD}"},
            "it-1",
            f"l-{index}",
        )
    for index in range(200):
        _append(
            lg,
            "item.note",
            {"text": f"note-{index} {PAD}"},
            "it-1",
            f"n-{index}",
        )
    card = project_card(lg, "it-1")
    text = format_card(card)
    assert last is not None
    assert word_count(text) <= WORD_LIMIT
    assert f"event={last.id} quote_ref=1:" in text
    assert "choose-29?" in text
    assert "choose-00?" not in text
    assert "## Next step" in text
    assert card.next_step in text
    assert "Continue claimed work (tester)." in text
    assert f"revision: {card.revision_id} {card.revision_hash}" in text
    assert "status: claimed" in text
    assert "decisions" in card.truncated
    assert "links" in card.truncated
    assert "notes" in card.truncated
    assert "(+" in text
    assert re.search(r"^\(\+\d+ cut; ledger export-item --json -- \S+\)$", text, re.M)
    assert "ledger query" not in text
    lg.close()


def test_blocker_heavy_card_fits(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "it-1", "ticket", "synthetic-one")
    blockers = [f"b{index:03d}" for index in range(200)]
    _append(
        lg,
        "item.state",
        {"status": "blocked", "blocked_by": blockers},
        "it-1",
        "s-1",
    )
    card = project_card(lg, "it-1")
    text = format_card(card)
    assert word_count(text) <= WORD_LIMIT
    assert "blockers" in card.truncated
    assert "## Next step" in text
    assert card.next_step.startswith("Clear blockers:")
    assert card.next_step in text
    assert "status: blocked" in text
    assert f"revision: {card.revision_id}" in text
    assert re.search(r"^\(\+\d+ cut; ledger export-item --json -- \S+\)$", text, re.M)
    assert "ledger query" not in text
    lg.close()


def test_open_question_heavy_card_fits_and_keeps_latest(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "it-1", "ticket", "synthetic-one")
    last = None
    for index in range(30):
        last = _append(
            lg,
            "decision.asked",
            {
                "question": f"question-{index:02d}? {PAD}",
                "options": [{"label": "yes"}, {"label": "no"}],
            },
            "it-1",
            f"ask-{index}",
        )
    card = project_card(lg, "it-1")
    text = format_card(card)
    assert last is not None
    assert word_count(text) <= WORD_LIMIT
    assert "open_questions" in card.truncated
    assert f"event={last.id}: question-29?" in text
    assert "question-00?" not in text
    assert "## Next step" in text
    assert card.next_step == "Answer open questions before continuing."
    assert card.next_step in text
    assert re.search(r"^\(\+\d+ cut; ledger export-item --json -- \S+\)$", text, re.M)
    assert "ledger query" not in text
    lg.close()


def test_empty_blocked_by_clears_blockers(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "it-1", "ticket", "synthetic-one")
    _append(
        lg,
        "item.state",
        {"status": "blocked", "blocked_by": ["X"]},
        "it-1",
        "s-1",
    )
    card = project_card(lg, "it-1")
    assert card.blockers == ("X",)
    assert card.next_step == "Clear blockers: X."
    _append(
        lg,
        "item.state",
        {"status": "open", "blocked_by": []},
        "it-1",
        "s-2",
    )
    card = project_card(lg, "it-1")
    text = format_card(card)
    assert card.status == "open"
    assert card.blockers == ()
    assert "Clear blockers" not in card.next_step
    assert "Clear blockers" not in text
    assert card.next_step == "Resume with: ledger resume --item=it-1"
    lg.close()


def test_absent_blocked_by_keeps_previous_set(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "it-1", "ticket", "synthetic-one")
    _append(
        lg,
        "item.state",
        {"status": "blocked", "blocked_by": ["X", "Y"]},
        "it-1",
        "s-1",
    )
    _append(
        lg,
        "item.state",
        {"status": "blocked"},
        "it-1",
        "s-2",
    )
    card = project_card(lg, "it-1")
    text = format_card(card)
    assert card.blockers == ("X", "Y")
    assert card.next_step == "Clear blockers: X, Y."
    assert "- X" in text
    assert "- Y" in text
    lg.close()

def test_reopened_item_matches_ready_after_blocker_closes(
    tmp_path: Path, monkeypatch
) -> None:
    db = str(_db(tmp_path))
    steps = [
        ["new", "--type", "ticket", "--title", "x", "--id", "X"],
        ["new", "--type", "ticket", "--title", "c", "--id", "C"],
        ["state", "C", "--status", "blocked", "--blocked-by", "X"],
        ["state", "X", "--status", "closed"],
        ["state", "C", "--status", "open"],
    ]
    for argv in steps:
        code, out, err = invoke(monkeypatch, ["--db", db, *argv])
        assert code == 0, err
    code, ready_out, err = invoke(monkeypatch, ["--db", db, "ready"])
    assert code == 0, err
    assert "C" in ready_out
    code, card_out, err = invoke(monkeypatch, ["--db", db, "card", "C"])
    assert code == 0, err
    assert "## Blockers\n(none)" in card_out
    assert "Clear blockers" not in card_out
    assert f"Resume with: ledger --db {db} resume --item=C" in card_out


def test_reopen_clears_blockers_while_blocker_still_open(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "X", "ticket", "x")
    _create(lg, "C", "ticket", "c")
    _append(
        lg,
        "item.state",
        {"status": "blocked", "blocked_by": ["X"]},
        "C",
        "s-1",
    )
    card = project_card(lg, "C")
    assert card.blockers == ("X",)
    assert card.next_step == "Clear blockers: X."
    _append(lg, "item.state", {"status": "open"}, "C", "s-2")
    card = project_card(lg, "C")
    assert card.status == "open"
    assert card.blockers == ()
    assert "Clear blockers" not in card.next_step
    assert card.next_step == "Resume with: ledger resume --item=C"
    lg.close()


def test_closed_blocker_not_listed_while_item_still_blocked(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "X", "ticket", "x")
    _create(lg, "C", "ticket", "c")
    _append(
        lg,
        "item.state",
        {"status": "blocked", "blocked_by": ["X"]},
        "C",
        "s-1",
    )
    _append(lg, "item.state", {"status": "closed"}, "X", "s-2")
    card = project_card(lg, "C")
    text = format_card(card)
    assert card.status == "blocked"
    assert card.blockers == ()
    assert "## Blockers\n(none)" in text
    assert "- X" not in text
    lg.close()


def test_open_blocker_on_blocked_item_stays_listed(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "X", "ticket", "x")
    _create(lg, "C", "ticket", "c")
    _append(
        lg,
        "item.state",
        {"status": "blocked", "blocked_by": ["X"]},
        "C",
        "s-1",
    )
    card = project_card(lg, "C")
    text = format_card(card)
    assert card.blockers == ("X",)
    assert card.next_step == "Clear blockers: X."
    assert "- X" in text
    lg.close()


def test_md_overwrites_existing_via_replace(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _create(lg, "it-1", "ticket", "synthetic-one")
    lg.close()
    dest = tmp_path / "copy.md"
    dest.write_text("stale\n", encoding="utf-8")
    replaced: list[tuple[Path, Path]] = []
    real_replace = os.replace

    def spy(src, dst):
        replaced.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    code, out, err = invoke(
        monkeypatch, ["--db", db, "card", "it-1", "--md", str(dest)]
    )
    assert code == 0, err
    written = dest.read_text(encoding="utf-8")
    assert written == out
    assert written != "stale\n"
    assert footer_line("it-1", f"ledger --db {db}") in written
    assert replaced
    src, dst = replaced[0]
    assert dst.resolve() == dest.resolve()
    assert src.parent.resolve() == dest.parent.resolve()


def test_render_is_byte_identical_at_same_revision(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "it-1", "ticket", "synthetic-one")
    first = format_card(project_card(lg, "it-1"))
    second = format_card(project_card(lg, "it-1"))
    assert first == second
    assert first.encode("utf-8") == second.encode("utf-8")
    note = _append(lg, "item.note", {"text": "later"}, "it-1", "n-later")
    after = format_card(project_card(lg, "it-1"))
    assert after != first
    before_rev = next(
        line for line in first.splitlines() if line.startswith("revision: ")
    )
    after_rev = next(
        line for line in after.splitlines() if line.startswith("revision: ")
    )
    assert before_rev != after_rev
    assert str(note.id) in after_rev
    lg.close()


def test_blocks_link_direction_matches_ready(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _create(lg, "blk-a", "ticket", "synthetic-blocker")
    _create(lg, "blk-b", "ticket", "synthetic-blocked")
    _append(
        lg,
        "item.linked",
        {"rel": "blocks", "target": "blk-b"},
        "blk-a",
        "l-1",
    )
    lg.close()
    code, ready_out, err = invoke(monkeypatch, ["--db", db, "ready"])
    assert code == 0, err
    assert "blk-a" in ready_out
    assert "blk-b" not in ready_out
    code, out_a, err = invoke(monkeypatch, ["--db", db, "card", "blk-a"])
    assert code == 0, err
    assert "## Blockers\n(none)" in out_a
    assert "Clear blockers" not in out_a
    assert f"Resume with: ledger --db {db} resume --item=blk-a" in out_a
    code, out_b, err = invoke(monkeypatch, ["--db", db, "card", "blk-b"])
    assert code == 0, err
    assert "## Blockers\n- blk-a" in out_b
    assert "Clear blockers: blk-a." in out_b
    assert "Resume with:" not in out_b
    lg = open_ledger(db)
    _append(lg, "item.state", {"status": "closed"}, "blk-a", "s-a")
    lg.close()
    code, ready_out, err = invoke(monkeypatch, ["--db", db, "ready"])
    assert code == 0, err
    assert "blk-b" in ready_out
    code, out_b, err = invoke(monkeypatch, ["--db", db, "card", "blk-b"])
    assert code == 0, err
    assert "## Blockers\n(none)" in out_b
    assert "Clear blockers" not in out_b
    assert f"Resume with: ledger --db {db} resume --item=blk-b" in out_b


def test_blocker_flood_next_step_bounded(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "it-1", "ticket", "synthetic-one")
    blockers = [f"b{index:03d}" for index in range(600)]
    _append(
        lg,
        "item.state",
        {"status": "blocked", "blocked_by": blockers},
        "it-1",
        "s-1",
    )
    card = project_card(lg, "it-1")
    text = format_card(card)
    assert word_count(text) <= WORD_LIMIT
    # The step names the first five blockers of the full set, then a count;
    # it does not follow the capped Blockers section.
    assert card.next_step == "Clear blockers: b000, b001, b002, b003, b004 (+595 cut; ledger export-item --json -- it-1)."
    assert "b599" not in card.next_step
    assert "## Blockers\n- b000\n" in text
    assert re.search(r"^\(\+\d+ cut; ledger export-item --json -- \S+\)$", text, re.M)
    data = card.as_dict()
    assert data["next_step"] == card.next_step
    assert "b599" not in data["next_step"]
    lg.close()


def test_title_flood_bounded(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    title = " ".join(f"w{index:03d}" for index in range(600))
    _create(lg, "it-1", "ticket", title)
    card = project_card(lg, "it-1")
    text = format_card(card)
    assert word_count(text) <= WORD_LIMIT
    assert "title" in card.truncated
    assert "w000" in card.title
    assert "w599" not in card.title
    assert "w599" not in text
    assert re.search(
        r"^title: w000 .* \(\+\d+ words cut; ledger export-item --json -- \S+\)$", text, re.M
    )
    assert "ledger query" not in text
    data = card.as_dict()
    assert data["title"] == card.title
    assert "w599" not in data["title"]
    lg.close()


# --- Decision cards show the ask --------------------------------------------

def _decision(lg, item_id="dc-1", *, status=None):
    payload = {"type": "decision", "title": "synthetic-decision"}
    if status:
        payload["status"] = status
    return _append(lg, "item.created", payload, item_id, f"c-{item_id}")


def _full_ask(lg, item_id="dc-1", *, ask="Which synthetic lane should the probe use?"):
    return _append(
        lg,
        "decision.asked",
        {
            "question": "pick the probe lane?",
            "ask": ask,
            "options": [
                {"label": "A", "description": "fast synthetic lane"},
                {"label": "B", "description": "slow synthetic lane"},
            ],
            "recommendation": "A — fast synthetic lane",
            "default": "B — slow synthetic lane",
            "effort": "two minutes",
            "added": "2000-01-01",
            "unblocks": "synthetic follow-up",
            "ask_kind": "option-pick",
            "question_id": "probe-lane",
        },
        item_id,
        f"ask-{item_id}",
    )


def _colour_election(lg, item_id, key="elect-1", chosen="blue"):
    return _append(
        lg,
        "decision.elected",
        {
            "question": "pick a colour?",
            "chosen": chosen,
            "chosen_description": "cool",
            "rejected": ["red"],
            "scope": "palette",
            "quote_ref": 1,
        },
        item_id,
        key,
    )


def _words(prefix: str, count: int) -> str:
    return " ".join(f"{prefix}{index:03d}" for index in range(count))


def _ask_text_line(text: str) -> str:
    line = next(
        (line for line in text.splitlines() if line.startswith("  ask: ")),
        None,
    )
    assert line is not None, "missing ask line"
    return line


def test_decision_card_shows_every_ask_field(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _decision(lg)
    asked = _full_ask(lg)
    _decision(lg, "dc-2")
    _append(
        lg,
        "decision.asked",
        {
            "question": "pick a colour?",
            "options": [{"label": "red"}, {"label": "blue", "description": "cool"}],
            "default": "red",
        },
        "dc-2",
        "ask-dc-2",
    )
    lg.close()
    code, out, err = invoke(monkeypatch, ["--db", db, "card", "dc-1"])
    assert code == 0, err
    section = out.split("## Open questions\n", 1)[1].split("\n\n## Next step", 1)[0]
    assert section.splitlines() == [
        f"- event={asked.id}: pick the probe lane?",
        "  ask: Which synthetic lane should the probe use?",
        "  options:",
        "    A) fast synthetic lane",
        "    B) slow synthetic lane",
        "  recommendation: A — fast synthetic lane",
        "  default: B — slow synthetic lane",
        "  effort: two minutes",
        "  added: 2000-01-01",
        "  unblocks: synthetic follow-up",
    ]
    assert "Answer open questions before continuing." in out
    assert word_count(out) <= WORD_LIMIT

    code, json_out, err = invoke(
        monkeypatch, ["--json", "--db", db, "card", "dc-1"]
    )
    assert code == 0, err
    row = json.loads(json_out)["data"]["open_questions"][0]
    assert row["asked_event_id"] == asked.id
    assert row["question"] == "pick the probe lane?"
    assert row["ask"] == "Which synthetic lane should the probe use?"
    assert row["options"] == [
        {"label": "A", "description": "fast synthetic lane"},
        {"label": "B", "description": "slow synthetic lane"},
    ]
    assert row["recommendation"] == "A — fast synthetic lane"
    assert row["default"] == "B — slow synthetic lane"
    assert row["effort"] == "two minutes"
    assert row["added"] == "2000-01-01"
    assert row["unblocks"] == "synthetic follow-up"
    assert row["default_only"] is False
    assert "omitted_words" not in row

    # Free-text labels get letters assigned by position; absent fields stay out.
    code, out, err = invoke(monkeypatch, ["--db", db, "card", "dc-2"])
    assert code == 0, err
    assert "    A) red\n    B) blue — cool\n  default: red\n" in out
    for absent in ("  ask:", "  recommendation:", "  effort:", "  added:", "  unblocks:"):
        assert absent not in out


def test_single_default_option_shows_default_once(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _decision(lg)
    default = "keep the synthetic probe as it is"
    _append(
        lg,
        "decision.asked",
        {
            "question": "change the probe?",
            "ask": "Should the synthetic probe change?",
            "options": [{"label": default}],
            "default": default,
            "recommendation": "change it",
        },
        "dc-1",
        "ask-1",
    )
    lg.close()
    code, out, err = invoke(monkeypatch, ["--db", db, "card", "dc-1"])
    assert code == 0, err
    assert out.count(default) == 1
    assert f"  default: {default}" in out
    assert "  options:" not in out
    assert "  recommendation: change it" in out
    code, json_out, err = invoke(
        monkeypatch, ["--json", "--db", db, "card", "dc-1"]
    )
    assert code == 0, err
    row = json.loads(json_out)["data"]["open_questions"][0]
    assert row["options"] == [{"label": default, "description": ""}]
    assert row["default"] == default
    assert row["default_only"] is True


@pytest.mark.parametrize(
    "option, default, expected",
    [
        # A single option that is not the default is still listed.
        ({"label": "go ahead"}, "wait", "    A) go ahead"),
        # A single option equal to the default but with a description keeps
        # the description visible.
        (
            {"label": "wait", "description": "until the probe settles"},
            "wait",
            "    A) wait — until the probe settles",
        ),
    ],
)
def test_single_option_unlike_default_is_listed(
    tmp_path: Path, option, default, expected
) -> None:
    lg = open_ledger(_db(tmp_path))
    _decision(lg)
    _append(
        lg,
        "decision.asked",
        {"question": "proceed?", "options": [option], "default": default},
        "dc-1",
        "ask-1",
    )
    text = format_card(project_card(lg, "dc-1"))
    assert "  options:\n" + expected + "\n" in text
    assert f"  default: {default}" in text
    lg.close()


def test_long_ask_is_shortened_first(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _decision(lg)
    elected = _colour_election(lg, "dc-1")
    asked = _full_ask(lg, ask=_words("a", 600))
    lg.close()
    reader = open_ledger(db)
    try:
        card = project_card(reader, "dc-1")
    finally:
        reader.close()
    # Pin the flake: a599 in the revision hash must not fail the ask-line check.
    card = dataclasses.replace(
        card,
        revision_hash="deadbeefa599cafe0123456789abcdef0123456789abcdef0123456789ab",
    )
    text = format_card(card)
    assert "a599" in text
    # Exact: the cut pays for its own marker and the Truncated line.
    assert word_count(text) == WORD_LIMIT
    # The ask is shortened, not dropped, and nothing else was capped.
    assert card.truncated == ("ask",)
    assert f"event={elected.id} quote_ref=1: pick a colour? -> blue" in text
    assert card.next_step == "Answer open questions before continuing."
    assert "## Next step\nAnswer open questions before continuing.\n" in text
    assert f"- event={asked.id}: pick the probe lane?" in text
    row = card.open_questions[0]
    cut = row["omitted_words"]["ask"]
    assert cut > 0
    assert len(row["ask"].split()) + cut == 600
    assert _ask_text_line(text) == (
        f"  ask: {row['ask']} … (+{cut} words cut; full text in event {asked.id}; "
        "ledger export-item --json -- dc-1)"
    )
    assert row["ask"].startswith("a000 a001")
    assert "a599" not in _ask_text_line(text)
    for line in (
        "    A) fast synthetic lane",
        "    B) slow synthetic lane",
        "  recommendation: A — fast synthetic lane",
        "  default: B — slow synthetic lane",
        "  effort: two minutes",
        "  unblocks: synthetic follow-up",
    ):
        assert line in text
    assert "dropped: ask" in text
    assert asked.id in card.source_event_ids
    assert elected.id in card.source_event_ids

    code, json_out, err = invoke(
        monkeypatch, ["--json", "--db", db, "card", "dc-1"]
    )
    assert code == 0, err
    data = json.loads(json_out)["data"]
    assert data["decisions"][0]["event_id"] == elected.id
    assert data["next_step"] == "Answer open questions before continuing."
    assert data["truncated"] == ["ask"]
    cli_row = data["open_questions"][0]
    cli_cut = cli_row["omitted_words"]["ask"]
    assert len(cli_row["ask"].split()) + cli_cut == 600
    assert cli_cut >= cut
    # The JSON names the ask marker's count, command and event, as printed.
    assert data["cuts"] == [
        {
            "section": "ask",
            "count": cli_cut,
            "unit": "words",
            "command": f"ledger --db {shlex.quote(db)} export-item --json -- dc-1",
            "event_id": asked.id,
        }
    ]
    assert "a599" not in data["open_questions"][0]["ask"]


def test_ask_text_line_fails_by_name_when_missing() -> None:
    try:
        _ask_text_line("# dc-1\nrevision: 1 abc\n")
    except AssertionError as err:
        assert "missing ask line" in str(err)
    else:
        raise AssertionError("expected missing ask line")


def test_huge_option_description_is_shortened(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _decision(lg)
    asked = _append(
        lg,
        "decision.asked",
        {
            "question": "pick the probe lane?",
            "options": [
                {"label": "A", "description": _words("d", 500)},
                {"label": "B", "description": "short"},
            ],
            "recommendation": "A — long lane",
            "default": "B — short",
        },
        "dc-1",
        "ask-1",
    )
    lg.close()
    reader = open_ledger(db)
    try:
        card = project_card(reader, "dc-1")
    finally:
        reader.close()
    text = format_card(card)
    assert word_count(text) <= WORD_LIMIT
    assert card.truncated == ("ask",)
    assert len(card.open_questions) == 1
    row = card.open_questions[0]
    description = row["options"][0]["description"]
    cut = row["omitted_words"]["options.0.description"]
    assert description.startswith("d000 d001")
    assert len(description.split()) + cut == 500
    assert f"- event={asked.id}: pick the probe lane?" in text
    assert (
        f"    A) {description} … (+{cut} words cut; full text in event {asked.id}; "
        "ledger export-item --json -- dc-1)\n"
        in text
    )
    assert "    B) short\n" in text
    assert "  recommendation: A — long lane" in text
    assert "  default: B — short" in text
    option_block = text[
        text.index("  options:\n") : text.index("\n  recommendation:")
    ]
    assert "d499" not in option_block
    code, json_out, err = invoke(
        monkeypatch, ["--json", "--db", db, "card", "dc-1"]
    )
    assert code == 0, err
    data = json.loads(json_out)["data"]
    assert len(data["open_questions"]) == 1
    assert data["open_questions"][0]["options"][1] == {
        "label": "B",
        "description": "short",
    }


def test_ask_floor_then_question_cap_keeps_next_step(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _decision(lg)
    pad = " ".join(["beta"] * 120)
    asked = []
    for index in range(14):
        asked.append(
            _append(
                lg,
                "decision.asked",
                {
                    "question": f"question-{index:02d}?",
                    "ask": f"ask-{index:02d} {pad}",
                    "options": [
                        {"label": "A", "description": f"first {pad}"},
                        {"label": "B", "description": "second"},
                    ],
                    "default": "B — second",
                },
                "dc-1",
                f"ask-{index}",
            )
        )
    card = project_card(lg, "dc-1")
    text = format_card(card)
    assert word_count(text) <= WORD_LIMIT
    assert "ask" in card.truncated
    assert "open_questions" in card.truncated
    assert "question-13?" in text
    assert "question-00?" not in text
    # Every shown ask was cut to exactly the ten-word floor before any
    # question was capped.
    assert card.open_questions
    for row in card.open_questions:
        assert len(row["ask"].split()) == 10
        assert len(row["options"][0]["description"].split()) == 10
    # Capping hides questions; the next step still says they need answers,
    # and the header still names every ask the next step was read from.
    assert card.next_step == "Answer open questions before continuing."
    for event in asked:
        assert event.id in card.source_event_ids
    # Every ask word the card cut is counted in the JSON: the printed markers
    # name their events; the words of asks capped away have one entry left.
    omitted = dict(card.omitted)
    cuts = [entry for entry in card.as_dict()["cuts"] if entry["section"] == "ask"]
    assert sum(entry["count"] for entry in cuts) == omitted["ask"]
    shown = {int(row["asked_event_id"]) for row in card.open_questions}
    assert {entry["event_id"] for entry in cuts if "event_id" in entry} == shown
    assert [entry for entry in cuts if "event_id" not in entry] == [
        {
            "section": "ask",
            "count": omitted["ask"] - sum(e["count"] for e in cuts if "event_id" in e),
            "unit": "words",
            "command": "ledger export-item --json -- dc-1",
        }
    ]
    lg.close()


def test_newest_ask_is_shortened_last(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _decision(lg)
    for index, prefix in enumerate(("o", "n")):
        _append(
            lg,
            "decision.asked",
            {
                "question": f"question-{index}?",
                "ask": _words(prefix, 200),
                "options": [{"label": "yes"}, {"label": "no"}],
            },
            "dc-1",
            f"ask-{index}",
        )
    card = project_card(lg, "dc-1")
    text = format_card(card)
    assert word_count(text) <= WORD_LIMIT
    oldest, newest = card.open_questions
    assert oldest["omitted_words"]["ask"] > 0
    assert "omitted_words" not in newest
    assert newest["ask"] == _words("n", 200)
    lg.close()


def test_shortening_never_lengthens_the_card(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _decision(lg)
    for index in range(30):
        _elect(lg, "dc-1", index, f"e-{index}", pad=PAD)
    short_ask = _words("s", 12)
    _append(
        lg,
        "decision.asked",
        {
            "question": "keep going?",
            "ask": short_ask,
            "options": [{"label": "yes"}, {"label": "no"}],
        },
        "dc-1",
        "ask-1",
    )
    card = project_card(lg, "dc-1")
    text = format_card(card)
    assert word_count(text) <= WORD_LIMIT
    # Cutting two words would add a four-word marker: leave the ask whole.
    assert "ask" not in card.truncated
    assert f"  ask: {short_ask}\n" in text
    assert "words cut" not in text
    assert "decisions" in card.truncated
    lg.close()


def test_capped_away_question_still_needs_an_answer(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _decision(lg)
    # Labels are never shortened, so a flood label caps the whole question.
    asked = _append(
        lg,
        "decision.asked",
        {
            "question": "too-long-to-show?",
            "options": [{"label": _words("l", 600)}, {"label": "short"}],
            "default": "short",
        },
        "dc-1",
        "ask-1",
    )
    card = project_card(lg, "dc-1")
    text = format_card(card)
    assert word_count(text) <= WORD_LIMIT
    assert card.open_questions == ()
    assert "open_questions" in card.truncated
    assert "## Open questions\n(+1 cut; ledger export-item --json -- dc-1)\n" in text
    # The question is hidden, not answered, so the next step is unchanged,
    # and the header still names the ask it was derived from.
    assert card.next_step == "Answer open questions before continuing."
    assert asked.id in card.source_event_ids
    header = text.splitlines()[2]
    assert header.startswith("source_event_ids: ")
    assert str(asked.id) in header.split(": ", 1)[1].split(", ")
    lg.close()


def test_capped_elected_decision_still_names_the_choice(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _decision(lg)
    last = None
    for index in range(30):
        last = _elect(lg, "dc-1", index, f"e-{index}", pad=PAD)
    for index in range(50):
        _append(lg, "item.note", {"text": f"note-{index} {PAD}"}, "dc-1", f"n-{index}")
    card = project_card(lg, "dc-1")
    text = format_card(card)
    assert last is not None
    assert word_count(text) <= WORD_LIMIT
    assert "decisions" in card.truncated
    assert card.next_step == "Decided: yes-29. Nothing left to answer."
    assert "## Next step\nDecided: yes-29. Nothing left to answer.\n" in text
    assert last.id in card.source_event_ids
    lg.close()


def test_decision_next_step_says_what_is_true(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _decision(lg, "dc-held", status="blocked")
    _decision(lg, "dc-open")
    _decision(lg, "dc-claimed")
    _append(
        lg,
        "item.state",
        {"status": "claimed", "owner": "tester", "claim": "tester"},
        "dc-claimed",
        "s-claimed",
    )
    _decision(lg, "dc-elected")
    _colour_election(lg, "dc-elected", "elect-a", chosen="red")
    _colour_election(lg, "dc-elected", "elect-b", chosen="blue")
    _decision(lg, "dc-elected-claimed")
    _colour_election(lg, "dc-elected-claimed", "elect-c")
    _append(
        lg,
        "item.state",
        {"status": "claimed", "owner": "tester", "claim": "tester"},
        "dc-elected-claimed",
        "s-elected-claimed",
    )
    _decision(lg, "dc-elected-held", status="blocked")
    _colour_election(lg, "dc-elected-held", "elect-d")
    _decision(lg, "dc-elected-blocker")
    _colour_election(lg, "dc-elected-blocker", "elect-e")
    _append(
        lg,
        "item.state",
        {"status": "blocked", "blocked_by": ["Y"]},
        "dc-elected-blocker",
        "s-elected-blocker",
    )
    _decision(lg, "dc-period")
    _colour_election(lg, "dc-period", "elect-f", chosen="Held.")
    _decision(lg, "dc-held-asked", status="blocked")
    _full_ask(lg, "dc-held-asked")
    _decision(lg, "dc-named-blocker")
    _full_ask(lg, "dc-named-blocker")
    _append(
        lg,
        "item.state",
        {"status": "blocked", "blocked_by": ["X"]},
        "dc-named-blocker",
        "s-named",
    )
    lg.close()
    expected = {
        "dc-held": (
            "Held: no open ask or election is recorded for this decision; "
            "nothing to answer until its ask is reopened."
        ),
        "dc-open": (
            "No open ask or election is recorded for this decision; "
            "record the ask before it can be answered."
        ),
        "dc-claimed": (
            "No open ask or election is recorded for this decision. "
            "Continue claimed work (tester)."
        ),
        "dc-elected": "Decided: blue. Nothing left to answer.",
        "dc-elected-claimed": "Decided: blue. Continue claimed work (tester).",
        "dc-elected-held": "Decided: blue. Clear blockers.",
        "dc-elected-blocker": "Decided: blue. Clear blockers: Y.",
        "dc-period": "Decided: Held. Nothing left to answer.",
        "dc-held-asked": "Answer open questions before continuing.",
        "dc-named-blocker": "Clear blockers: X; then answer open questions.",
    }
    for item_id, step in expected.items():
        code, out, err = invoke(monkeypatch, ["--db", db, "card", item_id])
        assert code == 0, err
        assert f"## Next step\n{step}\n" in out, item_id
        assert "Resume with:" not in out
        code, json_out, err = invoke(
            monkeypatch, ["--json", "--db", db, "card", item_id]
        )
        assert code == 0, err
        assert json.loads(json_out)["data"]["next_step"] == step
    # An elected decision keeps today's Decisions rendering.
    code, out, err = invoke(monkeypatch, ["--db", db, "card", "dc-elected"])
    assert code == 0, err
    assert "quote_ref=1: pick a colour? -> red\n" in out
    assert "quote_ref=1: pick a colour? -> blue\n" in out
    assert "## Open questions\n(none)" in out


def test_reask_shows_only_latest_ask(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _decision(lg)
    old = _append(
        lg,
        "decision.asked",
        {
            "question": "first wording?",
            "options": [{"label": "yes"}, {"label": "no"}],
            "question_id": "same-question",
        },
        "dc-1",
        "ask-old",
    )
    new = _append(
        lg,
        "decision.asked",
        {
            "question": "second wording?",
            "options": [{"label": "yes"}, {"label": "no"}],
            "question_id": "same-question",
        },
        "dc-1",
        "ask-new",
    )
    other = _append(
        lg,
        "decision.asked",
        {"question": "unrelated?", "options": [{"label": "ok"}]},
        "dc-1",
        "ask-other",
    )
    lg.close()
    code, out, err = invoke(monkeypatch, ["--db", db, "card", "dc-1"])
    assert code == 0, err
    assert f"- event={new.id}: second wording?" in out
    assert f"- event={other.id}: unrelated?" in out
    assert "first wording?" not in out
    assert f"event={old.id}:" not in out
    code, json_out, err = invoke(
        monkeypatch, ["--json", "--db", db, "card", "dc-1"]
    )
    assert code == 0, err
    data = json.loads(json_out)["data"]
    assert [row["asked_event_id"] for row in data["open_questions"]] == [
        new.id,
        other.id,
    ]
    assert old.id not in data["source_event_ids"]


def test_reask_then_elect_newest_closes_the_question(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _decision(lg)
    old = _append(
        lg,
        "decision.asked",
        {"question": "first?", "options": [{"label": "yes"}], "question_id": "q"},
        "dc-1",
        "ask-old",
    )
    new = _append(
        lg,
        "decision.asked",
        {"question": "second?", "options": [{"label": "yes"}], "question_id": "q"},
        "dc-1",
        "ask-new",
    )
    # Naming the newest ask closes only that ask in the open-decisions view;
    # the older ask for the same question is superseded all the same.
    elected = _append(
        lg,
        "decision.elected",
        {
            "question": "second?",
            "chosen": "yes",
            "chosen_description": "",
            "rejected": [],
            "scope": "probe",
            "quote_ref": 1,
            "asked_event_id": new.id,
        },
        "dc-1",
        "elect-1",
    )
    assert any(
        int(row["asked_event_id"]) == old.id for row in lg.open_decisions()
    )
    card = project_card(lg, "dc-1")
    text = format_card(card)
    assert card.open_questions == ()
    assert "## Open questions\n(none)" in text
    assert card.next_step == "Decided: yes. Nothing left to answer."
    assert old.id not in card.source_event_ids
    assert elected.id in card.source_event_ids
    lg.close()


def test_four_hundred_asks_keep_header_inside_cap(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    created = _decision(lg)
    last = None
    for index in range(400):
        last = _append(
            lg,
            "decision.asked",
            {"question": f"q{index:03d}?", "options": [{"label": "yes"}]},
            "dc-1",
            f"ask-{index}",
        )
    card = project_card(lg, "dc-1")
    text = format_card(card)
    assert last is not None
    assert word_count(text) <= WORD_LIMIT
    header = text.splitlines()[2]
    assert header.endswith(
        f" (+{card.source_omitted} cut; ledger export-item --json -- dc-1)"
    )
    assert len(card.source_event_ids) == 40
    assert len(card.source_event_ids) + card.source_omitted == 401
    # The creation event and the newest ask are always named.
    assert created.id in card.source_event_ids
    assert last.id in card.source_event_ids
    assert card.next_step == "Answer open questions before continuing."
    assert f"- event={last.id}: q399?" in text
    lg.close()
    code, json_out, err = invoke(monkeypatch, ["--json", "--db", db, "card", "dc-1"])
    assert code == 0, err
    data = json.loads(json_out)["data"]
    assert len(data["source_event_ids"]) == 40
    assert data["source_event_ids_omitted"] == 361


def test_capping_never_changes_the_next_step(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _decision(lg)
    pad = " ".join(["w"] * 60)
    for index in range(8):
        _append(
            lg,
            "decision.asked",
            {
                "question": f"q{index}?",
                "ask": pad,
                "options": [
                    {"label": "A", "description": pad},
                    {"label": "B", "description": pad},
                ],
                "recommendation": pad,
                "default": "B",
            },
            "dc-1",
            f"ask-{index}",
        )
    _append(
        lg,
        "item.state",
        {"status": "blocked", "blocked_by": ["blk-1", "blk-2"]},
        "dc-1",
        "s-1",
    )
    full = project_item(lg, "dc-1")
    card = project_card(lg, "dc-1")
    text = format_card(card)
    assert word_count(text) <= WORD_LIMIT
    assert "blockers" in card.truncated
    assert full.next_step == "Clear blockers: blk-1, blk-2; then answer open questions."
    assert card.next_step == full.next_step
    assert f"## Next step\n{full.next_step}\n" in text
    lg.close()


def test_huge_election_capped_still_names_choice_and_event(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _decision(lg)
    elected = _append(
        lg,
        "decision.elected",
        {
            "question": _words("q", 600),
            "chosen": "blue",
            "chosen_description": "cool",
            "rejected": ["red"],
            "scope": "palette",
            "quote_ref": 1,
        },
        "dc-1",
        "elect-1",
    )
    card = project_card(lg, "dc-1")
    text = format_card(card)
    assert word_count(text) <= WORD_LIMIT
    assert card.decisions == ()
    assert "## Decisions\n(+1 cut; ledger export-item --json -- dc-1)\n" in text
    assert card.next_step == "Decided: blue. Nothing left to answer."
    assert elected.id in card.source_event_ids
    lg.close()


def _shown_event_ids(text: str) -> set[int]:
    return {int(match) for match in re.findall(r"event=(\d+)", text)}


def _header_ids(text: str) -> list[int]:
    header = text.splitlines()[2].split(": ", 1)[1]
    header = re.sub(r" \(\+\d+ cut\)$", "", header)
    return [int(part) for part in header.split(", ")]


def test_header_names_every_shown_row(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    created = _decision(lg)
    older = _append(
        lg,
        "decision.asked",
        {"question": "older?", "options": [{"label": "yes"}]},
        "dc-1",
        "ask-older",
    )
    newer = _append(
        lg,
        "decision.asked",
        {"question": "newer?", "options": [{"label": "yes"}]},
        "dc-1",
        "ask-newer",
    )
    notes = [
        _append(lg, "item.note", {"text": f"n{index}"}, "dc-1", f"n-{index}")
        for index in range(45)
    ]
    card = project_card(lg, "dc-1")
    text = format_card(card)
    assert word_count(text) <= WORD_LIMIT
    # 48 ids would not fit the 40-id header, so the oldest notes are capped
    # instead and the header names every row the card shows.
    assert card.truncated == ("notes",)
    assert "## Notes\n" in text and "\n(+8 cut; ledger export-item --json -- dc-1)\n" in text
    assert card.source_omitted == 0
    assert len(card.source_event_ids) == 40
    assert _shown_event_ids(text) <= set(card.source_event_ids)
    for event in (created, older, newer, notes[-1]):
        assert event.id in card.source_event_ids
    assert notes[7].id not in card.source_event_ids
    assert notes[8].id in card.source_event_ids
    lg.close()


def test_bound_ids_keeps_required_then_visible_then_newest() -> None:
    ids, cut = _bound_ids(set(range(1, 49)), {1, 3}, {2, 3})
    assert cut == 8
    assert {1, 2, 3} <= set(ids)
    assert set(ids) == {1, 2, 3} | set(range(12, 49))
    # Required ids win over visible ones, and visible over the rest.
    ids, _ = _bound_ids(set(range(1, 101)), {5}, set(range(10, 60)))
    assert 5 in ids
    assert set(ids) == {5} | set(range(21, 60))
    assert _bound_ids({1, 2}, set(), set()) == ((1, 2), 0)


def test_key_step_ids_are_newest_ask_and_latest_election() -> None:
    card = Card(
        item_id="x",
        type="decision",
        title="t",
        status="open",
        owner=None,
        claim=None,
        revision_id=9,
        revision_hash="h",
        source_event_ids=(1,),
        decisions=(
            {"event_id": 7, "chosen": "a"},
            {"event_id": 4, "chosen": "b"},
        ),
        open_questions=({"asked_event_id": 2}, {"asked_event_id": 5}),
        next_step="",
        links=(),
        blockers=(),
        notes=(),
    )
    assert _key_step_ids(card) == {5, 7}


def test_blocker_flood_step_keeps_count_and_question(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _decision(lg)
    _full_ask(lg)
    blockers = [" ".join([f"w{index}"] * 120) for index in range(6)]
    _append(
        lg, "item.state", {"status": "blocked", "blocked_by": blockers}, "dc-1", "s-1"
    )
    full = project_item(lg, "dc-1")
    card = project_card(lg, "dc-1")
    text = format_card(card)
    assert word_count(text) <= WORD_LIMIT
    name = " ".join(["w0"] * 8) + " …"
    assert card.next_step.startswith(f"Clear blockers: {name}, ")
    assert card.next_step.endswith(
        " (+1 cut; ledger export-item --json -- dc-1); then answer open questions."
    )
    assert card.next_step == full.next_step
    assert "next_step" not in card.truncated
    lg.close()


@pytest.mark.parametrize("field", ["owner", "claim"])
def test_long_owner_or_claim_is_trimmed(tmp_path: Path, field: str) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "it-1", "ticket", "synthetic-one")
    state = {"status": "claimed", "owner": "tester", "claim": "tester"}
    state[field] = _words("o", 600)
    _append(lg, "item.state", state, "it-1", "s-1")
    _elect(lg, "it-1", 0, "e-0")
    card = project_card(lg, "it-1")
    text = format_card(card)
    assert word_count(text) <= WORD_LIMIT
    assert field in card.truncated
    value = getattr(card, field)
    assert value.startswith("o000 o001")
    assert len(value.split()) >= 10
    assert "o599" not in text
    line = next(line for line in text.splitlines() if line.startswith(f"{field}: "))
    cut = 600 - len(value.split())
    assert line == f"{field}: {value} (+{cut} words cut; ledger export-item --json -- it-1)"
    # The decision stays: the long field is trimmed before content is capped.
    assert "choose-00? -> yes-00" in text
    # The next step reads the full field, clipped to eight words.
    if field == "claim":
        expected = "Continue claimed work (" + _words("o", 8) + " …)."
    else:
        expected = "Continue claimed work (tester)."
    assert card.next_step == expected
    lg.close()


def test_short_field_is_never_trimmed(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "it-1", "ticket", "synthetic-one")
    owner = _words("o", 12)
    _append(
        lg, "item.state", {"status": "claimed", "owner": owner, "claim": "t"}, "it-1", "s-1"
    )
    for index in range(30):
        _elect(lg, "it-1", index, f"e-{index}", pad=PAD)
    card = project_card(lg, "it-1")
    text = format_card(card)
    assert word_count(text) <= WORD_LIMIT
    # Cutting two words would add a three-word marker: the owner stays whole.
    assert f"owner: {owner}\n" in text
    assert "owner" not in card.truncated
    lg.close()


def test_blocks_link_from_another_item_names_the_blocker(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _decision(lg, "dc-open")
    _decision(lg, "dc-elected")
    _colour_election(lg, "dc-elected", "elect-1")
    _create(lg, "blk-1", "ticket", "blocker")
    for target in ("dc-open", "dc-elected"):
        _append(
            lg,
            "item.linked",
            {"rel": "blocks", "target": target},
            "blk-1",
            f"blocks-{target}",
        )
    open_card = project_card(lg, "dc-open")
    elected_card = project_card(lg, "dc-elected")
    assert open_card.status == "open"
    assert open_card.blockers == ("blk-1",)
    assert open_card.next_step == "Clear blockers: blk-1."
    assert elected_card.next_step == "Decided: blue. Clear blockers: blk-1."
    lg.close()


@pytest.mark.parametrize("seed", range(4))
def test_fuzzed_cards_fit_and_keep_the_step(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    for case in range(15):
        lg = open_ledger(tmp_path / f"fuzz-{case}.db")
        item_type = rng.choice(["decision", "decision", "ticket"])
        _append(
            lg,
            "item.created",
            {"type": item_type, "title": _words("t", rng.choice([1, 5, 200, 600]))},
            "it",
            "c",
        )
        for index in range(rng.choice([0, 1, 3, 45])):
            payload = {
                "question": rng.choice(["q?", _words("Q", rng.choice([1, 60]))]),
                "options": [
                    {
                        "label": rng.choice(["A", "B", _words("l", rng.choice([1, 50]))]),
                        "description": _words("d", rng.choice([0, 1, 300])),
                    }
                    for _ in range(rng.choice([1, 2, 6]))
                ],
            }
            for name, most in (("ask", 500), ("recommendation", 200), ("default", 200)):
                if rng.random() < 0.5:
                    payload[name] = _words(name[:2], rng.randint(1, most))
            if rng.random() < 0.5:
                payload["question_id"] = f"q{rng.randint(0, 4)}"
            _append(lg, "decision.asked", payload, "it", f"a-{index}")
        for index in range(rng.choice([0, 1, 5, 60])):
            _append(
                lg,
                "decision.elected",
                {
                    "question": f"e{index}?",
                    "chosen": _words("c", rng.choice([1, 8, 600])),
                    "chosen_description": "",
                    "rejected": [],
                    "scope": "s",
                    "quote_ref": 1,
                },
                "it",
                f"e-{index}",
            )
        for index in range(rng.choice([0, 3, 60])):
            _append(lg, "item.note", {"text": _words("n", rng.choice([3, 30]))}, "it", f"n-{index}")
        status = rng.choice([None, "blocked", "claimed"])
        if status:
            state: dict = {"status": status}
            if status == "blocked" and rng.random() < 0.8:
                state["blocked_by"] = [
                    _words("b", rng.choice([1, 20, 150])) + f"-{index}"
                    for index in range(rng.choice([1, 3, 9]))
                ]
            if status == "claimed":
                state["owner"] = _words("o", rng.choice([1, 3, 40, 600]))
                state["claim"] = _words("k", rng.choice([1, 3, 40, 600]))
            _append(lg, "item.state", state, "it", "s")
        full = project_item(lg, "it")
        card = project_card(lg, "it")
        text = format_card(card)
        where = f"seed={seed} case={case}"
        assert word_count(text) <= WORD_LIMIT, where
        assert len(card.source_event_ids) <= 40, where
        assert _shown_event_ids(text) <= set(_header_ids(text)), where
        assert card.next_step == full.next_step, where
        assert "next_step" not in card.truncated, where
        lg.close()


_MARKER = re.compile(r"^\(\+(\d+) cut; (.+)\)$")


def _section_markers(text: str, heading: str) -> list[tuple[int, str]]:
    """The ``(+K cut; COMMAND)`` lines that end one section of a card."""
    body = text.split(f"\n## {heading}\n", 1)[1].split("\n\n", 1)[0]
    markers = []
    for line in body.splitlines():
        match = _MARKER.match(line)
        if match:
            markers.append((int(match.group(1)), match.group(2)))
    return markers


def _run_printed(monkeypatch, command: str, db: str) -> str:
    """Run a command the card printed, as printed, and return its output."""
    argv = shlex.split(command)
    assert argv[:3] == ["ledger", "--db", db]
    code, out, err = invoke(monkeypatch, argv[1:])
    assert code == 0, err
    return out


def _queried_ids(out: str) -> set[str]:
    return {line.split("\t", 1)[0] for line in out.splitlines() if line}


def _exported_events(out: str) -> list[dict]:
    return json.loads(out)["data"]["events"]


def _printed_card(monkeypatch, db: str, item_id: str) -> str:
    code, out, err = invoke(monkeypatch, ["--db", db, "card", "--", item_id])
    assert code == 0, err
    return out


def test_cut_duplicated_by_links_come_back_from_the_printed_query(
    tmp_path: Path, monkeypatch
) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _create(lg, "it-kept", "ticket")
    dups = []
    for index in range(3):
        dup = f"it-dup-{index}"
        _create(lg, dup, "ticket")
        _append(lg, "item.linked", {"rel": "duplicate-of", "target": "it-kept"}, dup, f"d-{index}")
        dups.append(dup)
    # A duplicate of another item is not one of this card's rows.
    _create(lg, "it-elsewhere", "ticket")
    _create(lg, "it-dup-x", "ticket")
    _append(
        lg, "item.linked", {"rel": "duplicate-of", "target": "it-elsewhere"}, "it-dup-x", "d-x"
    )
    for index in range(45):
        _append(
            lg, "item.linked", {"rel": "pr", "target": f"org/repo#{index}"}, "it-kept", f"pr-{index}"
        )
    full = project_item(lg, "it-kept")
    lg.close()
    text = _printed_card(monkeypatch, db, "it-kept")
    shown = {line for line in text.splitlines() if line.startswith("- ")}
    removed = [
        row for row in full.links
        if f"- {row['rel']}: {row['target']} (event={row['event_id']})" not in shown
    ]
    removed_inbound = [row for row in removed if row["rel"] == "duplicated-by"]
    removed_own = [row for row in removed if row["rel"] != "duplicated-by"]
    assert {row["target"] for row in removed_inbound} == set(dups)
    assert removed_own
    prefix = f"ledger --db {shlex.quote(db)}"
    markers = _section_markers(text, "Links")
    assert markers == [
        (len(removed_own), f"{prefix} export-item --json -- it-kept"),
        (len(dups), f"{prefix} query --duplicate-of=it-kept --all"),
    ]
    # Every cut duplicated-by row comes back from the query, and nothing else.
    assert _queried_ids(_run_printed(monkeypatch, markers[1][1], db)) == set(dups)
    # Every cut own link comes back from the item's own export.
    exported = {
        event["id"] for event in _exported_events(_run_printed(monkeypatch, markers[0][1], db))
    }
    assert {row["event_id"] for row in removed_own} <= exported
    assert word_count(text, command_prefix=prefix) <= WORD_LIMIT


def test_cut_blockers_come_back_from_the_printed_commands(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _create(lg, "it-x", "ticket")
    named = [f"own-{index:03d}" for index in range(250)]
    _append(lg, "item.state", {"status": "blocked", "blocked_by": named}, "it-x", "s-1")
    linked = []
    for index in range(60):
        blocker = f"blk-{index:03d}"
        _create(lg, blocker, "ticket")
        _append(lg, "item.linked", {"rel": "blocks", "target": "it-x"}, blocker, f"b-{index}")
        linked.append(blocker)
    # A blocks link to another item is not one of this card's rows.
    _create(lg, "it-y", "ticket")
    _append(lg, "item.linked", {"rel": "blocks", "target": "it-y"}, "blk-000", "b-y")
    full = project_item(lg, "it-x")
    lg.close()
    text = _printed_card(monkeypatch, db, "it-x")
    body = text.split("\n## Blockers\n", 1)[1].split("\n\n", 1)[0]
    shown = {line[2:] for line in body.splitlines() if line.startswith("- ")}
    removed = [name for name in full.blockers if name not in shown]
    removed_linked = [name for name in removed if name in linked]
    removed_named = [name for name in removed if name in named]
    assert removed_linked and removed_named
    assert len(removed) == len(removed_linked) + len(removed_named)
    prefix = f"ledger --db {shlex.quote(db)}"
    markers = _section_markers(text, "Blockers")
    assert markers == [
        (len(removed_named), f"{prefix} export-item --json -- it-x"),
        (len(removed_linked), f"{prefix} query --blocks=it-x --open --all"),
    ]
    queried = _queried_ids(_run_printed(monkeypatch, markers[1][1], db))
    assert set(removed_linked) <= queried
    assert queried == set(linked)
    held = set()
    for event in _exported_events(_run_printed(monkeypatch, markers[0][1], db)):
        held.update(event["payload"].get("blocked_by") or ())
    assert set(removed_named) <= held
    assert word_count(text, command_prefix=prefix) <= WORD_LIMIT


def test_word_count_counts_the_new_query_prefixes_as_commands() -> None:
    prefix = "ledger --db '/tmp/a b/ledger.db'"
    for verb in ("query --duplicate-of=it-1 --all", "query --blocks=it-1 --all"):
        # The quoted path is one argument: three words for the prefix.
        assert word_count(f"(+3 cut; {prefix} {verb})", command_prefix=prefix) == 8
    verb = "query --blocks=it-1 --open --all"
    assert word_count(f"(+3 cut; {prefix} {verb})", command_prefix=prefix) == 9


_HEADINGS = {
    "Decisions": "decisions",
    "Open questions": "open_questions",
    "Links": "links",
    "Blockers": "blockers",
    "Notes": "notes",
    "Decision records": "decision_records",
    "Tried": "tried",
    "Progress": "progress",
    "Review rounds": "reviews",
    "References": "refs",
    "Conversation pointers": "pointers",
}


def _text_row_cuts(text: str) -> list[tuple[str, int, str]]:
    """Every (section, count, command) row marker the text card prints."""
    cuts = []
    section = None
    for line in text.splitlines():
        if line.startswith("## "):
            heading = line[3:]
            section = "children" if heading.startswith("Children (") else _HEADINGS.get(heading)
            continue
        match = _MARKER.match(line)
        if match and section is not None:
            cuts.append((section, int(match.group(1)), match.group(2)))
    return cuts


def _card_json(monkeypatch, db: str, item_id: str) -> dict:
    code, out, err = invoke(monkeypatch, ["--db", db, "--json", "card", "--", item_id])
    assert code == 0, err
    return json.loads(out)["data"]


def test_json_cut_sections_carry_the_text_count_and_command(
    tmp_path: Path, monkeypatch
) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _create(lg, "it-anchor", "ticket")
    blockers = [f"blk-{index}" for index in range(7)]
    for blocker in blockers:
        _create(lg, blocker, "ticket")
        _append(lg, "item.linked", {"rel": "blocks", "target": "it-anchor"}, blocker, f"b-{blocker}")
    for index in range(40):
        child = f"it-child-{index:02d}"
        _create(lg, child, "ticket", " ".join([f"t{index}"] * 12))
        _append(lg, "item.linked", {"rel": "child-of", "target": "it-anchor"}, child, f"l-{child}")
    lg.close()
    text = _printed_card(monkeypatch, db, "it-anchor")
    data = _card_json(monkeypatch, db, "it-anchor")
    assert "children" in data["truncated"]
    prefix = f"ledger --db {shlex.quote(db)}"
    # Every name the card cut has an entry, each with its count and command.
    assert {entry["section"] for entry in data["cuts"]} == set(data["truncated"])
    for entry in data["cuts"]:
        assert isinstance(entry["count"], int) and entry["count"] > 0, entry
        assert entry["command"].startswith(prefix + " "), entry
    # The row markers are the text form's, word for word and in order.
    rows = [entry for entry in data["cuts"] if entry["unit"] == "rows"]
    assert [(e["section"], e["count"], e["command"]) for e in rows] == _text_row_cuts(text)
    children = [entry for entry in rows if entry["section"] == "children"]
    assert [(e["count"], e["command"]) for e in children] == [
        (40 - len(data["children"]), f"{prefix} query --parent=it-anchor --all")
    ]
    # The next step names five blockers and the query that prints the rest,
    # not export-item, which holds no blocks link of another item.
    command = f"{prefix} query --blocks=it-anchor --open --all"
    assert data["next_step"] == (
        f"Clear blockers: {', '.join(blockers[:5])} (+2 cut; {command})."
    )
    assert "export-item" not in data["next_step"]
    assert f"## Next step\n{data['next_step']}\n" in text
    assert set(blockers[5:]) <= _queried_ids(_run_printed(monkeypatch, command, db))
    assert word_count(text, command_prefix=prefix) <= WORD_LIMIT


def test_next_step_names_each_kind_of_blocker_it_cut(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "it-x", "ticket")
    named = [f"own-{index}" for index in range(6)]
    _append(lg, "item.state", {"status": "blocked", "blocked_by": named}, "it-x", "s-1")
    for index in range(2):
        _create(lg, f"blk-{index}", "ticket")
        _append(lg, "item.linked", {"rel": "blocks", "target": "it-x"}, f"blk-{index}", f"b-{index}")
    card = project_card(lg, "it-x")
    lg.close()
    # The state names own-0..own-5, the links add blk-0 and blk-1: the step
    # names five, then the item's export for own-5 and the query for the
    # linked two, in the order a capped Blockers section prints them.
    assert card.next_step == (
        "Clear blockers: own-0, own-1, own-2, own-3, own-4 "
        "(+1 cut; ledger export-item --json -- it-x) "
        "(+2 cut; ledger query --blocks=it-x --open --all)."
    )
    assert card.as_dict()["cuts"] == []


def test_cut_blockers_query_returns_only_the_open_blockers(
    tmp_path: Path, monkeypatch
) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _decision(lg, "dc-x")
    pad = " ".join(["w"] * 60)
    for index in range(8):
        _append(
            lg,
            "decision.asked",
            {
                "question": f"q{index}?",
                "ask": pad,
                "options": [
                    {"label": "A", "description": pad},
                    {"label": "B", "description": pad},
                ],
                "recommendation": pad,
                "default": "B",
            },
            "dc-x",
            f"ask-{index}",
        )
    for blocker in ("blk-open-1", "blk-open-2", "blk-closed"):
        _create(lg, blocker, "ticket")
        _append(lg, "item.linked", {"rel": "blocks", "target": "dc-x"}, blocker, f"b-{blocker}")
    _append(lg, "item.state", {"status": "closed"}, "blk-closed", "s-closed")
    lg.close()
    text = _printed_card(monkeypatch, db, "dc-x")
    prefix = f"ledger --db {shlex.quote(db)}"
    command = f"{prefix} query --blocks=dc-x --open --all"
    assert _section_markers(text, "Blockers") == [(2, command)]
    data = _card_json(monkeypatch, db, "dc-x")
    assert [
        (entry["count"], entry["command"]) for entry in data["cuts"]
        if entry["section"] == "blockers"
    ] == [(2, command)]
    # The printed command returns exactly the two open blockers the card cut.
    assert _queried_ids(_run_printed(monkeypatch, command, db)) == {"blk-open-1", "blk-open-2"}
    # Without the flag, query --blocks still returns every linking item.
    unfiltered = f"{prefix} query --blocks=dc-x --all"
    assert _queried_ids(_run_printed(monkeypatch, unfiltered, db)) == {
        "blk-open-1", "blk-open-2", "blk-closed"
    }


_WORDS_MARKER = re.compile(r"\(\+(\d+) words cut; (.+)\)$")
_ASK_MARKER = re.compile(r"… \(\+(\d+) words cut; full text in event (\d+); ([^)]+)\)")
_FIELD_LINE = re.compile(r"^(title|owner|claim): ")


def _text_cut_markers(text: str) -> list[tuple[str, int, str, str]]:
    """Every (section, count, unit, command) cut marker the text card prints
    for a ``truncated`` name, in the order it prints them."""
    cuts = []
    heading = None
    for line in text.splitlines():
        if line.startswith("## "):
            heading = line[3:]
            continue
        field = _FIELD_LINE.match(line)
        if heading is None and field:
            match = _WORDS_MARKER.search(line)
            if match:
                cuts.append((field.group(1), int(match.group(1)), "words", match.group(2)))
            continue
        if heading == "Next step":
            match = _WORDS_MARKER.search(line)
            if match:
                cuts.append(("next_step", int(match.group(1)), "words", match.group(2)))
            continue
        if heading == "Open questions":
            for match in _ASK_MARKER.finditer(line):
                cuts.append(("ask", int(match.group(1)), "words", match.group(3)))
        match = _MARKER.match(line)
        if match and heading is not None:
            section = "children" if heading.startswith("Children (") else _HEADINGS[heading]
            cuts.append((section, int(match.group(1)), "rows", match.group(2)))
    return cuts


def _json_cut_markers(data: dict) -> list[tuple[str, int, str, str]]:
    return [(e["section"], e["count"], e["unit"], e["command"]) for e in data["cuts"]]


def test_json_cuts_follow_the_text_order_of_title_owner_and_claim(
    tmp_path: Path, monkeypatch
) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _create(lg, "it-1", "ticket", _words("t", 300))
    state = {"status": "claimed", "owner": _words("o", 300), "claim": _words("k", 300)}
    _append(lg, "item.state", state, "it-1", "s-1")
    lg.close()
    text = _printed_card(monkeypatch, db, "it-1")
    data = _card_json(monkeypatch, db, "it-1")
    # The card trims owner, then claim, then title; the text prints the
    # title first, then owner and claim. The JSON follows the text.
    assert data["truncated"] == ["owner", "claim", "title"]
    markers = _text_cut_markers(text)
    assert [marker[0] for marker in markers] == ["title", "owner", "claim"]
    assert _json_cut_markers(data) == markers


def test_json_cuts_follow_the_text_order_of_an_ask_children_and_blockers(
    tmp_path: Path, monkeypatch
) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _decision(lg, "dc-x")
    pad = " ".join(["w"] * 60)
    for index in range(2):
        _append(
            lg,
            "decision.asked",
            {
                "question": f"q{index}?",
                "ask": pad,
                "options": [
                    {"label": "A", "description": pad},
                    {"label": "B", "description": pad},
                ],
                "recommendation": pad,
                "default": "B",
            },
            "dc-x",
            f"ask-{index}",
        )
    for index in range(6):
        child = f"ch-{index:02d}"
        _create(lg, child, "ticket", " ".join([f"c{index}"] * 12))
        _append(lg, "item.linked", {"rel": "child-of", "target": "dc-x"}, child, f"l-{child}")
    for index in range(80):
        blocker = f"blk-{index:02d}"
        _create(lg, blocker, "ticket")
        _append(lg, "item.linked", {"rel": "blocks", "target": "dc-x"}, blocker, f"b-{blocker}")
    lg.close()
    text = _printed_card(monkeypatch, db, "dc-x")
    data = _card_json(monkeypatch, db, "dc-x")
    assert data["truncated"] == ["ask", "children", "blockers"]
    markers = _text_cut_markers(text)
    sections = [marker[0] for marker in markers]
    assert sections[-2:] == ["children", "blockers"]
    assert set(sections[:-2]) == {"ask"}
    assert _json_cut_markers(data) == markers
    # Each ask entry names the event the text marker names, in the same order.
    asked = [int(m.group(2)) for m in _ASK_MARKER.finditer(text.split("\n## Open questions\n")[1])]
    assert [e["event_id"] for e in data["cuts"] if e["section"] == "ask"] == asked


def test_a_truncated_name_with_no_marker_gets_the_export_entry(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "it-x", "ticket")
    card = project_card(lg, "it-x")
    lg.close()
    # Notes is named as cut, but no count was recorded, so the text prints
    # no marker for it. The JSON still gives it one entry, under the export.
    card = dataclasses.replace(card, truncated=("notes",))
    text = format_card(card)
    assert _text_cut_markers(text) == []
    assert "dropped: notes" in text
    assert card.as_dict()["cuts"] == [
        {
            "section": "notes",
            "count": 0,
            "unit": "rows",
            "command": "ledger export-item --json -- it-x",
        }
    ]


def _links_body(text: str) -> list[str]:
    return text.split("\n## Links\n", 1)[1].split("\n\n", 1)[0].splitlines()


def test_bounded_card_caps_links_to_the_newest_eight(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _create(lg, "it-l", "ticket")
    links = [
        _append(lg, "item.linked", {"rel": "url", "target": f"https://example.invalid/{n}"},
                "it-l", f"l-{n}")
        for n in range(11)
    ]
    lg.close()
    text = _printed_card(monkeypatch, db, "it-l")
    assert LINK_LIMIT == 8
    assert word_count(text) <= WORD_LIMIT
    prefix = f"ledger --db {shlex.quote(db)}"
    assert _links_body(text) == [
        f"- url: https://example.invalid/{n} (event={event.id})"
        for n, event in enumerate(links)
        if n >= 3
    ] + [f"(+3 cut; {prefix} export-item --json -- it-l)"]
    assert "dropped: links" in text


def test_link_cap_counts_cut_duplicated_by_rows_under_their_query(
    tmp_path: Path, monkeypatch
) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _create(lg, "it-l", "ticket")
    for n in range(3):
        _create(lg, f"it-dup-{n}", "ticket")
        _append(lg, "item.linked", {"rel": "duplicate-of", "target": "it-l"}, f"it-dup-{n}",
                f"d-{n}")
    for n in range(8):
        _append(lg, "item.linked", {"rel": "url", "target": f"https://example.invalid/{n}"},
                "it-l", f"l-{n}")
    lg.close()
    text = _printed_card(monkeypatch, db, "it-l")
    body = _links_body(text)
    assert len(body) == 9
    assert not any("duplicated-by" in line for line in body)
    command = f"ledger --db {shlex.quote(db)} query --duplicate-of=it-l --all"
    assert body[-1] == f"(+3 cut; {command})"
    assert _queried_ids(_run_printed(monkeypatch, command, db)) == {
        "it-dup-0", "it-dup-1", "it-dup-2"
    }


def test_full_card_shows_every_row_and_no_cut_marker(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    title = _words("t", 600)
    _create(lg, "it-f", "ticket", title)
    for n in range(3):
        _create(lg, f"it-dup-{n}", "ticket")
        _append(lg, "item.linked", {"rel": "duplicate-of", "target": "it-f"}, f"it-dup-{n}",
                f"d-{n}")
    for n in range(4):
        _create(lg, f"it-kid-{n}", "ticket", f"kid {n}")
        _append(lg, "item.linked", {"rel": "child-of", "target": "it-f"}, f"it-kid-{n}",
                f"k-{n}")
    blockers = [f"{_words('b', 12)} blocker-{n}" for n in range(9)]
    _append(lg, "item.state", {"status": "blocked", "blocked_by": blockers}, "it-f", "s-1")
    for n in range(4):
        _create(lg, f"it-blk-{n}", "ticket")
        _append(lg, "item.linked", {"rel": "blocks", "target": "it-f"}, f"it-blk-{n}", f"b-{n}")
    elected = [_elect(lg, "it-f", n, f"e-{n}", pad=PAD) for n in range(30)]
    asked = [
        _append(lg, "decision.asked",
                {"question": f"ask-{n}? {PAD}", "options": [{"label": "yes"}, {"label": "no"}]},
                "it-f", f"a-{n}")
        for n in range(6)
    ]
    links = [
        _append(lg, "item.linked",
                {"rel": "url", "target": f"https://example.invalid/{n}/{PAD.replace(' ', '-')}"},
                "it-f", f"l-{n}")
        for n in range(12)
    ]
    notes = [
        _append(lg, "item.note", {"text": f"note-{n} {PAD}"}, "it-f", f"n-{n}")
        for n in range(60)
    ]
    long_text = _words("x", 70)
    tried = _append(lg, "item.tried", {"what": long_text, "outcome": _words("o", 50)}, "it-f",
                    "tr-1")
    findings = [f"finding-{n} {_words('f', 40)}" for n in range(9)]
    _append(lg, "review.recorded", {"round": 1, "verdict": "changes", "findings": findings},
            "it-f", "rv-1")
    point = _words("p", 110)
    refs = [f"plans/plan-{n}.md" for n in range(3)]
    _append(lg, "item.stopped", {"point": point, "phase": "build", "refs": refs}, "it-f",
            "st-1")
    full = project_item(lg, "it-f", full=True)
    bounded = project_card(lg, "it-f")
    lg.close()

    # The bounded card of the same item does cut: every cap is exceeded.
    assert "cut;" in format_card(bounded)
    assert bounded.truncated and len(bounded.links) <= LINK_LIMIT

    code, text, err = invoke(monkeypatch, ["--db", db, "card", "--full", "--", "it-f"])
    assert code == 0, err
    assert "cut;" not in text
    assert "(+" not in text
    assert "…" not in text
    assert "full text in event" not in text
    assert "## Truncated" not in text
    assert word_count(text) > WORD_LIMIT
    assert f"title: {title}\n" in text
    for event in elected + asked + links + notes:
        assert f"event={event.id}" in text
    for n in range(3):
        assert f"- duplicated-by: it-dup-{n} (event=" in text
    for n in range(4):
        assert f"- it-kid-{n}: kid {n} [open] holder: -" in text
        assert f"- it-blk-{n}\n" in text
    for name in blockers:
        assert f"- {name}\n" in text
    # The next step names every blocker in full.
    step = text.split("\n## Next step\n", 1)[1].split("\n", 1)[0]
    for name in blockers:
        assert name in step
    assert f"event={tried.id}: {long_text} -> {_words('o', 50)}" in text
    for finding in findings:
        assert f"  finding: {finding}\n" in text
    assert f"[build]: {point}\n" in text
    for ref in refs:
        assert f"- {ref}\n" in text
    header = [int(part) for part in text.splitlines()[2].split(": ", 1)[1].split(", ")]
    assert len(header) > 40
    assert _shown_event_ids(text) <= set(header)
    assert header == list(full.source_event_ids)

    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "card", "--full", "it-f"])
    assert code == 0, err
    data = json.loads(out)["data"]
    assert data["truncated"] == []
    assert data["source_event_ids_omitted"] == 0
    assert len(data["notes"]) == 60
    assert len(data["links"]) == 15
    assert len(data["decisions"]) == 30
    assert len(data["open_questions"]) == 6
    assert len(data["blockers"]) == 13
    assert len(data["children"]) == 4
    assert data["reviews"][0]["findings_cut"] == 0


def test_full_card_header_names_decision_standing_and_result_events(
    tmp_path: Path, monkeypatch
) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _create(lg, "it-d", "ticket")
    _create(lg, "it-o", "ticket")
    recorded = {
        "who": "tester",
        "who_kind": "person",
        "standing": "lean",
        "effect": "synthetic effect",
        "tags": [{"type": "ticket", "id": "it-d"}],
        "verbatim": "synthetic words",
        "phase": "build",
    }
    # The source event and the replacing decision live on another item, so
    # nothing else on this card names them.
    source = _append(lg, "item.note", {"text": "source"}, "it-o", "src")
    decision = _append(
        lg, "decision.recorded", {**recorded, "source_event_id": source.id}, "it-d", "dec"
    )
    replacement = _append(
        lg, "decision.recorded", {**recorded, "tags": [{"type": "ticket", "id": "it-o"}]},
        "it-o", "dec-2",
    )
    _append(
        lg, "decision.standing",
        {"decision_event_id": decision.id, "standing": "lean", "source_event_id": source.id},
        "it-d", "st-0",
    )
    result = _append(
        lg, "decision.result", {"decision_event_id": decision.id, "result": "shipped"},
        "it-d", "res",
    )
    standing = _append(
        lg, "decision.standing",
        {
            "decision_event_id": decision.id,
            "standing": "superseded",
            "superseded_by": replacement.id,
            "source_event_id": source.id,
        },
        "it-d", "st-1",
    )
    lg.close()
    code, text, err = invoke(monkeypatch, ["--db", db, "card", "--full", "--", "it-d"])
    assert code == 0, err
    header = {int(part) for part in text.splitlines()[2].split(": ", 1)[1].split(", ")}
    assert {source.id, standing.id, result.id, replacement.id} <= header
    assert decision.id in header
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "card", "--full", "it-d"])
    assert code == 0, err
    [row] = json.loads(out)["data"]["decision_records"]
    assert row["standing_event_id"] == standing.id
    assert row["result_event_id"] == result.id
    assert row["superseded_by"] == replacement.id
    assert row["source_event_id"] == source.id


def test_full_card_keeps_every_pointer_unclipped(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, "it-p", "ticket")
    pointers = [{"ref": f"{_words('q', 20)}-{n}", "note": _words("m", 30)} for n in range(15)]
    full = project_item(lg, "it-p", pointers=lambda _item: pointers, full=True)
    capped = project_item(lg, "it-p", pointers=lambda _item: pointers)
    lg.close()
    assert [row["ref"] for row in full.pointers] == [row["ref"] for row in pointers]
    assert [row["note"] for row in full.pointers] == [row["note"] for row in pointers]
    assert len(capped.pointers) < len(pointers)


def _child_ledger(lg):
    """A parent and one child: created, state set, linked child-of the parent."""
    parent = _create(lg, "it-parent", "ticket", "parent")
    child = _create(lg, "it-child", "ticket", "child")
    state = _append(
        lg, "item.state", {"status": "claimed", "owner": "tester"}, "it-child", "s-child"
    )
    link = _append(
        lg, "item.linked", {"rel": "child-of", "target": "it-parent"}, "it-child", "l-child"
    )
    return parent, child, state, link


def test_full_card_names_child_creation_state_and_link_events(
    tmp_path: Path, monkeypatch
) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    parent, child, state, link = _child_ledger(lg)
    card = project_card(lg, "it-parent", full=True)
    lg.close()
    assert card.children[0]["item_id"] == "it-child"
    assert card.source_event_ids == tuple(sorted({parent.id, child.id, state.id, link.id}))
    code, text, err = invoke(monkeypatch, ["--db", db, "card", "--full", "--", "it-parent"])
    assert code == 0, err
    header = {int(part) for part in text.splitlines()[2].split(": ", 1)[1].split(", ")}
    assert {child.id, state.id, link.id} <= header


def test_full_card_child_without_state_names_creation_and_link_only(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    parent = _create(lg, "it-parent", "ticket", "parent")
    child = _create(lg, "it-child", "ticket", "child")
    link = _append(
        lg, "item.linked", {"rel": "child-of", "target": "it-parent"}, "it-child", "l-child"
    )
    card = project_card(lg, "it-parent", full=True)
    lg.close()
    assert card.children[0]["status"] == "open"
    assert card.source_event_ids == tuple(sorted({parent.id, child.id, link.id}))


def test_full_card_child_linked_twice_names_the_resolved_link(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    parent = _create(lg, "it-parent", "ticket", "parent")
    _create(lg, "it-other", "ticket", "other")
    child = _create(lg, "it-child", "ticket", "child")
    first = _append(
        lg, "item.linked", {"rel": "child-of", "target": "it-parent"}, "it-child", "l-1"
    )
    moved = _append(
        lg, "item.linked",
        {"rel": "child-of", "target": "it-other", "moved_from": "it-parent"},
        "it-child", "l-2",
    )
    back = _append(
        lg, "item.linked",
        {"rel": "child-of", "target": "it-parent", "moved_from": "it-other"},
        "it-child", "l-3",
    )
    card = project_card(lg, "it-parent", full=True)
    other = project_card(lg, "it-other", full=True)
    lg.close()
    # v_items resolves the child's anchor from its latest child-of link.
    assert [row["item_id"] for row in card.children] == ["it-child"]
    assert card.source_event_ids == tuple(sorted({parent.id, child.id, back.id}))
    assert first.id not in card.source_event_ids
    assert moved.id not in card.source_event_ids
    assert other.children == ()
    assert child.id not in other.source_event_ids


def test_bounded_card_source_ids_unchanged_by_child_rows(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    parent, child, state, link = _child_ledger(lg)
    card = project_card(lg, "it-parent")
    unbounded = project_item(lg, "it-parent")
    lg.close()
    assert [row["item_id"] for row in card.children] == ["it-child"]
    assert card.source_event_ids == (parent.id,)
    assert unbounded.source_event_ids == (parent.id,)
    assert card.source_omitted == 0


def test_card_json_child_rows_keep_their_keys(tmp_path: Path, monkeypatch) -> None:
    db = str(_db(tmp_path))
    lg = open_ledger(db)
    _child_ledger(lg)
    lg.close()
    expected = [
        {"item_id": "it-child", "title": "child", "status": "claimed", "holder": "tester"}
    ]
    for argv in (["card", "it-parent"], ["card", "--full", "it-parent"]):
        code, out, err = invoke(monkeypatch, ["--json", "--db", db, *argv])
        assert code == 0, err
        rows = json.loads(out)["data"]["children"]
        assert [sorted(row) for row in rows] == [["holder", "item_id", "status", "title"]]
        assert rows == expected


def _append_after_events_read(lg, db, append):
    """Make ``append`` land through a second connection right after the
    projection's first ``events`` read, before any later read.

    The injection is the test-local ledger's own ``events`` method; the writer
    is another connection to the same file, as a concurrent process would be.
    """
    writer = open_ledger(db)
    read = lg.events
    landed = []

    def events(*args, **kwargs):
        result = read(*args, **kwargs)
        if not landed:
            landed.append(append(writer))
        return result

    lg.events = events
    return writer, landed


def test_full_card_child_link_race_reads_one_snapshot(tmp_path: Path) -> None:
    db = _db(tmp_path)
    lg = open_ledger(db)
    parent = _create(lg, "it-parent", "ticket", "parent")
    child = _create(lg, "it-child", "ticket", "child")
    writer, landed = _append_after_events_read(
        lg,
        db,
        lambda w: _append(
            w, "item.linked", {"rel": "child-of", "target": "it-parent"}, "it-child", "l-race"
        ),
    )
    card = project_card(lg, "it-parent", full=True)
    assert len(landed) == 1
    # The link committed mid-projection; the card is the state before it.
    assert card.revision_id == child.id
    assert card.children == ()
    assert card.source_event_ids == (parent.id,)
    assert max(card.source_event_ids) <= card.revision_id
    # A fresh projection sees the link, with every source in its own revision.
    del lg.events
    after = project_card(lg, "it-parent", full=True)
    writer.close()
    lg.close()
    assert [row["item_id"] for row in after.children] == ["it-child"]
    assert after.source_event_ids == tuple(sorted({parent.id, child.id, landed[0].id}))
    assert max(after.source_event_ids) <= after.revision_id == landed[0].id


def test_full_card_child_state_race_reads_one_snapshot(tmp_path: Path) -> None:
    db = _db(tmp_path)
    lg = open_ledger(db)
    parent = _create(lg, "it-parent", "ticket", "parent")
    child = _create(lg, "it-child", "ticket", "child")
    link = _append(
        lg, "item.linked", {"rel": "child-of", "target": "it-parent"}, "it-child", "l-child"
    )
    writer, landed = _append_after_events_read(
        lg,
        db,
        lambda w: _append(
            w, "item.state", {"status": "claimed", "owner": "tester"}, "it-child", "s-race"
        ),
    )
    card = project_card(lg, "it-parent", full=True)
    writer.close()
    lg.close()
    assert len(landed) == 1
    state = landed[0]
    # The child row and the ids agree: the row is the open child, not the
    # claimed one, and no id names the state event appended mid-projection.
    assert card.revision_id == link.id
    assert card.children == (
        {"item_id": "it-child", "title": "child", "status": "open", "holder": None},
    )
    assert card.source_event_ids == tuple(sorted({parent.id, child.id, link.id}))
    assert state.id not in card.source_event_ids
    assert max(card.source_event_ids) <= card.revision_id


def test_card_projection_keeps_a_callers_transaction(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _child_ledger(lg)
    conn = lg._conn
    # With no caller transaction, the projection ends the one it opened.
    project_card(lg, "it-parent", full=True)
    assert not conn.in_transaction
    conn.execute("BEGIN")
    full = project_card(lg, "it-parent", full=True)
    assert conn.in_transaction
    bounded = project_card(lg, "it-parent")
    assert conn.in_transaction
    project_item(lg, "it-parent", full=True)
    assert conn.in_transaction
    conn.execute("ROLLBACK")
    lg.close()
    assert [row["item_id"] for row in full.children] == ["it-child"]
    assert [row["item_id"] for row in bounded.children] == ["it-child"]


def test_card_projection_ends_its_snapshot_on_an_exception(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _child_ledger(lg)
    conn = lg._conn

    def broken(*args, **kwargs):
        assert conn.in_transaction
        raise OSError("events read failed")

    lg.events = broken
    with pytest.raises(OSError, match="events read failed"):
        project_card(lg, "it-parent", full=True)
    assert not conn.in_transaction
    with pytest.raises(OSError, match="events read failed"):
        project_item(lg, "it-parent")
    assert not conn.in_transaction
    lg.close()


def _append_after_item_read(lg, db, append):
    """Make ``append`` land through a second connection right after the
    caller's first ``item`` read, before any later read.

    The injection is the test-local ledger's own ``item`` method; the writer
    is another connection to the same file, as a concurrent process would be.
    """
    writer = open_ledger(db)
    read = lg.item
    landed = []

    def item(*args, **kwargs):
        row = read(*args, **kwargs)
        if not landed:
            landed.extend(append(writer))
        return row

    lg.item = item
    return writer, landed


def test_item_spec_moved_reads_one_snapshot(tmp_path: Path) -> None:
    db = _db(tmp_path)
    lg = open_ledger(db)
    _create(lg, "it-s", "ticket", "spec")
    set_spec(lg, "it-s", {"title": "T", "body_md": "one"}, harness="pytest")
    _append(
        lg,
        "item.state",
        {"status": "claimed", "owner": "tester", "spec_version": 1},
        "it-s",
        "s-claim",
    )
    # Before the writes: claimed against v1, and v1 is current.
    assert item_spec_moved(lg, "it-s") is None

    def release_then_move(w):
        release = _append(w, "item.state", {"status": "open"}, "it-s", "s-release")
        spec, created = set_spec(w, "it-s", {"title": "T", "body_md": "two"}, harness="pytest")
        assert created
        return [release, spec]

    writer, landed = _append_after_item_read(lg, db, release_then_move)
    moved = item_spec_moved(lg, "it-s")
    assert len(landed) == 2
    # Every state the store passed through says no line: claimed on v1 with
    # v1 current, then released, then released with v2 current. The claimed
    # row read before the writes with the v2 spec read after them would say
    # "spec moved: v1 → v2", which matches none of them.
    assert moved is None
    del lg.item
    assert item_spec_moved(lg, "it-s") is None
    assert lg.item("it-s")["status"] == "open"
    writer.close()
    lg.close()


def test_project_item_reads_its_row_in_the_snapshot(tmp_path: Path) -> None:
    db = _db(tmp_path)
    lg = open_ledger(db)
    created = _create(lg, "it-r", "ticket", "row")
    writer, landed = _append_after_item_read(
        lg,
        db,
        lambda w: [
            _append(w, "item.state", {"status": "claimed", "owner": "tester"}, "it-r", "s-race")
        ],
    )
    card = project_item(lg, "it-r", full=True)
    assert len(landed) == 1
    state = landed[0]
    # The claim committed right after the row read; the card is the state
    # before it, row and events alike.
    assert (card.status, card.owner) == ("open", None)
    assert card.revision_id == created.id
    assert card.source_event_ids == (created.id,)
    del lg.item
    after = project_item(lg, "it-r", full=True)
    assert (after.status, after.owner) == ("claimed", "tester")
    assert after.revision_id == state.id
    assert after.source_event_ids == (created.id, state.id)
    with pytest.raises(KeyError):
        project_item(lg, "it-missing")
    assert not lg._conn.in_transaction
    writer.close()
    lg.close()


def test_full_card_names_the_blocks_link_behind_a_blockers_row(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    created = _create(lg, "it-a", "ticket", "a")
    _create(lg, "it-b", "ticket", "b")
    link = _append(lg, "item.linked", {"rel": "blocks", "target": "it-a"}, "it-b", "l-b")
    state = _append(lg, "item.state", {"status": "claimed", "owner": "tester"}, "it-a", "s-a")
    # Only it-b's link names the blocker.
    card = project_card(lg, "it-a", full=True)
    assert card.blockers == ("it-b",)
    assert card.inbound_blockers == ("it-b",)
    assert "\n## Blockers\n- it-b\n" in format_card(card)
    assert link.id in card.source_event_ids
    assert card.source_event_ids == (created.id, link.id, state.id)
    # This item's own state names the blocker too: the link stays a source.
    named = _append(
        lg, "item.state", {"status": "blocked", "blocked_by": ["it-b"]}, "it-a", "s-named"
    )
    card = project_card(lg, "it-a", full=True)
    assert card.blockers == ("it-b",)
    assert card.inbound_blockers == ()
    assert card.source_event_ids == (created.id, link.id, named.id)
    # A later "blocked" state that does not restate the set carries it
    # forward; the state that named the blocker stays a source.
    carried = _append(lg, "item.state", {"status": "blocked"}, "it-a", "s-carried")
    card = project_card(lg, "it-a", full=True)
    assert card.blockers == ("it-b",)
    assert card.source_event_ids == (created.id, link.id, named.id, carried.id)
    # A closed blocker leaves the row, and its link leaves the sources.
    _append(lg, "item.state", {"status": "closed"}, "it-b", "s-b-closed")
    card = project_card(lg, "it-a", full=True)
    assert card.blockers == ()
    assert card.source_event_ids == (created.id, carried.id)
    lg.close()


def test_full_card_names_the_state_behind_a_named_blocker(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    created = _create(lg, "it-a", "ticket", "a")
    named = _append(
        lg, "item.state", {"status": "blocked", "blocked_by": ["it-x"]}, "it-a", "s-named"
    )
    carried = _append(lg, "item.state", {"status": "blocked"}, "it-a", "s-carried")
    card = project_card(lg, "it-a", full=True)
    assert card.blockers == ("it-x",)
    assert card.source_event_ids == (created.id, named.id, carried.id)
    lg.close()


def test_a_blockers_row_without_a_source_is_refused() -> None:
    sources = {"state_event_id": None, "link_event_ids": ()}
    with pytest.raises(RuntimeError, match="blocker it-b of it-a has no source event"):
        _blocker_source_ids("it-a", [("it-b", sources)])
    named = {"state_event_id": 4, "link_event_ids": (3, 5)}
    assert _blocker_source_ids("it-a", [("it-b", named)]) == {3, 4, 5}
