"""A refusal says the limit it enforces and the forms it allows.

The reason and path stay first (``too-long:point``, ``bad-type:refs[0]``), so a
caller that matches the start of the message still matches; only text after a
semicolon is added. The rules themselves are unchanged: a URL in ``refs`` is
still refused.
"""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ledger.cli import main
from ledger.schema import REF_FORMS, ValidationError, validate_payload
from ledger.verbs import EXIT_OK, EXIT_REFUSED

# The text a ref refusal carries after its prefix, written out here on purpose:
# a change to the allowed forms is a change to this test too.
REF_ALLOWED = (
    "; allowed: a file path (src/app.py:12), a pull request or issue (#12), "
    "a pull request or issue with its repository (owner/repo#12), "
    "a branch (branch:feat/x), "
    "a commit sha of 7 to 40 lowercase hex digits (3f2a9c1), an event (event:42); "
    "a URL is not a ref: put it on the item with ledger link ITEM --rel url --target URL"
)


def stop(**changes) -> dict:
    payload = {"point": "p", "phase": "build"}
    payload.update(changes)
    return payload


def refusal(payload: dict, kind: str = "item.stopped") -> str:
    with pytest.raises(ValidationError) as caught:
        validate_payload(kind, payload)
    return str(caught.value)


# --- too-long carries the limit and the length given --------------------------


@pytest.mark.parametrize("length", [601, 643, 5000])
def test_a_too_long_point_says_the_limit_and_the_length_given(length: int) -> None:
    assert refusal(stop(point="x" * length)) == f"too-long:point; max 600, got {length}"


def test_a_point_of_exactly_the_limit_is_accepted() -> None:
    assert validate_payload("item.stopped", stop(point="x" * 600))["point"] == "x" * 600


def test_a_too_long_array_says_the_limit_and_the_length_given() -> None:
    assert refusal(stop(refs=["a.md"] * 21)) == "too-long:refs; max 20, got 21"
    assert refusal(stop(skills=["review"] * 13)) == "too-long:skills; max 12, got 13"


def test_a_too_long_ref_says_the_limit_and_the_length_given() -> None:
    ref = "a" * 297 + ".md"  # 300 characters is the limit; this is 300
    assert validate_payload("item.stopped", stop(refs=[ref]))["refs"] == [ref]
    assert refusal(stop(refs=[ref + "x"])) == "too-long:refs[0]; max 300, got 301"


def test_too_long_keeps_its_prefix_first() -> None:
    assert refusal(stop(point="x" * 700)).startswith("too-long:point")
    assert refusal(stop(refs=["a.md"] * 30)).startswith("too-long:refs")


# --- a ref refusal names the allowed forms and the route for a URL ----------


@pytest.mark.parametrize("given", [
    "https://github.com/owner/repo/pull/12",
    "http://example.com/a",
    "ftp://host/file",
    "owner/repo/pull/12:",
    "task",
    "",
    5,
    None,
    ["a.md"],
])
def test_a_refused_ref_names_the_allowed_forms(given) -> None:
    assert refusal(stop(refs=[given])) == "bad-type:refs[0]" + REF_ALLOWED


def test_the_index_in_the_prefix_is_the_refused_entry() -> None:
    assert refusal(stop(refs=["a.md", "b.md", "https://x.example/y"])) == (
        "bad-type:refs[2]" + REF_ALLOWED
    )


@pytest.mark.parametrize("url", [
    "https://github.com/owner/repo/pull/12",
    "http://example.com/a",
    "git@github.com:owner/repo.git",
])
def test_a_url_is_still_refused_as_a_ref(url: str) -> None:
    with pytest.raises(ValidationError, match=r"^bad-type:refs\[0\]"):
        validate_payload("item.stopped", stop(refs=[url]))


@pytest.mark.parametrize("form, example", REF_FORMS)
def test_every_listed_example_is_accepted_as_a_ref(form: str, example: str) -> None:
    assert validate_payload("item.stopped", stop(refs=[example]))["refs"] == [example]


def test_the_listed_forms_are_the_ones_the_message_prints() -> None:
    message = refusal(stop(refs=["https://x.example/y"]))
    for form, example in REF_FORMS:
        assert f"{form} ({example})" in message


# --- what is not described keeps the bare prefix ----------------------------


@pytest.mark.parametrize("payload, expected", [
    (stop(point=""), "bad-type:point"),
    (stop(point=5), "bad-type:point"),
    (stop(skills=["has space"]), "bad-type:skills[0]"),
    (stop(skills=[""]), "bad-type:skills[0]"),
])
def test_a_field_with_nothing_to_list_keeps_the_bare_message(payload, expected) -> None:
    assert refusal(payload) == expected


def test_the_phase_enum_refusal_is_unchanged() -> None:
    assert refusal({"point": "p", "phase": "later"}) == (
        "bad-type:phase; allowed: orient, plan, build, review, close"
    )


# --- through the CLI --------------------------------------------------------


def _run(monkeypatch, argv: list[str]) -> tuple[int, dict]:
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
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


def _append_stop(monkeypatch, db: str, payload: dict) -> tuple[int, dict]:
    return _run(monkeypatch, ["--json", "--db", db, "append", "--kind", "item.stopped",
                              "--item", "it-1", "--payload", json.dumps(payload)])


def test_the_cli_refusal_for_a_long_point_carries_the_limit(db: str, monkeypatch) -> None:
    code, body = _append_stop(monkeypatch, db, stop(point="x" * 643))
    assert code == EXIT_REFUSED
    assert body["error"]["code"] == "validation"
    assert body["error"]["message"] == "too-long:point; max 600, got 643"


def test_the_cli_refusal_for_a_url_ref_lists_the_forms(db: str, monkeypatch) -> None:
    code, body = _append_stop(monkeypatch, db, stop(refs=["https://github.com/o/r/pull/1"]))
    assert code == EXIT_REFUSED
    assert body["error"]["message"] == "bad-type:refs[0]" + REF_ALLOWED
