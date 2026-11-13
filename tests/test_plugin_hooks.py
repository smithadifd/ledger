"""The ledger-hooks Claude Code plugin: its manifests, its docs and its three hooks.

Every hook test runs the command ``hooks.json`` names for the event (and, for
PostToolUse, the matcher), through ``/bin/sh -c`` with ``CLAUDE_PLUGIN_ROOT``
set, as Claude Code does, with a recorded hook input on stdin. ``LEDGER_DB``
names a database in a temporary folder, ``HOME`` is a temporary folder, every
other ``LEDGER_*`` variable is removed, and ``PATH`` is a temporary folder
holding a ``ledger`` that runs this checkout's package, then ``/usr/bin:/bin``.

These tests live outside ``ledger/``: a vendored copy of the package has no
plugin folder beside it.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from ledger.store import open_ledger
from ledger.verbs.capture import HARNESS_FRAMES

from test_package_names_no_machine_paths import find_violations

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "ledger-hooks"
HOOKS_JSON = PLUGIN / "hooks" / "hooks.json"
PLUGIN_JSON = PLUGIN / ".claude-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ledger-hooks"
BASE_PATH = "/usr/bin:/bin"
FRAMES = (
    "<task-notification>",
    "<cross-session-message",
    "<agent-message",
    "<system-reminder>",
    "[Subagent hand-back]",
)
ITEM = "it-plugin"
QUESTION = "Ship the release today?"
OPTIONS = [
    {"label": "ship", "description": "Ship it today"},
    {"label": "hold", "description": "Hold it until Friday"},
]
GUARD_SENTENCE = (
    "A setup with no guard rail cannot stop an agent from running `ledger capture` itself"
)


@dataclass
class Rig:
    tmp: Path
    bin_dir: Path
    db: Path
    env: dict[str, str]


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "ledger"
    wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m ledger "$@"\n')
    wrapper.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    db = tmp_path / "data" / "ledger.db"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LEDGER_", "CLAUDE_")) and key != "PYTHONPATH"
    }
    env.update(
        PATH=f"{bin_dir}:{BASE_PATH}",
        HOME=str(home),
        LEDGER_DB=str(db),
        PYTHONPATH=str(ROOT),
        PYTHONDONTWRITEBYTECODE="1",
    )
    return Rig(tmp_path, bin_dir, db, env)


def recorded(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def prompt_input(prompt: str | None = None, **changes) -> dict:
    hook = recorded("user_prompt_submit.json")
    if prompt is not None:
        hook["prompt"] = prompt
    for key, value in changes.items():
        if value is None:
            hook.pop(key, None)
        else:
            hook[key] = value
    return hook


def hook_commands(event: str, tool: str | None = None) -> list[tuple[str, int]]:
    """The (command, timeout) pairs hooks.json runs for ``event`` (and ``tool``)."""
    config = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    commands = []
    for entry in config["hooks"].get(event, []):
        matcher = entry.get("matcher", "")
        if tool is not None and matcher not in ("", "*") and not re.fullmatch(matcher, tool):
            continue
        for hook in entry["hooks"]:
            commands.append((hook["command"], hook["timeout"]))
    return commands


@dataclass
class Ran:
    returncode: int
    stdout: str
    stderr: str


def run_hook(rig: Rig, event: str, stdin: str | bytes, *, tool: str | None = None,
             env: dict[str, str | None] | None = None) -> Ran:
    """Run every command hooks.json names for the event; their outputs joined."""
    run_env = dict(rig.env, CLAUDE_PLUGIN_ROOT=str(PLUGIN))
    for key, value in (env or {}).items():
        if value is None:
            run_env.pop(key, None)
        else:
            run_env[key] = value
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    codes, outs, errs = [], [], []
    for command, timeout in hook_commands(event, tool):
        done = subprocess.run(["/bin/sh", "-c", command], input=data, env=run_env,
                              capture_output=True, timeout=timeout, cwd=rig.tmp)
        codes.append(done.returncode)
        outs.append(done.stdout.decode("utf-8"))
        errs.append(done.stderr.decode("utf-8"))
    return Ran(max(codes, default=0), "".join(outs), "".join(errs))


def prompt_hook(rig: Rig, hook: dict | str, **kwargs) -> Ran:
    stdin = hook if isinstance(hook, str) else json.dumps(hook)
    return run_hook(rig, "UserPromptSubmit", stdin, **kwargs)


def picker_hook(rig: Rig, hook: dict, **kwargs) -> Ran:
    return run_hook(rig, "PostToolUse", json.dumps(hook), tool="AskUserQuestion", **kwargs)


def session_hook(rig: Rig, hook: dict | None = None, **kwargs) -> Ran:
    return run_hook(rig, "SessionStart", json.dumps(hook or recorded("session_start.json")),
                    **kwargs)


def ledger(rig: Rig, *argv: str, stdin: str = "", env: dict[str, str] | None = None
           ) -> subprocess.CompletedProcess[str]:
    """The checkout's CLI, run directly with the rig's environment."""
    run_env = dict(rig.env, LEDGER_HARNESS="pytest")
    run_env.update(env or {})
    return subprocess.run([str(rig.bin_dir / "ledger"), *argv], input=stdin, env=run_env,
                          capture_output=True, text=True, timeout=60, cwd=rig.tmp)


def ledger_json(rig: Rig, *argv: str, **kwargs) -> tuple[int, dict]:
    done = ledger(rig, "--json", *argv, **kwargs)
    return done.returncode, json.loads(done.stdout)


def captures(rig: Rig, channel: str) -> list:
    if not rig.db.exists():
        return []
    with open_ledger(rig.db, create=False) as lg:
        return [e for e in lg.events(kind="utterance.captured") if e.channel == channel]


def stub_ledger(rig: Rig, body: str) -> None:
    """Replace the rig's ``ledger`` with a shell script."""
    stub = rig.bin_dir / "ledger"
    stub.write_text("#!/bin/sh\n" + body)
    stub.chmod(0o755)


def new_item(rig: Rig) -> None:
    done = ledger(rig, "new", "--id", ITEM, "--type", "gate", "--title", "plugin fixture")
    assert done.returncode == 0, done.stderr


def ask(rig: Rig) -> tuple[int, list, str]:
    """An open ask on ITEM: its id, the questions ask-display prints, its fingerprint."""
    new_item(rig)
    payload = {"question": QUESTION, "options": OPTIONS, "scope": "this release"}
    code, env = ledger_json(rig, "append", "--item", ITEM, "--kind", "decision.asked",
                            "--payload", json.dumps(payload))
    assert code == 0, env
    asked = env["data"]["event"]["id"]
    code, env = ledger_json(rig, "ask-display", str(asked))
    assert code == 0, env
    return asked, env["data"]["input"]["questions"], env["data"]["display_sha256"]


def answered(questions: list, label: str) -> dict:
    hook = recorded("post_tool_use_ask.json")
    hook["tool_input"]["questions"] = questions
    hook["tool_response"] = {"questions": questions, "answers": {questions[0]["question"]: label}}
    return hook


def assert_quiet(ran: Ran) -> None:
    assert (ran.returncode, ran.stdout, ran.stderr) == (0, "", "")


# --- T1, T2: session start ----------------------------------------------------


def test_t1_session_start_prints_the_brief(rig: Rig) -> None:
    new_item(rig)
    expected = ledger(rig, "resume", "--brief")
    assert expected.returncode == 0 and expected.stdout.startswith("LEDGER AGENT CONTRACT")
    ran = session_hook(rig)
    assert (ran.returncode, ran.stderr) == (0, "")
    assert ran.stdout == expected.stdout


def test_t2_session_start_appends_what_hook_env_prints(rig: Rig) -> None:
    env_file = rig.tmp / "claude-env"
    env_file.write_text("export ALREADY_THERE=1\n")
    hook = recorded("session_start.json")
    expected = subprocess.run(
        [str(rig.bin_dir / "ledger"), "resume", "--hook-env"], input=json.dumps(hook),
        env=rig.env, capture_output=True, text=True, timeout=60,
    )
    assert expected.returncode == 0 and expected.stdout, expected.stderr
    ran = session_hook(rig, hook, env={"CLAUDE_ENV_FILE": str(env_file)})
    assert (ran.returncode, ran.stderr) == (0, "")
    assert ran.stdout.startswith("LEDGER AGENT CONTRACT")
    assert env_file.read_text() == "export ALREADY_THERE=1\n" + expected.stdout
    sourced = subprocess.run(
        ["/bin/sh", "-c",
         '. "$1"; printf "%s|%s|%s|%s" "$LEDGER_SESSION" "$LEDGER_AGENT" "$LEDGER_HARNESS"'
         ' "$LEDGER_MODEL"', "sh", str(env_file)],
        env={"PATH": BASE_PATH}, capture_output=True, text=True, timeout=30,
    )
    assert sourced.stdout == f"{hook['session_id']}|claude-code|claude-code|{hook['model']}"


def test_t2_without_claude_env_file_no_file_is_written_and_the_brief_prints(rig: Rig) -> None:
    before = sorted(p.name for p in rig.tmp.iterdir())
    ran = session_hook(rig)
    assert (ran.returncode, ran.stderr) == (0, "")
    assert ran.stdout.startswith("LEDGER AGENT CONTRACT")
    assert sorted(p.name for p in rig.tmp.iterdir()) == before


# --- T3, T4: the two captures -------------------------------------------------


def test_t3_a_submitted_prompt_yields_one_prompt_capture(rig: Rig) -> None:
    hook = prompt_input()
    assert_quiet(prompt_hook(rig, hook))
    [event] = captures(rig, "hook:prompt")
    assert event.payload == {
        "text": hook["prompt"],
        "origin": "person",
        "session_id": hook["session_id"],
        "prompt_id": hook["prompt_id"],
    }
    assert event.harness == "claude-code"
    assert event.provenance_class == "submitted-prompt"


def test_t3_a_harness_already_set_wins(rig: Rig) -> None:
    assert_quiet(prompt_hook(rig, prompt_input(), env={"LEDGER_HARNESS": "x"}))
    [event] = captures(rig, "hook:prompt")
    assert event.harness == "x"


def test_t4_a_picker_answer_yields_one_capture_that_pick_binds(rig: Rig) -> None:
    asked, questions, display_sha256 = ask(rig)
    assert_quiet(picker_hook(rig, answered(questions, "hold")))
    [event] = captures(rig, "hook:picker")
    assert event.payload["picker"]["display_sha256"] == display_sha256
    assert event.payload["picker"]["asked_event_id"] == asked
    assert event.harness == "claude-code"
    code, env = ledger_json(rig, "pick", ITEM, "--ask", str(asked))
    assert code == 0, env
    election = env["data"]["election"]["payload"]
    assert election["chosen"] == "hold" and election["quote_ref"] == event.id


# --- T5: harness frames -------------------------------------------------------


def test_t5_the_frame_list_is_the_five_literals() -> None:
    assert HARNESS_FRAMES == FRAMES


@pytest.mark.parametrize("lead", ["", " \n\t"], ids=["bare", "after-whitespace"])
@pytest.mark.parametrize("frame", FRAMES)
def test_t5_a_harness_frame_writes_nothing(rig: Rig, frame: str, lead: str) -> None:
    assert_quiet(prompt_hook(rig, prompt_input(f"{lead}{frame} the harness said this")))
    assert not rig.db.exists()


# --- T7: the ledger is off ----------------------------------------------------


OFF_CASES = {
    "LEDGER_DB unset": {"LEDGER_DB": None},
    "LEDGER_DB empty": {"LEDGER_DB": ""},
    "no ledger on PATH": {"PATH": BASE_PATH},
}


def test_t7_the_base_path_has_no_ledger() -> None:
    assert shutil.which("ledger", path=BASE_PATH) is None


@pytest.mark.parametrize("case", sorted(OFF_CASES))
@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "PostToolUse"])
def test_t7_with_no_ledger_configured_every_hook_is_silent(rig: Rig, event: str,
                                                          case: str) -> None:
    env_file = rig.tmp / "claude-env"
    env_file.write_bytes(b"export KEEP=1\n")
    env = dict(OFF_CASES[case], CLAUDE_ENV_FILE=str(env_file))
    stdin = {
        "SessionStart": recorded("session_start.json"),
        "UserPromptSubmit": prompt_input(),
        "PostToolUse": answered([{"question": QUESTION, "header": "q", "multiSelect": False,
                                  "options": OPTIONS}], "ship"),
    }[event]
    tool = "AskUserQuestion" if event == "PostToolUse" else None
    assert hook_commands(event, tool), event
    assert_quiet(run_hook(rig, event, json.dumps(stdin), tool=tool, env=env))
    assert not rig.db.exists() and not rig.db.parent.exists()
    assert env_file.read_bytes() == b"export KEEP=1\n"


# --- T8, T10: config.json through the hooks -------------------------------------


def write_config(rig: Rig, text: str) -> None:
    rig.db.parent.mkdir(parents=True, exist_ok=True)
    (rig.db.parent / "config.json").write_text(text)


def test_t8_prompts_off_skips_the_prompt_but_the_picker_still_binds(rig: Rig) -> None:
    asked, questions, _ = ask(rig)
    write_config(rig, '{"capture_prompts": false}')
    assert_quiet(prompt_hook(rig, prompt_input()))
    assert captures(rig, "hook:prompt") == []
    assert_quiet(picker_hook(rig, answered(questions, "ship")))
    assert len(captures(rig, "hook:picker")) == 1
    code, env = ledger_json(rig, "pick", ITEM, "--ask", str(asked))
    assert code == 0, env


def test_t10_an_invalid_config_is_one_failure_line_and_the_picker_still_binds(rig: Rig) -> None:
    asked, questions, _ = ask(rig)
    write_config(rig, '{"capture_prompts": "false"}')
    hook = prompt_input()
    ran = prompt_hook(rig, hook)
    assert (ran.returncode, ran.stdout) == (0, "")
    assert re.fullmatch(r"ledger-hooks: prompt capture failed \(exit 3\)\n", ran.stderr)
    assert captures(rig, "hook:prompt") == []
    assert_quiet(picker_hook(rig, answered(questions, "ship")))
    code, env = ledger_json(rig, "pick", ITEM, "--ask", str(asked))
    assert code == 0, env


# --- T11-T14: one input run twice ---------------------------------------------


def test_t11_the_prompt_hook_run_twice_leaves_one_capture(rig: Rig) -> None:
    hook = prompt_input()
    assert_quiet(prompt_hook(rig, hook))
    assert_quiet(prompt_hook(rig, hook))
    [event] = captures(rig, "hook:prompt")
    code, env = ledger_json(rig, "capture", "--kind", "prompt", stdin=json.dumps(hook),
                            env={"LEDGER_HARNESS": "claude-code"})
    assert code == 0 and env["data"]["event"]["id"] == event.id
    assert len(captures(rig, "hook:prompt")) == 1


def test_t11_the_prompt_hook_run_twice_at_once_leaves_one_capture(rig: Rig) -> None:
    new_item(rig)
    hook = prompt_input()
    results: list[Ran] = []

    def one() -> None:
        results.append(prompt_hook(rig, hook))

    threads = [threading.Thread(target=one) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for ran in results:
        assert_quiet(ran)
    assert len(results) == 2
    assert len(captures(rig, "hook:prompt")) == 1


def test_t12_the_picker_hook_run_twice_leaves_one_capture(rig: Rig) -> None:
    _, questions, _ = ask(rig)
    hook = answered(questions, "ship")
    assert_quiet(picker_hook(rig, hook))
    assert_quiet(picker_hook(rig, hook))
    assert len(captures(rig, "hook:picker")) == 1


def test_t13_the_same_text_under_two_prompt_ids_is_two_captures(rig: Rig) -> None:
    assert_quiet(prompt_hook(rig, prompt_input(prompt_id="prompt-one")))
    assert_quiet(prompt_hook(rig, prompt_input(prompt_id="prompt-two")))
    assert [e.payload["prompt_id"] for e in captures(rig, "hook:prompt")] == [
        "prompt-one", "prompt-two"]


def test_t14_an_input_without_prompt_id_run_twice_is_two_captures_never_one(rig: Rig) -> None:
    # The documented rule: with no prompt_id a re-run and a second identical
    # submission look the same, so each gets a fresh key. A duplicate, never a merge.
    hook = prompt_input(prompt_id=None)
    assert_quiet(prompt_hook(rig, hook))
    assert_quiet(prompt_hook(rig, hook))
    events = captures(rig, "hook:prompt")
    assert len(events) == 2 and events[0].idempotency_key != events[1].idempotency_key


# --- T15-T18, T26: failures stay bounded ---------------------------------------


ECHO_AND_FAIL = 'input=$(cat)\nprintf "%s\\n" "$input"\nprintf "%s\\n" "$input" >&2\nexit 3\n'


@pytest.mark.parametrize("kind", ["prompt", "picker"])
def test_t15_a_failing_cli_is_one_bounded_stderr_line(rig: Rig, kind: str) -> None:
    stub_ledger(rig, ECHO_AND_FAIL)
    secret = "the person's private words"
    if kind == "prompt":
        ran = prompt_hook(rig, prompt_input(secret))
    else:
        ran = picker_hook(rig, answered([{"question": secret, "header": "q",
                                          "multiSelect": False, "options": OPTIONS}], "ship"))
    assert (ran.returncode, ran.stdout) == (0, "")
    assert ran.stderr == f"ledger-hooks: {kind} capture failed (exit 3)\n"
    assert secret not in ran.stderr


@pytest.mark.parametrize("kind", ["prompt", "picker"])
def test_t16_cli_stdout_never_reaches_the_session(rig: Rig, kind: str) -> None:
    stub_ledger(rig, "cat\necho 'captured to stdout'\nexit 0\n")
    if kind == "prompt":
        ran = prompt_hook(rig, prompt_input())
    else:
        ran = picker_hook(rig, answered([{"question": QUESTION, "header": "q",
                                          "multiSelect": False, "options": OPTIONS}], "ship"))
    assert_quiet(ran)


def test_t17_a_failing_brief_is_one_line_on_stdout(rig: Rig) -> None:
    stub_ledger(rig, 'echo "partial brief"\necho "ledger: broken" >&2\nexit 1\n')
    ran = session_hook(rig)
    assert (ran.returncode, ran.stderr) == (0, "")
    assert ran.stdout == "ledger-hooks: ledger resume --brief exited 1\n"


def test_t18_malformed_prompt_input_is_one_line_and_writes_nothing(rig: Rig) -> None:
    ran = prompt_hook(rig, "this is not json")
    assert (ran.returncode, ran.stdout) == (0, "")
    assert ran.stderr == "ledger-hooks: prompt capture failed (exit 3)\n"
    assert not rig.db.exists()


def test_t26_a_failing_hook_env_leaves_the_env_file_and_the_brief_prints(rig: Rig) -> None:
    stub_ledger(rig, (
        'case "$*" in\n'
        '  "resume --hook-env") cat >/dev/null; echo "refused" >&2; exit 3 ;;\n'
        '  "resume --brief") echo "BRIEF"; exit 0 ;;\n'
        'esac\nexit 9\n'
    ))
    env_file = rig.tmp / "claude-env"
    env_file.write_bytes(b"export KEEP=1\n")
    ran = session_hook(rig, env={"CLAUDE_ENV_FILE": str(env_file)})
    assert ran.returncode == 0
    assert ran.stderr == "ledger-hooks: session env failed (exit 3)\n"
    assert ran.stdout == "BRIEF\n"
    assert env_file.read_bytes() == b"export KEEP=1\n"


def test_t26_the_real_cli_refusal_leaves_the_env_file_byte_identical(rig: Rig) -> None:
    env_file = rig.tmp / "claude-env"
    env_file.write_bytes(b"export KEEP=1\n")
    hook = dict(recorded("session_start.json"), hook_event_name="UserPromptSubmit")
    ran = session_hook(rig, hook, env={"CLAUDE_ENV_FILE": str(env_file)})
    assert ran.returncode == 0
    assert ran.stderr == "ledger-hooks: session env failed (exit 3)\n"
    assert ran.stdout.startswith("LEDGER AGENT CONTRACT")
    assert env_file.read_bytes() == b"export KEEP=1\n"


# --- T19: the manifests -------------------------------------------------------


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_t19_the_marketplace_names_its_owner_and_the_plugin() -> None:
    market = load(MARKETPLACE)
    assert market["name"] == "ledger-plugins"
    assert type(market["owner"]) is dict
    assert type(market["owner"].get("name")) is str and market["owner"]["name"].strip()
    [entry] = market["plugins"]
    assert entry["name"] == "ledger-hooks" == load(PLUGIN_JSON)["name"]
    assert entry["source"] == "./plugins/ledger-hooks"
    assert (ROOT / entry["source"]).resolve() == PLUGIN.resolve()


def test_t19_the_plugin_is_not_named_ledger_and_nothing_sets_a_version() -> None:
    market, plugin = load(MARKETPLACE), load(PLUGIN_JSON)
    assert plugin["name"] != "ledger"
    assert "version" not in market and "version" not in plugin
    assert all("version" not in entry for entry in market["plugins"])
    assert "version" not in market.get("metadata", {})


def test_t19_hooks_json_runs_three_bounded_shims() -> None:
    config = load(HOOKS_JSON)["hooks"]
    assert set(config) == {"SessionStart", "UserPromptSubmit", "PostToolUse"}
    [post] = config["PostToolUse"]
    assert post["matcher"] == "AskUserQuestion"
    assert all("matcher" not in entry for entry in config["SessionStart"] + config["UserPromptSubmit"])
    for event, entries in config.items():
        for entry in entries:
            for hook in entry["hooks"]:
                assert hook["type"] == "command"
                assert "async" not in hook, event
                assert type(hook["timeout"]) is int and 1 <= hook["timeout"] <= 30, event
                match = re.fullmatch(r'sh "\$\{CLAUDE_PLUGIN_ROOT\}/(hooks/[a-z-]+\.sh)"',
                                     hook["command"])
                assert match is not None, hook["command"]
                assert (PLUGIN / match.group(1)).is_file(), match.group(1)


def test_t19_no_plugin_file_names_a_home_path() -> None:
    files = [p for base in (PLUGIN, ROOT / ".claude-plugin") for p in base.rglob("*")
             if p.is_file()]
    assert len(files) >= 6, files
    violations = []
    for path in files:
        violations.extend(find_violations(path.relative_to(ROOT),
                                          path.read_text(encoding="utf-8")))
    assert not violations, violations


def test_t19_the_plugin_sits_outside_the_vendored_package() -> None:
    assert not PLUGIN.resolve().is_relative_to((ROOT / "ledger").resolve())
    vendoring = (ROOT / "VENDORING.md").read_text(encoding="utf-8")
    assert "Nothing outside `ledger/` is vendored" in vendoring


# --- T20: the docs ------------------------------------------------------------


def _flat(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_t20_cli_page_documents_the_frames_the_config_key_and_hook_env() -> None:
    page = _flat(ROOT / "docs" / "cli.md")
    for token in ("capture_prompts", "capture_config", "`skipped`", "prompts_off",
                  "--hook-env", "hook_env_bad_input", "hook_env_not_session_start",
                  *(f"`{frame}`" for frame in FRAMES)):
        assert token in page, token
    assert "running it twice leaves one capture" in page
    assert "a re-run may store a duplicate; it never merges two submissions" in page


def test_t20_readme_and_known_limits_carry_the_guard_rail_sentence() -> None:
    for path in (ROOT / "README.md", ROOT / "KNOWN-LIMITS.md"):
        assert GUARD_SENTENCE in _flat(path), path.name
    readme = _flat(ROOT / "README.md")
    assert "claude plugin install ledger-hooks@ledger-plugins" in readme
    start = _flat(ROOT / "docs" / "start-here.md")
    assert "claude plugin install ledger-hooks@ledger-plugins" in start
