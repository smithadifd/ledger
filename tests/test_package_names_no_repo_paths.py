"""The ``ledger/`` folder stands alone: no tracked file under it names a path outside it.

A consumer vendors ``ledger/`` by itself, so a reference to the repository's
own files would dangle there. The grammar, deliberately small:

* a path token: one of the repository's top-level entries (``docs/``,
  ``tests/``, ``.github/``, ``README.md``, ``CHANGELOG.md``, ``pyproject.toml``,
  ``VENDORING.md``, ``SECURITY.md``, ``LICENSE``) not preceded by a path
  character or a dot, so ``ledger/docs/x`` and ``a.tests/`` do not match;
* a path expression: ``parent``/``parents[...]`` (or ``../``) on one line with
  a quoted ``"docs"``, ``"tests"`` or ``".github"`` (the file names above are
  already caught as tokens).

Bare prose ("the docs folder", "the README") passes. A line may carry the
marker ``lint: allow-repo-path`` to opt out; none do today.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_NAMES = r"(?:docs/|tests/|\.github/|README\.md|CHANGELOG\.md|pyproject\.toml|VENDORING\.md|SECURITY\.md|LICENSE\b)"
_TOKEN = re.compile(r"(?<![\w./-])" + _NAMES)
_QUOTED = re.compile(r"""["'](?:docs|tests|\.github)/?["']""")
_PARENT = re.compile(r"\.parents?\b|\.\./")


def violations(rel: str, text: str) -> list[str]:
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        if "lint: allow-repo-path" in line:
            continue
        if _TOKEN.search(line) or (_PARENT.search(line) and _QUOTED.search(line)):
            found.append(f"{rel}:{number}: {line.strip()[:100]}")
    return found


def _tracked() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "ledger"], cwd=ROOT,
        capture_output=True, check=True,
    ).stdout.decode()
    return [p for p in out.split("\0") if p]


def _scan_all() -> list[str]:
    found: list[str] = []
    for rel in _tracked():
        path = ROOT / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        found += violations(rel, text)
    return found


def test_package_names_no_repository_path() -> None:
    assert _scan_all() == []


def test_the_grammar_flags_what_it_claims_to() -> None:
    assert violations("x", "see docs/agent-contract.md")
    assert violations("x", "(REPO / 'pyproject.toml').is_file()")
    assert violations("x", "if (Path(__file__).parent.parent / 'pyproject.toml').is_file():")
    assert violations("x", "root = Path(__file__).parents[2] / 'docs'")
    assert violations("x", "the docs folder and the README") == []
    assert violations("x", "ledger/docs/x and a.tests/y") == []
