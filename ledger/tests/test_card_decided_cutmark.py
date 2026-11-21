"""A cut decided: note is reported under Decisions, an ordinary note under Notes."""
from __future__ import annotations

from pathlib import Path

from ledger.card import (
    SOURCE_ID_LIMIT,
    WORD_LIMIT,
    format_card,
    project_card,
    recover_command,
    word_count,
)
from ledger.store import open_ledger
from ledger.tests.test_card import PAD, _append, _create, _db

PARENT = "parent-item"
MARKER = "(+{count} cut; ledger export-item --json -- parent-item)"


def _body(text: str, heading: str) -> str:
    return text.split(f"\n## {heading}\n", 1)[1].split("\n\n", 1)[0]


def _truncated_line(text: str) -> str:
    return text.split("\n## Truncated\n", 1)[1].split("\n", 1)[0]


def _cut_entry(section: str, count: int) -> dict:
    return {
        "section": section,
        "count": count,
        "unit": "rows",
        "command": recover_command(PARENT),
    }


def test_only_decided_note_over_budget_is_cut_under_decisions(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, PARENT, "ticket", "quiet-parent")
    ruling_text = "decided: " + " ".join(["retain"] * (WORD_LIMIT + 50))
    _append(lg, "item.note", {"text": ruling_text}, PARENT, "ruling-1")
    card = project_card(lg, PARENT)
    text = format_card(card)
    lg.close()
    assert word_count(text) <= WORD_LIMIT
    assert card.truncated == ("decisions",)
    assert dict(card.omitted) == {"decisions": 1}
    assert _body(text, "Decisions") == MARKER.format(count=1)
    assert _body(text, "Notes") == "(none)"
    assert _truncated_line(text) == "dropped: decisions"
    data = card.as_dict()
    assert data["truncated"] == ["decisions"]
    assert data["cuts"] == [_cut_entry("decisions", 1)]


def test_cut_ordinary_and_decided_notes_each_get_a_marker_under_their_heading(
    tmp_path: Path,
) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, PARENT, "ticket", "busy-parent")
    ordinary = [
        _append(lg, "item.note", {"text": f"note-{index} {PAD}"}, PARENT, f"n-{index}")
        for index in range(6)
    ]
    big = " ".join(["retain"] * 150)
    decided = [
        _append(lg, "item.note", {"text": f"decided: ruling-{index} {big}"}, PARENT, f"d-{index}")
        for index in range(4)
    ]
    card = project_card(lg, PARENT)
    text = format_card(card)
    lg.close()
    assert word_count(text) <= WORD_LIMIT
    kept = {row["event_id"] for row in card.notes}
    assert not kept & {event.id for event in ordinary}
    cut_decided = [event for event in decided if event.id not in kept]
    assert cut_decided and len(cut_decided) < len(decided)
    # Oldest decided notes go first.
    assert [event.id for event in cut_decided] == [event.id for event in decided[: len(cut_decided)]]
    assert _body(text, "Notes") == MARKER.format(count=len(ordinary))
    decisions_body = _body(text, "Decisions")
    assert decisions_body.splitlines()[-1] == MARKER.format(count=len(cut_decided))
    assert _truncated_line(text) == "dropped: notes, decisions"
    assert dict(card.omitted) == {"notes": len(ordinary), "decisions": len(cut_decided)}
    assert card.as_dict()["cuts"] == [
        _cut_entry("decisions", len(cut_decided)),
        _cut_entry("notes", len(ordinary)),
    ]


def test_id_cap_reports_cut_decided_and_ordinary_notes_under_their_own_headings(
    tmp_path: Path,
) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, PARENT, "ticket", "id-cap-parent")
    ordinary = [
        _append(lg, "item.note", {"text": f"note-{index}"}, PARENT, f"n-{index}")
        for index in range(3)
    ]
    decided = [
        _append(lg, "item.note", {"text": f"decided: ruling-{index}"}, PARENT, f"d-{index}")
        for index in range(SOURCE_ID_LIMIT + 5)
    ]
    card = project_card(lg, PARENT)
    text = format_card(card)
    lg.close()
    assert len(card.source_event_ids) <= SOURCE_ID_LIMIT
    kept = {row["event_id"] for row in card.notes}
    assert not kept & {event.id for event in ordinary}
    cut_decided = [event for event in decided if event.id not in kept]
    assert cut_decided
    # Every cut row is reported exactly once, against its own section.
    assert dict(card.omitted) == {"notes": len(ordinary), "decisions": len(cut_decided)}
    assert _body(text, "Notes") == MARKER.format(count=len(ordinary))
    assert _body(text, "Decisions").splitlines()[-1] == MARKER.format(count=len(cut_decided))
    assert _truncated_line(text) == "dropped: notes, decisions"


def test_id_cap_with_only_decided_notes_never_marks_notes(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, PARENT, "ticket", "id-cap-parent")
    decided = [
        _append(lg, "item.note", {"text": f"decided: ruling-{index}"}, PARENT, f"d-{index}")
        for index in range(SOURCE_ID_LIMIT + 5)
    ]
    card = project_card(lg, PARENT)
    text = format_card(card)
    lg.close()
    kept = {row["event_id"] for row in card.notes}
    cut = len([event for event in decided if event.id not in kept])
    assert cut >= 5
    assert card.truncated == ("decisions",)
    assert dict(card.omitted) == {"decisions": cut}
    assert _body(text, "Notes") == "(none)"
    assert _body(text, "Decisions").splitlines()[-1] == MARKER.format(count=cut)
    assert _truncated_line(text) == "dropped: decisions"
