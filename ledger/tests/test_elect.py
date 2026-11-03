"""Election records and the quote lint."""
from __future__ import annotations

import io
import json
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

import ledger.verbs.elect as elect_module
from ledger.cli import main
from ledger.store import open_ledger
from ledger.verbs import CliError, Context, EXIT_REFUSED, EXIT_USAGE


def invoke(monkeypatch, argv, *, env=None):
    merged = {
        "LEDGER_HARNESS": "pytest",
        "LEDGER_AGENT": "tester",
        "LEDGER_MODEL": "test-model",
        "LEDGER_SESSION": "sess-1",
    }
    if env:
        merged.update(env)
    for key, value in merged.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "ledger.db")


def seed_item(monkeypatch, db: str, item_id: str = "it-1") -> None:
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "new", "--id", item_id, "--type", "decision", "--title", "synthetic"],
    )
    assert code == 0, err


def seed_utterance(monkeypatch, db: str, text: str) -> int:
    payload = json.dumps({"text": text})
    code, out, err = invoke(
        monkeypatch,
        [
            "--json",
            "--db",
            db,
            "append",
            "--kind",
            "utterance.captured",
            "--payload",
            payload,
            "--channel",
            "terminal",
            "--provenance-class",
            "submitted-prompt",
        ],
    )
    assert code == 0, err
    return json.loads(out)["data"]["event"]["id"]


def seed_other_event(monkeypatch, db: str) -> int:
    code, out, err = invoke(
        monkeypatch,
        [
            "--json",
            "--db",
            db,
            "append",
            "--kind",
            "session.opened",
            "--payload",
            json.dumps({"purpose": "synthetic"}),
        ],
    )
    assert code == 0, err
    return json.loads(out)["data"]["event"]["id"]


def seed_item_note(monkeypatch, db: str, text: str, item_id: str = "it-1") -> int:
    """A non-utterance event whose payload carries text, unlike session.opened."""
    code, out, err = invoke(
        monkeypatch,
        [
            "--json",
            "--db",
            db,
            "append",
            "--kind",
            "item.note",
            "--item",
            item_id,
            "--payload",
            json.dumps({"text": text}),
        ],
    )
    assert code == 0, err
    return json.loads(out)["data"]["event"]["id"]


def seed_ask(
    monkeypatch,
    db: str,
    *,
    item_id: str = "it-1",
    question: str = "pick a colour?",
    labels: tuple[str, ...] = ("red", "blue"),
    question_id: str | None = None,
) -> int:
    payload: dict = {"question": question, "options": [{"label": label} for label in labels]}
    if question_id is not None:
        payload["question_id"] = question_id
    code, out, err = invoke(
        monkeypatch,
        [
            "--json",
            "--db",
            db,
            "append",
            "--kind",
            "decision.asked",
            "--item",
            item_id,
            "--payload",
            json.dumps(payload),
        ],
    )
    assert code == 0, err
    return json.loads(out)["data"]["event"]["id"]


def elect_verb(
    monkeypatch,
    db: str,
    item_id: str,
    quote_ref: int,
    *,
    question: str = "pick a colour?",
    chosen: str = "blue",
    chosen_description: str = "cool",
    rejected: list[str] | None = None,
    scope: str = "palette",
    quotes: list[str] | None = None,
    ask: int | None = None,
    json_mode: bool = True,
) -> tuple[int, str, str]:
    if rejected is None:
        rejected = ["red"]
    if quotes is None:
        quotes = ["pick blue"]
    argv = ["--db", db, "elect", item_id]
    if json_mode:
        argv.insert(0, "--json")
    argv += [
        "--question",
        question,
        "--chosen",
        chosen,
        "--chosen-description",
        chosen_description,
        "--scope",
        scope,
        "--quote-ref",
        str(quote_ref),
    ]
    for label in rejected:
        argv += ["--rejected", label]
    for quote in quotes:
        argv += ["--quote", quote]
    if ask is not None:
        argv += ["--ask", str(ask)]
    return invoke(monkeypatch, argv)


def append_elected(
    monkeypatch,
    db: str,
    item_id: str,
    quote_ref: int,
    *,
    question: str = "pick a colour?",
    chosen: str = "blue",
    chosen_description: str = "cool",
    rejected: list[str] | None = None,
    scope: str = "palette",
    quotes: list[str] | None = None,
    json_mode: bool = True,
) -> tuple[int, str, str]:
    if rejected is None:
        rejected = ["red"]
    payload = {
        "question": question,
        "chosen": chosen,
        "chosen_description": chosen_description,
        "rejected": rejected,
        "scope": scope,
        "quote_ref": quote_ref,
    }
    if quotes is not None:
        payload["quotes"] = quotes
    argv = [
        "--db",
        db,
        "append",
        "--kind",
        "decision.elected",
        "--item",
        item_id,
        "--payload",
        json.dumps(payload),
    ]
    if json_mode:
        argv.insert(0, "--json")
    return invoke(monkeypatch, argv)


def open_ask_ids(db: str) -> list[int]:
    lg = open_ledger(db)
    try:
        return [row["asked_event_id"] for row in lg.open_decisions()]
    finally:
        lg.close()


def elected_events(db: str) -> list:
    lg = open_ledger(db)
    try:
        return lg.events(kind="decision.elected")
    finally:
        lg.close()


def lint(monkeypatch, db: str) -> tuple[int, str, str]:
    return invoke(monkeypatch, ["--json", "--db", db, "lint-quotes"])


def test_elect_records_valid_election(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")

    code, out, err = elect_verb(
        monkeypatch,
        db,
        "it-1",
        utterance_id,
        quotes=["please", "pick blue"],
    )
    assert code == 0, err
    body = json.loads(out)
    assert body["ok"] is True
    event = body["data"]["event"]
    assert event["kind"] == "decision.elected"
    payload = event["payload"]
    assert payload["question"] == "pick a colour?"
    assert payload["chosen"] == "blue"
    assert payload["chosen_description"] == "cool"
    assert payload["rejected"] == ["red"]
    assert payload["scope"] == "palette"
    assert payload["quote_ref"] == utterance_id
    assert payload["quotes"] == ["please", "pick blue"]


def test_elect_refuses_missing_quote_ref(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    code, out, err = elect_verb(monkeypatch, db, "it-1", 999)
    assert code == EXIT_REFUSED, err
    body = json.loads(out)
    assert body["ok"] is False
    assert body["error"]["code"] == "unresolvable_ref"
    assert "quote_ref not found" in body["error"]["message"]


def test_elect_refuses_quote_ref_not_utterance(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    other_id = seed_other_event(monkeypatch, db)
    code, out, err = elect_verb(monkeypatch, db, "it-1", other_id)
    assert code == EXIT_REFUSED, err
    body = json.loads(out)
    assert body["error"]["code"] == "unresolvable_ref"
    assert "utterance.captured" in body["error"]["message"]


@pytest.mark.parametrize(
    "drop",
    [
        "--question",
        "--chosen",
        "--chosen-description",
        "--rejected",
        "--scope",
        "--quote-ref",
        "--quote",
    ],
)
def test_elect_refuses_missing_field(tmp_path: Path, monkeypatch, drop: str) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    option_pairs = [
        ("--question", "pick a colour?"),
        ("--chosen", "blue"),
        ("--chosen-description", "cool"),
        ("--rejected", "red"),
        ("--scope", "palette"),
        ("--quote-ref", "1"),
        ("--quote", "pick blue"),
    ]
    argv = ["--db", db, "elect", "it-1"]
    for flag, value in option_pairs:
        if flag != drop:
            argv.extend([flag, value])
    code, _out, err = invoke(monkeypatch, argv)
    assert code == EXIT_USAGE, err
    assert "Traceback" not in err


def test_elect_refuses_empty_chosen_description(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")
    code, out, err = elect_verb(
        monkeypatch,
        db,
        "it-1",
        utterance_id,
        chosen_description="",
    )
    assert code == EXIT_REFUSED, err
    source = out.lower() if out else err.lower()
    assert "missing field" in source


@pytest.mark.parametrize(
    "field",
    ["question", "chosen", "chosen_description", "scope"],
)
def test_elect_refuses_whitespace_only_field(
    tmp_path: Path, monkeypatch, field: str
) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")
    kwargs = {field: "  "}
    code, out, err = elect_verb(monkeypatch, db, "it-1", utterance_id, **kwargs)
    assert code == EXIT_REFUSED, err
    body = json.loads(out)
    assert body["error"]["code"] == "missing_field", body["error"]["message"]
    assert "missing field" in body["error"]["message"]
    assert elected_events(db) == []


@pytest.mark.parametrize("label", ["", "  "])
def test_elect_refuses_empty_rejected_label(tmp_path: Path, monkeypatch, label: str) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")
    code, out, err = elect_verb(
        monkeypatch,
        db,
        "it-1",
        utterance_id,
        rejected=[label],
    )
    assert code == EXIT_REFUSED, err
    body = json.loads(out)
    assert body["error"]["code"] == "missing_field", body["error"]["message"]
    assert "rejected labels must not be empty" in body["error"]["message"]
    assert elected_events(db) == []


def test_elect_refuses_whitespace_only_quote(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")
    code, out, err = elect_verb(
        monkeypatch,
        db,
        "it-1",
        utterance_id,
        quotes=[" "],
    )
    assert code == EXIT_REFUSED, err
    body = json.loads(out)
    assert body["error"]["code"] == "missing_field", body["error"]["message"]
    assert elected_events(db) == []


def test_elect_refuses_nonverbatim_quote(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")
    code, out, err = elect_verb(
        monkeypatch,
        db,
        "it-1",
        utterance_id,
        quotes=["pick green"],
    )
    assert code == EXIT_REFUSED, err
    body = json.loads(out)
    assert body["error"]["code"] == "quote_mismatch", body["error"]["message"]
    assert "pick green" in body["error"]["message"]
    assert elected_events(db) == []


def test_elect_refuses_case_folded_quote(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")
    code, out, err = elect_verb(
        monkeypatch,
        db,
        "it-1",
        utterance_id,
        quotes=["Pick Blue"],
    )
    assert code == EXIT_REFUSED, err
    assert json.loads(out)["error"]["code"] == "quote_mismatch"
    assert elected_events(db) == []


def test_elect_refuses_whitespace_collapsed_quote(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    utterance_id = seed_utterance(monkeypatch, db, "please  pick blue")
    code, out, err = elect_verb(
        monkeypatch,
        db,
        "it-1",
        utterance_id,
        quotes=["please pick blue"],
    )
    assert code == EXIT_REFUSED, err
    assert json.loads(out)["error"]["code"] == "quote_mismatch"
    assert elected_events(db) == []


def test_elect_ask_closes_exactly_that_ask(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    ask_a = seed_ask(
        monkeypatch, db, question="ship it?", labels=("yes", "no"), question_id="shared"
    )
    ask_b = seed_ask(
        monkeypatch, db, question="ship it?", labels=("now", "later"), question_id="shared"
    )
    utterance_id = seed_utterance(monkeypatch, db, "ship it now, not later")

    code, out, err = elect_verb(
        monkeypatch,
        db,
        "it-1",
        utterance_id,
        question="Ship it?",  # paraphrased: only the ask link can close ask_b
        chosen="immediately",  # free-text answer, not among ask_b's offered labels
        rejected=["later"],
        scope="release",
        quotes=["ship it now"],
        ask=ask_b,
    )
    assert code == 0, err
    payload = json.loads(out)["data"]["event"]["payload"]
    assert payload["asked_event_id"] == ask_b
    assert payload["question_id"] == "shared"
    assert open_ask_ids(db) == [ask_a]


def test_elect_without_ask_closes_no_ask(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    ask_id = seed_ask(
        monkeypatch, db, question="pick a colour?", question_id="qa"
    )
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")

    code, out, err = elect_verb(
        monkeypatch,
        db,
        "it-1",
        utterance_id,
        question="pick a colour?",  # identical text: must not close the ask
    )
    assert code == 0, err
    assert "asked_event_id" not in json.loads(out)["data"]["event"]["payload"]
    assert open_ask_ids(db) == [ask_id]


@pytest.mark.parametrize("question_id", [None, "qid-1"])
def test_elect_ask_records_question_id_from_ask(
    tmp_path: Path, monkeypatch, question_id: str | None
) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    ask_id = seed_ask(monkeypatch, db, question_id=question_id)
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")

    code, out, err = elect_verb(monkeypatch, db, "it-1", utterance_id, ask=ask_id)
    assert code == 0, err
    payload = json.loads(out)["data"]["event"]["payload"]
    assert payload["asked_event_id"] == ask_id
    if question_id is None:
        assert "question_id" not in payload
    else:
        assert payload["question_id"] == question_id


def test_elect_refuses_ask_not_open(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    ask_id = seed_ask(monkeypatch, db)
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")

    code, _out, err = elect_verb(monkeypatch, db, "it-1", utterance_id, ask=ask_id)
    assert code == 0, err
    assert open_ask_ids(db) == []

    code, out, err = elect_verb(monkeypatch, db, "it-1", utterance_id, ask=ask_id)
    assert code == EXIT_REFUSED, err
    assert "Traceback" not in err
    body = json.loads(out)
    assert body["error"]["code"] == "unresolvable_ask", body["error"]["message"]
    assert len(elected_events(db)) == 1


def test_concurrent_elect_ask_refuses_second(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    ask_id = seed_ask(monkeypatch, db, question_id="shared")
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")
    args = SimpleNamespace(
        item="it-1",
        question="pick a colour?",
        chosen="blue",
        chosen_description="cool",
        rejected=["red"],
        scope="palette",
        quote_ref=utterance_id,
        quotes=["pick blue"],
        ask=ask_id,
    )
    ctx = Context(
        db=db,
        json_mode=True,
        actor="tester",
        harness="pytest",
        model="test-model",
        session="sess-1",
    )
    both_checked_open = threading.Barrier(2)
    original_resolve_ask = elect_module._resolve_ask

    def synchronized_resolve_ask(conn, item_id, asked_event_id):
        if not conn.in_transaction:
            both_checked_open.wait(timeout=5)
        return original_resolve_ask(conn, item_id, asked_event_id)

    monkeypatch.setattr(elect_module, "_resolve_ask", synchronized_resolve_ask)
    outcomes = {}

    def call_elect(name):
        try:
            outcomes[name] = elect_module.run(args, ctx)
        except Exception as exc:
            outcomes[name] = exc

    threads = [
        threading.Thread(target=call_elect, args=(name,))
        for name in ("first", "second")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()

    successes = [value for value in outcomes.values() if not isinstance(value, Exception)]
    refusals = [value for value in outcomes.values() if isinstance(value, CliError)]
    assert len(successes) == 1
    assert len(refusals) == 1
    assert refusals[0].code == "unresolvable_ask"
    assert refusals[0].exit_code == EXIT_REFUSED
    assert len(elected_events(db)) == 1


def test_elect_refuses_ask_not_found(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")
    code, out, err = elect_verb(monkeypatch, db, "it-1", utterance_id, ask=999)
    assert code == EXIT_REFUSED, err
    assert "Traceback" not in err
    body = json.loads(out)
    assert body["error"]["code"] == "unresolvable_ask", body["error"]["message"]


def test_elect_refuses_ask_not_asked_kind(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    seed_ask(monkeypatch, db)  # an open ask exists; the utterance id is not it
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")
    code, out, err = elect_verb(monkeypatch, db, "it-1", utterance_id, ask=utterance_id)
    assert code == EXIT_REFUSED, err
    body = json.loads(out)
    assert body["error"]["code"] == "unresolvable_ask", body["error"]["message"]


def test_elect_refuses_ask_on_other_item(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db, "it-1")
    seed_item(monkeypatch, db, "it-2")
    ask_id = seed_ask(monkeypatch, db, item_id="it-2")
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")
    code, out, err = elect_verb(monkeypatch, db, "it-1", utterance_id, ask=ask_id)
    assert code == EXIT_REFUSED, err
    body = json.loads(out)
    assert body["error"]["code"] == "unresolvable_ask", body["error"]["message"]
    assert open_ask_ids(db) == [ask_id]


def test_lint_quotes_pass(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")
    code, out, err = elect_verb(
        monkeypatch,
        db,
        "it-1",
        utterance_id,
        quotes=["pick blue", "blue"],
    )
    assert code == 0, err
    code, out, err = lint(monkeypatch, db)
    assert code == 0, err
    body = json.loads(out)
    assert body["ok"] is True
    assert body["data"]["checked"] == 1


def test_lint_quotes_fail(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")
    code, out, err = append_elected(
        monkeypatch,
        db,
        "it-1",
        utterance_id,
        quotes=["pick red"],
    )
    assert code == 0, err
    event_id = json.loads(out)["data"]["event"]["id"]
    code, out, err = lint(monkeypatch, db)
    assert code == EXIT_REFUSED, err
    body = json.loads(out)
    assert body["ok"] is False
    assert body["error"]["code"] == "quote_mismatch"
    message = body["error"]["message"]
    assert f"event={event_id}" in message
    assert "pick red" in message


def test_lint_quotes_case_only_mismatch(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    utterance_id = seed_utterance(monkeypatch, db, "Please pick blue")
    code, out, err = append_elected(
        monkeypatch,
        db,
        "it-1",
        utterance_id,
        quotes=["please pick blue"],
    )
    assert code == 0, err
    code, out, err = lint(monkeypatch, db)
    assert code == EXIT_REFUSED, err
    body = json.loads(out)
    assert body["error"]["code"] == "quote_mismatch"


def test_lint_quotes_whitespace_only_mismatch(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    utterance_id = seed_utterance(monkeypatch, db, "Please  pick blue")
    code, out, err = append_elected(
        monkeypatch,
        db,
        "it-1",
        utterance_id,
        quotes=["Please pick blue"],
    )
    assert code == 0, err
    code, out, err = lint(monkeypatch, db)
    assert code == EXIT_REFUSED, err
    body = json.loads(out)
    assert body["error"]["code"] == "quote_mismatch"


@pytest.mark.parametrize("quotes", [None, []])
def test_lint_quotes_fails_election_without_quotes(
    tmp_path: Path, monkeypatch, quotes: list[str] | None
) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    utterance_id = seed_utterance(monkeypatch, db, "please pick blue")
    code, out, err = append_elected(
        monkeypatch,
        db,
        "it-1",
        utterance_id,
        quotes=quotes,
    )
    assert code == 0, err
    code, out, err = lint(monkeypatch, db)
    assert code == EXIT_REFUSED, err
    body = json.loads(out)
    assert body["error"]["code"] == "quote_mismatch"
    assert "election has no quotes" in body["error"]["message"]


@pytest.mark.parametrize("ref_kind", ["missing", "non_utterance"])
def test_lint_quotes_resolves_bad_ref_without_quotes(
    tmp_path: Path, monkeypatch, ref_kind: str
) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    quote_ref = 999
    if ref_kind == "non_utterance":
        quote_ref = seed_item_note(monkeypatch, db, "pick blue")
    code, out, err = append_elected(
        monkeypatch,
        db,
        "it-1",
        quote_ref,
        quotes=[],
    )
    assert code == 0, err
    code, out, err = lint(monkeypatch, db)
    assert code == EXIT_REFUSED, err
    message = json.loads(out)["error"]["message"]
    assert "election has no quotes" in message
    assert "unresolvable quote_ref" in message


def test_lint_quotes_fails_non_utterance_quote_ref(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    # A non-utterance event whose payload text matches the quote: only the
    # kind check can tell it apart from the referenced utterance.
    note_id = seed_item_note(monkeypatch, db, "pick blue")
    code, out, err = append_elected(
        monkeypatch,
        db,
        "it-1",
        note_id,
        quotes=["pick blue"],
    )
    assert code == 0, err
    code, out, err = lint(monkeypatch, db)
    assert code == EXIT_REFUSED, err
    assert "Traceback" not in err
    body = json.loads(out)
    assert body["error"]["code"] == "quote_mismatch"
    assert "unresolvable quote_ref" in body["error"]["message"]


def test_lint_quotes_fails_missing_quote_ref(tmp_path: Path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed_item(monkeypatch, db)
    code, out, err = append_elected(
        monkeypatch,
        db,
        "it-1",
        999,
        quotes=["pick blue"],
    )
    assert code == 0, err
    code, out, err = lint(monkeypatch, db)
    assert code == EXIT_REFUSED, err
    assert "Traceback" not in err
    body = json.loads(out)
    assert body["error"]["code"] == "quote_mismatch"
    assert "unresolvable quote_ref" in body["error"]["message"]


def test_elect_and_lint_discovered(tmp_path: Path, monkeypatch) -> None:
    from ledger.verbs import discover_verbs

    names = set(discover_verbs())
    assert "elect" in names
    assert "lint-quotes" in names
