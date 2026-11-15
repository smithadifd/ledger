"""Checks that read this repository's own files (README, docs/).

These live outside ``ledger/`` on purpose: the package is vendored as a folder
on its own, and a consumer's copy has no README or docs beside it. Here the
files are always present, so nothing skips. ``LEDGER_REPO_ROOT`` is not needed.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import NamedTuple

import pytest

from ledger import cli
from ledger.paths import SUPPORTED_LOCATION
from ledger.schema import SCHEMAS
from ledger.verbs import discover_verbs
from ledger.verbs.resume import SHORT_FORM

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "agent-contract.md"
PACKAGED_PAGE = ROOT / "ledger" / "agent_contract.md"
README = ROOT / "README.md"
CLI_PAGE = ROOT / "docs" / "cli.md"
START = ROOT / "docs" / "start-here.md"
CHANGELOG = ROOT / "CHANGELOG.md"
DB_HOME_PAGES = (README, CLI_PAGE, PAGE, START)
DB_HOME = f"{SUPPORTED_LOCATION}/<system>.db"


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
        "ledger link NEW --rel child-of --target PARENT",
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


# ---- the finding verbs ---------------------------------------------------------

# One line per verb, nothing else: ``- `ledger VERB [ARGS]`: GIST; reach for it ...``.
# The gist is what the short form repeats beside the command.
_FINDING_LINE = re.compile(
    r"^- `(?P<command>ledger (?P<verb>[a-z][a-z0-9-]*)(?: [^`]*)?)`: "
    r"(?P<gist>[^;`]+); (?P<when>reach for it [^`]+\.)$"
)
_FINDING_PREFIX = "Finding things: "


def _finding_section(text: str) -> str:
    assert "\n## Finding things\n" in text, "the contract page has no '## Finding things' section"
    return text.split("## Finding things\n", 1)[1].split("\n## ", 1)[0]


def _finding_entries(text: str) -> list[dict[str, str]]:
    """The finding section's lines, parsed; a line outside the grammar raises.

    A blank line is the only thing the section may hold besides a verb line.
    """
    entries = []
    for line in _finding_section(text).splitlines():
        if not line.strip():
            continue
        match = _FINDING_LINE.match(line)
        assert match, f"finding section line is not `- `ledger VERB ...`: GIST; reach for it ...`: {line!r}"
        entries.append(match.groupdict())
    assert entries, "the finding section holds no verb line"
    verbs = [entry["verb"] for entry in entries]
    assert len(verbs) == len(set(verbs)), f"a verb has two lines in the finding section: {verbs}"
    return entries


def _short_form_finding_lines() -> list[str]:
    return [ln for ln in SHORT_FORM.splitlines() if ln.startswith(_FINDING_PREFIX)]


def test_finding_section_names_the_five_finding_verbs() -> None:
    verbs = {entry["verb"] for entry in _finding_entries(PAGE.read_text(encoding="utf-8"))}
    assert {"search", "context", "links", "questions", "decisions"} <= verbs, verbs


def test_every_verb_the_finding_section_names_is_registered() -> None:
    registered = set(discover_verbs())
    for entry in _finding_entries(PAGE.read_text(encoding="utf-8")):
        assert entry["verb"] in registered, (
            f"the finding section names `{entry['command']}` but {entry['verb']!r} "
            f"is not a registered verb: {sorted(registered)}"
        )


def test_the_finding_section_refuses_an_unregistered_verb_and_a_line_off_the_grammar() -> None:
    page = PAGE.read_text(encoding="utf-8")
    good = _finding_entries(page)
    registered = set(discover_verbs())
    planted = page.replace(
        "## Electing a ratification\n",
        "- `ledger frobnicate WORD`: a thing; reach for it when you need a thing.\n\n"
        "## Electing a ratification\n",
        1,
    )
    names = [entry["verb"] for entry in _finding_entries(planted)]
    assert names == [entry["verb"] for entry in good] + ["frobnicate"]
    assert "frobnicate" not in registered
    for off_grammar in (
        "- `ledger links ITEM`: both ways.",  # no "; reach for it"
        "- ledger links ITEM: both ways; reach for it when you need them.",  # no backticks
        "* `ledger links ITEM`: both ways; reach for it when you need them.",  # not a "-" bullet
        "A sentence about `ledger links ITEM` that names no verb line.",
    ):
        bad = page.replace(
            "## Electing a ratification\n", off_grammar + "\n\n## Electing a ratification\n", 1
        )
        try:
            _finding_entries(bad)
        except AssertionError:
            continue
        raise AssertionError(f"a finding line off the grammar was accepted: {off_grammar!r}")


def test_short_form_has_one_finding_line_with_the_pages_words() -> None:
    entries = _finding_entries(PAGE.read_text(encoding="utf-8"))
    lines = _short_form_finding_lines()
    assert len(lines) == 1, f"the short form needs exactly one {_FINDING_PREFIX!r} line: {lines}"
    [line] = lines
    for entry in entries:
        piece = f"{entry['command']} ({entry['gist']})"
        assert piece in line, f"the short form's finding line lacks {piece!r}: {line!r}"
    # Nothing else rides on the line: it is exactly the pieces, in the page's order.
    expected = _FINDING_PREFIX + "; ".join(
        f"{entry['command']} ({entry['gist']})" for entry in entries
    ) + "."
    assert line == expected, (line, expected)


# ---- the agent contract ships with the package -------------------------------


def test_the_packaged_contract_is_the_page_byte_for_byte() -> None:
    assert PACKAGED_PAGE.read_bytes() == PAGE.read_bytes(), (
        "ledger/agent_contract.md (what `ledger contract` prints) differs from "
        "docs/agent-contract.md; after editing the page, copy it over the packaged one"
    )


_BUILD_DEBRIS = shutil.ignore_patterns(
    ".git", "build", "dist", "*.egg-info", "__pycache__", ".venv", ".pytest_cache"
)


def test_an_installed_wheel_prints_the_contract_page(tmp_path) -> None:
    # Build from a clean copy so a stale build/ folder in the checkout cannot
    # supply a file the package data no longer lists. Building needs pip (and,
    # for the isolated build, the network or a cached setuptools).
    source = tmp_path / "source"
    shutil.copytree(ROOT, source, ignore=_BUILD_DEBRIS)
    wheels = tmp_path / "wheels"

    def run(args, **kwargs):
        done = subprocess.run(args, capture_output=True, text=True, timeout=600, **kwargs)
        assert done.returncode == 0, (args, done.stdout[-2000:], done.stderr[-2000:])
        return done

    run([sys.executable, "-m", "pip", "wheel", str(source), "-w", str(wheels),
         "--no-deps", "-q"])
    [wheel] = wheels.glob("ledger-*.whl")
    venv = tmp_path / "venv"
    run([sys.executable, "-m", "venv", str(venv)])
    run([str(venv / "bin" / "python"), "-m", "pip", "install", "--no-deps",
         "--no-index", "-q", str(wheel)])

    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "LEDGER_DB")}
    env["PYTHONIOENCODING"] = "utf-8"
    where = run([str(venv / "bin" / "python"), "-c", "import ledger; print(ledger.__file__)"],
                cwd=tmp_path, env=env)
    assert Path(where.stdout.strip()).resolve().is_relative_to(venv.resolve()), where.stdout
    db = tmp_path / "never" / "ledger.db"
    printed = subprocess.run(
        [str(venv / "bin" / "ledger"), "--db", str(db), "contract"],
        cwd=tmp_path, env=env, capture_output=True, timeout=120,
    )
    assert printed.returncode == 0, printed.stderr
    assert printed.stdout == PAGE.read_bytes()
    assert not db.parent.exists()


# ---- install, upgrade, first-run and database-home docs ----------------------


def _flat(path: Path) -> str:
    return _squash(path.read_text(encoding="utf-8"))


# Facts that drift: a pinned version, a schema number, a verb or test count, a
# claim that no tag exists. The docs drop them; a number that stays is tied to
# its source by the tests below.
DRIFTING_FACTS = [
    r"schema_version: \d",
    r"schema version \d",
    r"[Vv]ersion \d+\.\d+\.\d+",
    r"no tag yet",
    r"\b(?:\d+|(?:twenty|thirty|forty|fifty)(?:-[a-z]+)?) verbs\b",
    r"about \d+ of them",
]


def test_readme_and_start_here_state_no_fact_that_drifts() -> None:
    for page in (README, START):
        flat = _flat(page)
        for pattern in DRIFTING_FACTS:
            found = re.search(pattern, flat)
            assert found is None, f"{page.name} states a drifting fact: {found.group(0)!r}"


def test_python_versions_the_docs_name_match_their_sources() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requires = project["project"]["requires-python"]
    assert requires.startswith(">="), requires
    minimum = requires.removeprefix(">=")
    for page in (README, START):
        assert f"Python {minimum} or newer" in _flat(page), (page.name, minimum)
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    matrix = re.search(r"python-version: \[(.*?)\]", workflow).group(1)
    tested = re.findall(r"\d+\.\d+", matrix)
    assert tested, matrix
    stated = re.search(r"run in CI on Python ((?:\d+\.\d+(?:,? and |, )?)+)", _flat(README))
    assert stated is not None, "the README no longer says which Python versions CI runs"
    assert re.findall(r"\d+\.\d+", stated.group(1)) == tested


# GitHub's defaults when `pull_request:` names no types; a draft pull request
# marked ready for review starts a run only if `ready_for_review` is listed too.
PULL_REQUEST_TYPES = {"opened", "synchronize", "reopened", "ready_for_review"}


def test_ci_runs_on_the_draft_to_ready_flip() -> None:
    # Read without a YAML parser (PyYAML is not a test dependency). Accepted
    # shape: `pull_request:` two spaces in under the top-level `on:`, with one
    # `types: [...]` flow list among its children. Any other shape (a bare
    # trigger, a block list, a second `types:` line) fails here.
    lines = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8").splitlines()
    assert "on:" in lines, "ci.yml has no top-level `on:` line"
    triggers: list[str] = []
    for line in lines[lines.index("on:") + 1 :]:
        if line and not line.startswith(" "):
            break
        triggers.append(line)
    assert "  pull_request:" in triggers, triggers
    children: list[str] = []
    for line in triggers[triggers.index("  pull_request:") + 1 :]:
        if line.strip() and not line.startswith("   "):
            break
        children.append(line)
    types = [line for line in children if line.lstrip().startswith("types:")]
    assert len(types) == 1, f"pull_request needs exactly one `types:` line: {children!r}"
    match = re.fullmatch(r"    types: \[([^\]]*)\]\s*", types[0])
    assert match is not None, f"pull_request types are not a flow list: {types[0]!r}"
    listed = {name.strip() for name in match.group(1).split(",")}
    missing = sorted(PULL_REQUEST_TYPES - listed)
    assert not missing, f"ci.yml pull_request types lack {missing}: {types[0]!r}"


def test_install_and_upgrade_use_pipx_never_a_bare_pip() -> None:
    tag_install = "pipx install git+https://github.com/smithadifd/ledger@vX.Y.Z"
    for page in (README, START):
        flat = _flat(page)
        assert tag_install in flat, page.name
        # The whole page, flattened, so prose wrapped across two lines is seen
        # as one; a shell line continuation (a backslash at the end of a line)
        # is joined first. Allowed: the virtual environment's own pip (`.venv`,
        # with an optional leading `./`, starting the path: at the start of the
        # text, or after whitespace or a backtick), and the sentence that tells
        # the reader not to run a bare `pip install`; nothing else. Covers pip,
        # pip3 and pip3.NN, with any dash-prefixed options before `install`.
        joined = _squash(re.sub(r"\\\r?\n", " ", page.read_text(encoding="utf-8")))
        text = re.sub(r"(?<![^\s`])(?:\./)?\.venv/bin/python -m pip install", "", joined)
        text = re.sub(r"Do not (?:run|use) a bare `pip install`", "", text)
        bare = re.search(r"\bpip(?:3(?:\.\d+)?)?(?:\s+-\S*)*\s+install\b", text)
        assert bare is None, (
            page.name,
            text[max(0, bare.start() - 40) : bare.end() + 40],
        )
    readme = README.read_text(encoding="utf-8")
    assert "python3 -m venv" in _squash(readme)
    upgrade = _squash(readme.split("\n## Upgrade", 1)[1].split("\n## ", 1)[0])
    assert "pipx upgrade ledger" in upgrade
    assert "pipx install --force git+https://github.com/smithadifd/ledger@vX.Y.Z" in upgrade
    assert "ledger --version" in upgrade and "ledger doctor" in upgrade
    assert "no other step" in upgrade and "no wrapper" in upgrade


def test_ledger_has_no_default_database_path(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.delenv("LEDGER_DB", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    assert cli.main(["doctor"]) == 2
    assert "pass --db or set LEDGER_DB" in capsys.readouterr().err
    assert not (tmp_path / ".local").exists()


def test_every_page_spells_the_database_home_as_paths_py_does() -> None:
    assert SUPPORTED_LOCATION == "~/.local/share/ledger"
    for page in DB_HOME_PAGES:
        flat = _flat(page)
        assert DB_HOME in flat, f"{page.name} lacks {DB_HOME!r}"
        assert "no default" in flat.lower(), f"{page.name} does not say there is no default"
        for token in re.findall(r"[~\w/.<>-]*\.local/share[\w/.<>-]*", flat):
            token = token.rstrip(".")
            assert token == SUPPORTED_LOCATION or token.startswith(
                SUPPORTED_LOCATION + "/"
            ), f"{page.name} spells the database home {token!r}"


def test_start_here_export_line_points_under_the_database_home() -> None:
    # Every such line is checked whole: after the value (up to whitespace or a
    # shell separator `;`, `&`, `|`) only whitespace and an optional `# comment`
    # may follow, so a second assignment or command on the line fails.
    lines = [
        line
        for line in START.read_text(encoding="utf-8").splitlines()
        if re.match(r"\s*export LEDGER_DB=", line)
    ]
    assert lines, "docs/start-here.md has no `export LEDGER_DB=` line"
    for line in lines:
        whole = re.fullmatch(r"\s*export LEDGER_DB=([^\s;&|]+)(?:\s+#.*)?\s*", line)
        assert whole is not None, (
            f"start-here's export line {line!r} has something after the value "
            "(only whitespace and a # comment may follow)"
        )
        value = whole.group(1)
        assert "$" not in value, f"start-here's export line {value!r} uses a shell variable ($)"
        assert ".." not in value.split("/"), (
            f"start-here's export line {value!r} has a parent-directory (..) segment"
        )
        assert value.startswith(SUPPORTED_LOCATION + "/"), (
            f"start-here's export line points at {value!r}, not under {SUPPORTED_LOCATION}/"
        )
        assert value.endswith(".db"), f"start-here's export line {value!r} does not end in .db"


def test_start_here_exists_and_the_readme_links_it_from_the_top() -> None:
    assert START.is_file()
    readme = README.read_text(encoding="utf-8")
    link = "(docs/start-here.md)"
    assert link in readme
    assert readme.index(link) < readme.index("\n## Why")


def test_start_here_runs_in_order_and_names_the_environment_the_code_reads() -> None:
    flat = _flat(START)
    steps = [
        "Python 3.11 or newer",
        "pipx --version",
        "pipx install git+https://github.com/smithadifd/ledger@vX.Y.Z",
        "no default database path",
        "LEDGER_HARNESS",
        "There is no `init` verb",
        "ledger new --type ticket",
        "ledger doctor",
    ]
    positions = [flat.index(step) for step in steps]
    assert positions == sorted(positions), dict(zip(steps, positions))
    source = (ROOT / "ledger" / "cli.py").read_text(encoding="utf-8")
    for name in ("LEDGER_DB", "LEDGER_HARNESS", "LEDGER_AGENT"):
        assert name in flat, name
        assert f'"{name}"' in source, f"cli.py no longer reads {name}"
    assert not (ROOT / "ledger" / "verbs" / "init.py").exists()
    assert "https://github.com/smithadifd/skills" in flat


def test_changelog_0_1_16_names_the_pipx_docs_and_the_start_here_page() -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    sections = re.split(r"^## ", text, flags=re.MULTILINE)[1:]
    assert sections, "CHANGELOG.md has no second-level heading"
    heading = sections[0].partition("\n")[0]
    assert re.fullmatch(r"Unreleased|\d+\.\d+\.\d+ \u2014 \d{4}-\d{2}-\d{2}", heading), (
        f"newest CHANGELOG heading is neither Unreleased nor a dated version: {heading!r}"
    )
    added = [s for s in sections if s.startswith("0.1.16 ")]
    assert len(added) == 1, "CHANGELOG.md must keep exactly one 0.1.16 section"
    body = _squash(added[0].partition("\n")[2])
    assert "pipx" in body and "docs/start-here.md" in body


def _readme_verb_rows() -> set[str]:
    """The verb names that have a row in the README's ``## Verbs`` table."""
    text = README.read_text(encoding="utf-8")
    section = text.split("\n## Verbs\n", 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `([^`]+)` \|", section, flags=re.MULTILINE))


def test_readme_verb_table_has_a_row_for_every_registered_verb() -> None:
    """A new verb module fails here until its README row exists."""
    from ledger.verbs import discover_verbs

    registered = set(discover_verbs())
    assert registered, "the verb registry is empty"
    missing = sorted(registered - _readme_verb_rows())
    assert not missing, f"README verb table has no row for: {', '.join(missing)}"


DATED_HEADING = re.compile(r"(\d+\.\d+\.\d+) \u2014 \d{4}-\d{2}-\d{2}")


def test_newest_changelog_heading_names_the_package_version() -> None:
    # Headings are the `## ` lines. `Unreleased` is skipped; the first other
    # heading must be a dated version (`X.Y.Z`, an em dash, `YYYY-MM-DD`)
    # and name pyproject.toml's version. A CHANGELOG with no dated heading, or
    # whose newest heading is not of that form, fails: it never falls through
    # to an older heading.
    headings = re.findall(r"^## (.+?)[ \t]*$", CHANGELOG.read_text(encoding="utf-8"), re.MULTILINE)
    dated = [heading for heading in headings if heading != "Unreleased"]
    assert dated, f"CHANGELOG.md has no dated heading: {headings!r}"
    newest = DATED_HEADING.fullmatch(dated[0])
    assert newest is not None, (
        f"newest CHANGELOG heading is not a dated version (X.Y.Z \u2014 YYYY-MM-DD): {dated[0]!r}"
    )
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    assert newest.group(1) == version, (
        f"newest CHANGELOG heading names {newest.group(1)!r}, "
        f"but pyproject.toml's version is {version!r}"
    )


EXPORT_GUARANTEE = (
    "The guard guarantees that every byte the ledger's own commit and push send is "
    "reachable from a pinned commit whose full history passed the scan; it does not "
    "guarantee which branch, index or destination ends up updated when another "
    "same-user git process changes HEAD, refs or config while the command runs."
)


def test_the_export_entry_states_the_commit_and_push_guarantee_word_for_word() -> None:
    # The export entry runs from its heading to the next verb heading.
    page = CLI_PAGE.read_text(encoding="utf-8")
    entry = page.split("**`export [--dir DIR]", 1)[1].split("\n**`", 1)[0]
    assert _squash(EXPORT_GUARANTEE) in _squash(entry)


def test_the_docs_name_the_claim_system_verb_and_its_rule() -> None:
    cli = CLI_PAGE.read_text(encoding="utf-8")
    assert "**`claim-system NAME`**" in cli
    assert "lowercase letter" in cli
    assert "read-only database file refuses even the repeat" in _squash(cli)
    start = START.read_text(encoding="utf-8")
    assert "ledger claim-system" in start


def test_concepts_documents_note_search_and_severity() -> None:
    text = (ROOT / "docs" / "concepts.md").read_text(encoding="utf-8")
    section = text.split("## The search index", 1)[1].split("\n## ", 1)[0]
    assert "A note is searched as part of its item, not as its own document" in _squash(section)
    for severity in ("`ok`", "`info`", "`warn`"):
        assert f"| {severity} |" in section


# ---- the install path, run as printed -------------------------------------------
#
# The walk from nothing to a working ledger runs the commands the docs print, in
# order, in an empty home, with only `ledger` on PATH (a wrapper that runs this
# checkout). A step the docs do not print is a step the run does not have.
#
# No byte of a page is ever run, and none becomes a path or an environment value.
# Every block of a walked section (fenced with any info string, fenced inside a
# list, or indented) is pinned below, in order, as a text constant of this file,
# and the page's block must equal its constant exactly. A test that runs a block
# first checks the block's section against its pins, so a new, removed,
# reordered, retagged or changed block fails by name before anything runs. The
# commands then come from the constant alone, and run as argument vectors with no
# shell, in the throwaway home, with an explicit minimal environment.


def _fenced(text: str, lang: str) -> list[str]:
    """The unindented fenced blocks of one language, in order, each with its final newline."""
    return re.findall(rf"^```{lang}\n(.*?)^```$", text, flags=re.MULTILINE | re.DOTALL)


def _between(text: str, start: str, end: str) -> str:
    assert text.count(start) == 1, f"{start!r} occurs {text.count(start)} times"
    return text.split(start, 1)[1].split(end, 1)[0]


class Block(NamedTuple):
    indent: str  # the indent of the opening fence; "" for an indented block
    info: str  # the fence's info string, or "indented"
    text: str  # every line between the fences, verbatim, each with its newline


class Pin(NamedTuple):
    name: str
    block: Block
    runs: bool
    why: str


_FENCE = re.compile(r"^([ \t]*)(`{3,}|~{3,})(.*)$")
_INDENTED = re.compile(r"^(?: {4,}|\t)\S")

# The container forms the enumeration does not model. A line that has one is
# refused by name before anything is enumerated, even when it is harmless prose.
_REFUSED = (
    ("blockquote line", re.compile(r"^[ \t]*>")),
    ("HTML tag line", re.compile(r"^[ \t]*<[a-z/!]", re.IGNORECASE)),
    ("list-marker fence", re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])[ \t]+(?:`{3,}|~{3,})")),
)


def _refused(text: str) -> list[tuple[int, str, str]]:
    """Every line of a stretch that has a container form the walk refuses, as (line, rule, text)."""
    return [
        (number, rule, line)
        for number, line in enumerate(text.split("\n"), 1)
        for rule, pattern in _REFUSED
        if pattern.match(line)
    ]


def _blocks(text: str) -> list[Block]:
    """Every top-level block of a stretch of a page, in order.

    A fence is three or more backticks or tildes at the start of a line, after
    any indent; it closes at the first line of the same character, at least as
    long, with nothing else on it. A block with no closing fence keeps an info
    string starting `unclosed`, which no pin has. Outside a fence, a run of
    lines indented four spaces or a tab (with the blank lines between them) is
    one `indented` block. Blockquotes, HTML tags and fences opened on a list
    marker line are not modelled: `_refused` fails the section on them first.
    """
    lines = text.split("\n")
    found: list[Block] = []
    i = 0
    while i < len(lines):
        fence = _FENCE.match(lines[i])
        if fence:
            indent, marker, info = fence.groups()
            close = re.compile(rf"^[ \t]*{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$")
            body: list[str] = []
            i += 1
            while i < len(lines) and not close.match(lines[i]):
                body.append(lines[i] + "\n")
                i += 1
            found.append(Block(indent, info if i < len(lines) else f"unclosed {info}", "".join(body)))
            i += 1
        elif _INDENTED.match(lines[i]):
            body = []
            while i < len(lines) and (_INDENTED.match(lines[i]) or not lines[i].strip()):
                body.append(lines[i] + "\n")
                i += 1
            found.append(Block("", "indented", "".join(body)))
        else:
            i += 1
    return found


# The one pipeline the docs print. Its block is pinned like every other; the pipe
# is never given to a shell: the test splits the constant at its pipe and runs
# the two halves as argument vectors, the first one's output fed to the second.
TAG_COMMAND = (
    "git ls-remote --tags --refs --sort=-v:refname https://github.com/smithadifd/ledger 'v*'"
    " | sed -n '1s|.*refs/tags/||p'"
)
TAG_REPOSITORY = "https://github.com/smithadifd/ledger"
TAG_BLOCK = TAG_COMMAND + "\n"

PIPX_BLOCK = "brew install pipx\npipx ensurepath\n"
PIPX_INSTALL_BLOCK = (
    "pipx install git+https://github.com/smithadifd/ledger@vX.Y.Z\n"
    "ledger --version\n"
)
CLONE_BLOCK = (
    "git clone https://github.com/smithadifd/ledger\n"
    "cd ledger\n"
    "python3 -m venv .venv\n"
    ".venv/bin/python -m pip install .\n"
    ".venv/bin/ledger --version\n"
)
README_ENV_BLOCK = (
    "export LEDGER_DB=~/.local/share/ledger/work.db\n"
    "export LEDGER_OPERATOR=me\n"
    "export LEDGER_HARNESS=cli\n"
)
SETTINGS_ENV_BLOCK = (
    "{\n"
    '  "env": {\n'
    '    "LEDGER_DB": "/absolute/path/to/work.db",\n'
    '    "LEDGER_OPERATOR": "me"\n'
    "  }\n"
    "}\n"
)
UNINSTALL_PLUGINS_BLOCK = (
    "   claude plugin uninstall ledger-hooks@ledger-plugins\n"
    "   claude plugin marketplace remove ledger-plugins\n"
)
UNINSTALL_COMMAND_BLOCK = "   pipx uninstall ledger\n   brew uninstall pipx\n"
QUICK_START_BLOCK = (
    "mkdir -p /tmp/ledger-demo\n"
    "export LEDGER_DB=/tmp/ledger-demo/ledger.db   # throwaway; real: ~/.local/share/ledger/<system>.db\n"
    "export LEDGER_HARNESS=cli                     # required for writes: what is writing\n"
    "export LEDGER_OPERATOR=me                     # who the writes are for\n"
    "\n"
    'ledger new --type ticket --title "Write the quick start" --id demo-1\n'
    "ledger doctor\n"
    'ledger note demo-1 --text "First draft of the quick start is done"\n'
    "ledger append --item demo-1 --kind item.progress \\\n"
    "  --payload '{\"text\": \"examples checked against a fresh database\"}'\n"
    "ledger card demo-1\n"
    "ledger search draft\n"
    "ledger verify\n"
)
QUICK_START_DOCTOR_OUTPUT = (
    "path: /tmp/ledger-demo/ledger.db\n"
    "schema_version: N\n"
    "search: missing (ledger index rebuilds it)\n"
)
QUICK_START_CARD_OUTPUT = (
    "# demo-1\n"
    "revision: 3 <hash of event 3>\n"
    "source_event_ids: 1, 2, 3\n"
    "type: ticket\n"
    "title: Write the quick start\n"
    "status: open\n"
    "...\n"
    "## Notes\n"
    "- event=2: First draft of the quick start is done\n"
    "\n"
    "## Progress\n"
    "- event=3: examples checked against a fresh database\n"
    "\n"
    "This card is a projection; the events are the authority (ledger export-item -- demo-1)\n"
)
# README.md and docs/cli.md print the same ask, so one constant pins both.
ASK_BLOCK = (
    "ledger append --item demo-1 --kind decision.asked --payload '{\n"
    '  "question": "Which database name?",\n'
    '  "options": [\n'
    '    {"label": "work", "description": "one file for work"},\n'
    '    {"label": "home", "description": "one file for home"}\n'
    "  ],\n"
    '  "recommendation": "work",\n'
    '  "scope": "the name of the first database"\n'
    "}'\n"
    "# appended  decision.asked  event=6  item=demo-1\n"
)
# The two commands docs/cli.md prints for several acceptance bullets. The block
# uses shell syntax (`$'...'` and a command substitution) a shell would expand,
# so the test derives from the constant the one value both forms stand for and
# runs `spec set` with that value as an argument vector.
ACCEPTANCE_BLOCK = (
    'ledger spec set demo-1 --title "Quick start" --body "What to write" \\\n'
    "  --acceptance $'- the examples run\\n- the output is checked'\n"
    'ledger spec set demo-1 --title "Quick start" --body "What to write" \\\n'
    "  --acceptance \"$(printf '%s\\n' '- the examples run' '- the output is checked')\"\n"
)
START_PYTHON_BLOCK = "python3 --version\n"
START_PIPX_CHECK_BLOCK = "pipx --version\n"
START_DB_BLOCK = "export LEDGER_DB=~/.local/share/ledger/work.db\n"
START_ENV_BLOCK = "export LEDGER_OPERATOR=me\nexport LEDGER_HARNESS=cli\n"
START_FIRST_WRITE_BLOCK = 'ledger new --type ticket --title "My first item" --id first-1\n'
START_CLAIM_BLOCK = "ledger claim-system work\n"
START_CHECK_BLOCK = "ledger doctor\nledger verify\n"
START_CHECK_OUTPUT = (
    "path: <the database path from step 4>\n"
    "schema_version: N\n"
    "search: missing (ledger index rebuilds it)\n"
)
START_HOOKS_BLOCK = (
    "claude plugin marketplace add smithadifd/ledger\n"
    "claude plugin install ledger-hooks@ledger-plugins\n"
)


def _pin(name: str, text: str, *, runs: bool, why: str, info: str = "sh", indent: str = "") -> Pin:
    return Pin(name, Block(indent, info, text), runs, why)


_OUTPUT = "printed output to compare by eye, not a command"
_HOST = "changes the machine it runs on"

# Each walked section, in page order, with every block it holds, in order.
PINS: dict[str, tuple[Pin, ...]] = {
    "README.md, Install": (
        _pin("install pipx", PIPX_BLOCK, runs=False, why=f"installs pipx; {_HOST}"),
        _pin("newest tag", TAG_BLOCK, runs=True, why="TAG_COMMAND, against a local repository"),
        _pin("install at a tag", PIPX_INSTALL_BLOCK, runs=False,
             why="installs from the network; the walk runs this checkout's ledger"),
        _pin("install from a clone", CLONE_BLOCK, runs=False, why="clones from the network"),
        _pin("shell environment", README_ENV_BLOCK, runs=False,
             why="the values of start-here steps 4 and 5, which the walk runs"),
        _pin("settings env block", SETTINGS_ENV_BLOCK, info="json", runs=False,
             why="settings file content, not a command"),
    ),
    "README.md, Uninstall": (
        _pin("remove the plugins", UNINSTALL_PLUGINS_BLOCK, indent="   ", runs=False,
             why=f"removes Claude Code plugins; {_HOST}"),
        _pin("remove the command", UNINSTALL_COMMAND_BLOCK, indent="   ", runs=False,
             why=f"removes ledger and pipx; {_HOST}"),
    ),
    "README.md, Quick start": (
        _pin("quick start", QUICK_START_BLOCK, runs=True,
             why="QUICK_START_BLOCK, with /tmp/ledger-demo moved under the test's folder"),
        _pin("doctor output", QUICK_START_DOCTOR_OUTPUT, info="text", runs=False, why=_OUTPUT),
        _pin("card output", QUICK_START_CARD_OUTPUT, info="text", runs=False, why=_OUTPUT),
    ),
    "README.md, Asking a question": (
        _pin("ask", ASK_BLOCK, runs=True, why="ASK_BLOCK, after a first item"),
    ),
    "docs/cli.md, spec set": (
        _pin("acceptance", ACCEPTANCE_BLOCK, runs=True,
             why="the one value both forms of ACCEPTANCE_BLOCK stand for, passed to spec set"),
    ),
    "docs/cli.md, Asking a question": (
        _pin("ask", ASK_BLOCK, runs=True, why="ASK_BLOCK, after a first item"),
    ),
    "docs/start-here.md, before the first step": (),
    "docs/start-here.md, ## 1. Python 3.11 or newer": (
        _pin("python version", START_PYTHON_BLOCK, runs=False,
             why="the interpreter running this suite is the check"),
    ),
    "docs/start-here.md, ## 2. pipx": (
        _pin("pipx check", START_PIPX_CHECK_BLOCK, runs=False,
             why="reads the machine's pipx, which the walk does not use"),
        _pin("install pipx", PIPX_BLOCK, runs=False, why=f"installs pipx; {_HOST}"),
    ),
    "docs/start-here.md, ## 3. Install the ledger at a tag": (
        _pin("newest tag", TAG_BLOCK, runs=True, why="TAG_COMMAND, against a local repository"),
        _pin("install at a tag", PIPX_INSTALL_BLOCK, runs=False,
             why="installs from the network; the walk runs this checkout's ledger"),
    ),
    "docs/start-here.md, ## 4. Decide where the database lives": (
        _pin("database path", START_DB_BLOCK, runs=True, why="the walk, step 4"),
    ),
    "docs/start-here.md, ## 5. Set the environment": (
        _pin("operator and harness", START_ENV_BLOCK, runs=True, why="the walk, step 5"),
        _pin("settings env block", SETTINGS_ENV_BLOCK, info="json", runs=False,
             why="settings file content, not a command"),
    ),
    "docs/start-here.md, ## 6. Make the first write": (
        _pin("first write", START_FIRST_WRITE_BLOCK, runs=True, why="the walk, step 6"),
    ),
    "docs/start-here.md, ## 7. Claim the database for its system": (
        _pin("claim", START_CLAIM_BLOCK, runs=True, why="the walk, step 7"),
    ),
    "docs/start-here.md, ## 8. Check with `doctor`": (
        _pin("check", START_CHECK_BLOCK, runs=True, why="the walk, step 8"),
        _pin("check output", START_CHECK_OUTPUT, info="text", runs=False, why=_OUTPUT),
    ),
    "docs/start-here.md, ## 9. Connect Claude Code: the hooks plugin": (
        _pin("hooks plugin", START_HOOKS_BLOCK, runs=False,
             why=f"installs a Claude Code plugin from the network; {_HOST}"),
    ),
    "docs/start-here.md, ## 10. Optional: the skills plugin": (),
    "docs/start-here.md, ## 11. Optional: a scheduled export": (),
    "docs/start-here.md, ## Next": (),
}

# Where each section of README.md and docs/cli.md starts and ends. docs/start-here.md
# is walked whole, one section per `## ` heading.
_SLICES = {
    "README.md, Install": (README, "\n## Install\n", "\n## Upgrade\n"),
    "README.md, Uninstall": (README, "\n## Uninstall\n", "\n## Quick start\n"),
    "README.md, Quick start": (README, "\n## Quick start\n", "\n## The model\n"),
    "README.md, Asking a question": (README, "**Asking a question.**", "\n## Verbs\n"),
    "docs/cli.md, spec set": (CLI_PAGE, "**`spec set|show|diff ITEM ...`**", "\n**`"),
    "docs/cli.md, Asking a question": (
        CLI_PAGE, "**Asking a question: `append --kind decision.asked", "\n**`"
    ),
}
_START_PREFIX = "docs/start-here.md, "


def _slice(text: str, start: str, end: str) -> str:
    assert text.count(start) == 1, f"{start!r} occurs {text.count(start)} times"
    rest = text.split(start, 1)[1]
    assert end in rest, f"nothing ends the section after {start!r} with {end!r}"
    return rest.split(end, 1)[0]


def _walked_sections() -> list[tuple[str, str]]:
    sections = [
        (name, _slice(page.read_text(encoding="utf-8"), start, end))
        for name, (page, start, end) in _SLICES.items()
    ]
    parts = re.split(r"^(## .*)$", START.read_text(encoding="utf-8"), flags=re.MULTILINE)
    sections.append((_START_PREFIX + "before the first step", parts[0]))
    sections += [(_START_PREFIX + heading, body) for heading, body in zip(parts[1::2], parts[2::2])]
    return sections


def _start_section(number: int) -> str:
    [name] = [name for name in PINS if name.startswith(f"{_START_PREFIX}## {number}. ")]
    return name


def _assert_pinned(*names: str) -> None:
    """Each named section's blocks are exactly its pins, in order; fails by section and block."""
    sections = dict(_walked_sections())
    for name in names:
        assert name in sections, f"{name}: the page no longer has this section"
        for number, rule, line in _refused(sections[name]):
            raise AssertionError(
                f"{name}: line {number} is a {rule}, refused: the walk models only top-level "
                f"fenced and indented blocks, so a container form can hold code it does not "
                f"enumerate: {line!r}"
            )
        found = _blocks(sections[name])
        pins = PINS[name]
        for index, (pin, block) in enumerate(zip(pins, found), 1):
            assert block == pin.block, (
                f"{name}: block {index} is not the pinned {pin.name!r} block: "
                f"pinned {pin.block!r}, found {block!r}"
            )
        assert len(found) == len(pins), (
            f"{name}: {len(found)} blocks found, {len(pins)} pinned; "
            f"unpinned {found[len(pins):]!r}; missing {[pin.name for pin in pins[len(found):]]!r}"
        )


def test_every_block_of_a_walked_section_is_pinned_in_order() -> None:
    names = [name for name, _ in _walked_sections()]
    assert names == list(PINS), f"the walked sections are not the pinned ones: {names!r}"
    _assert_pinned(*PINS)
    for name, pins in PINS.items():
        for pin in pins:
            assert pin.why.strip(), f"{name}: {pin.name!r} gives no reason"


_PLANTS = (
    ("> ```sh\n> ledger sentinel-verb\n> ```\n", "blockquote line"),
    ("> > ```sh\n> > ledger sentinel-verb\n> > ```\n> ```\n", "blockquote line"),
    (">     ledger sentinel-verb\n", "blockquote line"),
    ("  > a quoted line of prose\n", "blockquote line"),
    ("<pre><code>ledger sentinel-verb</code></pre>\n", "HTML tag line"),
    ("<PRE><code>ledger sentinel-verb</code></PRE>\n", "HTML tag line"),
    ("  <div>\n", "HTML tag line"),
    ("</div>\n", "HTML tag line"),
    ("<!-- a comment -->\n", "HTML tag line"),
    ("- ```sh\n  ledger sentinel-verb\n  ```\n", "list-marker fence"),
    ("  * ~~~sh\n    ledger sentinel-verb\n    ~~~\n", "list-marker fence"),
    ("1. ```sh\n   ledger sentinel-verb\n   ```\n", "list-marker fence"),
    ("12) ```sh\n    ledger sentinel-verb\n    ```\n", "list-marker fence"),
    ("- ```sh\n  ledger sentinel-verb\n", "list-marker fence"),
)


def test_a_container_form_in_a_walked_section_fails_by_name_before_anything_runs(monkeypatch) -> None:
    # The enumeration models only top-level fenced and indented blocks.
    assert _blocks("Lead.\n\n```sh\nledger a\n```\n") == [Block("", "sh", "ledger a\n")]
    assert _blocks("Lead.\n\n    ledger b\n\nProse.\n") == [Block("", "indented", "    ledger b\n\n")]
    name = "docs/start-here.md, ## 6. Make the first write"
    clean = dict(_walked_sections())[name]
    assert _refused(clean) == []
    for plant, rule in _PLANTS:
        planted = clean + "\n" + plant
        monkeypatch.setattr(sys.modules[__name__], "_walked_sections", lambda planted=planted: [(name, planted)])
        with pytest.raises(AssertionError, match=rf"{re.escape(name)}: line \d+ is a {rule}, refused") as caught:
            _assert_pinned(name)
        assert plant.split("\n")[0] in str(caught.value) or plant.split("\n")[0].strip() in str(caught.value)


def test_the_pins_marked_run_are_the_constants_the_walk_runs() -> None:
    runs = [pin.block.text for pins in PINS.values() for pin in pins if pin.runs]
    assert runs == [
        TAG_BLOCK, QUICK_START_BLOCK, ASK_BLOCK, ACCEPTANCE_BLOCK, ASK_BLOCK, TAG_BLOCK,
        START_DB_BLOCK, START_ENV_BLOCK, START_FIRST_WRITE_BLOCK, START_CLAIM_BLOCK,
        START_CHECK_BLOCK,
    ]


def _start_steps() -> dict[int, str]:
    """docs/start-here.md's numbered steps: number -> the text from the heading's title on."""
    parts = re.split(r"^## (\d+)\. ", START.read_text(encoding="utf-8"), flags=re.MULTILINE)
    return {int(number): body for number, body in zip(parts[1::2], parts[2::2])}


def _empty_home(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """An empty home and a minimal environment: PATH has a `ledger` and the system tools only."""
    home = tmp_path / "home"
    home.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    wrapper = bin_dir / "ledger"
    wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m ledger "$@"\n')
    wrapper.chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PYTHONPATH": str(ROOT),
        "PYTHONIOENCODING": "utf-8",
    }
    return home, env


_BASE_ENV = frozenset({"HOME", "PATH", "PYTHONPATH", "PYTHONIOENCODING"})
_LEDGER_ENV = frozenset({"LEDGER_DB", "LEDGER_HARNESS", "LEDGER_OPERATOR"})


def _constant_steps(constant: str, home: Path) -> list[tuple[str, ...]]:
    """A block constant of this file as ("env", NAME, value) and ("run", program, args...) steps.

    It is only ever given a constant defined above, never text read from a page.
    A backslash before a newline joins two lines and a `#` that starts a word
    begins a comment. A value that starts `~/` is put under the throwaway home.
    """
    steps: list[tuple[str, ...]] = []
    for line in constant.replace("\\\n", " ").splitlines():
        words = shlex.split(line, comments=True)
        if not words:
            continue
        if words[0] == "export":
            [assignment] = words[1:]
            name, value = assignment.split("=", 1)
            assert name in _LEDGER_ENV, f"a constant exports {name!r}"
            if value.startswith("~/"):
                value = str(home / value[2:])
            steps.append(("env", name, value))
        elif words[0] in ("mkdir", "ledger"):
            steps.append(("run", *words))
        else:
            raise AssertionError(f"a constant names a program the walk does not run: {line!r}")
    return steps


def _quick_start_steps(demo: Path, home: Path) -> list[tuple[str, ...]]:
    script = QUICK_START_BLOCK.replace("/tmp/ledger-demo", str(demo))
    assert script != QUICK_START_BLOCK, "QUICK_START_BLOCK no longer uses /tmp/ledger-demo"
    return _constant_steps(script, home)


def _start_here_steps(home: Path) -> list[tuple[str, ...]]:
    """Steps 4 to 8 of docs/start-here.md, in order, from their constants."""
    walk = (START_DB_BLOCK, START_ENV_BLOCK, START_FIRST_WRITE_BLOCK, START_CLAIM_BLOCK, START_CHECK_BLOCK)
    return [step for constant in walk for step in _constant_steps(constant, home)]


def _ask_argv() -> list[str]:
    argv = shlex.split(ASK_BLOCK, comments=True)
    assert argv[:2] == ["ledger", "append"], argv
    return argv


def _acceptance_values() -> tuple[str, str]:
    """The value each form of ACCEPTANCE_BLOCK stands for: `$'...'`, then `$(printf ...)`."""
    [ansi] = re.findall(r"--acceptance \$'([^'\\]*(?:\\n[^'\\]*)*)'", ACCEPTANCE_BLOCK)
    [items] = re.findall(r"\$\(printf '%s\\n'((?: '[^']*')+)\)", ACCEPTANCE_BLOCK)
    # `$(...)` drops the final newline printf writes.
    return ansi.replace("\\n", "\n"), "\n".join(shlex.split(items))


def _tag_argvs() -> tuple[list[str], list[str]]:
    listing, picking = (shlex.split(part) for part in TAG_COMMAND.split(" | "))
    assert listing[0] == "git" and picking[0] == "sed", (listing, picking)
    assert listing.count(TAG_REPOSITORY) == 1, listing
    return listing, picking


def _run_argv(argv: list[str], home: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=home, env=env, capture_output=True, text=True, timeout=180, shell=False,
    )


def _ledger(args: list[str], home: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return _run_argv(["ledger", *args], home, env)


def _run_steps(
    steps: list[tuple[str, ...]], home: Path, env: dict[str, str]
) -> subprocess.CompletedProcess:
    """Run derived steps in order as argument vectors; stop at the first failure."""
    env = dict(env)
    stdout = stderr = ""
    for step in steps:
        if step[0] == "env":
            env[step[1]] = step[2]
            continue
        done = _run_argv(list(step[1:]), home, env)
        stdout += done.stdout
        stderr += done.stderr
        if done.returncode != 0:
            return subprocess.CompletedProcess(list(step[1:]), done.returncode, stdout, stderr)
    return subprocess.CompletedProcess([], 0, stdout, stderr)


def test_every_run_derives_its_commands_from_its_own_constant(tmp_path, monkeypatch) -> None:
    module = sys.modules[__name__]
    home = tmp_path / "home"
    sentinel = ("run", "ledger", "sentinel-verb")
    for name in (
        "QUICK_START_BLOCK", "START_DB_BLOCK", "START_ENV_BLOCK", "START_FIRST_WRITE_BLOCK",
        "START_CLAIM_BLOCK", "START_CHECK_BLOCK",
    ):
        with monkeypatch.context() as patch:
            patch.setattr(module, name, getattr(module, name) + "ledger sentinel-verb\n")
            if name == "QUICK_START_BLOCK":
                derived = _quick_start_steps(tmp_path / "demo", home)
            else:
                derived = _start_here_steps(home)
            assert sentinel in derived, f"the walk's commands do not follow {name}"
    with monkeypatch.context() as patch:
        patch.setattr(module, "ASK_BLOCK", ASK_BLOCK.replace("Which database name?", "Sentinel?"))
        assert '"question": "Sentinel?"' in _ask_argv()[-1], "the ask does not follow ASK_BLOCK"
    with monkeypatch.context() as patch:
        patch.setattr(module, "ACCEPTANCE_BLOCK", ACCEPTANCE_BLOCK.replace("examples run", "sentinel"))
        assert all("sentinel" in value for value in _acceptance_values()), (
            "the acceptance value does not follow ACCEPTANCE_BLOCK"
        )
    with monkeypatch.context() as patch:
        patch.setattr(module, "TAG_COMMAND", TAG_COMMAND.replace("-v:refname", "v:refname"))
        assert "--sort=v:refname" in _tag_argvs()[0], "the tag command does not follow TAG_COMMAND"


def test_the_readme_quick_start_runs_as_printed_in_an_empty_home(tmp_path) -> None:
    _assert_pinned("README.md, Quick start")
    section = _slice(README.read_text(encoding="utf-8"), "\n## Quick start\n", "\n## The model\n")
    lead = section.split("```sh", 1)[0]
    assert "throwaway" in lead, "the quick start does not call its values throwaway"
    assert "(#2-set-the-environment)" in lead, "the quick start does not point at step 2"
    assert "throwaway" in QUICK_START_BLOCK, "the quick start's values are not marked as throwaway"
    demo = tmp_path / "demo"
    home, env = _empty_home(tmp_path)
    done = _run_steps(_quick_start_steps(demo, home), home, env)
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert "ok events=3" in done.stdout, done.stdout
    assert (demo / "ledger.db").is_file()


def test_start_here_setup_steps_run_as_printed_in_an_empty_home(tmp_path) -> None:
    _assert_pinned(*(name for name in PINS if name.startswith(_START_PREFIX)))
    steps = _start_steps()
    titles = {number: body.split("\n", 1)[0] for number, body in steps.items()}
    for number, word in (
        (3, "Install the ledger"), (4, "database lives"), (5, "environment"),
        (6, "first write"), (7, "Claim"), (8, "Check"), (9, "hooks"), (10, "skills"),
    ):
        assert word in titles[number], (number, titles.get(number))
    home, env = _empty_home(tmp_path)
    walk = _start_here_steps(home)
    assert ("run", "ledger", "new", "--type", "ticket") == walk[3][:5], walk
    assert ("run", "ledger", "doctor") in walk, walk
    assert all(step[1] != "mkdir" for step in walk), "the walk needs no folder made by hand"
    done = _run_steps(walk, home, env)
    assert done.returncode == 0, (done.stdout, done.stderr)
    database = home / ".local" / "share" / "ledger" / "work.db"
    assert database.is_file()
    assert f"path: {database.resolve()}" in done.stdout, done.stdout
    assert "claimed for work" in done.stdout and "schema_version:" in done.stdout
    assert "ok events=" in done.stdout


def test_a_check_before_the_first_write_fails_as_start_here_says(tmp_path) -> None:
    body = _start_steps()[8]
    assert "fails with `database does not exist`" in _squash(body)
    home, env = _empty_home(tmp_path)
    env["LEDGER_DB"] = str(home / ".local" / "share" / "ledger" / "work.db")
    done = _ledger(["doctor"], home, env)
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "database does not exist" in done.stderr


def test_the_walk_runs_each_command_as_an_argument_vector_in_the_home_never_a_shell(
    tmp_path, monkeypatch
) -> None:
    _assert_pinned("README.md, Quick start")
    home, env = _empty_home(tmp_path)
    real = subprocess.run
    calls: list[list[str]] = []

    def recorder(args, **kwargs):
        assert isinstance(args, list) and all(isinstance(part, str) for part in args), args
        assert kwargs.get("shell", False) is False, f"a command ran through a shell: {args}"
        assert kwargs.get("cwd") == home, f"a command ran outside the throwaway home: {args}"
        assert set(kwargs["env"]) <= _BASE_ENV | _LEDGER_ENV, sorted(kwargs["env"])
        calls.append(args)
        return real(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recorder)
    done = _run_steps(_quick_start_steps(tmp_path / "demo", home), home, env)
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert [call[0] for call in calls][:2] == ["mkdir", "ledger"], calls
    assert {call[0] for call in calls} == {"mkdir", "ledger"}, calls
    assert len(calls) == 8, calls


def _documented_tag_command(text: str, name: str) -> str:
    commands = [b for b in _fenced(text, "sh") if "ls-remote" in b]
    assert len(commands) == 1, name
    assert commands[0] == TAG_BLOCK, (
        f"{name}: the tag command is not the one closed shape this test knows: "
        f"{commands[0]!r}"
    )
    return TAG_COMMAND


def test_the_printed_tag_command_prints_the_newest_release_tag(tmp_path) -> None:
    _assert_pinned("README.md, Install", _start_section(3))
    for page in (README, START):
        _documented_tag_command(page.read_text(encoding="utf-8"), page.name)
    remote = tmp_path / "remote"
    remote.mkdir()
    env = {
        "HOME": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    git = ["git", "-c", "user.name=docs", "-c", "user.email=docs@example.invalid"]
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "a"]):
        subprocess.run(
            [*git, *args], cwd=remote, env=env, check=True, capture_output=True, shell=False
        )
    for tag in ("v0.2.1", "v0.10.0", "v0.3.0", "v0.9.9", "not-a-release"):
        subprocess.run(
            [*git, "tag", tag], cwd=remote, env=env, check=True, capture_output=True, shell=False
        )
    listing, picking = _tag_argvs()
    listing[listing.index(TAG_REPOSITORY)] = str(remote)
    listed = subprocess.run(
        listing, env=env, capture_output=True, text=True, timeout=60, shell=False
    )
    assert listed.returncode == 0, listed.stderr
    picked = subprocess.run(
        picking, input=listed.stdout, env=env, capture_output=True, text=True,
        timeout=60, shell=False,
    )
    assert (picked.returncode, picked.stdout) == (0, "v0.10.0\n"), (
        f"the tag command printed {picked.stdout!r} ({picked.stderr!r}), not the newest tag"
    )


def test_a_tag_command_off_the_closed_shape_fails_by_name() -> None:
    for page in (README, START):
        text = page.read_text(encoding="utf-8")
        for planted in (
            TAG_COMMAND + " ; touch marker",
            TAG_COMMAND.replace(" | sed", " | tee marker | sed"),
            TAG_COMMAND.replace("git ls-remote", "git -c core.pager=x ls-remote"),
            TAG_COMMAND + " ",
        ):
            changed = text.replace(TAG_COMMAND, planted)
            assert changed != text
            with pytest.raises(AssertionError, match="closed shape"):
                _documented_tag_command(changed, page.name)


def test_both_docs_say_what_to_do_when_the_tag_command_prints_nothing() -> None:
    for page in (README, START):
        text = page.read_text(encoding="utf-8")
        flat = _flat(page)
        failed = "If the command failed because there is no network, reconnect and run it again."
        nothing = "If it ran and printed nothing, use the tag named on"
        assert failed in flat, f"{page.name} does not say what to do when the tag command fails offline"
        assert nothing in flat, f"{page.name} does not say what to do when the tag command prints nothing"
        assert "https://github.com/smithadifd/ledger/releases/latest" in flat, page.name
        first, second = text.index(failed.split(",")[0]), text.index(nothing.split(",")[0])
        assert text.index("git ls-remote") < first < second < text.index("Put what it prints where"), (
            page.name
        )


def test_both_docs_give_the_two_pipx_commands_and_say_what_pipx_writes() -> None:
    for page in (README, START):
        text = page.read_text(encoding="utf-8")
        assert "brew install pipx\npipx ensurepath\n" in _fenced(text, "sh"), (
            f"{page.name} does not print the two pipx commands in one block"
        )
        flat = _flat(page)
        for needle in ("shell's profile file", "`~/.local/bin`", "folder of its own"):
            assert needle in flat, (page.name, needle)


def test_the_four_setup_parts_come_in_one_order_in_both_docs() -> None:
    readme = README.read_text(encoding="utf-8")
    intro = _between(readme, "\n## Install\n", "\n### 1. ")
    order = [
        "Install the `ledger` command",
        "Set the environment",
        "Install the `ledger-hooks` plugin",
        "Install the skills plugin",
    ]
    positions = [intro.index(item) for item in order]
    assert positions == sorted(positions), dict(zip(order, positions))
    assert "after step 2 and before the self-check" in _squash(intro)
    headings = re.findall(r"^#{2,3} (.+)$", readme, flags=re.MULTILINE)
    where = [
        headings.index(name)
        for name in (
            "Install", "1. Install the ledger command", "2. Set the environment",
            "Quick start", "Claude Code plugin", "Working with agent skills",
        )
    ]
    assert where == sorted(where), where
    start = START.read_text(encoding="utf-8")
    start_intro = _between(start, "Setup has four parts", "\n## 1. ")
    parts = ["the `ledger` command", "the environment", "the `ledger-hooks` plugin", "the skills plugin"]
    spots = [start_intro.index(part) for part in parts]
    assert spots == sorted(spots), dict(zip(parts, spots))
    steps = _start_steps()
    assert "(steps 6 to 8)" in _squash(start_intro)
    for number, word in ((5, "environment"), (6, "first write"), (8, "Check"), (9, "hooks"), (10, "skills")):
        assert word in steps[number].split("\n", 1)[0], (number, word)


def test_both_docs_set_the_environment_one_way() -> None:
    for page in (README, START):
        text = page.read_text(encoding="utf-8")
        [settings] = [json.loads(block) for block in _fenced(text, "json")]
        env = settings["env"]
        assert set(env) == {"LEDGER_DB", "LEDGER_OPERATOR"}, (page.name, env)
        assert env["LEDGER_DB"].startswith("/"), (page.name, env["LEDGER_DB"])
        assert re.search(r"/(?:Users|home)/", env["LEDGER_DB"]) is None
        shell = [b for b in _fenced(text, "sh") if "export LEDGER_OPERATOR=" in b]
        assert shell and all("export LEDGER_HARNESS=cli" in b or "LEDGER_DB" in b for b in shell), page.name
        flat = _flat(page)
        assert "sets `claude-code`" in flat or "sets it to `claude-code`" in flat, page.name
        assert "settings" in flat and "`env`" in flat, page.name


def test_the_documented_ask_is_accepted_and_the_picker_can_show_it(tmp_path) -> None:
    _assert_pinned("README.md, Asking a question", "docs/cli.md, Asking a question")
    schema = SCHEMAS[("decision.asked", 1)]
    assert set(schema["required"]) == {"question", "options"}
    assert {"recommendation", "scope"} <= set(schema["optional"])
    for page in (README, CLI_PAGE):
        text = page.read_text(encoding="utf-8")
        for field in ("question", "options", "label", "description", "scope", "recommendation"):
            assert f"`{field}`" in text, (page.name, field)
    home, env = _empty_home(tmp_path)
    env.update(LEDGER_DB=str(home / "ledger.db"), LEDGER_HARNESS="cli", LEDGER_OPERATOR="me")
    item = _ledger(["new", "--type", "ticket", "--title", "T", "--id", "demo-1"], home, env)
    assert item.returncode == 0, (item.stdout, item.stderr)
    made = _run_argv(_ask_argv(), home, env)
    assert made.returncode == 0, (made.stdout, made.stderr)
    event = re.findall(r"event=(\d+)", made.stdout)[-1]
    shown = _ledger(["ask-display", event], home, env)
    assert shown.returncode == 0, (shown.stdout, shown.stderr)
    assert f"[ask {event}] Which database name? (scope: the name of the first database)" in shown.stdout
    assert "under `claude -p`" in _flat(README)
    assert "interactive" in _flat(README) and "capture_missing" in _flat(README)


def test_decisions_lists_records_not_elections_and_the_docs_say_so(tmp_path) -> None:
    home, env = _empty_home(tmp_path)
    env.update(LEDGER_DB=str(home / "ledger.db"), LEDGER_HARNESS="cli", LEDGER_OPERATOR="me")
    for args in (
        ["new", "--type", "ticket", "--title", "T", "--id", "demo-1"],
        [
            "append", "--item", "demo-1", "--kind", "utterance.captured", "--channel", "terminal",
            "--provenance-class", "submitted-prompt",
            "--payload", '{"text": "Ship the short version first.", "origin": "person"}',
        ],
        [
            "elect", "demo-1", "--question", "Short or long?", "--chosen", "short",
            "--chosen-description", "five commands", "--rejected", "long",
            "--scope", "the README", "--quote-ref", "2", "--quote", "short version",
        ],
    ):
        made = _ledger(args, home, env)
        assert made.returncode == 0, (args, made.stdout, made.stderr)
    listed = _ledger(["decisions"], home, env).returncode
    assert listed == 1, "ledger decisions now lists elections: update the README row and docs/cli.md"
    card = _ledger(["card", "demo-1"], home, env).stdout
    assert "## Decisions" in card and "Short or long? -> short" in card
    found = _ledger(["search", "short"], home, env).stdout
    assert "elected: Short or long?" in found
    row = next(
        line for line in README.read_text(encoding="utf-8").splitlines()
        if line.startswith("| `decisions` |")
    )
    assert "does not list elections" in row and "`card`" in row and "`search`" in row
    entry = _squash(CLI_PAGE.read_text(encoding="utf-8").split("**`decisions [", 1)[1].split("\n**`", 1)[0])
    assert "It does not list elections" in entry and "card" in entry and "`search`" in entry


def _documented_acceptance_block(text: str) -> str:
    [block] = [b for b in _fenced(text, "sh") if "--acceptance" in b]
    assert block == ACCEPTANCE_BLOCK, (
        f"docs/cli.md prints a different acceptance block than the closed shape: {block!r}"
    )
    return ACCEPTANCE_BLOCK


def test_an_acceptance_block_off_the_closed_shape_fails_by_name() -> None:
    text = CLI_PAGE.read_text(encoding="utf-8")
    for planted in (
        ACCEPTANCE_BLOCK + "touch marker\n",
        ACCEPTANCE_BLOCK.replace("the examples run", "$(touch marker)", 1),
    ):
        changed = text.replace(ACCEPTANCE_BLOCK, planted)
        assert changed != text
        with pytest.raises(AssertionError, match="closed shape"):
            _documented_acceptance_block(changed)


def test_several_acceptance_bullets_pass_as_one_value_as_documented(tmp_path) -> None:
    _assert_pinned("docs/cli.md, spec set")
    text = CLI_PAGE.read_text(encoding="utf-8")
    _documented_acceptance_block(text)
    from_ansi, from_printf = _acceptance_values()
    value = "- the examples run\n- the output is checked"
    assert from_ansi == from_printf == value, (from_ansi, from_printf)
    home, env = _empty_home(tmp_path)
    env.update(LEDGER_DB=str(home / "ledger.db"), LEDGER_HARNESS="cli", LEDGER_OPERATOR="me")
    made = _ledger(["new", "--type", "ticket", "--title", "T", "--id", "demo-1"], home, env)
    assert made.returncode == 0, (made.stdout, made.stderr)
    spec = [
        "spec", "set", "demo-1", "--title", "Quick start", "--body", "What to write",
        "--acceptance", from_ansi,
    ]
    first = _ledger(spec, home, env)
    second = _ledger(spec[:-1] + [from_printf], home, env)
    assert first.returncode == 0 and second.returncode == 0, (first.stderr, second.stderr)
    assert "\tv1\tevent=" in first.stdout and "\tv1\tunchanged" in second.stdout, (
        first.stdout, second.stdout
    )
    shown = _ledger(["spec", "show", "demo-1"], home, env).stdout
    assert "## Acceptance\n- the examples run\n- the output is checked\n" in shown, shown
    repeated = _ledger(
        ["spec", "set", "demo-1", "--title", "T", "--body", "b",
         "--acceptance", "- a", "--acceptance", "- b"],
        home, env,
    )
    assert repeated.returncode == 0, repeated.stderr
    shown = _ledger(["spec", "show", "demo-1"], home, env).stdout
    assert "## Acceptance\n- b\n" in shown and "- a" not in shown, shown
    assert "the last one replaces" in _squash(text)


def test_the_readme_uninstall_names_each_piece_and_keeps_the_database() -> None:
    section = _between(README.read_text(encoding="utf-8"), "\n## Uninstall\n", "\n## ")
    flat = _squash(section)
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    plugin = market["plugins"][0]["name"]
    for needle in (
        f"claude plugin uninstall {plugin}@{market['name']}",
        f"claude plugin marketplace remove {market['name']}",
        "pipx uninstall ledger",
        "brew uninstall pipx",
        "your shell profile",
        "`~/.local/bin`",
        "`LEDGER_` keys from the `env` block",
        "is data",
        "it stays until you delete it yourself",
    ):
        assert needle in flat, f"the uninstall section lacks: {needle}"
    assert "~/.local/share/ledger" in flat


def test_the_docs_say_the_brief_goes_to_the_model_and_the_picker_needs_a_session() -> None:
    for page in (README, START):
        flat = _flat(page)
        assert "model's context" in flat, f"{page.name} does not say the brief goes to the model"
        assert "terminal does not show it" in flat, (
            f"{page.name} does not say the terminal does not show the brief"
        )
        assert "interactive" in flat and "`claude -p`" in flat, (
            f"{page.name} does not say the picker needs an interactive session"
        )
    assert "`claude -p`" in _flat(CLI_PAGE) and "interactive" in _flat(CLI_PAGE)
