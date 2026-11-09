"""An answer opens a gate only when the harness captured it for this ask.

One test per case of the design's matrix (M1-M28 and M33-M66; M29-M32 are
the toolkit's), named ``test_mNN_...``, plus the invariant tests at the end.
K is an open ``decision.asked`` on ITEM with options ``apply`` and ``hold``
and a scope. A capture U is written by ``ledger capture --kind picker`` from a
PostToolUse fixture built from ``ledger ask-display K``, unless a case says
otherwise. A "forged" election is a raw ``ledger append`` of the election
``derive_election`` would write, so every field is right except the one a
case changes: the strongest forgery a seat can type.
"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import io
import itertools
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from ledger.answers import (
    AskNotPickable,
    BoundAnswer,
    ask_tag,
    bound_answer,
    derive_election,
    fingerprint,
    render_ask_display,
)
from ledger.cli import main
from ledger.export import export_events
from ledger.restore import restore_export
from ledger.schema import PICKER_CHANNEL, PROMPT_CHANNEL, ValidationError
from ledger.store import open_ledger
from ledger.verbs import CliError
from ledger.verbs.capture import _CAPTURE_WRITER_CAPABILITY, picker_capture, write_capture
from ledger.verbs.elect import validate_quote_provenance

REPO_ROOT = Path(__file__).resolve().parents[2]
ITEM = "it-x"
OTHER = "it-y"
# The question names "apply", so a forged quote of that label is a verbatim
# span of every capture of K and a forgery reaches the rules after R8.
QUESTION = "Should we apply the LC41 graduation?"
SCOPE = "the LC41 graduation only"
OPTIONS = (
    {"label": "apply", "description": "Apply the graduation now"},
    {"label": "hold", "description": "Hold it for later"},
)
_TOOL_USE = itertools.count(1)


# --- helpers ------------------------------------------------------------------


def run_cli(monkeypatch, argv, *, stdin: str | None = None):
    for key, value in {
        "LEDGER_HARNESS": "pytest",
        "LEDGER_AGENT": "tester",
        "LEDGER_OPERATOR": None,
        "LEDGER_MODEL": None,
        "LEDGER_SESSION": None,
        "LEDGER_DB": None,
    }.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def cli(monkeypatch, db: str, *argv: str, stdin: str | None = None) -> tuple[int, dict]:
    code, out, err = run_cli(monkeypatch, ["--json", "--db", db, *argv], stdin=stdin)
    return code, (json.loads(out) if out.strip() else {"stderr": err})


def refusal(envelope: dict) -> tuple[str, str]:
    return envelope["error"]["code"], envelope["error"]["message"]


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "ledger.db")
    new_item(monkeypatch, path, ITEM)
    return path


def new_item(monkeypatch, db: str, item_id: str) -> None:
    code, env = cli(monkeypatch, db, "new", "--id", item_id, "--type", "gate", "--title", item_id)
    assert code == 0, env


def append(monkeypatch, db: str, kind: str, payload: dict, *, item: str | None = ITEM,
           channel: str | None = None, provenance: str | None = None) -> tuple[int, dict]:
    argv = ["append", "--kind", kind, "--payload", json.dumps(payload)]
    if item is not None:
        argv += ["--item", item]
    if channel is not None:
        argv += ["--channel", channel]
    if provenance is not None:
        argv += ["--provenance-class", provenance]
    return cli(monkeypatch, db, *argv)


def ask_payload(*, question: str = QUESTION, options=OPTIONS, scope: str | None = SCOPE,
                question_id: str | None = None) -> dict:
    payload: dict[str, Any] = {"question": question, "options": [dict(o) for o in options]}
    if scope is not None:
        payload["scope"] = scope
    if question_id is not None:
        payload["question_id"] = question_id
    return payload


def ask(monkeypatch, db: str, *, item: str = ITEM, **fields) -> int:
    code, env = append(monkeypatch, db, "decision.asked", ask_payload(**fields), item=item)
    assert code == 0, env
    return env["data"]["event"]["id"]


def ask_display(monkeypatch, db: str, asked: int) -> list[dict]:
    code, env = cli(monkeypatch, db, "ask-display", str(asked))
    assert code == 0, env
    assert env["data"]["display_sha256"] == fingerprint(env["data"]["input"]["questions"])
    return env["data"]["input"]["questions"]


def picker_hook(questions: list, answers: dict, *, annotations=None, **overrides) -> dict:
    response: dict[str, Any] = {"questions": questions, "answers": answers}
    if annotations is not None:
        response["annotations"] = annotations
    hook = {
        "session_id": "sess-fixture",
        "transcript_path": "/fixture/transcript.jsonl",
        "cwd": "/fixture",
        "hook_event_name": "PostToolUse",
        "tool_name": "AskUserQuestion",
        "tool_use_id": f"toolu_{next(_TOOL_USE):04d}",
        "tool_input": {"questions": questions},
        "tool_response": response,
    }
    hook.update(overrides)
    return hook


def prompt_hook(prompt: str) -> dict:
    return {
        "session_id": "sess-fixture",
        "transcript_path": "/fixture/transcript.jsonl",
        "cwd": "/fixture",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    }


def capture(monkeypatch, db: str, hook: dict, kind: str = "picker") -> tuple[int, dict]:
    return cli(monkeypatch, db, "capture", "--kind", kind, stdin=json.dumps(hook))


def captured(monkeypatch, db: str, hook: dict, kind: str = "picker") -> int:
    code, env = capture(monkeypatch, db, hook, kind)
    assert code == 0, env
    return env["data"]["event"]["id"]


def answer(monkeypatch, db: str, asked: int, label: str, *, edit=None, annotations=None) -> int:
    """The owner answers ask K with ``label`` in the picker ask-display printed
    (after ``edit`` changes it, when a case needs another display)."""
    questions = copy.deepcopy(ask_display(monkeypatch, db, asked))
    if edit is not None:
        edit(questions)
    return captured(
        monkeypatch, db,
        picker_hook(questions, {questions[0]["question"]: label}, annotations=annotations),
    )


def stored(db: str, event_id: int):
    with open_ledger(db, create=False) as lg:
        return lg.events(after_id=event_id - 1, limit=1)[0]


def forged(db: str, asked: int, capture_id: int, *, position: int | None = None,
           **changes) -> dict:
    """The election derive_election writes for K and U (at ``position`` when
    given), then ``changes`` (a value of None drops the field)."""
    ask_event = stored(db, asked)
    capture_event = stored(db, capture_id)
    if position is not None:
        picker = dict(capture_event.payload["picker"], chosen_position=position)
        capture_event = dataclasses.replace(
            capture_event, payload=dict(capture_event.payload, picker=picker)
        )
    payload = derive_election(asked, ask_event.payload, capture_event)
    assert payload is not None
    for key, value in changes.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return payload


def probe_election(asked: int, quote_ref: int) -> dict:
    return {
        "question": QUESTION,
        "chosen": "apply",
        "chosen_description": "Apply the graduation now",
        "rejected": ["hold"],
        "scope": SCOPE,
        "quote_ref": quote_ref,
        "quotes": ["apply"],
        "asked_event_id": asked,
    }


def raw_elect(monkeypatch, db: str, payload: dict, *, item: str = ITEM) -> int:
    code, env = append(monkeypatch, db, "decision.elected", payload, item=item)
    assert code == 0, env
    return env["data"]["event"]["id"]


def bound(db: str, asked: int) -> BoundAnswer:
    with open_ledger(db, create=False) as lg:
        return bound_answer(lg, asked)


def reason(db: str, asked: int) -> str | None:
    return bound(db, asked).reason


def pick(monkeypatch, db: str, asked: int, *extra: str, item: str = ITEM) -> tuple[int, dict]:
    return cli(monkeypatch, db, "pick", item, "--ask", str(asked), *extra)


def event_count(db: str, kind: str | None = None) -> int:
    with open_ledger(db, create=False) as lg:
        return len(lg.events(kind=kind))


def open_asks(db: str) -> list[int]:
    with open_ledger(db, create=False) as lg:
        return [row["asked_event_id"] for row in lg.open_decisions()]


def utterance(monkeypatch, db: str, text: str, *, channel: str = "terminal",
              provenance: str = "submitted-prompt") -> int:
    code, env = append(monkeypatch, db, "utterance.captured", {"text": text, "origin": "person"},
                       channel=channel, provenance=provenance)
    assert code == 0, env
    return env["data"]["event"]["id"]


def internal_capture(db: str, payload: dict, *, key: str, channel: str = PICKER_CHANNEL) -> int:
    """A capture written through the capture writer's own store path, for the
    cases that need a capture `ledger capture` would never build."""
    with open_ledger(db, create=False) as lg:
        return lg._append_capture_event(
            _CAPTURE_WRITER_CAPABILITY,
            kind="utterance.captured",
            payload=payload,
            idempotency_key=key,
            channel=channel,
            provenance_class="tool-answer",
            harness="pytest",
        ).id


def grant_check(monkeypatch, db: str, grant_id: str) -> tuple[int, dict]:
    return cli(monkeypatch, db, "grants", "--check", grant_id)


GRANT_OPTIONS = (
    {"label": "granted", "description": "The grant stands"},
    {"label": "withdrawn", "description": "The grant is withdrawn"},
)


# --- M1-M14: bound_answer's reasons -------------------------------------------


def test_m1_sol_probe_on_a_pre_cutover_prompt_reads_pre_binding(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    prompt = utterance(monkeypatch, db, "hold this graduation; do not apply it")
    raw_elect(monkeypatch, db, probe_election(asked, prompt))
    assert reason(db, asked) == "pre_binding"


def test_m2_sol_probe_on_a_hook_prompt_capture_reads_typed_answer(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    code, env = capture(monkeypatch, db, prompt_hook("hold this graduation; do not apply it"),
                        kind="prompt")
    assert code == 0, env
    prompt = env["data"]["event"]
    assert prompt["channel"] == PROMPT_CHANNEL and prompt["id"] > asked
    raw_elect(monkeypatch, db, probe_election(asked, prompt["id"]))
    assert reason(db, asked) == "typed_answer"


def test_m3_a_picked_answer_verifies_and_closes_the_ask(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply")
    code, env = pick(monkeypatch, db, asked)
    assert code == 0, env
    election = env["data"]["election"]
    assert env["data"]["utterance"]["id"] == capture_id
    result = bound(db, asked)
    assert result.reason is None
    assert result.election.id == election["id"]
    assert asked not in open_asks(db)
    payload = election["payload"]
    picker = stored(db, capture_id).payload["picker"]
    assert payload["display_sha256"] == picker["display_sha256"]
    assert payload["chosen_position"] == 1
    assert payload["chosen"] == "apply"
    assert payload["chosen_description"] == "Apply the graduation now"
    assert payload["rejected"] == ["hold"]
    assert payload["question"] == QUESTION
    assert payload["scope"] == SCOPE
    assert payload["quotes"] == ["apply"]
    assert payload["quote_ref"] == capture_id
    assert payload["asked_event_id"] == asked


def test_m4_a_capture_tagged_for_another_ask_reads_answer_other_ask(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    other = ask(monkeypatch, db, question="Should we apply the LC42 graduation?")
    capture_id = answer(monkeypatch, db, other, "apply")
    raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert reason(db, asked) == "answer_other_ask"


def test_m5_a_capture_without_a_tag_reads_answer_unbound(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)

    def untag(questions):
        questions[0]["question"] = questions[0]["question"].removeprefix(f"[ask {asked}] ")

    capture_id = answer(monkeypatch, db, asked, "apply", edit=untag)
    assert "asked_event_id" not in stored(db, capture_id).payload["picker"]
    raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert reason(db, asked) == "answer_unbound"


def test_m6_an_election_with_another_position_reads_choice_mismatch(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "hold")
    assert stored(db, capture_id).payload["picker"]["chosen_position"] == 2
    raw_elect(monkeypatch, db, forged(db, asked, capture_id, position=1))
    assert reason(db, asked) == "choice_mismatch"


def test_m7_a_capture_recorded_before_the_ask_reads_answer_before_ask(db, monkeypatch) -> None:
    with open_ledger(db, create=False) as lg:
        next_ask = lg.events()[-1].id + 2  # the capture lands first, then the ask
    questions = render_ask_display(next_ask, ask_payload())
    capture_id = captured(monkeypatch, db,
                          picker_hook(questions, {questions[0]["question"]: "apply"}))
    asked = ask(monkeypatch, db)
    assert asked == next_ask and capture_id < asked
    raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert reason(db, asked) == "answer_before_ask"


def test_m8_a_display_with_an_extra_option_reads_display_differs(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(
        monkeypatch, db, asked, "apply",
        edit=lambda qs: qs[0]["options"].append({"label": "defer", "description": "Defer it"}),
    )
    raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert reason(db, asked) == "display_differs"


def test_m9_options_in_another_order_read_display_differs(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply",
                        edit=lambda qs: qs[0]["options"].reverse())
    # The owner chose apply, at position 2 of what they saw.
    assert stored(db, capture_id).payload["picker"]["chosen_position"] == 2
    raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert reason(db, asked) == "display_differs"


def test_m10_a_typed_answer_reads_picker_typed(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "Other: maybe later")
    picker = stored(db, capture_id).payload["picker"]
    assert picker["typed"] is True and "chosen_position" not in picker
    assert picker["chosen_label"] == "Other: maybe later"
    raw_elect(monkeypatch, db, forged(db, asked, capture_id, position=1))
    assert reason(db, asked) == "picker_typed"


def test_m11_a_multi_select_display_reads_display_differs(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply",
                        edit=lambda qs: qs[0].update(multiSelect=True))
    raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert reason(db, asked) == "display_differs"


def test_m12_an_election_on_another_item_reads_answer_other_item(db, monkeypatch) -> None:
    new_item(monkeypatch, db, OTHER)
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply")
    raw_elect(monkeypatch, db, forged(db, asked, capture_id), item=OTHER)
    assert reason(db, asked) == "answer_other_item"


def test_m13_the_first_election_decides_and_recovery_is_a_fresh_ask(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    prompt = utterance(monkeypatch, db, "hold this graduation; do not apply it")
    raw_elect(monkeypatch, db, probe_election(asked, prompt))
    capture_id = answer(monkeypatch, db, asked, "apply")
    code, env = pick(monkeypatch, db, asked)
    assert code == 3 and refusal(env)[0] == "unresolvable_ask"
    # A later, otherwise valid election never replaces the first one.
    raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert reason(db, asked) == "pre_binding"
    fresh = ask(monkeypatch, db)
    answer(monkeypatch, db, fresh, "apply")
    code, env = pick(monkeypatch, db, fresh)
    assert code == 0, env
    assert reason(db, fresh) is None


def test_m14_a_quote_not_in_the_capture_reads_quote_mismatch(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply")
    raw_elect(monkeypatch, db, forged(db, asked, capture_id, quotes=["apply now"]))
    assert reason(db, asked) == "quote_mismatch"


# --- M15-M24: who can write a capture, and what capture writes ----------------


def test_m15_public_append_on_hook_picker_is_refused(db, monkeypatch) -> None:
    before = event_count(db)
    code, env = append(monkeypatch, db, "utterance.captured", {"text": "x"},
                       channel=PICKER_CHANNEL, provenance="tool-answer")
    assert code == 3
    assert refusal(env) == ("validation", "reserved-capture-channel")
    assert event_count(db) == before


def test_m16_append_many_with_a_hook_channel_refuses_the_whole_batch(db, monkeypatch) -> None:
    before = event_count(db)
    batch = [
        {"kind": "item.note", "payload": {"text": "harmless"}},
        {"kind": "utterance.captured", "payload": {"text": "yes"}, "channel": PROMPT_CHANNEL},
    ]
    code, env = cli(monkeypatch, db, "append-many", ITEM, stdin=json.dumps(batch))
    assert code == 3
    assert refusal(env) == ("validation", "reserved-capture-channel")
    assert event_count(db) == before


def test_m17_the_library_append_refuses_a_hook_channel(db) -> None:
    with open_ledger(db, create=False) as lg:
        before = len(lg.events())
        with pytest.raises(ValidationError, match="^reserved-capture-channel$"):
            lg.append(kind="utterance.captured", payload={"text": "x"}, item_id=ITEM,
                      idempotency_key="lib-1", channel=PICKER_CHANNEL)
        with pytest.raises(ValidationError, match="^reserved-capture-channel$"):
            lg.append_many([{"kind": "utterance.captured", "payload": {"text": "x"},
                             "idempotency_key": "lib-2", "channel": PROMPT_CHANNEL}])
        assert len(lg.events()) == before


def test_m18_the_retired_pick_form_is_refused(db, monkeypatch) -> None:
    before = event_count(db)
    code, env = cli(monkeypatch, db, "pick", ITEM, "--question", "q", "--option", "a",
                    "--option", "b", "--chosen", "a", "--scope", "s")
    assert code == 3
    assert refusal(env)[0] == "pick_needs_ask"
    assert event_count(db) == before


def test_m19_pick_checks_the_answer_before_writing(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    answer(monkeypatch, db, asked, "apply",
           edit=lambda qs: qs[0].update(question=qs[0]["question"] + " Really?"))
    code, env = pick(monkeypatch, db, asked)
    assert code == 3
    assert refusal(env)[0] == "display_differs"
    assert event_count(db, "decision.elected") == 0
    assert asked in open_asks(db)


def test_m20_capture_refuses_a_hook_input_for_another_tool(db, monkeypatch) -> None:
    before = event_count(db)
    hook = picker_hook([{"question": "q", "options": []}], {}, tool_name="Bash")
    code, env = capture(monkeypatch, db, hook)
    assert code == 3
    assert refusal(env)[0] == "capture_not_picker"
    assert event_count(db) == before


TWO_QUESTIONS = [
    {"question": "[ask 7] First?", "header": "one", "multiSelect": False,
     "options": [{"label": "a", "description": "A"}, {"label": "b", "description": "B"}]},
    {"question": "Second?", "header": "two", "multiSelect": False,
     "options": [{"label": "c", "description": "C"}, {"label": "d", "description": "D"}]},
]


def test_m21_a_two_question_picker_is_one_event_that_singles_out_no_answer(
        db, monkeypatch) -> None:
    before = event_count(db, "utterance.captured")
    event_id = captured(monkeypatch, db, picker_hook(
        copy.deepcopy(TWO_QUESTIONS), {"[ask 7] First?": "a", "Second?": "c"}))
    assert event_count(db, "utterance.captured") == before + 1
    payload = stored(db, event_id).payload
    picker = payload["picker"]
    assert picker["question_count"] == 2
    assert picker["asked_event_id"] == 7
    assert "chosen_label" not in picker and "chosen_position" not in picker
    assert picker["typed"] is False and picker["ambiguous"] is False
    assert payload["text"] == "[ask 7] First?\nchosen: a\n\nSecond?\nchosen: c"


def one_question(text: str) -> list[dict]:
    return [{"question": text, "header": "h", "multiSelect": False,
             "options": [{"label": "a", "description": "A"}, {"label": "b", "description": "B"}]}]


def test_m22_only_a_tag_at_the_start_binds(db, monkeypatch) -> None:
    first = captured(monkeypatch, db, picker_hook(one_question("[ask 7] Deploy? [ask 9]"),
                                                  {"[ask 7] Deploy? [ask 9]": "a"}))
    assert stored(db, first).payload["picker"]["asked_event_id"] == 7
    later = captured(monkeypatch, db, picker_hook(one_question("Deploy? [ask 7]"),
                                                  {"Deploy? [ask 7]": "a"}))
    assert "asked_event_id" not in stored(db, later).payload["picker"]


@pytest.mark.parametrize("text", ["[ask 0] Go?", "[ask -3] Go?", "[ask x] Go?"])
def test_m23_a_tag_that_is_no_positive_integer_binds_nothing(db, monkeypatch, text) -> None:
    event_id = captured(monkeypatch, db, picker_hook(one_question(text), {text: "a"}))
    assert "asked_event_id" not in stored(db, event_id).payload["picker"]


def test_m24_a_prompt_over_one_mib_arrives_on_stdin_and_lands_as_a_pointer(tmp_path) -> None:
    db = str(tmp_path / "ledger.db")
    body = "owner words " * 100_000  # 1.2 MB, past any argv limit
    assert len(body.encode("utf-8")) > 1024 * 1024
    env = {key: value for key, value in os.environ.items() if not key.startswith("LEDGER_")}
    env["LEDGER_HARNESS"] = "pytest"
    done = subprocess.run(
        [sys.executable, "-m", "ledger", "--json", "--db", db, "capture", "--kind", "prompt"],
        input=json.dumps(prompt_hook(body)),
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    event = json.loads(done.stdout)["data"]["event"]
    assert event["channel"] == PROMPT_CHANNEL
    assert event["provenance_class"] == "submitted-prompt"
    payload = event["payload"]
    assert payload["omitted_bytes"] == len(body.encode("utf-8"))
    assert payload["omitted_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    assert body not in payload["text"] and payload["origin"] == "person"


# --- M25-M28: ledger grants ---------------------------------------------------


def grant_ask(monkeypatch, db: str, grant_id: str = "grant:x", **fields) -> int:
    fields.setdefault("question", f"Stand {grant_id}?")
    fields.setdefault("options", GRANT_OPTIONS)
    return ask(monkeypatch, db, question_id=grant_id, **fields)


def grant_through_picker(monkeypatch, db: str, grant_id: str = "grant:x",
                         chosen: str = "granted") -> int:
    asked = grant_ask(monkeypatch, db, grant_id)
    answer(monkeypatch, db, asked, chosen)
    code, env = pick(monkeypatch, db, asked)
    assert code == 0, env
    return env["data"]["election"]["id"]


def test_m25_a_grant_picked_on_its_ask_stands(db, monkeypatch) -> None:
    election = grant_through_picker(monkeypatch, db)
    code, env = grant_check(monkeypatch, db, "grant:x")
    assert code == 0, env
    assert env["data"]["status"] == "STANDING"
    assert env["data"]["deciding_event_id"] == election


def test_m26_a_grant_quoting_a_typed_prompt_with_no_ask_is_unverified(db, monkeypatch) -> None:
    prompt = captured(monkeypatch, db, prompt_hook("Yes, I grant x for fixture rounds."),
                      kind="prompt")
    raw_elect(monkeypatch, db, {
        "question": "Stand grant:x?", "chosen": "granted", "chosen_description": "granted",
        "rejected": ["withdrawn"], "scope": "fixture rounds", "quote_ref": prompt,
        "quotes": ["I grant x"], "question_id": "grant:x",
    })
    code, env = grant_check(monkeypatch, db, "grant:x")
    assert code == 3
    code_name, message = refusal(env)
    assert code_name == "grant_unverified"
    assert "(grant_unasked)" in message


def test_m27_an_ask_for_another_grant_id_leaves_the_grant_unasked(db, monkeypatch) -> None:
    asked = grant_ask(monkeypatch, db, "grant:y")
    capture_id = answer(monkeypatch, db, asked, "granted")
    raw_elect(monkeypatch, db, forged(db, asked, capture_id, question_id="grant:x"))
    code, env = grant_check(monkeypatch, db, "grant:x")
    assert code == 3
    code_name, message = refusal(env)
    assert code_name == "grant_unverified"
    assert "(grant_unasked)" in message


def test_m28_an_unverifiable_withdrawal_still_withdraws(db, monkeypatch) -> None:
    grant_through_picker(monkeypatch, db)
    raw_elect(monkeypatch, db, {
        "question": "Stand grant:x?", "chosen": "withdrawn", "chosen_description": "withdrawn",
        "rejected": ["granted"], "scope": "fixture rounds", "quote_ref": 1,
        "quotes": ["words nobody said"], "question_id": "grant:x",
    })
    code, env = grant_check(monkeypatch, db, "grant:x")
    assert code == 3
    assert refusal(env)[0] == "grant_withdrawn"


# --- M33-M36: the whole display is compared ----------------------------------


def test_m33_a_capture_of_another_question_reads_display_differs(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db, question="Deploy the release to production?")
    capture_id = answer(
        monkeypatch, db, asked, "apply",
        edit=lambda qs: qs[0].update(question=f"[ask {asked}] Save this change as a local draft?"),
    )
    raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert reason(db, asked) == "display_differs"


def test_m34_another_option_description_reads_display_differs(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(
        monkeypatch, db, asked, "apply",
        edit=lambda qs: qs[0]["options"][0].update(description="Apply it to a scratch copy"),
    )
    raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert reason(db, asked) == "display_differs"


def test_m35_another_header_reads_display_differs(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply",
                        edit=lambda qs: qs[0].update(header="Draft"))
    raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert reason(db, asked) == "display_differs"


def test_m36_an_option_preview_is_kept_and_reads_display_differs(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(
        monkeypatch, db, asked, "apply",
        edit=lambda qs: qs[0]["options"][0].update(preview="apply means: delete production"),
    )
    display = stored(db, capture_id).payload["picker"]["display"]
    assert display[0]["options"][0]["preview"] == "apply means: delete production"
    raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert reason(db, asked) == "display_differs"


# --- M37-M41: schema, ask-display and pick refusals ---------------------------


def test_m37_a_picker_object_off_the_picker_channel_is_refused(db, monkeypatch) -> None:
    payload = picker_capture(picker_hook(one_question("[ask 2] Go?"), {"[ask 2] Go?": "a"}))
    before = event_count(db)
    code, env = append(monkeypatch, db, "utterance.captured", payload, channel="terminal",
                       provenance="tool-answer")
    assert code == 3
    assert refusal(env) == ("validation", "picker-needs-picker-channel")
    assert event_count(db) == before


@pytest.mark.parametrize("labels", [("a", "b", "c", "d", "e"), ("a",)])
def test_m38_an_ask_outside_the_picker_bounds_is_not_pickable(db, monkeypatch, labels) -> None:
    asked = ask(monkeypatch, db, options=[{"label": label} for label in labels])
    code, env = cli(monkeypatch, db, "ask-display", str(asked))
    assert code == 3
    assert refusal(env)[0] == "ask_not_pickable"


def test_m39_a_missing_description_renders_as_the_label_and_verifies(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db, options=({"label": "apply"}, {"label": "hold", "description": "Hold it"}))
    questions = ask_display(monkeypatch, db, asked)
    assert questions[0]["options"][0] == {"label": "apply", "description": "apply"}
    answer(monkeypatch, db, asked, "apply")
    code, env = pick(monkeypatch, db, asked)
    assert code == 0, env
    assert env["data"]["election"]["payload"]["chosen_description"] == "apply"
    assert reason(db, asked) is None


def test_m40_pick_refuses_a_missing_capture_and_conflicting_captures(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    code, env = pick(monkeypatch, db, asked)
    assert code == 3 and refusal(env)[0] == "capture_missing"
    answer(monkeypatch, db, asked, "apply")
    answer(monkeypatch, db, asked, "hold")
    code, env = pick(monkeypatch, db, asked)
    assert code == 3 and refusal(env)[0] == "capture_conflict"
    assert event_count(db, "decision.elected") == 0


def test_m41_a_picker_capture_text_quotes_the_chosen_label(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply")
    event = stored(db, capture_id)
    assert event.channel == PICKER_CHANNEL and event.provenance_class == "tool-answer"
    assert event.payload["text"] == f"[ask {asked}] {QUESTION} (scope: {SCOPE})\nchosen: apply"
    with open_ledger(db, create=False) as lg:
        validate_quote_provenance(lg, capture_id, ["apply"])
    code, env = pick(monkeypatch, db, asked)
    assert code == 0, env


# --- M42-M47: owner notes, scope and the derived fields ----------------------


def test_m42_an_owner_note_reads_picker_annotated(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply",
                        annotations={"notes": "Ratify as written, but first ..."})
    code, env = pick(monkeypatch, db, asked)
    assert code == 3 and refusal(env)[0] == "picker_annotated"
    raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert reason(db, asked) == "picker_annotated"


def test_m43_an_empty_owner_note_verifies(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    answer(monkeypatch, db, asked, "apply", annotations={"notes": ""})
    code, env = pick(monkeypatch, db, asked)
    assert code == 0, env
    assert reason(db, asked) is None


def test_m44_a_seat_chosen_scope_reads_election_differs_and_the_grant_is_unverified(
        db, monkeypatch) -> None:
    asked = grant_ask(monkeypatch, db, scope="staging only")
    capture_id = answer(monkeypatch, db, asked, "granted")
    raw_elect(monkeypatch, db, forged(db, asked, capture_id, scope="all production deploys"))
    assert reason(db, asked) == "election_differs"
    code, env = grant_check(monkeypatch, db, "grant:x")
    assert code == 3
    code_name, message = refusal(env)
    assert code_name == "grant_unverified" and "(election_differs)" in message


THREE_OPTIONS = OPTIONS + ({"label": "defer", "description": "Defer it a week"},)


def test_m45_an_election_omitting_a_rejected_label_reads_election_differs(
        db, monkeypatch) -> None:
    asked = ask(monkeypatch, db, options=THREE_OPTIONS)
    capture_id = answer(monkeypatch, db, asked, "apply")
    raw_elect(monkeypatch, db, forged(db, asked, capture_id, rejected=["hold"]))
    assert reason(db, asked) == "election_differs"


def test_m46_another_chosen_description_reads_election_differs(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply")
    raw_elect(monkeypatch, db, forged(db, asked, capture_id,
                                      chosen_description="Apply it everywhere"))
    assert reason(db, asked) == "election_differs"


def test_m47_an_ask_without_a_scope_is_not_pickable(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db, scope=None)
    code, env = cli(monkeypatch, db, "ask-display", str(asked))
    assert code == 3
    assert refusal(env)[0] == "ask_not_pickable"


def test_m48_pick_takes_no_scope(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    answer(monkeypatch, db, asked, "apply")
    before = event_count(db)
    code, env = pick(monkeypatch, db, asked, "--scope", "all production deploys")
    assert code == 2
    assert refusal(env)[0] == "usage"
    assert event_count(db) == before


def test_m49_an_ask_with_a_repeated_label_is_not_pickable(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db, options=(
        {"label": "approve", "description": "ship to staging"},
        {"label": "approve", "description": "ship to production"},
    ))
    code, env = cli(monkeypatch, db, "ask-display", str(asked))
    assert code == 3 and refusal(env)[0] == "ask_not_pickable"
    # A seat composes the picker itself; the owner picks "approve".
    question = f"[ask {asked}] {QUESTION} (scope: {SCOPE})"
    questions = [{"question": question, "header": f"ask {asked}", "multiSelect": False,
                  "options": [{"label": "approve", "description": "ship to staging"},
                              {"label": "approve", "description": "ship to production"}]}]
    capture_id = captured(monkeypatch, db, picker_hook(questions, {question: "approve"}))
    raw_elect(monkeypatch, db, forged(db, asked, capture_id, position=1))
    assert reason(db, asked) == "ask_not_pickable"


def test_m50_a_nested_nonempty_note_reads_picker_annotated(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply",
                        annotations={"notes": {"extra": ["", "but first ..."]}})
    raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert reason(db, asked) == "picker_annotated"


def test_m51_a_nested_all_empty_note_verifies(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    answer(monkeypatch, db, asked, "apply", annotations={"notes": {"extra": ["", {}]}})
    code, env = pick(monkeypatch, db, asked)
    assert code == 0, env
    assert reason(db, asked) is None


def test_m52_another_question_on_the_election_reads_election_differs(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply")
    raw_elect(monkeypatch, db, forged(db, asked, capture_id, question="Apply everything?"))
    assert reason(db, asked) == "election_differs"


def test_m53_an_extra_verbatim_quote_reads_election_differs(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply")
    raw_elect(monkeypatch, db, forged(db, asked, capture_id, quotes=["apply", f"[ask {asked}]"]))
    assert reason(db, asked) == "election_differs"


def test_m53_another_question_id_reads_election_differs(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply")
    raw_elect(monkeypatch, db, forged(db, asked, capture_id, question_id="grant:other"))
    assert reason(db, asked) == "election_differs"


# --- M54-M66 --------------------------------------------------------------------


def test_m54_a_display_changed_after_its_fingerprint_reads_display_fingerprint_bad(
        db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    questions = ask_display(monkeypatch, db, asked)
    payload = picker_capture(picker_hook(questions, {questions[0]["question"]: "apply"}))
    payload["picker"]["display"][0]["header"] = "changed"
    capture_id = internal_capture(db, payload, key="m54")
    raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert reason(db, asked) == "display_fingerprint_bad"


def test_m55_keys_in_another_order_verify(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)

    def reorder(questions):
        question = questions[0]
        question["options"] = [dict(reversed(list(option.items())))
                               for option in question["options"]]
        questions[0] = dict(reversed(list(question.items())))

    shown = ask_display(monkeypatch, db, asked)
    sent = copy.deepcopy(shown)
    reorder(sent)
    assert list(sent[0]) != list(shown[0]) and sent == shown  # same content, other order
    answer(monkeypatch, db, asked, "apply", edit=reorder)
    code, env = pick(monkeypatch, db, asked)
    assert code == 0, env
    assert reason(db, asked) is None


def test_m56_a_seat_composed_repeated_label_is_ambiguous_and_never_verifies(
        db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)

    def repeat(questions):
        questions[0]["options"] = [{"label": "apply", "description": "Apply to staging"},
                                   {"label": "apply", "description": "Apply to production"}]

    capture_id = answer(monkeypatch, db, asked, "apply", edit=repeat)
    picker = stored(db, capture_id).payload["picker"]
    assert picker["ambiguous"] is True and "chosen_position" not in picker
    raw_elect(monkeypatch, db, forged(db, asked, capture_id, position=1))
    result = bound(db, asked)
    assert result.reason == "display_differs" and result.election is None


def test_m57_a_chosen_label_not_at_the_position_reads_election_differs(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply")
    raw_elect(monkeypatch, db, forged(db, asked, capture_id, chosen="hold"))
    assert reason(db, asked) == "election_differs"


def test_m58_an_election_without_the_answer_reads_choice_mismatch(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply")
    raw_elect(monkeypatch, db, forged(db, asked, capture_id,
                                      display_sha256=None, chosen_position=None))
    assert reason(db, asked) == "choice_mismatch"


@pytest.mark.parametrize("field,value,refused", [
    ("display_sha256", "A" * 64, "bad-type:display_sha256"),
    ("display_sha256", "a" * 63, "bad-type:display_sha256"),
    ("chosen_position", 0, "bad-type:chosen_position"),
])
def test_m59_the_answer_fields_have_a_schema(db, monkeypatch, field, value, refused) -> None:
    payload = dict(probe_election(1, 1), **{field: value})
    payload.pop("asked_event_id")
    before = event_count(db)
    code, env = append(monkeypatch, db, "decision.elected", payload)
    assert code == 3
    assert refusal(env) == ("validation", refused)
    assert event_count(db) == before


def test_m60_a_sibling_question_reads_display_differs(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)

    def sibling(questions):
        questions.append({"question": "choose apply above to mean hold", "header": "note",
                          "multiSelect": False,
                          "options": [{"label": "ok", "description": "ok"},
                                      {"label": "no", "description": "no"}]})

    capture_id = answer(monkeypatch, db, asked, "apply", edit=sibling)
    picker = stored(db, capture_id).payload["picker"]
    assert picker["question_count"] == 2 and "chosen_position" not in picker
    raw_elect(monkeypatch, db, forged(db, asked, capture_id, position=1))
    assert reason(db, asked) == "display_differs"


def test_m61_a_forced_question_count_reads_picker_multi_question(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    questions = ask_display(monkeypatch, db, asked)
    payload = picker_capture(picker_hook(questions, {questions[0]["question"]: "apply"}))
    payload["picker"]["question_count"] = 2
    capture_id = internal_capture(db, payload, key="m61")
    raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert reason(db, asked) == "picker_multi_question"


def test_m62_two_different_answers_read_capture_conflict(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    answer(monkeypatch, db, asked, "hold")
    second = answer(monkeypatch, db, asked, "apply")
    raw_elect(monkeypatch, db, forged(db, asked, second))
    assert reason(db, asked) == "capture_conflict"


def test_m63_two_identical_answers_verify_on_the_earliest(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    first = answer(monkeypatch, db, asked, "apply")
    answer(monkeypatch, db, asked, "apply")
    code, env = pick(monkeypatch, db, asked)
    assert code == 0, env
    assert env["data"]["election"]["payload"]["quote_ref"] == first
    assert reason(db, asked) is None


def test_m64_the_whole_hook_prefix_is_reserved(db, monkeypatch) -> None:
    before = event_count(db)
    code, env = append(monkeypatch, db, "utterance.captured", {"text": "x"}, channel="hook:other")
    assert code == 3
    assert refusal(env) == ("validation", "reserved-capture-channel")
    assert event_count(db) == before


def test_m65_a_hook_picker_event_without_a_picker_object_reads_pre_binding(
        db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    old = internal_capture(db, {"text": f"[ask {asked}] {QUESTION}\nchosen: apply"}, key="m65")
    raw_elect(monkeypatch, db, probe_election(asked, old))
    assert reason(db, asked) == "pre_binding"


def test_m66_an_omitted_answer_is_never_read_as_a_typed_one(db, monkeypatch) -> None:
    answers = {"[ask 7] First?": "(no answer)"}
    event_id = captured(monkeypatch, db, picker_hook(copy.deepcopy(TWO_QUESTIONS), answers))
    payload = stored(db, event_id).payload
    assert payload["picker"]["answers"] == answers
    assert payload["text"] == "[ask 7] First?\nchosen: (no answer)\n\nSecond?\nunanswered"


# --- further reasons ------------------------------------------------------------


def test_an_unanswered_one_question_picker_reads_picker_unanswered(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    questions = ask_display(monkeypatch, db, asked)
    capture_id = captured(monkeypatch, db, picker_hook(questions, {}))
    payload = stored(db, capture_id).payload
    assert "chosen_label" not in payload["picker"]
    assert payload["text"] == f"{questions[0]['question']}\nunanswered"
    raw_elect(monkeypatch, db, forged(db, asked, capture_id, position=1))
    assert reason(db, asked) == "picker_unanswered"


def test_an_ambiguous_capture_reads_picker_ambiguous(db, monkeypatch) -> None:
    # ledger capture never writes an ambiguous capture of a display ask-display
    # renders (its labels are unique), so this one goes through the store path.
    asked = ask(monkeypatch, db)
    questions = ask_display(monkeypatch, db, asked)
    payload = picker_capture(picker_hook(questions, {questions[0]["question"]: "apply"}))
    del payload["picker"]["chosen_position"]
    payload["picker"]["ambiguous"] = True
    capture_id = internal_capture(db, payload, key="ambiguous")
    raw_elect(monkeypatch, db, forged(db, asked, capture_id, position=1))
    assert reason(db, asked) == "picker_ambiguous"


def test_an_ask_whose_header_would_pass_twelve_characters_is_not_pickable() -> None:
    assert render_ask_display(99_999_999, ask_payload())[0]["header"] == "ask 99999999"
    with pytest.raises(AskNotPickable, match="longer than 12 characters"):
        render_ask_display(100_000_000, ask_payload())


def test_bound_answer_names_a_missing_ask_and_a_missing_election(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    assert bound(db, asked) == BoundAnswer(None, "no_election")
    assert reason(db, 1) == "ask_not_found"  # item.created, not an ask
    assert reason(db, 999) == "ask_not_found"
    code, env = cli(monkeypatch, db, "ask-display", "1")
    assert code == 1 and refusal(env)[0] == "ask_not_found"
    # An id past SQLite's integer range is no ask either.
    code, env = cli(monkeypatch, db, "ask-display", str(2**64))
    assert code == 1 and refusal(env)[0] == "ask_not_found"


def test_an_ask_id_past_the_integer_range_is_refused_by_name(db, monkeypatch) -> None:
    code, env = pick(monkeypatch, db, 2**64)
    assert code == 3 and refusal(env)[0] == "unresolvable_ask"
    prompt = captured(monkeypatch, db, prompt_hook("I grant x"), kind="prompt")
    raw_elect(monkeypatch, db, {
        "question": "Stand grant:x?", "chosen": "granted", "chosen_description": "granted",
        "rejected": ["withdrawn"], "scope": "fixture rounds", "quote_ref": prompt,
        "quotes": ["I grant x"], "question_id": "grant:x", "asked_event_id": 2**64,
    })
    code, env = grant_check(monkeypatch, db, "grant:x")
    assert code == 3
    code_name, message = refusal(env)
    assert code_name == "grant_unverified" and "(grant_unasked)" in message


def test_a_grant_whose_ask_was_answered_first_by_another_election_is_unverified(
        db, monkeypatch) -> None:
    asked = grant_ask(monkeypatch, db)
    answer(monkeypatch, db, asked, "granted")
    code, env = pick(monkeypatch, db, asked)
    assert code == 0, env
    first = env["data"]["election"]["payload"]
    raw_elect(monkeypatch, db, first)  # the same election again, newer
    code, env = grant_check(monkeypatch, db, "grant:x")
    assert code == 3
    code_name, message = refusal(env)
    assert code_name == "grant_unverified" and "(not_first_answer)" in message


# --- the grant id on the display; R4a and R9 one guard at a time ----------------


def test_a_grant_id_the_display_never_named_does_not_stand(db, monkeypatch) -> None:
    # The seat asks under a grant id with a question that never names it, and
    # raises the picker the same ask would render without the id; the owner
    # clicks granted.
    grant_id = "grant:review-round-past-cap"
    fields = {"question": "Fix the README typo?", "scope": "the README only",
              "options": GRANT_OPTIONS}
    asked = grant_ask(monkeypatch, db, grant_id, **fields)
    questions = render_ask_display(asked, ask_payload(**fields))
    assert grant_id not in json.dumps(questions)
    question = questions[0]["question"]
    capture_id = captured(monkeypatch, db, picker_hook(questions, {question: "granted"}))
    assert stored(db, capture_id).payload["picker"]["chosen_position"] == 1
    code, env = pick(monkeypatch, db, asked)
    assert code == 3 and refusal(env)[0] == "display_differs"
    assert event_count(db, "decision.elected") == 0
    raw_elect(monkeypatch, db, forged(db, asked, capture_id))
    assert reason(db, asked) == "display_differs"
    code, env = grant_check(monkeypatch, db, grant_id)
    assert code == 3
    code_name, message = refusal(env)
    assert code_name == "grant_unverified" and "(display_differs)" in message
    # What ask-display prints names the grant id.
    assert ask_display(monkeypatch, db, asked)[0]["question"] == (
        f"[ask {asked}] Fix the README typo? (scope: the README only; question id: {grant_id})"
    )


def test_a_later_capture_of_another_display_at_the_same_position_reads_capture_conflict(
        db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    first = answer(monkeypatch, db, asked, "apply")
    later = answer(
        monkeypatch, db, asked, "apply",
        edit=lambda qs: qs[0]["options"][0].update(description="Apply it to a scratch copy"),
    )
    first_picker = stored(db, first).payload["picker"]
    later_picker = stored(db, later).payload["picker"]
    assert first_picker["chosen_position"] == later_picker["chosen_position"] == 1
    assert first_picker["display_sha256"] != later_picker["display_sha256"]
    code, env = pick(monkeypatch, db, asked)
    assert code == 3 and refusal(env)[0] == "capture_conflict"
    raw_elect(monkeypatch, db, forged(db, asked, first))
    assert reason(db, asked) == "capture_conflict"


def test_a_later_capture_carrying_an_owner_note_reads_capture_conflict(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    first = answer(monkeypatch, db, asked, "apply")
    later = answer(monkeypatch, db, asked, "apply", annotations={"notes": "only if tests pass"})
    first_picker = stored(db, first).payload["picker"]
    later_picker = stored(db, later).payload["picker"]
    assert "annotation" not in first_picker
    assert first_picker["chosen_position"] == later_picker["chosen_position"] == 1
    assert first_picker["display_sha256"] == later_picker["display_sha256"]
    code, env = pick(monkeypatch, db, asked)
    assert code == 3 and refusal(env)[0] == "capture_conflict"
    raw_elect(monkeypatch, db, forged(db, asked, first))
    assert reason(db, asked) == "capture_conflict"


def test_an_election_recording_another_fingerprint_reads_choice_mismatch(
        db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    capture_id = answer(monkeypatch, db, asked, "apply")
    wrong = fingerprint([{"question": "another display"}])
    assert wrong != stored(db, capture_id).payload["picker"]["display_sha256"]
    election = forged(db, asked, capture_id, display_sha256=wrong)
    assert election["chosen_position"] == 1  # only the fingerprint is forged
    raw_elect(monkeypatch, db, election)
    assert reason(db, asked) == "choice_mismatch"


# --- I2: the grammar's near misses refuse by name --------------------------------


@pytest.mark.parametrize("text", [
    "[Ask 7] Go?", "[ask  7] Go?", " [ask 7] Go?", "[ask 07] Go?", "[ask 7 Go?",
    "- [ask 7] Go?", "```[ask 7] Go?", "[ask 7.0] Go?", "[ask +7] Go?", "[ask\t7] Go?",
    "[ask 99999999999999999999] Go?",
])
def test_i2_a_near_miss_tag_binds_nothing(text) -> None:
    assert ask_tag(text) is None
    payload = picker_capture(picker_hook(one_question(text), {text: "a"}))
    assert "asked_event_id" not in payload["picker"]


def test_i2_the_exact_tag_binds() -> None:
    assert ask_tag("[ask 7] Go?") == 7
    assert ask_tag("[ask 7]") == 7


@pytest.mark.parametrize("override", [
    {"hook_event_name": "posttooluse"},
    {"hook_event_name": "PostToolUse "},
    {"hook_event_name": "PreToolUse"},
    {"tool_name": "askuserquestion"},
    {"tool_name": "AskUserQuestion "},
])
def test_i2_a_near_miss_hook_input_is_not_a_picker(db, monkeypatch, override) -> None:
    before = event_count(db)
    code, env = capture(monkeypatch, db,
                        picker_hook(one_question("[ask 2] Go?"), {"[ask 2] Go?": "a"}, **override))
    assert code == 3 and refusal(env)[0] == "capture_not_picker"
    assert event_count(db) == before


@pytest.mark.parametrize("raw,reason_code", [
    ("not json", "capture_bad_input"),
    ("[]", "capture_bad_input"),
    ('{"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion"}', "capture_bad_input"),
])
def test_i2_unparseable_hook_input_is_refused(db, monkeypatch, raw, reason_code) -> None:
    before = event_count(db)
    code, env = cli(monkeypatch, db, "capture", "--kind", "picker", stdin=raw)
    assert code == 3 and refusal(env)[0] == reason_code
    assert event_count(db) == before


def _malformed(change):
    hook = picker_hook(one_question("[ask 2] Go?"), {"[ask 2] Go?": "a"})
    change(hook)
    return hook


MALFORMED_PICKER_INPUTS = [
    (lambda h: h.pop("tool_use_id"), "tool_use_id must be a non-empty string"),
    (lambda h: h.update(tool_input=[]), "tool_input must be an object"),
    (lambda h: h.update(tool_input={"questions": []}),
     "tool_input.questions must be a non-empty array"),
    (lambda h: h.update(tool_input={"questions": ["q"]}),
     "tool_input.questions[0] must be an object"),
    (lambda h: h["tool_input"]["questions"][0].pop("question"),
     "tool_input.questions[0].question must be a non-empty string"),
    (lambda h: h["tool_input"]["questions"][0].update(options={}),
     "tool_input.questions[0].options must be an array"),
    (lambda h: h["tool_input"]["questions"][0]["options"].append({"description": "x"}),
     "tool_input.questions[0].options[2] must be an object with a label"),
    (lambda h: h.update(tool_response={"answers": []}),
     "tool_response.answers must be an object"),
    (lambda h: h.update(tool_response={"answers": {"[ask 2] Go?": 1}}),
     "tool_response.answers['[ask 2] Go?'] must be a string"),
    (lambda h: h.update(tool_response={"answers": {}, "annotations": []}),
     "tool_response.annotations must be an object"),
    (lambda h: h.update(session_id=""), "session_id must be a non-empty string"),
]


@pytest.mark.parametrize("change,message", MALFORMED_PICKER_INPUTS)
def test_i2_each_malformed_picker_input_is_refused_by_name(
        db, monkeypatch, change, message) -> None:
    before = event_count(db)
    code, env = capture(monkeypatch, db, _malformed(change))
    assert code == 3
    assert refusal(env) == ("capture_bad_input", message)
    assert event_count(db) == before


@pytest.mark.parametrize("raw,message", [
    ("not json", "stdin is not JSON: Expecting value: line 1 column 1 (char 0)"),
    ("[]", "stdin must be one JSON object (the hook input)"),
    ('{"hook_event_name": "UserPromptSubmit", "prompt": "\\ud800"}',
     "prompt is not UTF-8 text: surrogates not allowed"),
    ('{"hook_event_name": "UserPromptSubmit", "prompt": "yes", "prompt_id": 7}',
     "prompt_id must be a non-empty string"),
])
def test_i2_each_malformed_prompt_input_is_refused_by_name(db, monkeypatch, raw, message) -> None:
    before = event_count(db)
    code, env = cli(monkeypatch, db, "capture", "--kind", "prompt", stdin=raw)
    assert code == 3
    assert refusal(env) == ("capture_bad_input", message)
    assert event_count(db) == before


class _UndecodableStdin:
    def read(self):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


def test_i2_undecodable_stdin_is_refused(db, monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _UndecodableStdin())
    code, env = cli(monkeypatch, db, "capture", "--kind", "picker")
    assert code == 3
    code_name, message = refusal(env)
    assert code_name == "capture_bad_input" and message.startswith("stdin is not UTF-8 text: ")


def test_i2_the_library_writer_refuses_an_unknown_capture_kind(db) -> None:
    with open_ledger(db, create=False) as lg:
        with pytest.raises(CliError) as refused:
            write_capture(lg, "other", prompt_hook("yes"), harness="pytest")
    assert refused.value.code == "capture_bad_input"
    assert str(refused.value) == "unknown capture kind: 'other'"


def test_i2_a_prompt_capture_needs_a_prompt_hook_input(db, monkeypatch) -> None:
    hook = prompt_hook("yes")
    hook["hook_event_name"] = "PostToolUse"
    code, env = capture(monkeypatch, db, hook, kind="prompt")
    assert code == 3 and refusal(env)[0] == "capture_not_prompt"
    code, env = capture(monkeypatch, db, dict(prompt_hook("yes"), prompt=""), kind="prompt")
    assert code == 3 and refusal(env)[0] == "capture_bad_input"


@pytest.mark.parametrize("channel", ["Hook:picker", " hook:picker", "hookpicker"])
def test_i2_a_channel_that_only_looks_reserved_is_no_capture(db, monkeypatch, channel) -> None:
    asked = ask(monkeypatch, db)
    text = f"[ask {asked}] {QUESTION} (scope: {SCOPE})\nchosen: apply"
    lookalike = utterance(monkeypatch, db, text, channel=channel, provenance="tool-answer")
    raw_elect(monkeypatch, db, probe_election(asked, lookalike))
    assert reason(db, asked) == "pre_binding"


# --- I3: reruns and a second writer -----------------------------------------------


def test_i3_a_pick_rerun_with_its_key_returns_the_stored_election(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    answer(monkeypatch, db, asked, "apply")
    code, first = pick(monkeypatch, db, asked, "--idempotency-key", "pick-1")
    assert code == 0, first
    before = event_count(db)
    code, again = pick(monkeypatch, db, asked, "--idempotency-key", "pick-1")
    assert code == 0, again
    assert again["data"]["election"]["id"] == first["data"]["election"]["id"]
    assert event_count(db) == before
    code, env = pick(monkeypatch, db, asked)  # a rerun without the key
    assert code == 3 and refusal(env)[0] == "unresolvable_ask"
    assert event_count(db) == before


def test_i3_a_capture_retry_returns_the_stored_event(db, monkeypatch) -> None:
    hook = picker_hook(one_question("[ask 2] Go?"), {"[ask 2] Go?": "a"})
    first = captured(monkeypatch, db, hook)
    before = event_count(db)
    assert captured(monkeypatch, db, hook) == first
    assert event_count(db) == before
    changed = dict(hook, tool_response={"answers": {"[ask 2] Go?": "b"}})
    code, env = capture(monkeypatch, db, changed)
    assert code == 3 and refusal(env)[0] == "conflict"
    assert event_count(db) == before


def test_i3_two_concurrent_picks_write_one_election(db, monkeypatch) -> None:
    asked = ask(monkeypatch, db)
    answer(monkeypatch, db, asked, "apply")
    env = {key: value for key, value in os.environ.items() if not key.startswith("LEDGER_")}
    env["LEDGER_HARNESS"] = "pytest"
    argv = [sys.executable, "-m", "ledger", "--json", "--db", db, "pick", ITEM, "--ask", str(asked)]
    runs = [subprocess.Popen(argv, cwd=REPO_ROOT, env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True) for _ in range(2)]
    results = [(run.wait(timeout=120), run.stdout.read(), run.stderr.read()) for run in runs]
    for run in runs:
        run.stdout.close()
        run.stderr.close()
    codes = sorted(code for code, _out, _err in results)
    assert codes == [0, 3], results
    refused = next(json.loads(out) for code, out, _err in results if code == 3)
    assert refused["error"]["code"] == "unresolvable_ask"
    assert event_count(db, "decision.elected") == 1
    assert reason(db, asked) is None


# --- I4: every writer path, and a restored copy -----------------------------------


def test_i4_every_internal_writer_refuses_a_hook_channel(db) -> None:
    from ledger.loop import _LOOP_WRITER_CAPABILITY

    with open_ledger(db, create=False) as lg:
        with pytest.raises(ValidationError, match="^reserved-capture-channel$"):
            lg._append_loop_event(_LOOP_WRITER_CAPABILITY, kind="loop.managed", payload={},
                                  item_id=ITEM, idempotency_key="loop-hook",
                                  channel=PICKER_CHANNEL)
        with pytest.raises(ValidationError, match="^capture-writer-capability-required$"):
            lg._append_capture_event(object(), kind="utterance.captured",
                                     payload={"text": "x"}, idempotency_key="cap-1",
                                     channel=PICKER_CHANNEL)
        with pytest.raises(ValidationError, match="^capture-channel-required:hook:other$"):
            lg._append_capture_event(_CAPTURE_WRITER_CAPABILITY, kind="utterance.captured",
                                     payload={"text": "x"}, idempotency_key="cap-2",
                                     channel="hook:other")
        with pytest.raises(ValidationError, match="^capture-channel-required:hook:picker$"):
            lg._append_capture_event(_CAPTURE_WRITER_CAPABILITY, kind="item.note",
                                     payload={"text": "x"}, item_id=ITEM,
                                     idempotency_key="cap-3", channel=PICKER_CHANNEL)
        payload = picker_capture(picker_hook(one_question("[ask 2] Go?"), {"[ask 2] Go?": "a"}))
        with pytest.raises(ValidationError, match="^picker-needs-picker-channel$"):
            lg._append_capture_event(_CAPTURE_WRITER_CAPABILITY, kind="utterance.captured",
                                     payload=payload, idempotency_key="cap-4",
                                     channel=PROMPT_CHANNEL)


def test_i4_a_restored_copy_reads_the_same_bound_answer(db, monkeypatch, tmp_path) -> None:
    asked = ask(monkeypatch, db)
    answer(monkeypatch, db, asked, "apply")
    code, env = pick(monkeypatch, db, asked)
    assert code == 0, env
    export_dir = tmp_path / "export"
    with open_ledger(db, create=False) as lg:
        export_events(lg, export_dir)
    restored = tmp_path / "restored.db"
    restore_export(export_dir, restored, system="home")
    result = bound(str(restored), asked)
    assert result.reason is None
    assert result.election.id == env["data"]["election"]["id"]
