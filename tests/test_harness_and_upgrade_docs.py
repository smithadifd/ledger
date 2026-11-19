"""The install and upgrade pages say what the code does.

``resume --hook-env`` replaces an inherited ``LEDGER_HARNESS=cli`` and Claude
Code's settings ``env`` block carries ``LEDGER_HARNESS=claude-code``, so the
pages no longer tell a reader to ``unset`` it. ``claim-system`` on a repeat
run writes no event but still applies pending migrations, so no page or help
text says it "changes nothing". The upgrade page has the route for a ledger
that is not on ``PATH`` and the order of a plugin install and a settings edit.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from ledger.verbs import discover_verbs

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
START = ROOT / "docs" / "start-here.md"
UPGRADING = ROOT / "docs" / "upgrading.md"
CLI_PAGE = ROOT / "docs" / "cli.md"
# Every Markdown page of the repository except the changelog, which is history.
PAGES = sorted(
    [*ROOT.glob("*.md"), *(ROOT / "docs").glob("*.md")],
    key=lambda path: str(path),
)
PAGES = [path for path in PAGES if path.name != "CHANGELOG.md"]


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def squash(value: str) -> str:
    return " ".join(value.split())


@pytest.mark.parametrize("page", PAGES, ids=lambda path: path.name)
def test_no_page_tells_the_reader_to_unset_the_harness(page: Path) -> None:
    assert re.search(r"unset\s+LEDGER_HARNESS", text(page)) is None


@pytest.mark.parametrize("page", [README, START], ids=lambda path: path.name)
def test_the_settings_block_carries_the_claude_code_harness(page: Path) -> None:
    blocks = re.findall(r"```json\n(.*?)```", text(page), flags=re.DOTALL)
    settings = [block for block in blocks if '"env"' in block]
    assert settings, f"{page.name} has no settings block"
    for block in settings:
        assert '"LEDGER_HARNESS": "claude-code"' in block, block


@pytest.mark.parametrize("page", [README, START], ids=lambda path: path.name)
def test_the_page_states_the_measured_precedence(page: Path) -> None:
    flat = squash(text(page))
    assert "Claude Code 2.1.328" in flat
    assert "saw `claude-code` from its Bash tool" in flat


@pytest.mark.parametrize("page", [README, START], ids=lambda path: path.name)
def test_the_shell_profile_keeps_cli(page: Path) -> None:
    assert "export LEDGER_HARNESS=cli" in text(page)


def test_the_cli_page_lists_the_cli_case_for_the_harness_line() -> None:
    flat = squash(text(CLI_PAGE))
    assert "is absent from the environment, or is exactly `cli`" in flat


# --- claim-system on a repeat run ---------------------------------------------


def test_no_page_or_help_says_a_repeat_claim_changes_nothing() -> None:
    row = next(line for line in text(README).splitlines() if line.startswith("| `claim-system` |"))
    assert "changes nothing" not in row and "pending migration" in row
    start = squash(text(START).split("## 7. Claim the database", 1)[1].split("\n## ", 1)[0])
    assert "changes nothing" not in start
    assert "applies the pending migrations" in start
    assert "older copy of the ledger then refuses to open the file" in start
    cli = squash(text(CLI_PAGE).split("**`claim-system NAME`**", 1)[1].split("\n**`", 1)[0])
    assert "applies the pending migrations" in cli
    assert "read-only database file refuses even the repeat" in cli
    help_text = discover_verbs()["claim-system"].help
    assert "changes nothing" not in help_text
    assert "pending migration" in help_text


# --- the upgrade page ---------------------------------------------------------


def test_upgrading_has_the_route_for_a_ledger_that_is_not_on_path() -> None:
    section = squash(text(UPGRADING).split("### 3.1 The ledger", 1)[1].split("\n### 3.2", 1)[0])
    assert "If `command -v ledger` prints nothing" in section
    assert "a vendored copy, or a wrapper script" in section
    assert "virtual environment on a Python that passes the 1.1 probe" in section
    assert "ln -s <venv>/bin/ledger" in section
    assert "list every caller of the old copy" in section
    assert "the old copy cannot open the file" in section


def test_upgrading_says_plugin_install_rewrites_the_settings_file() -> None:
    section = squash(text(UPGRADING).split("## 3. The three upgrade commands", 1)[1])
    assert "`claude plugin install` (and `claude plugin update`) rewrites `~/.claude/settings.json`" in section
    assert "do the plugin installs first and the edit after" in section
    assert "re-read it" in section
