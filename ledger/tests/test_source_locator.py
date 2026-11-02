"""A recorded source locator never reaches git as an option.

``export-item --ticket`` reads ``rel:line@commit`` from an item's
``source`` and runs ``git show <commit>:<rel>``. Any text in that field is
accepted on write, so the commit and path parts are checked before git runs,
and the revision is passed after ``--end-of-options`` as well.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ledger import export
from ledger.export import read_source_blob, split_source_locator
from ledger.store import open_ledger

# The folder that holds the ``ledger`` package; the CLI runs from it as
# ``python -m ledger.cli``.
LIB = Path(__file__).resolve().parents[2]
SHA = "0123456789abcdef0123456789abcdef01234567"


def _no_subprocess(*_args, **_kwargs):
    raise AssertionError("subprocess.run was called for a refused locator")


@pytest.mark.parametrize(
    "source",
    [
        "a.md:1@--output=/tmp/sl43-never-written",
        "a.md:1@-p",
        "a.md:1@--exec=touch /tmp/sl43-never-written",
        "a.md:1@-0123456",
        "a.md:1@--output=/x+dirty",
    ],
)
def test_option_shaped_commit_is_refused_before_git(monkeypatch, tmp_path, source):
    monkeypatch.setattr(export.subprocess, "run", _no_subprocess)
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="invalid source locator"):
        split_source_locator(source)
    with pytest.raises(ValueError, match="invalid source locator"):
        read_source_blob(source, cwd=tmp_path)


@pytest.mark.parametrize(
    "sha",
    [
        "0123456g",           # non-hex character
        "ABCDEF0",            # upper case is never what git or a digest prints
        "abc123",             # shorter than 7
        "a" * 65,             # longer than 64
        "HEAD",               # a ref name, not a recorded object name
        "main~1",
        "abcdef0+clean",      # unknown suffix
        "abcdef0\n",          # trailing newline
        "untracked+dirty",
        "",
    ],
)
def test_non_hex_commit_is_refused(monkeypatch, tmp_path, sha):
    monkeypatch.setattr(export.subprocess, "run", _no_subprocess)
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="invalid source locator"):
        read_source_blob(f"a.md:1@{sha}", cwd=tmp_path)


@pytest.mark.parametrize(
    "rel",
    ["/etc/passwd", "../a.md", "roadmaps/../../a.md", "roadmaps/..", ".."],
)
def test_absolute_or_dotdot_path_is_refused(monkeypatch, tmp_path, rel):
    monkeypatch.setattr(export.subprocess, "run", _no_subprocess)
    (tmp_path / ".git").mkdir()
    with pytest.raises(ValueError, match="invalid source locator"):
        read_source_blob(f"{rel}:1@{SHA}", cwd=tmp_path)


@pytest.mark.parametrize(
    "source, expected",
    [
        (f"roadmaps/a.md:3@{SHA}", ("roadmaps/a.md", 3, SHA)),
        ("roadmaps/a.md:3@abcdef0", ("roadmaps/a.md", 3, "abcdef0")),
        (f"a.md:12@{SHA}+dirty", ("a.md", 12, f"{SHA}+dirty")),
        ("a.md:1@untracked", ("a.md", 1, "untracked")),
        (f"a.md:1@{'f' * 64}", ("a.md", 1, "f" * 64)),
        (f"dir..name/a..b.md:1@{SHA}", ("dir..name/a..b.md", 1, SHA)),
    ],
)
def test_valid_locators_still_parse(source, expected):
    assert split_source_locator(source) == expected


def test_revision_follows_end_of_options_even_without_the_check(monkeypatch, tmp_path):
    """Second layer: bypass the format check and git still sees an argument."""
    seen: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="blob\n", stderr="")

    monkeypatch.setattr(export, "_valid_locator_commit", lambda _sha: True)
    monkeypatch.setattr(export.subprocess, "run", fake_run)
    (tmp_path / ".git").mkdir()
    assert read_source_blob("a.md:1@--output=/x", cwd=tmp_path) == "blob\n"
    assert len(seen) == 1, seen
    argv = seen[0]
    assert argv[-1] == "--output=/x:a.md", argv
    assert argv[-2] == "--end-of-options", argv
    assert argv.index("show") < argv.index("--end-of-options"), argv


def test_valid_locator_reaches_git_with_end_of_options(monkeypatch, tmp_path):
    seen: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="blob\n", stderr="")

    monkeypatch.setattr(export.subprocess, "run", fake_run)
    (tmp_path / ".git").mkdir()
    assert read_source_blob(f"a.md:1@{SHA}", cwd=tmp_path) == "blob\n"
    assert seen[0][-2:] == ["--end-of-options", f"{SHA}:a.md"], seen


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, text=True, capture_output=True,
    ).stdout


def _repo_with_ticket(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.md").write_text(
        "### Ticket: SL1 — A ticket\n- Prompt: one.\n\n## Later\nnot it\n",
        encoding="utf-8",
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, _git(repo, "rev-parse", "HEAD").strip()


def _item_with_source(db: Path, item_id: str, source: str) -> None:
    lg = open_ledger(db)
    try:
        lg.append(
            kind="item.created",
            payload={"type": "ticket", "title": "A ticket", "source": source},
            item_id=item_id,
            idempotency_key=f"create-{item_id}",
            harness="pytest",
            actor="tester",
        )
    finally:
        lg.close()


def _export_ticket(db: Path, repo: Path, item_id: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(LEDGER_HARNESS="pytest", LEDGER_AGENT="tester", PYTHONPATH=str(LIB))
    return subprocess.run(
        [sys.executable, "-m", "ledger.cli", "--json", "--db", str(db),
         "export-item", "--ticket", item_id],
        cwd=repo, env=env, text=True, capture_output=True,
    )


def test_export_item_ticket_refuses_option_shaped_source(tmp_path):
    repo, _sha = _repo_with_ticket(tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    db = tmp_path / "ledger.db"
    _item_with_source(db, "tk-opt", f"a.md:1@--output={out}/written")

    result = _export_ticket(db, repo, "tk-opt")

    assert list(out.iterdir()) == [], "git wrote a file the locator named"
    assert result.returncode != 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "invalid source locator" in payload["error"]["message"], payload


def test_export_item_ticket_renders_a_valid_source(tmp_path):
    repo, sha = _repo_with_ticket(tmp_path)
    db = tmp_path / "ledger.db"
    _item_with_source(db, "tk-ok", f"a.md:1@{sha}")

    result = _export_ticket(db, repo, "tk-ok")

    assert result.returncode == 0, result.stderr
    ticket = json.loads(result.stdout)["data"]["ticket"]
    assert ticket == "### Ticket: SL1 — A ticket\n- Prompt: one.\n\n"


def test_dirty_locator_is_refused_before_git(monkeypatch, tmp_path):
    monkeypatch.setattr(export.subprocess, "run", _no_subprocess)
    (tmp_path / ".git").mkdir()
    with pytest.raises(
        ValueError,
        match="recorded from uncommitted text; the committed copy may differ",
    ):
        read_source_blob(f"a.md:1@{SHA}+dirty", cwd=tmp_path)


def test_export_item_ticket_refuses_dirty_import_instead_of_older_ticket(tmp_path):
    # The commit holds an older ticket at line 1; the dirty roadmap the item
    # was imported from held a new ticket at that line.
    repo, sha = _repo_with_ticket(tmp_path)
    db = tmp_path / "ledger.db"
    _item_with_source(db, "tk-dirty", f"a.md:1@{sha}+dirty")

    result = _export_ticket(db, repo, "tk-dirty")

    assert result.returncode != 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    message = payload["error"]["message"]
    assert "recorded from uncommitted text; the committed copy may differ" in message
    assert "A ticket" not in message
