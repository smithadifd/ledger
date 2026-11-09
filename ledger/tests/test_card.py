"""Resume cards: revision, types, truncation, JSON, and --md path guard."""
from __future__ import annotations

import io
import json
import os
import random
import re
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ledger.card import (
    Card,
    _bound_ids,
    _key_step_ids,
    DROP_ORDER,
    WORD_LIMIT,
    footer_line,
    format_card,
    project_card,
    project_item,
    word_count,
)
from ledger.cli import main
from ledger.schema import ITEM_TYPES
from ledger.store import open_ledger
from ledger.verbs import EXIT_NOT_FOUND, EXIT_REFUSED, discover_verbs

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
    return next(line for line in text.splitlines() if line.startswith("  ask: "))


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
    text = format_card(card)
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
    assert "a599" not in text
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
    assert "a599" not in data["open_questions"][0]["ask"]


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
    item = next(row for row in lg.items() if row["item_id"] == "dc-1")
    full = project_item(lg, item)
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
    item = next(row for row in lg.items() if row["item_id"] == "dc-1")
    full = project_item(lg, item)
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
        item = next(row for row in lg.items() if row["item_id"] == "it")
        full = project_item(lg, item)
        card = project_card(lg, "it")
        text = format_card(card)
        where = f"seed={seed} case={case}"
        assert word_count(text) <= WORD_LIMIT, where
        assert len(card.source_event_ids) <= 40, where
        assert _shown_event_ids(text) <= set(_header_ids(text)), where
        assert card.next_step == full.next_step, where
        assert "next_step" not in card.truncated, where
        lg.close()
