"""Checks that read this repository's own files (README, docs/).

These live outside ``ledger/`` on purpose: the package is vendored as a folder
on its own, and a consumer's copy has no README or docs beside it. Here the
files are always present, so nothing skips. ``LEDGER_REPO_ROOT`` is not needed.
"""
from __future__ import annotations

import re
from pathlib import Path

from ledger.verbs.resume import SHORT_FORM

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "agent-contract.md"


def _squash(text: str) -> str:
    return " ".join(text.split())


def _page_triggers() -> dict[str, list[str]]:
    """Each write trigger on the page, with the commands its bullet names.

    A command is a backticked span that starts with ``ledger `` or is the
    event name ``item.stopped``; line breaks inside a bullet are squashed.
    """
    text = PAGE.read_text(encoding="utf-8")
    section = text.split("## Write triggers", 1)[1].split("\n## ", 1)[0]
    bullets = re.split(r"^(?=- \*\*)", section, flags=re.MULTILINE)[1:]
    triggers: dict[str, list[str]] = {}
    for bullet in bullets:
        bullet = bullet.split("\n\n", 1)[0]  # a bullet ends at the blank line
        name = re.match(r"- \*\*(.+?)\*\*", bullet).group(1)
        spans = [_squash(s) for s in re.findall(r"`([^`]+)`", bullet)]
        triggers[name] = [
            s for s in spans if s.startswith("ledger ") or s == "item.stopped"
        ]
    return triggers


def _short_form_triggers() -> dict[str, str]:
    """Each trigger line of the short form, with its indented continuations."""
    triggers: dict[str, str] = {}
    name = None
    for line in SHORT_FORM.splitlines():
        match = re.match(r"- ([^:]+):(.*)$", line)
        if match:
            name = match.group(1)
            triggers[name] = match.group(2)
        elif name is not None and line.startswith("  "):
            triggers[name] += " " + line
        else:
            name = None
    return {key: _squash(value) for key, value in triggers.items()}


def test_short_form_names_every_trigger_on_the_page() -> None:
    page = _page_triggers()
    short = _short_form_triggers()
    assert len(page) >= 9, page
    assert set(short) == set(page), (sorted(short), sorted(page))


def test_short_form_gives_each_trigger_the_pages_commands() -> None:
    page = _page_triggers()
    short = _short_form_triggers()
    for trigger, commands in page.items():
        assert commands, f"page trigger names no command: {trigger}"
        for command in commands:
            assert command in short[trigger], (
                f"short form trigger {trigger!r} lacks the page's {command!r}: "
                f"{short[trigger]!r}"
            )


def test_follow_up_trigger_files_and_links_as_a_child() -> None:
    page = _page_triggers()["follow-up filed"]
    assert page == [
        "ledger new --type TYPE --title TITLE --source SRC",
        "ledger link NEW --rel child-of --target PARENT --idempotency-key SRC:child-of",
    ]


def test_concepts_state_the_database_first_creation_window() -> None:
    text = (ROOT / "docs" / "concepts.md").read_text(encoding="utf-8")
    section = text.split("## Opening the database file", 1)[1].split("\n## ", 1)[0]
    prose = " ".join(section.split())
    assert "opens or creates those side files by name" in prose
    assert "read-only open of a database path swapped to a FIFO" in prose


def test_readme_and_contract_page_never_say_pick_up() -> None:
    for path in (ROOT / "README.md", PAGE):
        assert "pick up" not in path.read_text(encoding="utf-8").lower(), path
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/agent-contract.md" in readme and "ledger resume --brief" in readme
