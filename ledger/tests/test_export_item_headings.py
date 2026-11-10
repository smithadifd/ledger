"""``export-item --ticket`` reads every ticket heading the roadmap importer reads.

The importer accepts two ticket-heading forms: a ``### Ticket:`` heading, with
or without one lead word (a ``[tag]`` or a lowercase slug) before the id, and a
bare ``### <ID>`` heading inside a ``## Tickets`` section. A ticket imported from
either form resolves to its section, by its ``rel:line@commit`` locator and by
its ``rel#ID`` anchor; a heading of any other shape is not a ticket heading, and
the view refuses it by name rather than guess a section.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ledger.export import anchor_ticket_line, ticket_section, ticket_heading
from ledger.store import open_ledger

LIB = Path(__file__).resolve().parents[2]
NOT_FOUND = "row_source_anchor_not_found"
AMBIGUOUS = "row_source_anchor_ambiguous"
SECTION_NOT_FOUND = "ticket section not found"

# --- the roadmap fixture: one block per heading shape ------------------------

SL12 = "### Ticket: [bridge] SL12 — tagged title\n- Prompt: twelve.\n\n"
SL14 = "### Ticket: [two words] SL14: a bracket tag with a space\n- Prompt: fourteen.\n\n"
SL15 = "### Ticket: bridge SL15 — a slug lead word\n- Prompt: fifteen.\n\n"
SL16 = "### Ticket: ledger-repo SL16 - a hyphenated slug\n- Prompt: sixteen.\n\n"
SL17 = "### Ticket: [a] [b] SL17 — two lead words\n- Prompt: seventeen.\n\n"
NODIGIT = "### Ticket: NODIGIT – a prefixed id needs no digit\n- Prompt: nd.\n\n"
RM25 = "### Ticket: RM-25 — reserved prefix\n- Prompt: rm.\n\n"
OUT1 = "### OUT1 — bare outside a Tickets section\n- Prompt: out.\n\n"
SL13 = (
    "### SL13 — bare title\n- Prompt: thirteen.\n\n"
    "#### detail\n- still SL13\n\n"
)
SL18 = "### SL18: colon title\n- Prompt: eighteen.\n\n"
SL19 = "### SL19\n- Prompt: nineteen.\n\n"
SL20 = (
    "### SL20 – en dash title\n- Prompt: twenty.\n\n"
    "```text\n### SL26 — fenced inside SL20\n### Ticket: SL27 — fenced too\n```\n\n"
)
SL21 = "### [bridge] SL21 — a lead word on a bare heading\n- Prompt: 21.\n\n"
SL22 = "### sl22 — lowercase id\n- Prompt: 22.\n\n"
SL = "### SL — a bare id needs a digit\n- Prompt: sl.\n\n"
SL23 = "### SL23 missing separator\n- Prompt: 23.\n\n"
RM24 = "### RM-24 — reserved prefix\n- Prompt: 24.\n\n"
DUP2_BARE = "### DUP2 — bare copy\n- a\n\n"
DUP2_PREFIXED = "### Ticket: DUP2 — prefixed copy\n- b\n\n"
OUT2 = "### OUT2 — after the Tickets section closed\n- Prompt: out2.\n\n"
OUT3 = "### OUT3 — after a level-one heading\n- Prompt: out3.\n"

ROADMAP = (
    "# Roadmap\n\n"
    + SL12 + SL14 + SL15 + SL16 + SL17 + NODIGIT + RM25 + OUT1
    + "## Tickets\n\n"
    + SL13 + SL18 + SL19 + SL20 + SL21 + SL22 + SL + SL23 + RM24
    + DUP2_BARE + DUP2_PREFIXED
    + "## Later\n\n"
    + OUT2
    + "## Tickets\n\n"
    + "# Top\n\n"
    + OUT3
)

# Section-name edge cases: the section heading is level two and exactly
# ``Tickets`` once stripped.
SL30 = "### SL30 — under a section heading with trailing spaces\n- Prompt: 30.\n\n"
SL31 = "### SL31 — under a lowercase section heading\n- Prompt: 31.\n\n"
SL32 = "### SL32 — under a level-three Tickets heading\n- Prompt: 32.\n\n"
SL33 = "### SL33 — under a level-one Tickets heading\n- Prompt: 33.\n"
SECTIONS = (
    "## Tickets  \n\n" + SL30
    + "## tickets\n\n" + SL31
    + "## Other\n\n### Tickets\n\n" + SL32
    + "# Tickets\n\n" + SL33
)


def _line_of(text: str, block: str) -> int:
    """The 1-indexed line where ``block`` starts in ``text``; it occurs once."""
    assert text.count(block) == 1, block
    return text[: text.index(block)].count("\n") + 1


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, text=True, capture_output=True,
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    (root / "roadmaps").mkdir(parents=True)
    (root / "roadmaps" / "t.md").write_text(ROADMAP, encoding="utf-8")
    (root / "roadmaps" / "s.md").write_text(SECTIONS, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root, _git(root, "rev-parse", "HEAD").strip()


def _append(db: Path, item_id: str, kind: str, payload: dict, key: str) -> None:
    lg = open_ledger(db)
    try:
        lg.append(
            kind=kind, payload=payload, item_id=item_id, idempotency_key=key,
            harness="pytest", actor="tester",
        )
    finally:
        lg.close()


def _item(db: Path, item_id: str, source: str, *notes: str) -> None:
    _append(
        db, item_id, "item.created",
        {"type": "ticket", "title": "A ticket", "source": source}, f"create-{item_id}",
    )
    for number, text in enumerate(notes):
        _append(db, item_id, "item.note", {"text": text}, f"note-{item_id}-{number}")


def _run(db: Path, repo: Path, item_id: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(LEDGER_HARNESS="pytest", LEDGER_AGENT="tester", PYTHONPATH=str(LIB))
    return subprocess.run(
        [sys.executable, "-m", "ledger.cli", "--json", "--db", str(db),
         "export-item", "--ticket", item_id],
        cwd=repo, env=env, text=True, capture_output=True,
    )


def _ticket(db: Path, repo: Path, item_id: str) -> str:
    result = _run(db, repo, item_id)
    assert result.returncode == 0, (result.stdout, result.stderr)
    return json.loads(result.stdout)["data"]["ticket"]


def _refusal(db: Path, repo: Path, item_id: str) -> str:
    result = _run(db, repo, item_id)
    assert result.returncode != 0, result.stdout
    assert "Traceback" not in result.stderr, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False, payload
    return payload["error"]["message"]


# --- export-item --ticket, end to end ------------------------------------------


def test_tagged_ticket_heading_prints_by_anchor(tmp_path, repo):
    root, _sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "tg-anchor", "row:SL12", "also at roadmaps/t.md#SL12")

    assert _ticket(db, root, "tg-anchor") == SL12


def test_tagged_ticket_heading_prints_by_line_locator(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "tg-line", f"roadmaps/t.md:{_line_of(ROADMAP, SL12)}@{sha}")

    assert _ticket(db, root, "tg-line") == SL12


def test_bare_heading_inside_tickets_prints_by_anchor(tmp_path, repo):
    root, _sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "br-anchor", "row:SL13", "also at roadmaps/t.md#SL13")

    assert _ticket(db, root, "br-anchor") == SL13


def test_bare_heading_inside_tickets_prints_by_line_locator(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "br-line", f"roadmaps/t.md:{_line_of(ROADMAP, SL13)}@{sha}")

    assert _ticket(db, root, "br-line") == SL13


def test_bare_heading_outside_tickets_is_refused_by_anchor(tmp_path, repo):
    root, _sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "out-anchor", "row:OUT1", "also at roadmaps/t.md#OUT1")

    message = _refusal(db, root, "out-anchor")

    assert message.startswith(f"{NOT_FOUND}: "), message


def test_bare_heading_outside_tickets_is_refused_by_line_locator(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "out-line", f"roadmaps/t.md:{_line_of(ROADMAP, OUT1)}@{sha}")

    assert SECTION_NOT_FOUND in _refusal(db, root, "out-line")


def test_bare_and_prefixed_headings_for_one_id_are_ambiguous(tmp_path, repo):
    root, _sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "dup-anchor", "row:DUP2", "also at roadmaps/t.md#DUP2")

    message = _refusal(db, root, "dup-anchor")

    assert message.startswith(f"{AMBIGUOUS}: "), message
    assert "bare copy" not in message and "prefixed copy" not in message


# --- the grammar, heading by heading ------------------------------------------


@pytest.mark.parametrize(
    "ticket_id, block",
    [
        ("SL12", SL12),        # Ticket: with a [tag] lead word
        ("SL14", SL14),        # a [tag] holding a space, then a ``: `` title
        ("SL15", SL15),        # a lowercase slug lead word
        ("SL16", SL16),        # a hyphenated slug lead word, hyphen separator
        ("NODIGIT", NODIGIT),  # a prefixed id needs no digit; en dash separator
        ("SL13", SL13),        # bare, em dash; the level-four heading stays inside
        ("SL18", SL18),        # bare, ``: `` separator
        ("SL19", SL19),        # bare id alone
        ("SL20", SL20),        # bare, en dash; a fenced heading stays inside
    ],
)
def test_importer_ticket_headings_are_found(ticket_id, block):
    line = anchor_ticket_line(ROADMAP, ticket_id)

    assert line == _line_of(ROADMAP, block)
    assert ticket_section(ROADMAP, line) == block


@pytest.mark.parametrize(
    "ticket_id",
    [
        "SL17",   # two lead words: the importer reads no id
        "RM-25",  # RM- is reserved, also after Ticket:
        "OUT1",   # bare, but no Tickets section above it yet
        "SL21",   # a lead word on a bare heading
        "sl22",   # lowercase id
        "SL",     # a bare id needs a digit
        "SL23",   # no separator before the title
        "RM-24",  # RM- is reserved
        "SL26",   # a bare heading inside a code fence
        "SL27",   # a prefixed heading inside a code fence
        "OUT2",   # the Tickets section ended at ``## Later``
        "OUT3",   # a level-one heading ends a Tickets section
    ],
)
def test_other_headings_are_not_found(ticket_id):
    assert anchor_ticket_line(ROADMAP, ticket_id) is None


@pytest.mark.parametrize(
    "ticket_id, found",
    [
        ("SL30", True),    # ``## Tickets`` with trailing spaces is the section
        ("SL31", False),   # ``## tickets``: the name is case-sensitive
        ("SL32", False),   # ``### Tickets`` is not a section heading
        ("SL33", False),   # ``# Tickets`` is not a section heading
    ],
)
def test_tickets_section_heading_is_level_two_and_exact(ticket_id, found):
    line = anchor_ticket_line(SECTIONS, ticket_id)

    assert (line is not None) is found


def test_ambiguous_ids_raise_by_name():
    with pytest.raises(ValueError, match=f"^{AMBIGUOUS}: 2 ticket headings for DUP2$"):
        anchor_ticket_line(ROADMAP, "DUP2")


@pytest.mark.parametrize(
    "block",
    [OUT1, SL21, SL22, SL, SL23, RM24, OUT2, OUT3],
)
def test_a_line_on_a_non_ticket_heading_is_refused(block):
    with pytest.raises(ValueError, match=f"^{SECTION_NOT_FOUND}$"):
        ticket_section(ROADMAP, _line_of(ROADMAP, block))


def test_a_prefixed_heading_with_no_id_still_shows_by_line():
    # The importer reads ``### Ticket:`` with any tail as a ticket heading (it
    # refuses one with no id at import); the line view shows it as before.
    for block in (SL17, RM25):
        assert ticket_section(ROADMAP, _line_of(ROADMAP, block)) == block


def test_a_line_inside_a_ticket_body_resolves_to_that_ticket():
    detail = _line_of(ROADMAP, "#### detail\n")
    fenced = _line_of(ROADMAP, "### SL26 — fenced inside SL20\n")

    assert ticket_section(ROADMAP, detail) == SL13
    assert ticket_section(ROADMAP, fenced) == SL20


def test_a_line_under_a_section_heading_is_not_the_ticket_above():
    later = _line_of(ROADMAP, "## Later\n")

    with pytest.raises(ValueError, match=f"^{SECTION_NOT_FOUND}$"):
        ticket_section(ROADMAP, later)
    with pytest.raises(ValueError, match=f"^{SECTION_NOT_FOUND}$"):
        ticket_section(ROADMAP, later + 1)


@pytest.mark.parametrize(
    "text, in_tickets, expected",
    [
        ("Ticket: [bridge] SL12 — t", False, (True, "SL12")),
        ("ticket: SL12", False, (True, "SL12")),          # the prefix is any case
        ("TICKET:SL12", False, (True, "SL12")),           # no space after the colon
        ("Ticket: SL12a: t", False, (True, "SL12a")),
        ("Ticket: CSF3-9-H1 - t", False, (True, "CSF3-9-H1")),
        ("Ticket: [bridge]SL12", False, (True, "")),      # no space after the tag
        ("Ticket: Bridge SL12", False, (True, "")),       # a capitalised lead word
        ("Ticket: SL12 t", False, (True, "")),            # no separator
        ("Ticket:", False, (True, "")),
        ("Ticket : SL12", False, (False, "")),            # a space before the colon
        ("SL13 — t", True, (True, "SL13")),
        ("SL13 — t", False, (False, "")),
        ("SL13 -t", True, (False, "")),                   # an unspaced separator
        ("SL13 — ", True, (False, "")),              # a separator and no title
        ("SL13:t", True, (False, "")),
        ("SL--13", True, (False, "")),
    ],
)
def test_ticket_heading_grammar(text, in_tickets, expected):
    assert ticket_heading(text, in_tickets=in_tickets) == expected
