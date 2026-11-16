"""The documented install command pins this repository's release tag.

``claude plugin marketplace add`` with this repository's bare name follows main,
and main can carry plugin changes (its hooks, ``bin/ledger``, its options) that
no release has shipped. A person who copies the install command from an install
page must get the released tag's plugin.

What this test asserts, and nothing more. On each install page (``README.md``,
``docs/start-here.md``, ``docs/upgrading.md``), among the lines inside its
fenced code blocks (``` or ~~~ fences, a fence inside a blockquote included):

- at least one line is exactly

      claude plugin marketplace add https://github.com/smithadifd/ledger.git#vX.Y.Z

  with ``X.Y.Z`` the current release, ``__version__``, compared character for
  character with no trimming (only the fence's own indentation, and a
  blockquote's ``>`` markers, are taken off each line first); and
- every fenced line that holds the word ``marketplace`` is that exact line.

So a pinned command split across two lines fails, and so do an unpinned add, a
branch, another tag, a flag, trailing words, and any other ``marketplace``
command (``update``, ``remove``); a page that needs one of those shows it in
prose instead. Before looking for the word, a fenced line that ends in a
backslash is joined to the next one, as the shell joins it, and quote
characters and backslashes are dropped, so ``market"place"``, ``market\\place``
and a word split by a continued line count as ``marketplace``; the comparison
ignores letter case.

A fence inside a blockquote is read as a fence: its opening line may start with
``>`` markers, and each line inside loses that many markers. A line without them
ends the blockquote, and so the fence, as in Markdown.

Not checked: prose outside fences, inline code spans included (a person reads
those rather than copies them as a block); any page other than the three
install pages; and a command spelled through shell expansion (a variable, a
glob, command substitution). This is not a shell or Markdown parser: it holds
the documented lines to one exact text. ``CHANGELOG.md`` is no longer scanned
for install commands: its entries are prose with no fenced command, so the
exact-line rule has nothing to hold there.

The release step in ``docs/releasing.md`` finds the "from the next release (on
main, ...)" markers with a line-based ``grep``. A marker wrapped across a line
break is one that grep misses, so this test runs that exact grep, counts every
match it prints (``-o``, so two markers on one line count twice), and requires
that count to equal the number of markers in each tracked Markdown file.
``CHANGELOG.md`` is counted only above its first released-version heading,
because a released entry is history and is not rewritten.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from collections import Counter
from pathlib import Path

from ledger import __version__

ROOT = Path(__file__).resolve().parents[1]
INSTALL_PAGES = ("README.md", "docs/start-here.md", "docs/upgrading.md")

# The one documented install line, compared as a whole string.
PINNED_SOURCE = f"https://github.com/smithadifd/ledger.git#v{__version__}"
PINNED_COMMAND = f"claude plugin marketplace add {PINNED_SOURCE}"
# One blockquote marker: a `>` and the single space that may follow it.
QUOTE = re.compile(r"[ \t]*> ?")
# A fence opens with three or more backticks or tildes, after any blockquote
# markers and indentation.
FENCE = re.compile(
    r"^(?P<quote>(?:[ \t]*> ?)*)(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})"
)
RELEASED_HEADING = re.compile(r"^## \d+\.\d+\.\d+", re.MULTILINE)
# A "from the next release (on main" marker, wherever its words wrap.
MARKER = re.compile(r"from\s+the\s+next\s+release\s+\(on\s+main", re.IGNORECASE)


def fenced_commands(text: str) -> list[tuple[int, str]]:
    """Each line inside the page's fenced code blocks: (line number, text).

    The fence's own indentation, and the `>` markers of a blockquote it sits
    in, are removed from each line inside it. A line that ends in a backslash
    continues onto the next fenced line, as in the shell; the joined text keeps
    the backslash and the line break, so it is never equal to a one-line
    command. A fence that is never closed runs to the end of the page, or to
    the first line that lacks its blockquote's markers.
    """
    found: list[tuple[int, str]] = []
    # (character, length, indent, blockquote depth)
    fence: tuple[str, int, int, int] | None = None
    pending: tuple[int, str] | None = None
    for number, line in enumerate(text.splitlines(), 1):
        if fence is not None:
            char, length, indent, depth = fence
            rest: str | None = line
            for _ in range(depth):
                marker = QUOTE.match(rest)
                if not marker:
                    rest = None
                    break
                rest = rest[marker.end():]
            if rest is not None:
                stripped = rest.strip()
                if stripped and set(stripped) == {char} and len(stripped) >= length:
                    if pending:
                        found.append(pending)
                        pending = None
                    fence = None
                    continue
                body = rest[min(indent, len(rest) - len(rest.lstrip(" \t"))):]
                pending = (pending[0], pending[1] + "\n" + body) if pending else (number, body)
                if not body.endswith("\\"):
                    found.append(pending)
                    pending = None
                continue
            # The blockquote ended, and the fence with it; read this line afresh.
            if pending:
                found.append(pending)
                pending = None
            fence = None
        opened = FENCE.match(line)
        if opened:
            mark = opened.group("fence")
            fence = (
                mark[0],
                len(mark),
                len(opened.group("indent")),
                opened.group("quote").count(">"),
            )
    if pending:
        found.append(pending)
    return found


def is_pinned(command: str) -> bool:
    return command == PINNED_COMMAND


def names_marketplace(command: str) -> bool:
    """Whether the shell would read the word `marketplace` in this text."""
    joined = command.replace("\\\n", "")
    bare = re.sub(r"[\"'\\]", "", joined)
    return "marketplace" in bare.casefold()


def install_problems(text: str) -> list[str]:
    """What keeps a page's fenced lines from meeting the pin rule."""
    commands = fenced_commands(text)
    problems = [
        f"{number}: {command}"
        for number, command in commands
        if names_marketplace(command) and not is_pinned(command)
    ]
    if not any(is_pinned(command) for _number, command in commands):
        problems.insert(0, f"no fenced line is exactly `{PINNED_COMMAND}`")
    return problems


def fence(*lines: str, indent: str = "") -> str:
    body = "".join(f"{indent}{line}\n" for line in lines)
    return f"Install it:\n\n{indent}```sh\n{body}{indent}```\n\nThen go on.\n"


def test_each_install_page_carries_exactly_the_pinned_command() -> None:
    for rel in INSTALL_PAGES:
        problems = install_problems((ROOT / rel).read_text(encoding="utf-8"))
        assert not problems, f"{rel}:\n" + "\n".join(problems)


def test_the_exact_pinned_line_passes() -> None:
    install = "claude plugin install ledger-hooks@ledger-plugins"
    assert install_problems(fence(PINNED_COMMAND, install)) == []
    assert install_problems(fence(PINNED_COMMAND, install, indent="   ")) == []
    assert install_problems(f"~~~sh\n{PINNED_COMMAND}\n~~~\n") == []
    assert fenced_commands(fence(PINNED_COMMAND, install, indent="   ")) == [
        (4, PINNED_COMMAND),
        (5, install),
    ]


def test_a_space_padded_pinned_line_fails() -> None:
    missing = f"no fenced line is exactly `{PINNED_COMMAND}`"
    for padded in (f" {PINNED_COMMAND}", f"{PINNED_COMMAND} ", f"  {PINNED_COMMAND}  "):
        assert install_problems(fence(PINNED_COMMAND, padded)) == [f"5: {padded}"], padded
        assert install_problems(fence(padded)) == [missing, f"4: {padded}"], padded
        assert install_problems(f"````\n{padded}\n````\n") == [missing, f"2: {padded}"], padded
    # Only the fence's own indentation is taken off, so padding beyond it fails.
    assert install_problems(fence(f" {PINNED_COMMAND}", indent="  ")) == [
        missing,
        f"4:  {PINNED_COMMAND}",
    ]


def test_the_pinned_line_in_a_blockquoted_fence_passes() -> None:
    missing = f"no fenced line is exactly `{PINNED_COMMAND}`"
    # The pinned line passes in a blockquoted fence, with and without the
    # space after each marker, nested, and with the fence indented in the quote.
    for lines in (
        ("> ```sh", f"> {PINNED_COMMAND}", "> ```"),
        ("```sh", f"{PINNED_COMMAND}", "```"),
        (">```sh", f">{PINNED_COMMAND}", ">```"),
        ("> > ```sh", f"> > {PINNED_COMMAND}", "> > ```"),
        ("> ~~~", f"> {PINNED_COMMAND}", "> ~~~"),
        (">   ```sh", f">   {PINNED_COMMAND}", ">   ```"),
    ):
        assert install_problems("\n".join(lines) + "\n") == [], lines
    assert fenced_commands(f"> ```sh\n> {PINNED_COMMAND}\n>\n> ```\n") == [
        (2, PINNED_COMMAND),
        (3, ""),
    ]
    # A space-padded pinned line fails in a blockquote as it does outside one.
    padded = f"> ```sh\n>  {PINNED_COMMAND}\n> ```\n"
    assert install_problems(padded) == [missing, f"2:  {PINNED_COMMAND}"]


def test_an_unpinned_command_in_a_blockquoted_fence_fails() -> None:
    missing = f"no fenced line is exactly `{PINNED_COMMAND}`"
    unpinned = "claude plugin marketplace add smithadifd/ledger"
    # An unpinned command in a blockquoted fence fails, alone and beside the exact line.
    for opener, prefix, closer in (
        ("> ```sh", "> ", "> ```"),
        (">```sh", ">", ">```"),
        ("> > ```sh", "> > ", "> > ```"),
        ("> ~~~", "> ", "> ~~~"),
    ):
        quoted = f"{opener}\n{prefix}{unpinned}\n{closer}\n"
        assert install_problems(quoted) == [missing, f"2: {unpinned}"], opener
        assert install_problems(fence(PINNED_COMMAND) + quoted) == [f"9: {unpinned}"], opener
        both = f"{opener}\n{prefix}{PINNED_COMMAND}\n{prefix}{unpinned}\n{closer}\n"
        assert install_problems(both) == [f"3: {unpinned}"], opener


def test_a_line_without_the_blockquote_markers_ends_the_fence() -> None:
    # As in Markdown, the quote ends where its markers stop, and the fence with
    # it; the later line is prose, and the pinned line above it still counts.
    page = f"> ```sh\n> {PINNED_COMMAND}\nclaude plugin marketplace add x\n"
    assert install_problems(page) == []
    # The line that ended the quote can open a fence of its own.
    page = f"> ```sh\n> {PINNED_COMMAND}\n```sh\nclaude plugin marketplace add x\n```\n"
    assert install_problems(page) == ["4: claude plugin marketplace add x"]
    # A nested quote that loses one marker ends the inner fence.
    page = f"> > ```sh\n> > {PINNED_COMMAND}\n> claude plugin marketplace add x\n"
    assert install_problems(page) == []


def test_a_pinned_command_split_after_marketplace_fails() -> None:
    # The command split after `marketplace`, with and without the shell's
    # line continuation; alone on the page and beside the exact line.
    # A continued line is reported as one command, from its first line.
    missing = f"no fenced line is exactly `{PINNED_COMMAND}`"
    for split, flagged in (
        (
            ("claude plugin marketplace \\", f"  add {PINNED_SOURCE}"),
            f"claude plugin marketplace \\\n  add {PINNED_SOURCE}",
        ),
        (
            ("claude plugin marketplace add \\", f"  {PINNED_SOURCE}"),
            f"claude plugin marketplace add \\\n  {PINNED_SOURCE}",
        ),
        (("claude plugin marketplace", f"add {PINNED_SOURCE}"), "claude plugin marketplace"),
    ):
        assert install_problems(fence(*split)) == [missing, f"4: {flagged}"], split
        assert install_problems(fence(PINNED_COMMAND, *split)) == [f"5: {flagged}"], split


def test_a_shell_joined_spelling_of_marketplace_fails() -> None:
    for line in (
        'claude plugin market"place" add smithadifd/ledger',
        "claude plugin market'place' add smithadifd/ledger",
        "claude plugin market\\place add smithadifd/ledger",
        "claude plugin 'marketplace' add smithadifd/ledger",
        "claude plugin Marketplace add smithadifd/ledger",
    ):
        assert install_problems(fence(PINNED_COMMAND, line)) == [f"5: {line}"], line
    # A word split by a continued line is one word to the shell.
    split = ("claude plugin market\\", "place add smithadifd/ledger")
    assert install_problems(fence(PINNED_COMMAND, *split)) == [
        "5: claude plugin market\\\nplace add smithadifd/ledger"
    ]


def test_each_near_miss_fails() -> None:
    v = __version__
    near_misses = [
        "claude plugin marketplace add smithadifd/ledger",
        f"claude plugin marketplace add smithadifd/ledger#v{v}",
        "claude plugin marketplace add smithadifd/ledger.git",
        "claude plugin marketplace add https://github.com/smithadifd/ledger",
        "claude plugin marketplace add https://github.com/smithadifd/ledger.git",
        "claude plugin marketplace add https://github.com/smithadifd/ledger.git#main",
        "claude plugin marketplace add https://github.com/smithadifd/ledger.git#v0.0.0",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#{v}",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#v{v}.0",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#v{v}.1",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#v{v.rsplit('.', 1)[0]}",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#v{v}-dev",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#v{v} extra",
        f"claude plugin marketplace add {PINNED_SOURCE} # pinned",
        f"claude plugin marketplace add --scope user {PINNED_SOURCE}",
        f"claude plugin marketplace add git@github.com:smithadifd/ledger.git#v{v}",
        f"claude plugin marketplace add http://github.com/smithadifd/ledger.git#v{v}",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#V{v}",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger/#v{v}",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git/#v{v}",
        f"claude plugin marketplace add https://www.github.com/smithadifd/ledger.git#v{v}",
        f"claude plugin marketplace add https://gitlab.com/smithadifd/ledger.git#v{v}",
        f"claude plugin marketplace add https://github.com/Smithadifd/ledger.git#v{v}",
        f"claude plugin marketplace add https://github.com/SMITHADIFD/LEDGER.git#v{v}",
        f"claude plugin marketplace add https://github.com/smithadifd/%6cedger.git#v{v}",
        "claude plugin marketplace add Smithadifd/Ledger",
        "claude plugin marketplace add smithadifd/skills",
        f"claude plugin marketplace add https://github.com/example/other.git#v{v}",
        f"claude  plugin marketplace add {PINNED_SOURCE}",
        f"claude plugin marketplace add\t{PINNED_SOURCE}",
        f"marketplace add {PINNED_SOURCE}",
        f"/plugin marketplace add {PINNED_SOURCE}",
        f"sudo claude plugin marketplace add {PINNED_SOURCE}",
        "claude plugin marketplace add",
        "claude plugin marketplace update ledger-plugins",
        "claude plugin marketplace remove ledger-plugins",
        "claude plugin configure ledger-hooks@<marketplace> --values-stdin",
    ]
    for line in near_misses:
        assert install_problems(fence(PINNED_COMMAND, line)) == [f"5: {line}"], line
        alone = install_problems(fence(line))
        assert alone == [f"no fenced line is exactly `{PINNED_COMMAND}`", f"4: {line}"], line


def test_prose_outside_fences_is_not_checked() -> None:
    prose = (
        "Run `claude plugin marketplace add smithadifd/ledger` to follow main.\n"
        "claude plugin marketplace add smithadifd/ledger\n"
        f"Or `{PINNED_COMMAND}` inline.\n"
    )
    # An unpinned command in prose is not held to the rule ...
    assert install_problems(prose + fence(PINNED_COMMAND)) == []
    # ... and a pinned one in prose does not stand in for the fenced line.
    assert install_problems(prose) == [f"no fenced line is exactly `{PINNED_COMMAND}`"]
    # Indented code that is not fenced is outside the rule too.
    assert install_problems("Text.\n\n    claude plugin marketplace add x\n") == [
        f"no fenced line is exactly `{PINNED_COMMAND}`"
    ]


def test_a_fence_that_is_never_closed_runs_to_the_end() -> None:
    page = f"```sh\n{PINNED_COMMAND}\n```\n\nText.\n\n```sh\nclaude plugin marketplace add x\n"
    assert install_problems(page) == ["8: claude plugin marketplace add x"]
    # A shorter or different closing mark does not close the fence.
    page = f"````sh\n{PINNED_COMMAND}\n```\nclaude plugin marketplace add x\n````\n"
    assert install_problems(page) == ["4: claude plugin marketplace add x"]
    page = f"~~~\n{PINNED_COMMAND}\n```\nclaude plugin marketplace add x\n~~~\n"
    assert install_problems(page) == ["4: claude plugin marketplace add x"]


def tracked_markdown() -> list[str]:
    done = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted(name for name in done.stdout.split("\0") if name)


def counted_lines(rel: str, text: str) -> int:
    """How many leading lines of a page hold markers this test counts."""
    lines = text.count("\n") + 1
    if rel == "CHANGELOG.md":
        released = RELEASED_HEADING.search(text)
        if released:
            return text.count("\n", 0, released.start())
    return lines


def release_grep() -> list[str]:
    """The marker grep that docs/releasing.md prints, as an argument list."""
    text = (ROOT / "docs/releasing.md").read_text(encoding="utf-8")
    printed = re.findall(r"`(grep [^`]*)`", text)
    assert len(printed) == 1, f"docs/releasing.md prints {len(printed)} grep commands"
    argv = shlex.split(printed[0])
    assert argv[0] == "grep"
    return argv


def marker_misses(root: Path, pages: list[str]) -> list[str]:
    """Each page whose marker count differs from what the release grep finds.

    The grep runs exactly as docs/releasing.md prints it, from `root`, with
    `-o` added so each match prints on its own line.
    """
    argv = release_grep()
    done = subprocess.run([argv[0], "-o", *argv[1:]], cwd=root, capture_output=True, text=True)
    assert done.returncode in (0, 1), done.stderr
    hits: Counter[tuple[str, int]] = Counter()  # (path, line) -> matches on it
    for hit in done.stdout.splitlines():
        path, number, _match = hit.split(":", 2)
        hits[(path, int(number))] += 1
    misses = []
    for rel in pages:
        text = (root / rel).read_text(encoding="utf-8")
        last = counted_lines(rel, text)
        head = "\n".join(text.split("\n")[:last])
        markers = len(MARKER.findall(head))
        found = sum(n for (path, number), n in hits.items() if path == rel and number <= last)
        if markers != found:
            misses.append(f"{rel}: {markers} marker(s), the release grep finds {found}")
    return misses


def scratch_tree(root: Path, page: str) -> list[str]:
    (root / "docs").mkdir()
    (root / "README.md").write_text("Readme.\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    (root / "docs/page.md").write_text(page, encoding="utf-8")
    return ["CHANGELOG.md", "README.md", "docs/page.md"]


def test_the_release_grep_finds_every_next_release_marker() -> None:
    misses = marker_misses(ROOT, tracked_markdown())
    assert not misses, (
        "next-release marker(s) the release grep does not find (keep each marker phrase on one line):\n"
        + "\n".join(misses)
    )


def test_a_wrapped_marker_is_a_miss(tmp_path: Path) -> None:
    pages = scratch_tree(
        tmp_path,
        "Intro.\nFrom the next release (on\nmain, not in v0.3.1), more.\n"
        "from the next release (on main, not in v0.3.1)\n",
    )
    assert marker_misses(tmp_path, pages) == ["docs/page.md: 2 marker(s), the release grep finds 1"]


def test_a_wrapped_marker_after_a_whole_one_on_the_same_line_is_a_miss(tmp_path: Path) -> None:
    pages = scratch_tree(
        tmp_path,
        "From the next release (on main, not in v0.3.1), this; from the next\n"
        "release (on main, not in v0.3.1), that.\n",
    )
    assert marker_misses(tmp_path, pages) == ["docs/page.md: 2 marker(s), the release grep finds 1"]
    # Two whole markers on one line are both found.
    two = tmp_path / "two"
    two.mkdir()
    pages = scratch_tree(two, "from the next release (on main) and from the next release (on main)\n")
    assert marker_misses(two, pages) == []


def test_released_changelog_entries_are_not_counted(tmp_path: Path) -> None:
    pages = scratch_tree(tmp_path, "Page.\n")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n- from the next release (on main) here\n\n"
        "## 0.2.0 - 2026-01-01\n\n- from the next release (on\n  main) there\n",
        encoding="utf-8",
    )
    assert marker_misses(tmp_path, pages) == []
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## Unreleased\n\n- from the next release (on\n  main) here\n\n"
        "## 0.2.0 - 2026-01-01\n\n- from the next release (on main) there\n",
        encoding="utf-8",
    )
    assert marker_misses(tmp_path, pages) == ["CHANGELOG.md: 1 marker(s), the release grep finds 0"]
