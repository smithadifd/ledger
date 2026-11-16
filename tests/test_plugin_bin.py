"""The ledger-hooks plugin's bundled CLI: it runs from an installed copy."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from ledger import __version__

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "ledger-hooks"
PLUGIN_JSON = PLUGIN / ".claude-plugin" / "plugin.json"

_USER_SITE_VERS = (
    "python",
    "python3.11",
    "python3.12",
    "python3.13",
    "python3.14",
)


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


def _write_marker_mod(path: Path, marker: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('hit\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )


def _clean_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "PYTHONPATH" and not key.startswith("LEDGER_")
    }
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    for key in (
        "PYTHONEXECUTABLE",
        "__PYVENV_LAUNCHER__",
        "PYTHONUSERBASE",
        "PYTHONNOUSERSITE",
    ):
        env.pop(key, None)
    env.update(HOME=str(home), PYTHONDONTWRITEBYTECODE="1")
    if extra:
        env.update(extra)
    return env


def _run_bin(
    dest: Path,
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(dest / "bin" / "ledger"), *args],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=cwd,
        env=env,
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
    done = _run_bin(dest, ["--help"], cwd=tmp_path, env=_clean_env(tmp_path))
    assert done.returncode == 0, done.stderr
    assert "usage:" in done.stdout.lower() or "ledger" in done.stdout.lower()


def test_isolated_plugin_copy_runs_ledger_version(tmp_path: Path) -> None:
    dest = tmp_path / "installed" / "ledger-hooks"
    _copy_plugin(dest)
    done = _run_bin(dest, ["--version"], cwd=tmp_path, env=_clean_env(tmp_path))
    assert done.returncode == 0, done.stderr
    assert done.stdout == f"ledger {__version__}\n"


def test_isolated_plugin_copy_verb_with_args_exits_nonzero(tmp_path: Path) -> None:
    dest = tmp_path / "installed" / "ledger-hooks"
    _copy_plugin(dest)
    done = _run_bin(
        dest,
        ["new", "--type", "ticket", "--title", "isolation"],
        cwd=tmp_path,
        env=_clean_env(tmp_path),
    )
    assert done.returncode != 0
    assert "LEDGER_DB" in done.stderr or "--db" in done.stderr


def test_isolated_plugin_copy_ignores_cwd_ledger_package(tmp_path: Path) -> None:
    dest = tmp_path / "installed" / "ledger-hooks"
    _copy_plugin(dest)
    cwd = tmp_path / "project"
    planted = cwd / "ledger"
    planted.mkdir(parents=True)
    marker = tmp_path / "cwd-ledger.marker"
    (planted / "__init__.py").write_text("", encoding="utf-8")
    _write_marker_mod(planted / "__main__.py", marker)
    done = _run_bin(dest, ["--help"], cwd=cwd, env=_clean_env(tmp_path))
    assert done.returncode == 0, done.stderr
    assert not marker.exists(), marker.read_text(encoding="utf-8")
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
    done = _run_bin(
        dest,
        ["--help"],
        cwd=tmp_path,
        env=_clean_env(tmp_path, {"PYTHONPATH": str(poison)}),
    )
    assert done.returncode == 0, done.stderr
    assert marker not in done.stdout
    assert marker not in done.stderr
    assert "usage:" in done.stdout.lower()


def test_isolated_plugin_copy_ignores_inherited_pythonpath_sitecustomize(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "installed" / "ledger-hooks"
    _copy_plugin(dest)
    poison = tmp_path / "poison-sitecustomize"
    poison.mkdir()
    marker = tmp_path / "pythonpath-sitecustomize.marker"
    _write_marker_mod(poison / "sitecustomize.py", marker)
    done = _run_bin(
        dest,
        ["--help"],
        cwd=tmp_path,
        env=_clean_env(tmp_path, {"PYTHONPATH": str(poison)}),
    )
    assert done.returncode == 0, done.stderr
    assert not marker.exists(), marker.read_text(encoding="utf-8")
    assert "usage:" in done.stdout.lower()


def test_isolated_plugin_copy_ignores_pythonuserbase_usercustomize(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "installed" / "ledger-hooks"
    _copy_plugin(dest)
    userbase = tmp_path / "userbase"
    marker = tmp_path / "usercustomize.marker"
    for ver in _USER_SITE_VERS:
        _write_marker_mod(
            userbase / "lib" / ver / "site-packages" / "usercustomize.py",
            marker,
        )
    done = _run_bin(
        dest,
        ["--help"],
        cwd=tmp_path,
        env=_clean_env(tmp_path, {"PYTHONUSERBASE": str(userbase)}),
    )
    assert done.returncode == 0, done.stderr
    assert not marker.exists(), marker.read_text(encoding="utf-8")
    assert "usage:" in done.stdout.lower()


def test_isolated_plugin_copy_ignores_pythonexecutable_venv_pth(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "installed" / "ledger-hooks"
    _copy_plugin(dest)
    fake = tmp_path / "pyexec-venv"
    (fake / "bin").mkdir(parents=True)
    (fake / "pyvenv.cfg").write_text(
        "home = {0}\ninclude-system-site-packages = false\n".format(
            Path(sys.executable).resolve().parent
        ),
        encoding="utf-8",
    )
    (fake / "bin" / "python").write_text("", encoding="utf-8")
    marker = tmp_path / "pyexec-pth.marker"
    for ver in _USER_SITE_VERS:
        sp = fake / "lib" / ver / "site-packages"
        sp.mkdir(parents=True, exist_ok=True)
        (sp / "poison.pth").write_text("import pth_marker\n", encoding="utf-8")
        _write_marker_mod(sp / "pth_marker.py", marker)
    done = _run_bin(
        dest,
        ["--help"],
        cwd=tmp_path,
        env=_clean_env(
            tmp_path,
            {"PYTHONEXECUTABLE": str(fake / "bin" / "python")},
        ),
    )
    assert done.returncode == 0, done.stderr
    assert not marker.exists(), marker.read_text(encoding="utf-8")
    assert "usage:" in done.stdout.lower()


def test_isolated_plugin_copy_ignores_venv_on_path(tmp_path: Path) -> None:
    dest = tmp_path / "installed" / "ledger-hooks"
    _copy_plugin(dest)
    venv = tmp_path / "path-venv"
    created = subprocess.run(
        [sys.executable, "-m", "venv", str(venv)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert created.returncode == 0, created.stderr
    sps = list(venv.glob("lib/python*/site-packages"))
    sps.extend(venv.glob("lib/python/site-packages"))
    assert sps, "venv has no site-packages"
    marker = tmp_path / "venv-pth.marker"
    for sp in sps:
        (sp / "poison.pth").write_text("import pth_marker\n", encoding="utf-8")
        _write_marker_mod(sp / "pth_marker.py", marker)
        planted = sp / "ledger"
        planted.mkdir()
        (planted / "__init__.py").write_text("", encoding="utf-8")
        (planted / "__main__.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('ledger\\n', encoding='utf-8')\n"
            "print('PLANTED_VENV_LEDGER')\n",
            encoding="utf-8",
        )
    env = _clean_env(tmp_path)
    env["PATH"] = str(venv / "bin") + os.pathsep + env.get("PATH", "")
    done = _run_bin(dest, ["--help"], cwd=tmp_path, env=env)
    assert done.returncode == 0, done.stderr
    assert not marker.exists(), marker.read_text(encoding="utf-8")
    assert "PLANTED_VENV_LEDGER" not in done.stdout
    assert "usage:" in done.stdout.lower()


def test_bin_ledger_without_python_prints_one_stderr_line(tmp_path: Path) -> None:
    dest = tmp_path / "plugin"
    _copy_plugin(dest)
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    done = _run_bin(
        dest,
        ["--help"],
        cwd=tmp_path,
        env=_clean_env(tmp_path, {"PATH": str(empty)}),
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
