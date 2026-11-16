"""Every plugin marketplace install command pins this repository's release tag.

``claude plugin marketplace add`` with this repository's bare name follows main,
and main can carry plugin changes (its hooks, ``bin/ledger``, its options) that
no release has shipped. A person who follows an install command in the docs must
get the released tag's plugin, so the one accepted command is

    claude plugin marketplace add https://github.com/smithadifd/ledger.git#vX.Y.Z

with ``X.Y.Z`` the current release, ``__version__``, and that source argument
written exactly so. The rule does not try to recognise which repository a
command names: every ``marketplace add`` in the scanned text must carry exactly
that source, and anything else fails (no pin, a branch, another tag, the short
``owner/repo`` form, a case variant, a percent-encoded name, another host, a
trailing slash, a flag, trailing words, no argument at all, or another
repository). No doc installs another repository's marketplace today; one that
needs to must change this test on purpose.

The release step in ``docs/releasing.md`` finds the "from the next release (on
main, ...)" markers with a line-based ``grep``; a marker wrapped across a line
break is one that grep misses, so this test also runs that exact grep and fails
on any marker it does not find.

Scope: the tracked Markdown files. ``CHANGELOG.md`` is scanned only above its
first released-version heading, because a released entry is history and is not
rewritten.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from ledger import __version__

ROOT = Path(__file__).resolve().parents[1]
INSTALL_PAGES = ("README.md", "docs/start-here.md", "docs/upgrading.md")

# The one source argument a `marketplace add` may carry, compared as a string.
PINNED_SOURCE = f"https://github.com/smithadifd/ledger.git#v{__version__}"
# `marketplace add` as words, even split across a line break (an inline code
# span can wrap); the source runs to a closing backtick or the line's end.
ADD = re.compile(r"(?P<prefix>claude plugin )?marketplace\s+add\b(?P<rest>[^`\n]*)")
RELEASED_HEADING = re.compile(r"^## \d+\.\d+\.\d+", re.MULTILINE)
# A "from the next release (on main" marker, wherever its words wrap.
MARKER = re.compile(r"from\s+the\s+next\s+release\s+\(on\s+main", re.IGNORECASE)


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


def install_commands(text: str) -> list[tuple[int, str, bool]]:
    """Each `marketplace add` in the text: (line, text, accepted).

    A line continued with a trailing backslash is read as one line, and the
    line number is that of the joined text. The source is whatever follows
    `marketplace add` up to a closing backtick or the end of the line. A
    command is accepted only when it says `claude plugin marketplace add` in
    full and its source is exactly PINNED_SOURCE; every other
    `marketplace add` is refused.
    """
    joined = re.sub(r"\\[ \t]*\n[ \t]*", " ", text)
    lines = joined.splitlines()
    found: list[tuple[int, str, bool]] = []
    for match in ADD.finditer(joined):
        number = joined.count("\n", 0, match.start()) + 1
        accepted = bool(match.group("prefix")) and match.group("rest").strip() == PINNED_SOURCE
        found.append((number, lines[number - 1].strip(), accepted))
    return found


def all_commands() -> dict[str, list[tuple[int, str, bool]]]:
    commands: dict[str, list[tuple[int, str, bool]]] = {}
    for rel in tracked_markdown():
        text = scanned_text(rel, (ROOT / rel).read_text(encoding="utf-8"))
        found = install_commands(text)
        if found:
            commands[rel] = found
    return commands


def test_every_marketplace_add_carries_exactly_the_pinned_source() -> None:
    bad = [
        f"{rel}:{number}: {line}"
        for rel, found in all_commands().items()
        for number, line, accepted in found
        if not accepted
    ]
    assert not bad, (
        f"every `marketplace add` must be `claude plugin marketplace add {PINNED_SOURCE}`:\n"
        + "\n".join(bad)
    )


def test_a_pin_to_another_release_is_refused() -> None:
    for tag in ("v0.0.0", f"v{__version__}.1", f"v{__version__}-dev", __version__):
        command = f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#{tag}"
        assert install_commands(command) == [(1, command, False)], command


def test_the_install_pages_each_carry_a_pinned_command() -> None:
    # Without this, a page whose command was deleted or reworded past the
    # scanner would pass the tests above with nothing to check.
    commands = all_commands()
    for rel in INSTALL_PAGES:
        accepted = [ok for _n, _line, ok in commands.get(rel, []) if ok]
        assert accepted, f"{rel} has no pinned `claude plugin marketplace add` command"
    assert "docs/releasing.md" in tracked_markdown()


def test_the_scanner_refuses_each_near_miss() -> None:
    v = __version__
    pinned = f"claude plugin marketplace add {PINNED_SOURCE}"
    assert install_commands(pinned) == [(1, pinned, True)]
    assert install_commands(f"```sh\n{pinned}\n```\n")[0][2] is True
    assert install_commands(f"Run `{pinned}` first.")[0][2] is True
    near_misses = [
        "claude plugin marketplace add smithadifd/ledger",
        f"claude plugin marketplace add smithadifd/ledger#v{v}",
        "claude plugin marketplace add smithadifd/ledger.git",
        "claude plugin marketplace add https://github.com/smithadifd/ledger",
        "claude plugin marketplace add https://github.com/smithadifd/ledger.git",
        "claude plugin marketplace add https://github.com/smithadifd/ledger.git#main",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#{v}",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#v{v}.0",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#v{v.rsplit('.', 1)[0]}",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#v{v}-dev",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#v{v} extra",
        f"claude plugin marketplace add --scope user https://github.com/smithadifd/ledger.git#v{v}",
        f"claude plugin marketplace add git@github.com:smithadifd/ledger.git#v{v}",
        f"claude plugin marketplace add http://github.com/smithadifd/ledger.git#v{v}",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#V{v}",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger/#v{v}",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git/#v{v}",
        f"claude plugin marketplace add https://www.github.com/smithadifd/ledger.git#v{v}",
        f"claude plugin marketplace add https://gitlab.com/smithadifd/ledger.git#v{v}",
        # Case variants and percent-encoding: git and GitHub resolve these to
        # this repository, and the exact comparison refuses them unrecognised.
        "claude plugin marketplace add https://github.com/Smithadifd/ledger.git#main",
        f"claude plugin marketplace add https://github.com/Smithadifd/ledger.git#v{v}",
        f"claude plugin marketplace add https://github.com/smithadifd/Ledger.git#v{v}",
        f"claude plugin marketplace add https://github.com/SMITHADIFD/LEDGER.git#v{v}",
        "claude plugin marketplace add Smithadifd/Ledger",
        "claude plugin marketplace add https://github.com/smithadifd/%6cedger.git#main",
        f"claude plugin marketplace add https://github.com/smithadifd/%6cedger.git#v{v}",
        f"claude plugin marketplace add https://github.com/%73mithadifd/ledger.git#v{v}",
        # Another repository is refused too: no doc installs one today.
        "claude plugin marketplace add smithadifd/skills",
        "claude plugin marketplace add smithadifd/ledger-plugins",
        "claude plugin marketplace add Smithadifd/Ledger-plugins",
        f"claude plugin marketplace add https://github.com/example/other.git#v{v}",
        # Wrong words around the command, or no argument at all.
        f"claude  plugin marketplace add {PINNED_SOURCE}x",
        f"marketplace add {PINNED_SOURCE}",
        "/plugin marketplace add smithadifd/ledger",
        "claude plugin marketplace add smithadifd/ledger follows main.",
        "claude plugin marketplace add \\\n  smithadifd/ledger",
        "Set the pin in the `claude plugin marketplace add` command.",
        "claude plugin marketplace add",
        "Run `claude plugin marketplace\nadd smithadifd/ledger` first.",
        "claude plugin marketplace add\tsmithadifd/ledger",
    ]
    for command in near_misses:
        found = install_commands(command)
        assert found, f"scanner did not see: {command!r}"
        assert [ok for _n, _line, ok in found] == [False], f"accepted: {command!r}"
    # A continued line is one command, pinned or not.
    continued = f"claude plugin marketplace add \\\n  {PINNED_SOURCE}"
    assert [ok for _n, _line, ok in install_commands(continued)] == [True]
    # Other marketplace verbs, and prose that only starts with "add", are not
    # install commands.
    for other in (
        "claude plugin marketplace update skills",
        "claude plugin marketplace remove ledger-plugins",
        "A marketplace added with a pin stays on that tag.",
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
    assert [number for number, _line, _ok in install_commands(scanned)] == [5]
    # Every other page is scanned whole, headings or not.
    assert scanned_text("README.md", page) == page


def release_grep() -> list[str]:
    """The marker grep that docs/releasing.md prints, as an argument list."""
    text = (ROOT / "docs/releasing.md").read_text(encoding="utf-8")
    printed = re.findall(r"`(grep [^`]*)`", text)
    assert len(printed) == 1, f"docs/releasing.md prints {len(printed)} grep commands"
    argv = shlex.split(printed[0])
    assert argv[0] == "grep"
    return argv


def marker_lines(text: str) -> list[int]:
    """The line each next-release marker starts on, wrapped or not."""
    return [text.count("\n", 0, match.start()) + 1 for match in MARKER.finditer(text)]


def test_the_marker_scan_sees_a_wrapped_marker() -> None:
    page = "Intro.\nFrom the next release (on\nmain, not in v0.3.1), more.\nfrom the next release (on main, not in v0.3.1)\n"
    assert marker_lines(page) == [2, 4]


def test_the_release_grep_finds_every_next_release_marker() -> None:
    done = subprocess.run(release_grep(), cwd=ROOT, capture_output=True, text=True)
    assert done.returncode in (0, 1), done.stderr
    found = set()
    for hit in done.stdout.splitlines():
        path, number, _text = hit.split(":", 2)
        found.add((path, int(number)))
    missed = [
        f"{rel}:{number}"
        for rel in tracked_markdown()
        for number in marker_lines(scanned_text(rel, (ROOT / rel).read_text(encoding="utf-8")))
        if (rel, number) not in found
    ]
    assert not missed, (
        "next-release marker(s) the release grep does not find (keep each marker phrase on one line):\n"
        + "\n".join(missed)
    )
