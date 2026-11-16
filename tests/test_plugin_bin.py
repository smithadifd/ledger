"""The ledger-hooks plugin's bundled CLI: it runs from an installed copy."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ledger import __version__

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "ledger-hooks"
PLUGIN_JSON = PLUGIN / ".claude-plugin" / "plugin.json"


def _shipped_relpaths() -> list[Path]:
    root = ROOT / "ledger"
    rels = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if rel.parts[0] == "tests" or "__pycache__" in rel.parts or path.suffix == ".pyc":
            continue
        rels.append(rel)
    return sorted(rels)


def _copy_plugin(dest: Path) -> None:
    shutil.copytree(
        PLUGIN,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )


def test_bundled_ledger_matches_package() -> None:
    bundled = PLUGIN / "ledger"
    shipped = {rel: (ROOT / "ledger" / rel).read_bytes() for rel in _shipped_relpaths()}
    found = {
        path.relative_to(bundled): path.read_bytes()
        for path in bundled.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    assert shipped.keys() == found.keys()
    drifted = [str(rel) for rel, data in shipped.items() if data != found[rel]]
    assert not drifted, drifted


def test_isolated_plugin_copy_runs_ledger_help(tmp_path: Path) -> None:
    dest = tmp_path / "installed" / "ledger-hooks"
    _copy_plugin(dest)
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run(
        [str(dest / "bin" / "ledger"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    assert "usage:" in done.stdout.lower() or "ledger" in done.stdout.lower()


def test_isolated_plugin_copy_ignores_cwd_ledger_package(tmp_path: Path) -> None:
    dest = tmp_path / "installed" / "ledger-hooks"
    _copy_plugin(dest)
    cwd = tmp_path / "project"
    planted = cwd / "ledger"
    planted.mkdir(parents=True)
    (planted / "__init__.py").write_text("", encoding="utf-8")
    marker = "PLANTED_CWD_LEDGER"
    (planted / "__main__.py").write_text(f"print({marker!r})\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run(
        [str(dest / "bin" / "ledger"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=cwd,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    assert marker not in done.stdout
    assert marker not in done.stderr
    assert "usage:" in done.stdout.lower()


def test_isolated_plugin_copy_ignores_inherited_pythonpath_sqlite3(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "installed" / "ledger-hooks"
    _copy_plugin(dest)
    poison = tmp_path / "poison-sqlite3"
    poison.mkdir()
    marker = "PLANTED_PYTHONPATH_SQLITE3"
    (poison / "sqlite3.py").write_text(f"print({marker!r})\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(
        HOME=str(home),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=str(poison),
    )
    done = subprocess.run(
        [str(dest / "bin" / "ledger"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    assert marker not in done.stdout
    assert marker not in done.stderr
    assert "usage:" in done.stdout.lower()


def test_isolated_plugin_copy_ignores_inherited_pythonpath_ledger(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "installed" / "ledger-hooks"
    _copy_plugin(dest)
    poison = tmp_path / "poison-ledger"
    planted = poison / "ledger"
    planted.mkdir(parents=True)
    marker = "PLANTED_PYTHONPATH_LEDGER"
    (planted / "__init__.py").write_text("", encoding="utf-8")
    (planted / "__main__.py").write_text(f"print({marker!r})\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update(
        HOME=str(home),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=str(poison),
    )
    done = subprocess.run(
        [str(dest / "bin" / "ledger"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
        env=env,
    )
    assert done.returncode == 0, done.stderr
    assert marker not in done.stdout
    assert marker not in done.stderr
    assert "usage:" in done.stdout.lower()


def test_bin_ledger_without_python_prints_one_stderr_line(tmp_path: Path) -> None:
    dest = tmp_path / "plugin"
    _copy_plugin(dest)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    done = subprocess.run(
        [str(dest / "bin" / "ledger"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=tmp_path,
        env={
            "PATH": str(empty),
            "HOME": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    assert done.returncode != 0
    assert done.stdout == ""
    assert done.stderr == "ledger: Python 3.11 or later is required\n"


def test_plugin_manifest_version_matches_package() -> None:
    plugin = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    assert plugin["version"] == __version__


def test_bin_ledger_is_executable() -> None:
    path = PLUGIN / "bin" / "ledger"
    assert path.is_file()
    assert path.stat().st_mode & stat.S_IXUSR
