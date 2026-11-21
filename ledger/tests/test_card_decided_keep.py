"""A busy card keeps decided: notes and lists them under Decisions as notes."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ledger.answers import first_election
from ledger.card import (
    SOURCE_ID_LIMIT,
    WORD_LIMIT,
    _one_line,
    format_card,
    project_card,
    word_count,
)
from ledger.search import DECIDED_PREFIX, decided_note_text
from ledger.store import Event, open_ledger
from ledger.tests.test_card import PAD, _append, _create, _db


PARENT = "parent-item"
RULING = "decided: keep the one store"


def _event(event_id: int, kind: str, item_id: str | None, payload: dict) -> Event:
    return Event(
        id=event_id,
        kind=kind,
        payload_version=1,
        payload=payload,
        item_id=item_id,
        idempotency_key=f"k{event_id}",
        actor=None,
        harness="fixture",
        model=None,
        session=None,
        channel=None,
        provenance_class=None,
        recorded_at="2026-11-20T09:00:00.000Z",
        prev_hash="",
        hash=f"h{event_id}",
    )


def _decisions_body(text: str) -> str:
    return text.split("\n## Decisions\n", 1)[1].split("\n## ", 1)[0]


def _notes_body(text: str) -> str:
    return text.split("\n## Notes\n", 1)[1].split("\n\n", 1)[0]


def _election_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if row.get("as") != "note"]


def test_long_decided_note_fits_budget_once(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, PARENT, "ticket", "quiet-parent")
    ruling_text = "decided: " + " ".join(["retain"] * 180)
    ruling = _append(lg, "item.note", {"text": ruling_text}, PARENT, "ruling-1")
    card = project_card(lg, PARENT)
    text = format_card(card)
    lg.close()
    assert ruling_text in _decisions_body(text)
    assert text.count(ruling_text) == 1
    assert _notes_body(text) == "(none)"
    assert card.truncated == ()
    assert word_count(text) <= WORD_LIMIT
    data = card.as_dict()
    assert data["notes"] == [{"event_id": ruling.id, "text": ruling_text}]
    assert data["decisions"] == [
        {"event_id": ruling.id, "as": "note", "text": ruling_text},
    ]


def test_over_budget_card_keeps_decided_notes_and_lists_them_under_decisions(
    tmp_path: Path,
) -> None:
    """A ruling on a busy parent item ranked past the default ten stays visible."""
    lg = open_ledger(_db(tmp_path))
    _create(lg, PARENT, "ticket", "busy-parent")
    for index in range(12):
        _append(
            lg,
            "item.note",
            {"text": f"note-{index} {PAD}"},
            PARENT,
            f"n-{index}",
        )
    ruling = _append(
        lg,
        "item.note",
        {"text": RULING},
        PARENT,
        "ruling-1",
    )
    card = project_card(lg, PARENT)
    text = format_card(card)
    lg.close()
    assert word_count(text) <= WORD_LIMIT
    assert "notes" in card.truncated
    assert "note-0 " not in text
    assert RULING in text
    assert ruling.id in {row["event_id"] for row in card.notes}
    body = _decisions_body(text)
    assert f"- event={ruling.id} note: {RULING}" in body
    assert "quote_ref=" not in body
    assert " -> " not in body


@pytest.mark.parametrize("full", [False, True])
def test_decided_prefix_progress_stays_under_progress(tmp_path: Path, full: bool) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, PARENT, "ticket", "quiet-parent")
    text = "decided: use the existing store"
    progress = _append(lg, "item.progress", {"text": text}, PARENT, "progress-1")
    card = project_card(lg, PARENT, full=full)
    rendered = format_card(card)
    lg.close()
    body = rendered.split("\n## Progress\n", 1)[1].split("\n\n", 1)[0]
    assert body == f"- event={progress.id}: {text}"
    assert _decisions_body(rendered) == "(none)\n"
    assert _notes_body(rendered) == "(none)"
    assert card.truncated == ()
    assert card.as_dict()["progress"] == [{"event_id": progress.id, "text": text}]


@pytest.mark.parametrize("full", [False, True])
def test_not_over_budget_notes_keep_event_order(tmp_path: Path, full: bool) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, PARENT, "ticket", "quiet-parent")
    first = _append(lg, "item.note", {"text": "alpha note"}, PARENT, "n-a")
    ruling = _append(lg, "item.note", {"text": RULING}, PARENT, "n-r")
    last = _append(lg, "item.note", {"text": "omega note"}, PARENT, "n-z")
    card = project_card(lg, PARENT, full=full)
    text = format_card(card)
    lg.close()
    assert card.truncated == ()
    notes = _notes_body(text)
    assert notes.splitlines() == [
        f"- event={first.id}: alpha note",
        f"- event={last.id}: omega note",
    ]
    assert f"- event={ruling.id} note: {RULING}" in _decisions_body(text)
    assert text.count(RULING) == 1


def test_a_decided_note_never_satisfies_a_pick_or_election_check(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, PARENT, "ticket", "child-item")
    asked = _append(
        lg,
        "decision.asked",
        {
            "question": "ship the change?",
            "options": [{"label": "yes"}, {"label": "no"}],
        },
        PARENT,
        "ask-1",
    )
    ruling = _append(lg, "item.note", {"text": RULING}, PARENT, "ruling-1")
    card = project_card(lg, PARENT)
    text = format_card(card)
    data = card.as_dict()
    assert first_election(lg._conn, asked.id) is None
    assert {row["asked_event_id"] for row in lg.open_decisions()} == {asked.id}
    assert card.decisions == ()
    assert _election_rows(data["decisions"]) == []
    for row in data["decisions"]:
        assert row.get("as") == "note"
        assert "chosen" not in row
        assert "quote_ref" not in row
        assert "asked_event_id" not in row
    assert data["decisions"] == [
        {"event_id": ruling.id, "as": "note", "text": RULING},
    ]
    assert "quote_ref=" not in _decisions_body(text)
    assert card.next_step == "Answer open questions before continuing."
    assert not card.next_step.startswith("Decided:")
    lg.close()


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize(
    "text",
    [
        "decided: keep the one store",
        "   decided: keep the one store",
        "\n\tdecided: keep the one store",
        "decided:keep the one store",
    ],
)
def test_accepted_decided_prefix_is_listed_under_decisions(
    tmp_path: Path, text: str, full: bool,
) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, PARENT, "ticket", "quiet-parent")
    note = _append(lg, "item.note", {"text": text}, PARENT, "n-1")
    card = project_card(lg, PARENT, full=full)
    rendered = format_card(card)
    lg.close()
    event = _event(note.id, "item.note", PARENT, {"text": text})
    assert decided_note_text(event) == text
    assert text.lstrip().startswith(DECIDED_PREFIX)
    assert f"- event={note.id} note: {_one_line(text)}" in _decisions_body(rendered)
    assert rendered.count(_one_line(text)) == 1
    assert _notes_body(rendered) == "(none)"


@pytest.mark.parametrize(
    "text",
    [
        "Decided: keep the one store",
        "DECIDED: keep the one store",
        "decided : keep the one store",
        "decided keep the one store",
        "- decided: keep the one store",
        "`decided:` keep the one store",
        "we decided: keep the one store",
        "undecided: which store",
    ],
)
def test_near_miss_prefix_is_not_a_decided_note_on_the_card(tmp_path: Path, text: str) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, PARENT, "ticket", "busy-parent")
    for index in range(12):
        _append(
            lg,
            "item.note",
            {"text": f"note-{index} {PAD}"},
            PARENT,
            f"n-{index}",
        )
    miss = _append(lg, "item.note", {"text": text}, PARENT, "miss-1")
    card = project_card(lg, PARENT)
    rendered = format_card(card)
    lg.close()
    event = _event(miss.id, "item.note", PARENT, {"text": text})
    assert decided_note_text(event) is None
    assert miss.id not in {row["event_id"] for row in card.notes}
    body = _decisions_body(rendered)
    assert "note:" not in body
    assert text not in rendered


def test_over_budget_drops_decided_notes_oldest_first_after_other_notes(
    tmp_path: Path,
) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, PARENT, "ticket", "busy-parent")
    ordinary = _append(
        lg,
        "item.note",
        {"text": f"bookkeeping {PAD}"},
        PARENT,
        "n-ord",
    )
    oldest = _append(
        lg,
        "item.note",
        {"text": f"decided: ruling-old {PAD}"},
        PARENT,
        "n-old",
    )
    newest = _append(
        lg,
        "item.note",
        {"text": f"decided: ruling-new {PAD}"},
        PARENT,
        "n-new",
    )
    extra = [
        _append(
            lg,
            "item.note",
            {"text": f"decided: ruling-{index:02d} {PAD}"},
            PARENT,
            f"n-d-{index}",
        )
        for index in range(20)
    ]
    card = project_card(lg, PARENT)
    text = format_card(card)
    lg.close()
    assert word_count(text) <= WORD_LIMIT
    assert "notes" in card.truncated
    kept = {row["event_id"] for row in card.notes}
    assert ordinary.id not in kept
    assert oldest.id not in kept
    assert newest.id in kept or extra[-1].id in kept
    assert max(kept) == extra[-1].id
    omitted = dict(card.omitted)
    assert omitted["notes"] >= 1


def test_id_cap_drops_ordinary_notes_before_decided(tmp_path: Path) -> None:
    lg = open_ledger(_db(tmp_path))
    _create(lg, PARENT, "ticket", "id-cap-parent")
    ruling = _append(lg, "item.note", {"text": RULING}, PARENT, "ruling-1")
    for index in range(SOURCE_ID_LIMIT + 5):
        _append(
            lg,
            "item.note",
            {"text": f"note-{index}"},
            PARENT,
            f"n-{index}",
        )
    card = project_card(lg, PARENT)
    lg.close()
    kept = {row["event_id"] for row in card.notes}
    assert ruling.id in kept
    assert len(card.source_event_ids) <= SOURCE_ID_LIMIT
