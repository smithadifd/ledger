"""No tracked file under ``ledger/`` names a home directory or a consumer's private root.

A consumer that vendors ``ledger/`` may install it into a profile that refuses
any file naming a user's home directory (``/home/<name>``, ``/Users/<name>``,
or the ``-Users-`` form some tools derive from it) or one of the consumer's own
root names. Examples in help text use ``~/`` instead.

The token list mirrors the strict-root scanner of the first consumer that
vendors this package (claude-toolkit's ``scripts/check-linear-profile-roots.py``),
matched case-insensitively. Keep the two in step when either changes. Like that
scanner, it leaves out ``ledger/tests/``, which never ships into a profile (the
tests clear such variables from the environment by name).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TOKEN_PATTERNS = (
    re.compile(r"harness_root", re.IGNORECASE),
    re.compile(r"harness-root", re.IGNORECASE),
    re.compile(r"DEFAULT_ROOT", re.IGNORECASE),
    re.compile(r"DEFAULT_MEMORY_INDEX", re.IGNORECASE),
    re.compile(r"\bfoundry\b", re.IGNORECASE),
    re.compile(r"-Users-", re.IGNORECASE),
    re.compile(r"/Users/[A-Za-z0-9._-]+", re.IGNORECASE),
    re.compile(r"/home/[A-Za-z0-9._-]+", re.IGNORECASE),
)


def _tracked_package_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "ledger"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return [
        ROOT / name
        for name in out.decode().split("\0")
        if name and not name.startswith("ledger/tests/")
    ]


def find_violations(path: Path, text: str) -> list[str]:
    found = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern in TOKEN_PATTERNS:
            match = pattern.search(line)
            if match:
                found.append(f"{path}:{lineno}: {match.group(0)}")
    return found


def test_no_tracked_package_file_names_a_machine_path():
    files = _tracked_package_files()
    assert files, "git ls-files found no files under ledger/"
    violations = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        violations.extend(find_violations(path.relative_to(ROOT), text))
    assert not violations, "\n".join(violations)


def test_each_token_is_caught():
    samples = [
        "ledger link x /home/me/notes/brief.md",
        "see /Users/someone/code",
        "-Users-someone-code",
        "HARNESS_ROOT=1",
        "harness-root",
        "DEFAULT_ROOT = 1",
        "DEFAULT_MEMORY_INDEX",
        "the Foundry repo",
    ]
    for sample in samples:
        assert find_violations(Path("x"), sample), sample


def test_home_shorthand_passes():
    assert not find_violations(Path("x"), "ledger link x ~/notes/brief.md")
    assert not find_violations(Path("x"), "/home/ alone names no user")
