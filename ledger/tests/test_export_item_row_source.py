"""``export-item --ticket`` on an item whose source is a row key.

An item minted from a roadmap row has ``row:<ID>`` as its created source, not
a ``path:line@commit`` locator. Its ticket is read at the locator in its newest
valid ``also at <locator>`` note. With no such note the view refuses by name
(``row_source_no_also_at``) and reads nothing. A source that is a locator is
read as it always was, and the write-side check ``valid_source_locator`` is
unchanged.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ledger.export import valid_source_locator
from ledger.store import open_ledger

# The folder that holds the ``ledger`` package; the CLI runs from it as
# ``python -m ledger.cli``.
LIB = Path(__file__).resolve().parents[2]
SHA = "0123456789abcdef0123456789abcdef01234567"

TICKET_A = "### Ticket: SL1 — A ticket\n- Prompt: one.\n\n"
TICKET_B = "### Ticket: SL2 — B ticket\n- Prompt: two.\n\n"
REFUSAL = "row_source_no_also_at"
NOT_FOUND = "row_source_anchor_not_found"
AMBIGUOUS = "row_source_anchor_ambiguous"

AB1 = "### Ticket: AB1 \u2014 First thing\n- Prompt: one.\n\n"
AB2 = "### Ticket: AB2: Second thing\n- Prompt: two.\n\n"
AB7 = "### Ticket: AB7 - Hyphen title\n- Prompt: seven.\n\n"
AB8 = "### Ticket: AB8\n- Prompt: eight.\n"
# The roadmap an importer-shaped ``also at roadmaps/x.md#ID`` note points into.
ROADMAP = (
    "# Roadmap\n\n"
    + AB1
    + AB2
    + "### AB3 \u2014 bare heading\n- Prompt: bare.\n\n"
    + "| AB4 | a table row |\n\n"
    + "```text\n### Ticket: AB5 \u2014 inside a fence\n```\n\n"
    + "### Ticket: DUP1 \u2014 first copy\n- a\n\n"
    + "### Ticket: DUP1 \u2014 second copy\n- b\n\n"
    + AB7
    + AB8
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, text=True, capture_output=True,
    ).stdout


@pytest.fixture()
def repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.md").write_text(TICKET_A + "## Later\nnot it\n", encoding="utf-8")
    (root / "c.md").write_text("no ticket here\n", encoding="utf-8")
    (root / "b.md").write_text(TICKET_B + "## Later\nnot it\n", encoding="utf-8")
    # A file literally named ``row``: ``row:1@<sha>`` is a locator, not a row key.
    (root / "row").write_text("### Ticket: ROW — file named row\n- Prompt: r.\n\n## X\n", encoding="utf-8")
    (root / "roadmaps").mkdir()
    (root / "roadmaps" / "x.md").write_text(ROADMAP, encoding="utf-8")
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


def _item(db: Path, item_id: str, source: str, notes: tuple[str, ...] = ()) -> None:
    _append(
        db, item_id, "item.created",
        {"type": "ticket", "title": "A ticket", "source": source}, f"create-{item_id}",
    )
    for number, text in enumerate(notes):
        _append(db, item_id, "item.note", {"text": text}, f"note-{item_id}-{number}")


def _run(db: Path, repo: Path, item_id: str, *flags: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(LEDGER_HARNESS="pytest", LEDGER_AGENT="tester", PYTHONPATH=str(LIB))
    return subprocess.run(
        [sys.executable, "-m", "ledger.cli", *flags, "--db", str(db),
         "export-item", "--ticket", item_id],
        cwd=repo, env=env, text=True, capture_output=True,
    )


def _ticket(db: Path, repo: Path, item_id: str) -> str:
    result = _run(db, repo, item_id, "--json")
    assert result.returncode == 0, (result.stdout, result.stderr)
    return json.loads(result.stdout)["data"]["ticket"]


def _refusal(db: Path, repo: Path, item_id: str) -> str:
    """Run the view, assert it failed cleanly, and return the error message."""
    result = _run(db, repo, item_id, "--json")
    assert result.returncode != 0, result.stdout
    assert "Traceback" not in result.stderr, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is False, payload
    return payload["error"]["message"]


def test_row_source_with_an_also_at_note_prints_its_ticket(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "rw-ok", "row:SL1", (f"also at a.md:1@{sha}",))

    assert _ticket(db, root, "rw-ok") == TICKET_A


def test_row_source_ticket_prints_as_text_too(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "rw-text", "row:SL1", (f"also at a.md:1@{sha}",))

    result = _run(db, root, "rw-text")

    assert result.returncode == 0, result.stderr
    assert result.stdout.rstrip("\n") == TICKET_A.rstrip("\n"), result.stdout


def test_row_source_without_a_note_is_refused_by_name(tmp_path, repo):
    root, _sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "rw-none", "row:SL1")

    message = _refusal(db, root, "rw-none")

    assert message.startswith(f"{REFUSAL}: "), message
    assert "invalid source locator" not in message, message


def test_newest_also_at_note_wins(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "rw-two", "row:SL1", (f"also at a.md:1@{sha}", f"also at b.md:1@{sha}"))

    assert _ticket(db, root, "rw-two") == TICKET_B


def test_older_note_does_not_win_over_a_newer_one(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "rw-rev", "row:SL1", (f"also at b.md:1@{sha}", f"also at a.md:1@{sha}"))

    assert _ticket(db, root, "rw-rev") == TICKET_A


def test_invalid_newest_note_falls_back_to_the_previous_valid_one(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(
        db, "rw-fall", "row:SL1",
        (f"also at b.md:1@{sha}", "also at roadmaps/x.md#1SL", "also at ../a.md:1@" + sha),
    )

    assert _ticket(db, root, "rw-fall") == TICKET_B


def test_only_invalid_notes_is_refused_by_name(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(
        db, "rw-bad", "row:SL1",
        ("also at roadmaps/x.md#", f"also at a.md:0@{sha}", "also at a.md:1@--output=/x"),
    )

    assert _refusal(db, root, "rw-bad").startswith(f"{REFUSAL}: ")


@pytest.mark.parametrize(
    "template",
    [
        "Also at a.md:1@{sha}",          # other case
        "ALSO AT a.md:1@{sha}",
        "also at  a.md:1@{sha}",         # extra space
        "also at a.md:1@{sha} ",         # trailing space
        "also at a.md:1@{sha}\n",        # trailing newline
        "also at\ta.md:1@{sha}",         # tab for the space
        "- also at a.md:1@{sha}",        # bullet
        "`also at a.md:1@{sha}`",        # code span
        "```\nalso at a.md:1@{sha}\n```",  # fence
        " also at a.md:1@{sha}",         # leading space
        "also at a.md:1@{sha}\nmore",    # extra line
        "also at a\nb.md:1@{sha}",      # line break inside the path
        "also at a\x01b.md:1@{sha}",    # control character inside the path
        "also at a\x7fb.md:1@{sha}",    # DEL inside the path
        "also at ",                      # missing locator
        "also at",
        "at a.md:1@{sha}",               # missing part
        "a.md:1@{sha}",                  # no prefix
    ],
)
def test_near_miss_note_text_is_not_an_also_at_note(tmp_path, repo, template):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "rw-near", "row:SL1", (template.format(sha=sha),))

    assert _refusal(db, root, "rw-near").startswith(f"{REFUSAL}: ")


def test_near_miss_newest_note_is_skipped_for_an_earlier_exact_one(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(
        db, "rw-skip", "row:SL1",
        (f"also at a.md:1@{sha}", f"Also at b.md:1@{sha}", f"also at  b.md:1@{sha}"),
    )

    assert _ticket(db, root, "rw-skip") == TICKET_A


def test_another_event_kind_with_also_at_text_is_not_a_note(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "rw-kind", "row:SL1")
    _append(db, "rw-kind", "item.progress", {"text": f"also at a.md:1@{sha}"}, "prog-1")

    assert _refusal(db, root, "rw-kind").startswith(f"{REFUSAL}: ")


def test_a_note_on_another_item_is_not_read(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "rw-mine", "row:SL1")
    _item(db, "rw-other", "row:SL2", (f"also at b.md:1@{sha}",))

    assert _refusal(db, root, "rw-mine").startswith(f"{REFUSAL}: ")


def test_a_dirty_note_locator_is_refused_not_replaced_by_an_older_note(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "rw-dirty", "row:SL1", (f"also at a.md:1@{sha}", f"also at b.md:1@{sha}+dirty"))

    message = _refusal(db, root, "rw-dirty")

    assert "recorded from uncommitted text; the committed copy may differ" in message
    assert "SL1" not in message


def test_an_unreadable_note_locator_is_refused_not_guessed(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "rw-gone", "row:SL1", (f"also at a.md:1@{sha}", f"also at missing.md:1@{sha}"))

    assert "source not found" in _refusal(db, root, "rw-gone")


def test_a_note_line_with_no_ticket_heading_is_refused(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "rw-line", "row:SL1", (f"also at c.md:1@{sha}",))

    assert "ticket section not found" in _refusal(db, root, "rw-line")


def test_locator_source_is_read_from_its_source_not_from_a_note(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "lc-ok", f"a.md:1@{sha}", (f"also at b.md:1@{sha}",))

    assert _ticket(db, root, "lc-ok") == TICKET_A


def test_a_locator_whose_path_is_row_is_still_a_locator(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "lc-row", f"row:1@{sha}", (f"also at b.md:1@{sha}",))

    assert _ticket(db, root, "lc-row").startswith("### Ticket: ROW ")


@pytest.mark.parametrize("source", ["row:", "row:A B", "row:A\tB", "row:A\x01B", "row:A\x7fB", "other:SL1", "SL1"])
def test_other_sources_keep_the_invalid_locator_refusal(tmp_path, repo, source):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _item(db, "rw-oth", source, (f"also at a.md:1@{sha}",))

    message = _refusal(db, root, "rw-oth")

    assert "invalid source locator" in message, message


def test_row_key_is_still_not_a_valid_write_side_locator():
    assert not valid_source_locator("row:SL1")
    assert valid_source_locator(f"a.md:1@{SHA}")


# --- the anchor form ``rel#ID`` the importer and row-item.sh write -------------


def _anchor_item(db: Path, item_id: str, *notes: str) -> None:
    _item(db, item_id, "row:AB", notes)


def test_importer_shaped_anchor_note_prints_that_tickets_section(tmp_path, repo):
    root, _sha = repo
    db = tmp_path / "ledger.db"
    _anchor_item(db, "an-ok", "also at roadmaps/x.md#AB1")

    assert _ticket(db, root, "an-ok") == AB1


@pytest.mark.parametrize(
    "ticket_id, expected",
    [("AB2", AB2), ("AB7", AB7), ("AB8", AB8)],
)
def test_anchor_heading_forms_are_found(tmp_path, repo, ticket_id, expected):
    root, _sha = repo
    db = tmp_path / "ledger.db"
    _anchor_item(db, "an-form", f"also at roadmaps/x.md#{ticket_id}")

    assert _ticket(db, root, "an-form") == expected


@pytest.mark.parametrize(
    "ticket_id",
    [
        "AB9",    # absent
        "AB",     # a prefix of AB1 is not AB1
        "ab1",    # ids are case-sensitive
        "AB3",    # a bare ``### AB3`` heading belongs to the importer's other form
        "AB4",    # a table row
        "AB5",    # a heading inside a code fence
    ],
)
def test_anchor_id_not_found_is_refused_by_name(tmp_path, repo, ticket_id):
    root, _sha = repo
    db = tmp_path / "ledger.db"
    _anchor_item(db, "an-miss", f"also at roadmaps/x.md#{ticket_id}")

    message = _refusal(db, root, "an-miss")

    assert message.startswith(f"{NOT_FOUND}: "), message


def test_repeated_anchor_heading_is_refused_not_guessed(tmp_path, repo):
    root, _sha = repo
    db = tmp_path / "ledger.db"
    _anchor_item(db, "an-dup", "also at roadmaps/x.md#DUP1")

    message = _refusal(db, root, "an-dup")

    assert message.startswith(f"{AMBIGUOUS}: "), message
    assert "first copy" not in message and "second copy" not in message


def test_anchor_reads_head_not_the_working_tree(tmp_path, repo):
    root, _sha = repo
    path = root / "roadmaps" / "x.md"
    path.write_text(ROADMAP + "### Ticket: NEW1 \u2014 not committed\n- n\n", encoding="utf-8")
    db = tmp_path / "ledger.db"
    _anchor_item(db, "an-head", "also at roadmaps/x.md#NEW1")

    assert _refusal(db, root, "an-head").startswith(f"{NOT_FOUND}: ")


def test_anchor_in_a_file_missing_at_head_is_refused(tmp_path, repo):
    root, _sha = repo
    db = tmp_path / "ledger.db"
    _anchor_item(db, "an-nofile", "also at roadmaps/gone.md#AB1")

    assert "source not found" in _refusal(db, root, "an-nofile")


def test_anchor_outside_a_repository_is_refused(tmp_path, repo):
    _root, _sha = repo
    elsewhere = tmp_path / "norepo"
    elsewhere.mkdir()
    db = tmp_path / "ledger.db"
    _anchor_item(db, "an-norepo", "also at roadmaps/x.md#AB1")

    assert "source not found" in _refusal(db, elsewhere, "an-norepo")


def test_newest_note_wins_across_both_forms(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _anchor_item(db, "an-new-loc", "also at roadmaps/x.md#AB1", f"also at b.md:1@{sha}")
    _anchor_item(db, "an-new-anc", f"also at b.md:1@{sha}", "also at roadmaps/x.md#AB1")

    assert _ticket(db, root, "an-new-loc") == TICKET_B
    assert _ticket(db, root, "an-new-anc") == AB1


def test_a_note_in_neither_form_is_skipped_for_an_earlier_anchor(tmp_path, repo):
    root, sha = repo
    db = tmp_path / "ledger.db"
    _anchor_item(
        db, "an-skip", "also at roadmaps/x.md#AB2", "also at roadmaps/x.md@AB1", "also at AB1"
    )

    assert _ticket(db, root, "an-skip") == AB2


@pytest.mark.parametrize(
    "text",
    [
        "also at roadmaps/x.md#",            # missing id
        "also at #AB1",                      # missing path
        "also at ../x.md#AB1",               # path leaves the repository
        "also at /roadmaps/x.md#AB1",        # absolute path
        "also at roadmaps/x.md#1AB",         # id must start with a letter
        "also at roadmaps/x.md#AB 1",        # whitespace in the id
        "also at roadmaps/x.md# AB1",
        "also at roadmaps/x.md#AB1 ",        # trailing space
        "also at  roadmaps/x.md#AB1",        # extra space
        "also at roadmaps/x.md#AB1\n",       # trailing newline
        "also at roadmaps/x.md#AB1#AB2x ",   # trailing junk
        "Also at roadmaps/x.md#AB1",         # other case
        "- also at roadmaps/x.md#AB1",       # bullet
        "roadmaps/x.md#AB1",                 # no prefix
        "also at roadmaps/x\x01.md#AB1",     # control character
    ],
)
def test_near_miss_anchor_note_is_not_an_also_at_note(tmp_path, repo, text):
    root, _sha = repo
    db = tmp_path / "ledger.db"
    _anchor_item(db, "an-near", text)

    assert _refusal(db, root, "an-near").startswith(f"{REFUSAL}: ")


def test_anchor_form_is_still_not_a_valid_write_side_locator():
    assert not valid_source_locator("roadmaps/x.md#AB1")


# --- a backtick opener whose info string holds a backtick opens no fence -------


def test_invalid_backtick_opener_does_not_hide_a_following_heading(tmp_path, repo):
    root, _sha = repo
    # CommonMark: a backtick fence's info string cannot contain a backtick, so
    # the first line is ordinary text and AB1 below it is a real heading.
    (root / "roadmaps" / "y.md").write_text(
        "# Roadmap\n\n```x`y\n\n" + AB1 + "## Later\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "invalid opener")
    db = tmp_path / "ledger.db"
    _anchor_item(db, "an-bt", "also at roadmaps/y.md#AB1")

    assert _ticket(db, root, "an-bt") == AB1


def test_valid_fence_openers_still_hide_a_heading_inside(tmp_path, repo):
    root, _sha = repo
    # A tilde opener may carry a backtick in its info string; a backtick opener
    # with a plain info string is a fence. Both still hide a heading.
    (root / "roadmaps" / "z.md").write_text(
        "~~~x`y\n" + AB1 + "~~~\n\n```js\n" + AB2 + "```\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "valid openers")
    db = tmp_path / "ledger.db"
    _anchor_item(db, "an-tl", "also at roadmaps/z.md#AB1")
    _anchor_item(db, "an-bk", "also at roadmaps/z.md#AB2")

    assert _refusal(db, root, "an-tl").startswith(f"{NOT_FOUND}: ")
    assert _refusal(db, root, "an-bk").startswith(f"{NOT_FOUND}: ")
