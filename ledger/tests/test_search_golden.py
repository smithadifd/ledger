"""Golden-query recall suite: a query must surface its document in the top ranks.

Each case is (query, expected document, max rank): it passes only when the
expected document ranks at or above its max rank for that query, with the
case's own ``--type`` filter if it has one. A case whose expected document is
absent from the results fails; no case is passed over.

The fixture ledger is synthetic and built under tmp_path. Its first case is a
miss that was found by accident: a ruling written as a ``decided:`` note on a
busy parent item (40 notes) ranked past the default ten cards for the query
that named it. A fix with no regression test drifts back, so the case stays.
A search that missed something it should have found adds a case here; the
``search-miss`` log and ``search-audit`` replay name the candidates.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

import pytest

from ledger.cli import main
from ledger.search import MAX_LIMIT
from ledger.store import open_ledger
from ledger.verbs import EXIT_NOT_FOUND, EXIT_OK

PARENT = "parent-item"
CHILD = "child-item"
RELEASE = "release-item"
RATE = "rate-item"
RULING = (
    "decided: the harness map is the one picture of the harness, owned by "
    f"{CHILD}; its tooltips are attested by machine, never by a reader"
)
DOTTED_RULING = "decided: release.notes are generated from the changelog, never written by hand"
NOTES_ON_PARENT = 40
RULING_AT = 20
DISTRACTORS = 7


@dataclass(frozen=True)
class Case:
    """``expected`` names a document of the fixture ledger by key (see ``busy``)."""

    name: str
    query: str
    expected: str
    max_rank: int
    doc_type: str | None = None


GOLDEN_CASES = (
    # The 2026-11-20 miss: a ruling on a busy parent, named by the child it
    # assigns, must be in the top ten for the words that describe it ...
    Case("ruling-on-a-busy-parent", "harness map", "ruling", 10),
    # ... and --type decision must isolate it.
    Case("ruling-isolated-by-type", "harness map", "ruling", 1, doc_type="decision"),
    # A dotted word is matched as a phrase of its parts.
    Case("dotted-token", "release.notes", "dotted-ruling", 3),
    # A hyphenated word stays one phrase.
    Case("hyphenated-token", "rate-limiter", "rate-item", 3),
)


def invoke(monkeypatch, argv: list[str]) -> tuple[int, str, str]:
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _append(lg, kind: str, item_id: str | None, payload: dict, key: str):
    return lg.append(kind=kind, item_id=item_id, payload=payload,
                     idempotency_key=key, harness="fixture")


def ranked_ids(monkeypatch, db: Path, query: str, doc_type: str | None) -> list[str]:
    """Every hit's document id for the query, best first, from the CLI."""
    argv = ["--json", "--db", str(db), "search", "--limit", str(MAX_LIMIT), query]
    if doc_type is not None:
        argv += ["--type", doc_type]
    code, out, err = invoke(monkeypatch, argv)
    assert "Traceback" not in err
    if code == EXIT_NOT_FOUND:
        return []
    assert code == EXIT_OK, out
    return [card["id"] for card in json.loads(out)["data"]["cards"]]


def check_case(monkeypatch, db: Path, docs: dict[str, str], case: Case) -> int:
    """The case's rank, or an AssertionError naming why it failed."""
    expected = docs[case.expected]
    ids = ranked_ids(monkeypatch, db, case.query, case.doc_type)
    assert expected in ids, (
        f"golden case {case.name!r}: {expected} is absent from the {len(ids)} results "
        f"for {case.query!r}"
    )
    rank = ids.index(expected) + 1
    assert rank <= case.max_rank, (
        f"golden case {case.name!r}: {expected} ranks {rank} for {case.query!r}, "
        f"past its max rank {case.max_rank}"
    )
    return rank


@pytest.fixture
def busy(tmp_path: Path, monkeypatch) -> dict:
    """The fixture ledger and the documents the cases name, by key."""
    monkeypatch.delenv("HARNESS_ROOT", raising=False)
    db = tmp_path / "state" / "home.db"
    docs: dict[str, str] = {}
    with open_ledger(db) as lg:
        _append(lg, "item.created", PARENT,
                {"type": "initiative", "title": "Readout of the first round"}, "parent")
        _append(lg, "item.created", CHILD,
                {"type": "ticket", "title": "Harness map tooltips"}, "child")
        for n in range(DISTRACTORS):
            _append(lg, "item.created", f"hw-{n}",
                    {"type": "ticket", "title": f"Harness wrapper cleanup {n}"}, f"hw-{n}")
            _append(lg, "item.created", f"mr-{n}",
                    {"type": "ticket", "title": f"Map renderer fix {n}"}, f"mr-{n}")
        for n in range(NOTES_ON_PARENT):
            if n == RULING_AT:
                event = _append(lg, "item.note", PARENT, {"text": RULING}, "ruling")
                docs["ruling"] = f"{PARENT}:{event.id}"
            else:
                # Bookkeeping: most notes never name the topic, a few name one
                # of its words.
                tail = " with the harness checks green" if n % 10 == 3 else ""
                _append(lg, "item.note", PARENT, {
                    "text": f"round {n} receipt recorded; lane {n} closed on the readout{tail}",
                }, f"note-{n}")
        # A dotted word: only the ruling holds the two words side by side;
        # the distractors hold both words in short titles, out of order, so
        # they outrank it the moment the dotted word is split into two words.
        _append(lg, "item.created", RELEASE,
                {"type": "initiative", "title": "Changelog generation"}, "release")
        event = _append(lg, "item.note", RELEASE, {"text": DOTTED_RULING}, "dotted-ruling")
        docs["dotted-ruling"] = f"{RELEASE}:{event.id}"
        for n in range(6):
            _append(lg, "item.created", f"rc-{n}",
                    {"type": "ticket", "title": f"Release checklist release steps {n}"}, f"rc-{n}")
            _append(lg, "item.created", f"nc-{n}",
                    {"type": "ticket", "title": "Notes before release"}, f"nc-{n}")
        # A hyphenated word: one ticket holds the phrase; the distractors hold
        # both words, out of order, in titles shorter than its own.
        _append(lg, "item.created", RATE,
                {"type": "ticket", "title": "Rate-limiter backoff"}, "rate")
        docs["rate-item"] = RATE
        for n in range(6):
            _append(lg, "item.created", f"rt-{n}",
                    {"type": "ticket", "title": f"Rate card rate review {n}"}, f"rt-{n}")
            _append(lg, "item.created", f"lm-{n}",
                    {"type": "ticket", "title": "Limiter rate"}, f"lm-{n}")
    (db.parent / "config.json").write_text(json.dumps({"search_roots": []}), encoding="utf-8")
    return {"db": db, "docs": docs}


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=lambda case: case.name)
def test_golden_query(busy, monkeypatch, case):
    check_case(monkeypatch, busy["db"], busy["docs"], case)


def test_the_fixture_parent_document_ranks_past_the_default_ten(busy, monkeypatch):
    # The fixture holds the problem the seed case guards: the parent's own
    # document, where the ruling is one note of forty, ranks past ten.
    ids = ranked_ids(monkeypatch, busy["db"], "harness map", None)
    assert PARENT in ids
    assert ids.index(PARENT) + 1 > 10, ids[:12]


def test_a_case_whose_expected_document_is_absent_fails(busy, monkeypatch):
    case = Case("absent", "harness map", "ghost", 10)
    docs = {**busy["docs"], "ghost": "no-such-item:99999"}
    with pytest.raises(AssertionError, match="is absent from the"):
        check_case(monkeypatch, busy["db"], docs, case)


def test_a_case_ranked_past_its_max_rank_fails(busy, monkeypatch):
    rank = check_case(monkeypatch, busy["db"], busy["docs"], GOLDEN_CASES[0])
    assert rank > 1, "the seed ruling must not rank first, or this case guards nothing"
    tight = Case("tight", "harness map", "ruling", rank - 1)
    with pytest.raises(AssertionError, match=f"ranks {rank} for 'harness map', past its max rank {rank - 1}"):
        check_case(monkeypatch, busy["db"], busy["docs"], tight)
