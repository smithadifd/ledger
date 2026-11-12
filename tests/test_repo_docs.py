"""Checks that read this repository's own files (README, docs/).

These live outside ``ledger/`` on purpose: the package is vendored as a folder
on its own, and a consumer's copy has no README or docs beside it. Here the
files are always present, so nothing skips. ``LEDGER_REPO_ROOT`` is not needed.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from ledger import cli
from ledger.paths import SUPPORTED_LOCATION
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


def test_concepts_documents_note_search_and_severity() -> None:
    text = (ROOT / "docs" / "concepts.md").read_text(encoding="utf-8")
    section = text.split("## The search index", 1)[1].split("\n## ", 1)[0]
    assert "A note is searched as part of its item, not as its own document" in _squash(section)
    for severity in ("`ok`", "`info`", "`warn`"):
        assert f"| {severity} |" in section
