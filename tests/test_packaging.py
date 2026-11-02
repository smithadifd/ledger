"""pyproject.toml declares a standard-library-only package with a ``ledger`` script."""
from __future__ import annotations

import fnmatch
import importlib
import tomllib
from pathlib import Path

from ledger import cli

REPO = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))


def test_requires_python_311_and_no_runtime_dependencies() -> None:
    project = PYPROJECT["project"]
    assert project["requires-python"] == ">=3.11"
    assert project.get("dependencies", []) == []


def test_console_script_is_the_cli(capsys) -> None:
    module_name, _, attr = PYPROJECT["project"]["scripts"]["ledger"].partition(":")
    entry = getattr(importlib.import_module(module_name), attr)
    assert entry is cli.main
    assert entry(["--help"]) == 0
    assert capsys.readouterr().out.startswith("usage: ledger ")


def test_package_data_ships_every_migration() -> None:
    patterns = PYPROJECT["tool"]["setuptools"]["package-data"]["ledger"]
    migrations = sorted((REPO / "ledger" / "migrations").glob("*.sql"))
    assert migrations
    for path in migrations:
        rel = path.relative_to(REPO / "ledger").as_posix()
        assert any(fnmatch.fnmatch(rel, pattern) for pattern in patterns), rel
