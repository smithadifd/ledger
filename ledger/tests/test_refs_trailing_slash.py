"""A directory ref with one trailing slash is stored without it.

``refs`` on ``item.stopped`` names files and directories. ``some/dir/`` used
to refuse as ``bad-type:refs[N]``; the writing verbs now strip one trailing
slash from a directory ref before validation. Every ledger is synthetic and
built under tmp_path.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ledger.cli import main
from ledger.schema import strip_directory_ref_slash
from ledger.store import open_ledger
from ledger.verbs import EXIT_OK, EXIT_REFUSED


def _run(monkeypatch, argv: list[str], stdin: str | None = None) -> tuple[int, dict]:
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    assert "Traceback" not in err.getvalue()
    return code, json.loads(out.getvalue())


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "ledger.db")
    code, body = _run(monkeypatch, ["--json", "--db", path, "new", "--id", "it-1",
                                     "--type", "ticket", "--title", "one"])
    assert code == EXIT_OK, body
    return path


def _stop(monkeypatch, db: str, refs: list[str]) -> tuple[int, dict]:
    payload = {"point": "p", "phase": "build", "refs": refs}
    return _run(monkeypatch, ["--json", "--db", db, "append", "--kind", "item.stopped",
                              "--item", "it-1", "--payload", json.dumps(payload)])


def _stored_refs(db: str) -> list[list[str]]:
    with open_ledger(db) as lg:
        return [event.payload["refs"] for event in lg.events(kind="item.stopped")]


@pytest.mark.parametrize("given, stored", [
    ("some/dir/", "some/dir"),
    ("some/dir", "some/dir"),
    ("/abs/some/dir/", "/abs/some/dir"),
    ("~/notes/", "~/notes"),
    ("plan.md", "plan.md"),
    ("some/file.md:3", "some/file.md:3"),
])
def test_append_stores_a_directory_ref_without_its_trailing_slash(db, monkeypatch, given, stored):
    code, body = _stop(monkeypatch, db, [given])
    assert code == EXIT_OK, body
    assert body["data"]["event"]["payload"]["refs"] == [stored]
    assert _stored_refs(db) == [[stored]]


@pytest.mark.parametrize("given", [
    "some/dir//",      # only one slash is stripped
    "dir/",            # a bare name is not a ref, slash or not
    "/",
    "branch:feat/",    # not a path: a branch name keeps its own rules
    "some/file.md:3/",  # a line ref is not a directory
    "some/../dir/",    # a ".." segment stays refused
])
def test_near_misses_still_refuse_by_name(db, monkeypatch, given):
    code, body = _stop(monkeypatch, db, ["ok/dir", given])
    assert code == EXIT_REFUSED
    assert body["error"]["code"] == "validation"
    assert body["error"]["message"] == "bad-type:refs[1]"
    assert _stored_refs(db) == []


def test_append_many_strips_the_slash_too(db, monkeypatch):
    batch = [{"kind": "item.stopped",
              "payload": {"point": "p", "phase": "build", "refs": ["a/b/", "c/d"]}}]
    code, body = _run(monkeypatch, ["--json", "--db", db, "append-many", "it-1"],
                      stdin=json.dumps(batch))
    assert code == EXIT_OK, body
    assert _stored_refs(db) == [["a/b", "c/d"]]


def test_the_helper_changes_only_ref_lists():
    unchanged = {"point": "p", "phase": "build", "refs": ["some/dir"]}
    assert strip_directory_ref_slash("item.stopped", unchanged) is unchanged
    evidence = {"round": 1, "verdict": "pass", "evidence": "some/dir/"}
    assert strip_directory_ref_slash("review.recorded", evidence) is evidence
    assert strip_directory_ref_slash("no.such.kind", {"refs": ["a/b/"]}) == {"refs": ["a/b/"]}
    assert strip_directory_ref_slash("item.stopped", "not a dict") == "not a dict"
    given = {"point": "p", "phase": "build", "refs": ["a/b/"]}
    assert strip_directory_ref_slash("item.stopped", given) == {**given, "refs": ["a/b"]}
    assert given["refs"] == ["a/b/"]  # the caller's payload is not mutated
