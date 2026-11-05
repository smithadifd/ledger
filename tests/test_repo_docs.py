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


def _triggers() -> list[str]:
    text = PAGE.read_text(encoding="utf-8")
    section = text.split("## Write triggers", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"^- \*\*(.+?)\*\*", section, flags=re.MULTILINE)


def test_short_form_names_every_trigger_on_the_page() -> None:
    triggers = _triggers()
    assert len(triggers) >= 7, triggers
    lowered = SHORT_FORM.lower()
    for trigger in triggers:
        assert trigger.lower() in lowered, f"short form lacks write trigger: {trigger}"


def test_readme_and_contract_page_never_say_pick_up() -> None:
    for path in (ROOT / "README.md", PAGE):
        assert "pick up" not in path.read_text(encoding="utf-8").lower(), path
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/agent-contract.md" in readme and "ledger resume --brief" in readme
