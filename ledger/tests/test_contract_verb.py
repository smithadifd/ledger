"""``ledger contract`` prints the agent contract from the installed package.

The repository's own tests keep the packaged page byte-equal to the page it
documents and build a wheel to prove the page ships; these tests read only
the package.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from importlib import resources
from pathlib import Path

from ledger.cli import main
from ledger.verbs import EXIT_OK
from ledger.verbs import contract as contract_verb
from ledger.verbs.resume import SHORT_FORM


def invoke(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _packaged() -> str:
    return resources.files("ledger").joinpath("agent_contract.md").read_text(encoding="utf-8")


def test_the_page_ships_beside_the_package_modules() -> None:
    page = _packaged()
    assert page.startswith("# The agent contract\n")
    assert "## Write triggers" in page and "## The close rule" in page
    assert contract_verb.contract_text() == page


def test_contract_prints_the_whole_page_and_never_opens_the_database(tmp_path: Path) -> None:
    db = tmp_path / "never" / "ledger.db"
    code, out, err = invoke(["--db", str(db), "contract"])
    assert (code, err) == (EXIT_OK, "")
    assert out == _packaged()
    assert not db.parent.exists()


def test_contract_json_carries_the_page(tmp_path: Path) -> None:
    code, out, err = invoke(["--json", "--db", str(tmp_path / "x.db"), "contract"])
    assert code == EXIT_OK, err
    assert json.loads(out) == {"data": {"contract": _packaged()}, "error": None, "ok": True}


def test_contract_is_a_read_verb_and_takes_no_arguments(tmp_path: Path) -> None:
    assert contract_verb.VERB.writes is False
    code, _out, err = invoke(["--db", str(tmp_path / "x.db"), "contract", "extra"])
    assert code == 2, err


def test_the_short_form_points_at_the_contract_verb() -> None:
    first = SHORT_FORM.splitlines()[0]
    assert "run ledger contract for the full page" in first, first
    assert "docs folder" not in first
