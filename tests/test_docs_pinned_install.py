"""Every install command for the plugin marketplace pins a release tag.

``claude plugin marketplace add`` with this repository's bare name follows main,
and main can carry plugin changes (its hooks, ``bin/ledger``, its options) that
no release has shipped. A person who follows an install command in the docs must
get the released tag's plugin, so every such command is

    claude plugin marketplace add https://github.com/smithadifd/ledger.git#vX.Y.Z

with ``X.Y.Z`` the current release, ``__version__``. Anything else that names
this repository after ``marketplace add`` (no pin, a branch, a tag without the
``v``, a different spelling of the source, a flag, trailing words) fails here.
Commands for other repositories are not this test's business.

Scope: the tracked Markdown files. ``CHANGELOG.md`` is scanned only above its
first released-version heading, because a released entry is history and is not
rewritten.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ledger import __version__

ROOT = Path(__file__).resolve().parents[1]
INSTALL_PAGES = ("README.md", "docs/start-here.md", "docs/upgrading.md")

# What a source argument must be, in full, for this repository.
PINNED_SOURCE = re.compile(r"https://github\.com/smithadifd/ledger\.git#(v\d+\.\d+\.\d+)")
# Any word that names this repository (and not smithadifd/ledger-something), in
# any letter case: GitHub resolves `Smithadifd/Ledger`, so a case variant is an
# install command for this repository that must be seen and then refused,
# because PINNED_SOURCE above stays exact.
NAMES_THIS_REPO = re.compile(r"smithadifd/ledger(?![\w-])", re.IGNORECASE)
ADD = re.compile(r"(?P<prefix>claude plugin )?marketplace add(?P<rest>[^`]*)")
RELEASED_HEADING = re.compile(r"^## \d+\.\d+\.\d+", re.MULTILINE)


def tracked_markdown() -> list[str]:
    done = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(name for name in done.stdout.split("\0") if name)


def scanned_text(rel: str, text: str) -> str:
    """The part of a page this test holds to the pin rule."""
    if rel == "CHANGELOG.md":
        released = RELEASED_HEADING.search(text)
        if released:
            return text[: released.start()]
    return text


def install_commands(text: str) -> list[tuple[int, str, str | None]]:
    """Each `marketplace add` for this repository: (line, text, pinned tag or None).

    A line continued with a trailing backslash is read as one line. The source
    is whatever follows `marketplace add` up to a closing backtick; it is valid
    only when it is exactly the pinned source and the line says
    `claude plugin marketplace add` in full.
    """
    joined = re.sub(r"\\[ \t]*\n[ \t]*", " ", text)
    found: list[tuple[int, str, str | None]] = []
    for number, line in enumerate(joined.splitlines(), start=1):
        for match in ADD.finditer(line):
            rest = match.group("rest")
            if not any(NAMES_THIS_REPO.search(word) for word in rest.split()):
                continue
            source = PINNED_SOURCE.fullmatch(rest.strip())
            tag = source.group(1) if source and match.group("prefix") else None
            found.append((number, line.strip(), tag))
    return found


def all_commands() -> dict[str, list[tuple[int, str, str | None]]]:
    commands: dict[str, list[tuple[int, str, str | None]]] = {}
    for rel in tracked_markdown():
        text = scanned_text(rel, (ROOT / rel).read_text(encoding="utf-8"))
        found = install_commands(text)
        if found:
            commands[rel] = found
    return commands


def test_every_install_command_for_this_repository_is_pinned() -> None:
    bad = [
        f"{rel}:{number}: {line}"
        for rel, found in all_commands().items()
        for number, line, tag in found
        if tag is None
    ]
    assert not bad, "unpinned or malformed install command(s):\n" + "\n".join(bad)


def test_the_pin_names_the_current_release() -> None:
    wrong = [
        f"{rel}:{number}: pins {tag}, the release is v{__version__}"
        for rel, found in all_commands().items()
        for number, _line, tag in found
        if tag is not None and tag != f"v{__version__}"
    ]
    assert not wrong, "\n".join(wrong)


def test_the_install_pages_each_carry_a_pinned_command() -> None:
    # Without this, a page whose command was deleted or reworded past the
    # scanner would pass the two tests above with nothing to check.
    commands = all_commands()
    for rel in INSTALL_PAGES:
        tags = [tag for _n, _line, tag in commands.get(rel, []) if tag]
        assert tags, f"{rel} has no pinned `claude plugin marketplace add` command"
    assert "docs/releasing.md" in tracked_markdown()


def test_the_scanner_refuses_each_near_miss() -> None:
    pinned = "claude plugin marketplace add https://github.com/smithadifd/ledger.git#v0.3.1"
    assert install_commands(pinned) == [(1, pinned, "v0.3.1")]
    assert install_commands(f"```sh\n{pinned}\n```\n")[0][2] == "v0.3.1"
    assert install_commands(f"Run `{pinned}` first.")[0][2] == "v0.3.1"
    near_misses = [
        "claude plugin marketplace add smithadifd/ledger",
        "claude plugin marketplace add smithadifd/ledger.git",
        "claude plugin marketplace add https://github.com/smithadifd/ledger",
        "claude plugin marketplace add https://github.com/smithadifd/ledger.git",
        "claude plugin marketplace add https://github.com/smithadifd/ledger.git#main",
        "claude plugin marketplace add https://github.com/smithadifd/ledger.git#0.3.1",
        "claude plugin marketplace add https://github.com/smithadifd/ledger.git#v0.3",
        "claude plugin marketplace add https://github.com/smithadifd/ledger.git#v0.3.1-dev",
        "claude plugin marketplace add https://github.com/smithadifd/ledger.git#v0.3.1 extra",
        "claude plugin marketplace add --scope user https://github.com/smithadifd/ledger.git#v0.3.1",
        "claude plugin marketplace add git@github.com:smithadifd/ledger.git#v0.3.1",
        "claude plugin marketplace add http://github.com/smithadifd/ledger.git#v0.3.1",
        "claude plugin marketplace add https://github.com/smithadifd/ledger.git#V0.3.1",
        # A case variant of the repository name is still this repository.
        "claude plugin marketplace add https://github.com/Smithadifd/ledger.git#main",
        "claude plugin marketplace add https://github.com/Smithadifd/ledger.git#v0.3.1",
        "claude plugin marketplace add https://github.com/smithadifd/Ledger.git#v0.3.1",
        "claude plugin marketplace add https://github.com/SMITHADIFD/LEDGER.git#v0.3.1",
        "claude plugin marketplace add Smithadifd/Ledger",
        "claude  plugin marketplace add https://github.com/smithadifd/ledger.git#v0.3.1x",
        "marketplace add https://github.com/smithadifd/ledger.git#v0.3.1",
        "claude plugin marketplace add smithadifd/ledger follows main.",
        "claude plugin marketplace add \\\n  smithadifd/ledger",
    ]
    for command in near_misses:
        found = install_commands(command)
        assert found, f"scanner did not see: {command!r}"
        assert [tag for _n, _line, tag in found] == [None], f"accepted: {command!r}"
    # A continued line is one command, pinned or not.
    continued = "claude plugin marketplace add \\\n  https://github.com/smithadifd/ledger.git#v0.3.1"
    assert [tag for _n, _line, tag in install_commands(continued)] == ["v0.3.1"]
    # Other repositories are not this test's business.
    for other in (
        "claude plugin marketplace add smithadifd/skills",
        "claude plugin marketplace add smithadifd/ledger-plugins",
        "claude plugin marketplace add Smithadifd/Skills",
        "claude plugin marketplace add Smithadifd/Ledger-plugins",
        "claude plugin marketplace update skills",
        "claude plugin marketplace remove ledger-plugins",
    ):
        assert install_commands(other) == [], other


def test_a_released_changelog_entry_is_history_but_unreleased_is_checked() -> None:
    page = (
        "# Changelog\n\n## Unreleased\n\n"
        "- claude plugin marketplace add smithadifd/ledger\n\n"
        "## 0.2.0 - 2026-01-01\n\n"
        "- claude plugin marketplace add smithadifd/ledger\n"
    )
    scanned = scanned_text("CHANGELOG.md", page)
    assert [number for number, _line, _tag in install_commands(scanned)] == [5]
    # Every other page is scanned whole, headings or not.
    assert scanned_text("README.md", page) == page
