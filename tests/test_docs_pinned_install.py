"""The documented install command pins this repository's release tag.

``claude plugin marketplace add`` with this repository's bare name follows main,
and main can carry plugin changes (its hooks, ``bin/ledger``, its options) that
no release has shipped. A person who copies the install command from an install
page must get the released tag's plugin.

What this test checks, line by line, on ``README.md``, ``docs/start-here.md``,
``docs/upgrading.md`` and ``CHANGELOG.md`` above its first released-version
heading:

- every line that names a marketplace add carries the exact pinned command

      claude plugin marketplace add https://github.com/smithadifd/ledger.git#vX.Y.Z

  with ``X.Y.Z`` the current release, ``__version__``; and
- each of the three install pages shows that command on at least one line.

Each line is normalized first: inline backticks and quote characters are
dropped, each run of whitespace (tabs included) becomes one space, and letter
case is ignored. A line names an add when its words hold ``marketplace`` and,
later on the same line, ``add``; a line that names only another marketplace verb
(``update``, ``remove``) is not an add. The pinned command may be followed by
more text on its line, but not by more of a tag or version (``-dev``, ``.1``,
another digit), so a longer tag does not pass for this one.

Not checked: whether the command renders as a code block (a pinned command in
prose passes), and an add command split across lines. The test reads lines of
text; it does not parse Markdown or the shell, so a fence, a list item or a
blockquote makes no difference to it, and a command spelled through shell
expansion (a variable, a glob, command substitution) is not seen.

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

# The one documented install command.
PINNED_SOURCE = f"https://github.com/smithadifd/ledger.git#v{__version__}"
PINNED_COMMAND = f"claude plugin marketplace add {PINNED_SOURCE}"
MISSING = f"no line shows `{PINNED_COMMAND}`"
RELEASED_HEADING = re.compile(r"^## \d+\.\d+\.\d+", re.MULTILINE)
# A "from the next release (on main" marker, wherever its words wrap.
MARKER = re.compile(r"from\s+the\s+next\s+release\s+\(on\s+main", re.IGNORECASE)


def normalize(line: str) -> str:
    """A line as both checks read it.

    Inline backticks and single and double quote characters are dropped, each
    run of whitespace becomes one space, and case is folded.
    """
    bare = re.sub(r"[`'\"]", "", line)
    bare = re.sub(r"\s+", " ", bare)
    return bare.strip().casefold()


PIN = normalize(PINNED_COMMAND)
# The pin, not followed by more of a tag or version: `-dev`, `/`, a letter or
# digit, or a dot and another one. A sentence's final dot is fine.
SHOWS_PIN = re.compile(re.escape(PIN) + r"(?![\w/-]|\.\w)")


def shows_pin(normalized: str) -> bool:
    return SHOWS_PIN.search(normalized) is not None


def names_add(normalized: str) -> bool:
    """Whether the word `marketplace` is followed, later on the line, by `add`."""
    words = normalized.split(" ")
    if "marketplace" not in words:
        return False
    return "add" in words[words.index("marketplace") + 1 :]


def unpinned_adds(text: str) -> list[str]:
    """Check 1: each line that names an add without the pin, as `number: line`."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        normalized = normalize(line)
        if names_add(normalized) and not shows_pin(normalized):
            found.append(f"{number}: {line}")
    return found


def install_problems(text: str) -> list[str]:
    """Checks 1 and 2 for one install page."""
    problems = unpinned_adds(text)
    if not any(shows_pin(normalize(line)) for line in text.splitlines()):
        problems.insert(0, MISSING)
    return problems


def above_first_release(text: str) -> str:
    """A changelog's Unreleased text: what precedes its first released heading."""
    released = RELEASED_HEADING.search(text)
    return text[: released.start()] if released else text


def with_pin(*lines: str) -> str:
    """A page of these lines, then the pinned command on its own line."""
    return "\n".join(lines) + f"\n\n{PINNED_COMMAND}\n"


def test_each_install_page_pins_every_add_and_shows_the_pin() -> None:
    for rel in INSTALL_PAGES:
        problems = install_problems((ROOT / rel).read_text(encoding="utf-8"))
        assert not problems, f"{rel}:\n" + "\n".join(problems)


def test_the_changelog_unreleased_section_pins_every_add() -> None:
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert RELEASED_HEADING.search(text), "CHANGELOG.md has no released-version heading"
    problems = unpinned_adds(above_first_release(text))
    assert not problems, "CHANGELOG.md:\n" + "\n".join(problems)


def test_changelog_history_below_the_first_release_is_not_checked() -> None:
    history = "# Changelog\n\n## Unreleased\n\nText.\n\n## 0.1.0 - 2026-01-01\n\n"
    unpinned = "Install it with `claude plugin marketplace add smithadifd/ledger`."
    assert unpinned_adds(above_first_release(history + unpinned + "\n")) == []
    above = "# Changelog\n\n## Unreleased\n\n" + unpinned + "\n\n## 0.1.0 - 2026-01-01\n"
    assert unpinned_adds(above_first_release(above)) == ["5: " + unpinned]


def test_the_pin_is_v_plus_the_package_version() -> None:
    assert PINNED_SOURCE.rsplit("#", 1) == [
        "https://github.com/smithadifd/ledger.git",
        "v" + __version__,
    ]
    assert PINNED_COMMAND == f"claude plugin marketplace add {PINNED_SOURCE}"


def test_the_pinned_command_passes_wherever_it_sits() -> None:
    install = "claude plugin install ledger-hooks@ledger-plugins"
    for page in (
        f"{PINNED_COMMAND}\n",
        f"```sh\n{PINNED_COMMAND}\n{install}\n```\n",
        f"~~~sh\n{PINNED_COMMAND}\n~~~\n",
        f"   {PINNED_COMMAND}\n",
        f"Run `{PINNED_COMMAND}` first.\n",
        f"Run {PINNED_COMMAND}.\n",
        f"- {PINNED_COMMAND}\n",
        f"1. `{PINNED_COMMAND}`\n",
        f"- ```sh\n  {PINNED_COMMAND}\n  ```\n",
        f"> {PINNED_COMMAND}\n",
        f"> ```sh\n> {PINNED_COMMAND}\n> ```\n",
        f"> - `{PINNED_COMMAND}`\n",
        f"{PINNED_COMMAND} # pinned\n",
    ):
        assert install_problems(page) == [], page


def test_two_spaces_between_marketplace_and_add() -> None:
    unpinned = "claude plugin marketplace  add smithadifd/ledger"
    assert install_problems(with_pin(unpinned)) == [f"1: {unpinned}"]
    # Whitespace runs collapse, so the pinned command passes with extra spaces too.
    spaced = f"claude  plugin marketplace   add {PINNED_SOURCE}"
    assert install_problems(f"{spaced}\n") == []


def test_a_tab_between_marketplace_and_add() -> None:
    unpinned = "claude plugin marketplace\tadd smithadifd/ledger"
    assert install_problems(with_pin(unpinned)) == [f"1: {unpinned}"]
    tabbed = f"claude plugin marketplace\tadd\t{PINNED_SOURCE}"
    assert install_problems(f"{tabbed}\n") == []


def test_quotes_around_add_or_marketplace() -> None:
    for unpinned in (
        "claude plugin marketplace 'add' smithadifd/ledger",
        'claude plugin marketplace "add" smithadifd/ledger',
        'claude plugin market"place" add smithadifd/ledger',
        "claude plugin 'marketplace' add smithadifd/ledger",
        "claude plugin marketplace '' add smithadifd/ledger",
    ):
        assert install_problems(with_pin(unpinned)) == [f"1: {unpinned}"], unpinned
    # Quote characters are dropped, so a quoted pinned command passes.
    for quoted in (
        f"claude plugin marketplace 'add' {PINNED_SOURCE}",
        f'claude plugin marketplace "add" {PINNED_SOURCE}',
    ):
        assert install_problems(f"{quoted}\n") == [], quoted


def test_marketplace_add_in_any_letter_case() -> None:
    for unpinned in (
        "claude plugin Marketplace Add smithadifd/ledger",
        "CLAUDE PLUGIN MARKETPLACE ADD smithadifd/ledger",
    ):
        assert install_problems(with_pin(unpinned)) == [f"1: {unpinned}"], unpinned


def test_an_unpinned_add_fails_wherever_it_sits() -> None:
    # The rule reads one line at a time, so no container makes a line safe.
    unpinned = "claude plugin marketplace add smithadifd/ledger"
    for line in (
        unpinned,
        f"Run `{unpinned}` to follow main.",
        f"    {unpinned}",
        f"```sh {unpinned}",
        f"- {unpinned}",
        f"* Run `{unpinned}`",
        f"1. `{unpinned}`",
        f"> {unpinned}",
        f"> > {unpinned}",
        f"> - {unpinned}",
        f"- > {unpinned}",
    ):
        assert install_problems(with_pin(line)) == [f"1: {line}"], line
    for opener, prefix, closer in (
        ("```sh", "", "```"),
        ("~~~", "", "~~~"),
        ("- ```sh", "  ", "  ```"),
        ("1. ```sh", "   ", "   ```"),
        ("> ```sh", "> ", "> ```"),
        ("> - ```sh", ">   ", ">   ```"),
    ):
        page = f"{opener}\n{prefix}{unpinned}\n{closer}\n\n{PINNED_COMMAND}\n"
        assert install_problems(page) == [f"2: {prefix}{unpinned}"], opener


def test_an_unpinned_add_inside_a_list_item_fence_fails() -> None:
    unpinned = "claude plugin marketplace add smithadifd/ledger"
    page = f"- Install:\n\n  ```sh\n  {PINNED_COMMAND}\n  {unpinned}\n  ```\n"
    assert install_problems(page) == [f"5:   {unpinned}"]
    page = f"- ```sh\n  {unpinned}\n  ```\n\n{PINNED_COMMAND}\n"
    assert install_problems(page) == [f"2:   {unpinned}"]


def test_an_unpinned_add_beside_a_pinned_one_fails() -> None:
    unpinned = "claude plugin marketplace add smithadifd/ledger"
    page = f"{PINNED_COMMAND}\n{unpinned}\n"
    assert install_problems(page) == [f"2: {unpinned}"]


def test_the_pinned_command_in_a_list_item_and_a_blockquote_passes() -> None:
    for page in (
        f"- Install it with `{PINNED_COMMAND}`.\n",
        f"- {PINNED_COMMAND}\n  - {PINNED_COMMAND}\n",
        f"> Install it with `{PINNED_COMMAND}`.\n",
        f"> {PINNED_COMMAND}\n> > {PINNED_COMMAND}\n",
    ):
        assert install_problems(page) == [], page


def test_a_page_with_no_pinned_line_fails() -> None:
    assert install_problems("Intro.\n\nNo install command here.\n") == [MISSING]
    assert install_problems("") == [MISSING]
    only_update = "Run `claude plugin marketplace update ledger-plugins`.\n"
    assert install_problems(only_update) == [MISSING]
    unpinned = "claude plugin marketplace add smithadifd/ledger"
    assert install_problems(f"{unpinned}\n") == [MISSING, f"1: {unpinned}"]


def test_marketplace_prose_that_is_not_an_add_passes() -> None:
    prose = (
        "Run `claude plugin marketplace update ledger-plugins` to refresh it.\n"
        "Remove it with `claude plugin marketplace remove ledger-plugins`.\n"
        "A marketplace added with a pin stays on that tag.\n"
        "The marketplace addition is described below.\n"
        "Add the marketplace with the pinned command:\n"
        "Configure it with `claude plugin configure ledger-hooks@<marketplace> --values-stdin`.\n"
    )
    assert install_problems(prose + f"\n{PINNED_COMMAND}\n") == []


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
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#v{v}0",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#v{v}-dev",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#v{v}/x",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger.git#v{v.rsplit('.', 1)[0]}",
        f"claude plugin marketplace add --scope user {PINNED_SOURCE}",
        f"claude plugin marketplace --scope user add {PINNED_SOURCE}",
        f"claude plugin marketplace add git@github.com:smithadifd/ledger.git#v{v}",
        f"claude plugin marketplace add http://github.com/smithadifd/ledger.git#v{v}",
        f"claude plugin marketplace add https://github.com/smithadifd/ledger/#v{v}",
        f"claude plugin marketplace add https://www.github.com/smithadifd/ledger.git#v{v}",
        f"claude plugin marketplace add https://gitlab.com/smithadifd/ledger.git#v{v}",
        f"claude plugin marketplace add https://github.com/smithadifd/%6cedger.git#v{v}",
        "claude plugin marketplace add smithadifd/skills",
        f"claude plugin marketplace add https://github.com/example/other.git#v{v}",
        f"marketplace add {PINNED_SOURCE}",
        "claude plugin marketplace add",
    ]
    for line in near_misses:
        assert install_problems(with_pin(line)) == [f"1: {line}"], line
        assert install_problems(f"{line}\n") == [MISSING, f"1: {line}"], line


def test_a_split_add_is_not_checked() -> None:
    # Documented limit: the rule reads one line at a time, so `marketplace`
    # and `add` on different lines are not seen.
    page = with_pin("claude plugin marketplace", "add smithadifd/ledger")
    assert install_problems(page) == []


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
