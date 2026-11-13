"""``ledger capture --kind prompt`` skips harness frames and honours ``capture_prompts``.

A prompt that starts, after ASCII whitespace, with one of the five harness
frames writes nothing and prints ``skipped<TAB>frame``. ``capture_prompts:
false`` in the ``config.json`` beside the database skips every prompt
(``skipped<TAB>prompts_off``); an unreadable or invalid config refuses
``capture_config``. Neither skip opens the database, and picker capture never
reads the config. Each case runs the CLI as a hook does: a subprocess with the
hook input on stdin.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ledger.store import open_ledger
from ledger.verbs.capture import HARNESS_FRAMES, PROMPT_CAP_BYTES

PKG_PARENT = Path(__file__).resolve().parents[2]  # makes ``python -m ledger`` importable
FRAMES = (
    "<task-notification>",
    "<cross-session-message",
    "<agent-message",
    "<system-reminder>",
    "[Subagent hand-back]",
)
ITEM = "it-filter"
QUESTION = "Ship the release today?"
OPTIONS = [
    {"label": "ship", "description": "Ship it today"},
    {"label": "hold", "description": "Hold it until Friday"},
]


def run(db: Path, *argv: str, stdin: str | bytes = "") -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env.update(LEDGER_HARNESS="pytest", LEDGER_DB=str(db), PYTHONDONTWRITEBYTECODE="1")
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    done = subprocess.run([sys.executable, "-m", "ledger", *argv], input=data, env=env,
                          capture_output=True, timeout=60, cwd=PKG_PARENT)
    return subprocess.CompletedProcess(done.args, done.returncode,
                                       done.stdout.decode("utf-8"), done.stderr.decode("utf-8"))


def run_json(db: Path, *argv: str, stdin: str = "") -> tuple[int, dict]:
    done = run(db, "--json", *argv, stdin=stdin)
    return done.returncode, json.loads(done.stdout)


def prompt_hook(prompt: str, **extra) -> str:
    hook = {
        "session_id": "sess-filter",
        "transcript_path": "/tmp/fixture/transcript.jsonl",
        "cwd": "/tmp/fixture",
        "hook_event_name": "UserPromptSubmit",
        "prompt_id": "prompt-filter-1",
        "prompt": prompt,
    }
    hook.update(extra)
    return json.dumps(hook)


def capture_prompt(db: Path, prompt: str, **extra) -> subprocess.CompletedProcess[str]:
    return run(db, "capture", "--kind", "prompt", stdin=prompt_hook(prompt, **extra))


def prompt_events(db: Path) -> list:
    if not db.exists():
        return []
    with open_ledger(db, create=False) as lg:
        return [e for e in lg.events(kind="utterance.captured") if e.channel == "hook:prompt"]


def write_config(db: Path, content: str | bytes) -> Path:
    db.parent.mkdir(parents=True, exist_ok=True)
    path = db.parent / "config.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "data" / "ledger.db"


def ask_and_answer(db: Path, label: str = "ship") -> int:
    """An ask on ITEM, then a picker capture of its ask-display; the ask's id."""
    assert run(db, "new", "--id", ITEM, "--type", "gate", "--title", "filter").returncode == 0
    payload = {"question": QUESTION, "options": OPTIONS, "scope": "this release"}
    code, env = run_json(db, "append", "--item", ITEM, "--kind", "decision.asked",
                         "--payload", json.dumps(payload))
    assert code == 0, env
    asked = env["data"]["event"]["id"]
    code, env = run_json(db, "ask-display", str(asked))
    assert code == 0, env
    questions = env["data"]["input"]["questions"]
    hook = {
        "session_id": "sess-filter",
        "hook_event_name": "PostToolUse",
        "tool_name": "AskUserQuestion",
        "tool_use_id": "toolu_filter",
        "tool_input": {"questions": questions},
        "tool_response": {"questions": questions,
                          "answers": {questions[0]["question"]: label}},
    }
    code, env = run_json(db, "capture", "--kind", "picker", stdin=json.dumps(hook))
    assert code == 0, env
    assert env["data"]["event"]["channel"] == "hook:picker"
    return asked


# --- frames -------------------------------------------------------------------


def test_the_frames_are_the_five_literals() -> None:
    assert HARNESS_FRAMES == FRAMES


@pytest.mark.parametrize("lead", ["", " ", "\n", "\t", " \t\r\n\v\f "],
                         ids=["bare", "space", "newline", "tab", "mixed"])
@pytest.mark.parametrize("frame", FRAMES)
def test_a_frame_is_skipped_before_the_database_opens(db: Path, frame: str, lead: str) -> None:
    done = capture_prompt(db, f"{lead}{frame}\nwhatever follows")
    assert (done.returncode, done.stdout, done.stderr) == (0, "skipped\tframe\n", "")
    assert not db.exists() and not db.parent.exists()


def test_a_frame_is_skipped_in_json_too(db: Path) -> None:
    code, env = run_json(db, "capture", "--kind", "prompt",
                         stdin=prompt_hook("<system-reminder>x</system-reminder>"))
    assert code == 0
    assert env == {"data": {"event": None, "skipped": "frame"}, "error": None, "ok": True}
    assert not db.exists()


def test_a_frame_over_the_size_cap_is_skipped(db: Path) -> None:
    body = "<task-notification>" + "x" * (PROMPT_CAP_BYTES + 10)
    done = capture_prompt(db, body)
    assert (done.returncode, done.stdout) == (0, "skipped\tframe\n")
    assert not db.exists()


def test_an_existing_ledger_gains_nothing_from_a_frame(db: Path) -> None:
    assert capture_prompt(db, "a person's words").returncode == 0
    before = prompt_events(db)
    assert capture_prompt(db, "<agent-message from x>", prompt_id="p-2").returncode == 0
    assert prompt_events(db) == before


@pytest.mark.parametrize("prompt", [
    "<Task-notification> case differs",
    " <system-reminder> after a no-break space",
    "x <system-reminder> not at the start",
    '<pasted_content id="1">',
    "<task-notification",
    "[subagent hand-back] lower case",
    "please read <cross-session-message from=x>",
])
def test_a_lookalike_is_captured(db: Path, prompt: str) -> None:
    done = capture_prompt(db, prompt)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("captured\thook:prompt\t")
    [event] = prompt_events(db)
    assert event.payload["text"] == prompt


def test_a_frame_with_a_bad_field_is_still_refused(db: Path) -> None:
    done = run(db, "capture", "--kind", "prompt",
               stdin=prompt_hook("<system-reminder>", prompt_id=7))
    assert done.returncode == 3
    assert done.stderr == "prompt_id must be a non-empty string\n"
    assert not db.exists()


# --- capture_prompts ----------------------------------------------------------


def test_prompts_off_skips_and_writes_nothing(db: Path) -> None:
    write_config(db, '{"capture_prompts": false}')
    done = capture_prompt(db, "a person's words")
    assert (done.returncode, done.stdout, done.stderr) == (0, "skipped\tprompts_off\n", "")
    assert not db.exists()
    code, env = run_json(db, "capture", "--kind", "prompt", stdin=prompt_hook("again"))
    assert code == 0 and env["data"] == {"event": None, "skipped": "prompts_off"}


def test_prompts_off_leaves_an_existing_ledger_unchanged(db: Path) -> None:
    ask_and_answer(db)
    write_config(db, '{"capture_prompts": false, "claim_ttl_seconds": 60}')
    with open_ledger(db, create=False) as lg:
        before = len(lg.events())
    assert capture_prompt(db, "a person's words").stdout == "skipped\tprompts_off\n"
    with open_ledger(db, create=False) as lg:
        assert len(lg.events()) == before


def test_prompts_off_leaves_picker_capture_on_and_pick_binds(db: Path) -> None:
    write_config(db, '{"capture_prompts": false}')
    asked = ask_and_answer(db)
    code, env = run_json(db, "pick", ITEM, "--ask", str(asked))
    assert code == 0, env
    assert env["data"]["election"]["payload"]["chosen"] == "ship"


@pytest.mark.parametrize("config", [None, "{}", '{"capture_prompts": true}',
                                    '{"search_roots": []}'],
                         ids=["no-file", "empty", "true", "key-absent"])
def test_prompts_are_captured_unless_the_key_is_false(db: Path, config: str | None) -> None:
    if config is not None:
        write_config(db, config)
    done = capture_prompt(db, "a person's words")
    assert done.returncode == 0, done.stderr
    assert len(prompt_events(db)) == 1


INVALID_CONFIGS = {
    "bad-json": "{capture_prompts: false}",
    "array": "[]",
    "string-false": '{"capture_prompts": "false"}',
    "zero": '{"capture_prompts": 0}',
    "null": '{"capture_prompts": null}',
    "not-utf8": b'{"capture_prompts": false, "x": "\xff"}',
}


@pytest.mark.parametrize("name", sorted(INVALID_CONFIGS))
def test_an_invalid_config_refuses_and_writes_nothing(db: Path, name: str) -> None:
    path = write_config(db, INVALID_CONFIGS[name])
    secret = "the person's private words"
    done = capture_prompt(db, secret)
    assert (done.returncode, done.stdout) == (3, "")
    assert str(path) in done.stderr and secret not in done.stderr
    assert not db.exists()
    code, env = run_json(db, "capture", "--kind", "prompt", stdin=prompt_hook(secret))
    assert code == 3 and env["error"]["code"] == "capture_config"


def test_an_unreadable_config_refuses(db: Path) -> None:
    # A directory where the file should be: no reader can open it as a file.
    db.parent.mkdir(parents=True)
    (db.parent / "config.json").mkdir()
    code, env = run_json(db, "capture", "--kind", "prompt", stdin=prompt_hook("words"))
    assert code == 3 and env["error"]["code"] == "capture_config"
    assert str(db.parent / "config.json") in env["error"]["message"]
    assert not db.exists()


@pytest.mark.parametrize("kind", ["dangling", "loop"])
def test_a_config_link_that_does_not_resolve_refuses(db: Path, kind: str) -> None:
    # The shared config reader takes such a link for no file at all; for prompt
    # capture that would store prompts a person may have turned off.
    db.parent.mkdir(parents=True)
    path = db.parent / "config.json"
    path.symlink_to(path if kind == "loop" else db.parent / "moved-away.json")
    secret = "the person's private words"
    code, env = run_json(db, "capture", "--kind", "prompt", stdin=prompt_hook(secret))
    assert code == 3 and env["error"]["code"] == "capture_config"
    assert str(path) in env["error"]["message"] and secret not in env["error"]["message"]
    assert not db.exists()
    asked = ask_and_answer(db)
    code, env = run_json(db, "pick", ITEM, "--ask", str(asked))
    assert code == 0, env
    assert prompt_events(db) == []


@pytest.mark.parametrize("name", sorted(INVALID_CONFIGS))
def test_an_invalid_config_never_stops_a_picker_capture(db: Path, name: str) -> None:
    write_config(db, INVALID_CONFIGS[name])
    asked = ask_and_answer(db)
    code, env = run_json(db, "pick", ITEM, "--ask", str(asked))
    assert code == 0, env


def test_a_frame_is_skipped_before_the_config_is_read(db: Path) -> None:
    write_config(db, "[]")
    done = capture_prompt(db, "<system-reminder>")
    assert (done.returncode, done.stdout) == (0, "skipped\tframe\n")


# --- idempotency --------------------------------------------------------------


def test_one_input_with_prompt_id_captured_twice_is_one_event(db: Path) -> None:
    stdin = prompt_hook("a person's words")
    code, first = run_json(db, "capture", "--kind", "prompt", stdin=stdin)
    assert code == 0, first
    code, second = run_json(db, "capture", "--kind", "prompt", stdin=stdin)
    assert code == 0, second
    assert first["data"]["event"]["id"] == second["data"]["event"]["id"]
    [event] = prompt_events(db)
    assert event.idempotency_key == "capture:prompt:prompt-filter-1"


def test_one_input_without_prompt_id_captured_twice_is_two_events(db: Path) -> None:
    hook = json.loads(prompt_hook("a person's words"))
    del hook["prompt_id"]
    for _ in range(2):
        assert run(db, "capture", "--kind", "prompt", stdin=json.dumps(hook)).returncode == 0
    assert len(prompt_events(db)) == 2
