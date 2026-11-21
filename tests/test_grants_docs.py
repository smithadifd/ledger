"""The grants docs name the gate call and its readers, in the module, its plugin
copy and the CLI page; none still says no gate reads the grants."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GRANT_FILES = (
    ROOT / "ledger" / "verbs" / "grants.py",
    ROOT / "plugins" / "ledger-hooks" / "ledger" / "verbs" / "grants.py",
    ROOT / "docs" / "cli.md",
)
READERS = ("merge command", "review-run round check", "apply-reviewed-patch", "design dispatch")


@pytest.mark.parametrize("path", GRANT_FILES, ids=["module", "plugin-copy", "cli-doc"])
def test_the_grants_docs_name_standing_and_its_readers(path: Path) -> None:
    text = " ".join(path.read_text(encoding="utf-8").split())
    assert "no gate reads it" not in text
    assert "ledger.verbs.grants.standing" in text
    for reader in READERS:
        assert reader in text, reader
