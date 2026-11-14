"""``ledger capture --kind prompt`` honours a cwd opt-out and an injected-prompt skip list.

Two keys in the ``config.json`` beside the database:

- ``capture_optout_paths``: a prompt whose hook ``cwd`` is at or under one of
  these absolute paths, compared canonically and at a path boundary, writes
  nothing (``skipped<TAB>cwd_optout``). An invalid value refuses
  ``capture_config`` (exit 3) and writes nothing.
- ``capture_skip_prefixes``: a prompt that starts, after ASCII whitespace,
  with one of these prefixes writes nothing (``skipped<TAB>injected``). A
  valid array replaces the built-in default; an absent key or an invalid
  value leaves the default in force and capture goes on.

Every skip is decided before the database is opened, so none creates it. Each
case runs the CLI as a hook does, a subprocess with the hook input on stdin,
except the few that patch the built-in default and so run in this process.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from ledger.cli import main
from ledger.store import open_ledger
from ledger.verbs import capture

PKG_PARENT = Path(__file__).resolve().parents[2]  # makes ``python -m ledger`` importable
SECRET = "the person's private words"
PREFIX = "<injected-by-a-tool>"


def run(db: Path, *argv: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env.update(LEDGER_HARNESS="pytest", LEDGER_DB=str(db), PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-m", "ledger", *argv], input=stdin.encode("utf-8"),
                          env=env, capture_output=True, timeout=60, cwd=PKG_PARENT)
    return subprocess.CompletedProcess(done.args, done.returncode,
                                       done.stdout.decode("utf-8"), done.stderr.decode("utf-8"))


def prompt_hook(prompt: str, cwd=None, **extra) -> str:
    hook = {
        "session_id": "sess-optout",
        "hook_event_name": "UserPromptSubmit",
        "prompt": prompt,
    }
    if cwd is not None:
        hook["cwd"] = cwd if not isinstance(cwd, Path) else str(cwd)
    hook.update(extra)
    return json.dumps(hook)


def capture_prompt(db: Path, prompt: str = SECRET, cwd=None, **extra):
    return run(db, "capture", "--kind", "prompt", stdin=prompt_hook(prompt, cwd, **extra))


def capture_json(db: Path, prompt: str = SECRET, cwd=None) -> tuple[int, dict]:
    done = run(db, "--json", "capture", "--kind", "prompt", stdin=prompt_hook(prompt, cwd))
    return done.returncode, json.loads(done.stdout)


def prompt_events(db: Path) -> list:
    if not db.exists():
        return []
    with open_ledger(db, create=False) as lg:
        return [e for e in lg.events(kind="utterance.captured") if e.channel == "hook:prompt"]


def write_config(db: Path, config) -> Path:
    db.parent.mkdir(parents=True, exist_ok=True)
    path = db.parent / "config.json"
    path.write_text(config if isinstance(config, str) else json.dumps(config), encoding="utf-8")
    return path


def assert_skipped(done: subprocess.CompletedProcess[str], reason: str, db: Path) -> None:
    assert (done.returncode, done.stdout, done.stderr) == (0, f"skipped\t{reason}\n", "")
    assert prompt_events(db) == []


def assert_captured(done: subprocess.CompletedProcess[str], db: Path, prompt: str = SECRET) -> None:
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("captured\thook:prompt\t"), done.stdout
    assert [e.payload["text"] for e in prompt_events(db)] == [prompt]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "data" / "ledger.db"


@pytest.fixture
def optout_dir(tmp_path: Path) -> Path:
    path = tmp_path / "optout"
    (path / "a-project").mkdir(parents=True)
    return path


def opt_out(db: Path, *paths: Path | str, **more) -> Path:
    return write_config(db, {"capture_optout_paths": [str(p) for p in paths], **more})


# --- capture_optout_paths: what opts out -----------------------------------------


@pytest.mark.parametrize("where", ["equal", "under", "deep", "not-yet-made", "trailing-sep"])
def test_a_cwd_at_or_under_an_optout_path_writes_nothing(db: Path, optout_dir: Path,
                                                         where: str) -> None:
    opt_out(db, optout_dir)
    cwd = {
        "equal": str(optout_dir),
        "under": str(optout_dir / "a-project"),
        "deep": str(optout_dir / "a-project" / "src" / "pkg"),
        "not-yet-made": str(optout_dir / "no-such-project" / "x"),
        "trailing-sep": str(optout_dir) + os.sep,
    }[where]
    assert_skipped(capture_prompt(db, cwd=cwd), "cwd_optout", db)
    assert not db.exists()


def test_a_cwd_optout_skip_in_json(db: Path, optout_dir: Path) -> None:
    opt_out(db, optout_dir)
    code, env = capture_json(db, cwd=optout_dir / "a-project")
    assert code == 0
    assert env == {"data": {"event": None, "skipped": "cwd_optout"}, "error": None, "ok": True}
    assert not db.exists()


def test_a_symbolic_link_into_an_optout_path_opts_out(db: Path, optout_dir: Path,
                                                     tmp_path: Path) -> None:
    link = tmp_path / "shortcut"
    link.symlink_to(optout_dir / "a-project", target_is_directory=True)
    opt_out(db, optout_dir)
    assert_skipped(capture_prompt(db, cwd=link), "cwd_optout", db)
    assert_skipped(capture_prompt(db, cwd=link / "sub"), "cwd_optout", db)
    assert not db.exists()


def test_an_optout_path_given_through_a_link_matches_the_real_cwd(db: Path, optout_dir: Path,
                                                                 tmp_path: Path) -> None:
    link = tmp_path / "optout-link"
    link.symlink_to(optout_dir, target_is_directory=True)
    opt_out(db, link)
    assert_skipped(capture_prompt(db, cwd=optout_dir / "a-project"), "cwd_optout", db)
    assert not db.exists()


def test_a_dotdot_path_into_an_optout_path_opts_out(db: Path, optout_dir: Path,
                                                   tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    opt_out(db, optout_dir)
    cwd = f"{tmp_path}{os.sep}elsewhere{os.sep}..{os.sep}optout{os.sep}a-project"
    assert_skipped(capture_prompt(db, cwd=cwd), "cwd_optout", db)
    assert not db.exists()


def test_an_optout_path_with_dotdot_is_canonicalised_too(db: Path, optout_dir: Path,
                                                        tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    opt_out(db, f"{tmp_path}{os.sep}elsewhere{os.sep}..{os.sep}optout")
    assert_skipped(capture_prompt(db, cwd=optout_dir / "a-project"), "cwd_optout", db)


def test_one_of_several_optout_paths_is_enough(db: Path, optout_dir: Path, tmp_path: Path) -> None:
    opt_out(db, tmp_path / "other", optout_dir)
    assert_skipped(capture_prompt(db, cwd=optout_dir / "a-project"), "cwd_optout", db)


def test_an_existing_ledger_gains_nothing_from_an_opted_out_cwd(db: Path, optout_dir: Path,
                                                               tmp_path: Path) -> None:
    assert_captured(capture_prompt(db, "before", cwd=tmp_path), db, "before")
    opt_out(db, optout_dir)
    done = capture_prompt(db, cwd=optout_dir / "a-project")
    assert done.stdout == "skipped\tcwd_optout\n"
    assert [e.payload["text"] for e in prompt_events(db)] == ["before"]


# --- capture_optout_paths: what is still captured --------------------------------


@pytest.mark.parametrize("where", ["sibling", "sibling-under", "dotdot-out", "parent"])
def test_a_cwd_outside_the_optout_path_is_captured(db: Path, optout_dir: Path, tmp_path: Path,
                                                  where: str) -> None:
    (tmp_path / "optoutfoo").mkdir()
    opt_out(db, optout_dir)
    cwd = {
        "sibling": str(tmp_path / "optoutfoo"),
        "sibling-under": str(tmp_path / "optoutfoo" / "a-project"),
        "dotdot-out": f"{optout_dir}{os.sep}..{os.sep}optoutfoo",
        "parent": str(tmp_path),
    }[where]
    assert_captured(capture_prompt(db, cwd=cwd), db)


def test_a_link_out_of_the_optout_path_is_captured(db: Path, optout_dir: Path,
                                                  tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    (optout_dir / "to-work").symlink_to(tmp_path / "work", target_is_directory=True)
    opt_out(db, optout_dir)
    assert_captured(capture_prompt(db, cwd=optout_dir / "to-work"), db)


@pytest.mark.parametrize("cwd", [None, ""], ids=["missing", "empty"])
def test_a_missing_or_empty_cwd_opts_nothing_out(db: Path, optout_dir: Path, cwd) -> None:
    opt_out(db, optout_dir)
    assert_captured(capture_prompt(db, cwd=cwd), db)


@pytest.mark.parametrize("config", [None, {}, {"capture_optout_paths": []}],
                         ids=["no-file", "no-key", "empty-array"])
def test_no_optout_path_opts_nothing_out(db: Path, optout_dir: Path, config) -> None:
    if config is not None:
        write_config(db, config)
    assert_captured(capture_prompt(db, cwd=optout_dir / "a-project"), db)


def test_a_tilde_cwd_is_never_expanded(db: Path, optout_dir: Path) -> None:
    # "~" is not an absolute path; it never stands for the home folder here.
    opt_out(db, optout_dir)
    done = capture_prompt(db, cwd="~/" + optout_dir.name)
    assert (done.returncode, done.stdout) == (3, "")
    assert done.stderr == "cwd must be an absolute path\n"


@pytest.mark.parametrize("cwd", ["relative/dir", 7, ["/x"], "/nul\u0000byte"],
                         ids=["relative", "number", "array", "nul"])
def test_a_cwd_that_is_not_an_absolute_path_refuses_when_paths_are_set(db: Path, optout_dir: Path,
                                                                     cwd) -> None:
    opt_out(db, optout_dir)
    done = capture_prompt(db, cwd=cwd)
    assert (done.returncode, done.stdout) == (3, "")
    assert done.stderr == "cwd must be an absolute path\n"
    assert not db.exists()


# --- capture_optout_paths: an invalid value refuses -------------------------------


INVALID_OPTOUTS = {
    "string": '"/abs/path"',
    "object": '{"/abs/path": true}',
    "null": "null",
    "number": "1",
    "non-string-entry": '["/abs/path", 7]',
    "null-entry": "[null]",
    "empty-entry": '[""]',
    "relative-entry": '["optout"]',
    "tilde-entry": '["~/optout_dir"]',
    "nul-entry": '["/abs\\u0000path"]',
}


@pytest.mark.parametrize("name", sorted(INVALID_OPTOUTS))
def test_an_invalid_optout_value_refuses_and_writes_nothing(db: Path, tmp_path: Path,
                                                           name: str) -> None:
    path = write_config(db, '{"capture_optout_paths": ' + INVALID_OPTOUTS[name] + "}")
    done = capture_prompt(db, cwd=tmp_path)
    assert (done.returncode, done.stdout) == (3, "")
    assert "capture_optout_paths" in done.stderr and str(path) in done.stderr
    assert SECRET not in done.stderr
    assert not db.exists()
    code, env = capture_json(db, cwd=tmp_path)
    assert code == 3 and env["error"]["code"] == "capture_config"
    assert not db.exists()


def test_an_invalid_optout_value_refuses_an_existing_ledger_too(db: Path,
                                                               tmp_path: Path) -> None:
    assert_captured(capture_prompt(db, "before", cwd=tmp_path), db, "before")
    write_config(db, '{"capture_optout_paths": "/abs/path"}')
    assert capture_prompt(db, cwd=tmp_path).returncode == 3
    assert [e.payload["text"] for e in prompt_events(db)] == ["before"]


# --- capture_skip_prefixes --------------------------------------------------------


def test_the_public_default_prefix_list_is_empty() -> None:
    assert capture.DEFAULT_SKIP_PREFIXES == ()


@pytest.mark.parametrize("lead", ["", " ", "\n", "\t", " \t\r\n\v\f "],
                         ids=["bare", "space", "newline", "tab", "mixed"])
def test_a_listed_prefix_writes_nothing(db: Path, lead: str) -> None:
    write_config(db, {"capture_skip_prefixes": ["/other", PREFIX]})
    assert_skipped(capture_prompt(db, f"{lead}{PREFIX} then the tool's text"), "injected", db)
    assert not db.exists()


def test_an_injected_skip_in_json(db: Path) -> None:
    write_config(db, {"capture_skip_prefixes": [PREFIX]})
    code, env = capture_json(db, PREFIX + " x")
    assert code == 0
    assert env == {"data": {"event": None, "skipped": "injected"}, "error": None, "ok": True}
    assert not db.exists()


@pytest.mark.parametrize("prompt", [
    PREFIX.upper() + " case differs",
    " " + PREFIX + " after a no-break space",
    "x " + PREFIX + " not at the start",
    PREFIX[:-1],
])
def test_a_prefix_lookalike_is_captured(db: Path, prompt: str) -> None:
    write_config(db, {"capture_skip_prefixes": [PREFIX]})
    assert_captured(capture_prompt(db, prompt), db, prompt)


def test_an_existing_ledger_gains_nothing_from_an_injected_prompt(db: Path) -> None:
    assert_captured(capture_prompt(db, "before"), db, "before")
    write_config(db, {"capture_skip_prefixes": [PREFIX]})
    assert capture_prompt(db, PREFIX).stdout == "skipped\tinjected\n"
    assert [e.payload["text"] for e in prompt_events(db)] == ["before"]


INVALID_PREFIX_LISTS = {
    "string": json.dumps(PREFIX),
    "object": json.dumps({PREFIX: True}),
    "null": "null",
    "number": "1",
    "non-string-entry": json.dumps([PREFIX, 7]),
    "null-entry": json.dumps([PREFIX, None]),
    "empty-entry": json.dumps([PREFIX, ""]),
}


@pytest.mark.parametrize("name", sorted(INVALID_PREFIX_LISTS))
def test_an_invalid_prefix_list_falls_back_and_captures(db: Path, name: str) -> None:
    # The public default is empty, so the fallback skips nothing; the invalid
    # list's own entries never apply.
    write_config(db, '{"capture_skip_prefixes": ' + INVALID_PREFIX_LISTS[name] + "}")
    assert_captured(capture_prompt(db, PREFIX + " x"), db, PREFIX + " x")


@pytest.mark.parametrize("config", [None, {}, {"capture_skip_prefixes": []}],
                         ids=["no-file", "no-key", "empty-array"])
def test_no_prefix_list_captures(db: Path, config) -> None:
    if config is not None:
        write_config(db, config)
    assert_captured(capture_prompt(db, PREFIX + " x"), db, PREFIX + " x")


def capture_in_process(monkeypatch, db: Path, prompt: str) -> tuple[int, dict]:
    """``capture --kind prompt --json`` in this process; the exit code and envelope."""
    for key in [key for key in os.environ if key.startswith("LEDGER_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("LEDGER_HARNESS", "pytest")
    monkeypatch.setattr(sys, "stdin", io.StringIO(prompt_hook(prompt)))
    out = io.StringIO()
    with redirect_stdout(out):
        code = main(["--json", "--db", str(db), "capture", "--kind", "prompt"])
    return code, json.loads(out.getvalue())


DEFAULT = "<a-default-prefix>"


@pytest.mark.parametrize("value", [None, *sorted(INVALID_PREFIX_LISTS)])
def test_with_a_default_an_absent_or_invalid_list_keeps_it(monkeypatch, db: Path,
                                                           value) -> None:
    monkeypatch.setattr(capture, "DEFAULT_SKIP_PREFIXES", (DEFAULT,))
    if value is None:
        write_config(db, "{}")
    else:
        write_config(db, '{"capture_skip_prefixes": ' + INVALID_PREFIX_LISTS[value] + "}")
    code, env = capture_in_process(monkeypatch, db, DEFAULT + " x")
    assert (code, env["data"]) == (0, {"event": None, "skipped": "injected"}), env
    assert not db.exists()


@pytest.mark.parametrize("value", [[], ["/other"]], ids=["empty-array", "other-prefix"])
def test_with_a_default_a_valid_list_replaces_it(monkeypatch, db: Path, value) -> None:
    monkeypatch.setattr(capture, "DEFAULT_SKIP_PREFIXES", (DEFAULT,))
    write_config(db, {"capture_skip_prefixes": value})
    code, env = capture_in_process(monkeypatch, db, DEFAULT + " x")
    assert code == 0, env
    assert env["data"]["event"]["payload"]["text"] == DEFAULT + " x"


# --- order ------------------------------------------------------------------------


def test_a_frame_comes_before_the_prefix_list(db: Path) -> None:
    write_config(db, {"capture_skip_prefixes": ["<system-reminder>"]})
    assert_skipped(capture_prompt(db, "<system-reminder>x"), "frame", db)


def test_the_prefix_list_never_replaces_the_frames(db: Path) -> None:
    write_config(db, {"capture_skip_prefixes": [PREFIX]})
    assert_skipped(capture_prompt(db, "<task-notification>x"), "frame", db)


def test_an_injected_prompt_comes_before_the_cwd_optout(db: Path, optout_dir: Path) -> None:
    write_config(db, {"capture_skip_prefixes": [PREFIX],
                      "capture_optout_paths": [str(optout_dir)]})
    assert_skipped(capture_prompt(db, PREFIX, cwd=optout_dir), "injected", db)


def test_the_cwd_optout_comes_before_prompts_off(db: Path, optout_dir: Path) -> None:
    opt_out(db, optout_dir, capture_prompts=False)
    assert_skipped(capture_prompt(db, cwd=optout_dir), "cwd_optout", db)
    assert_skipped(capture_prompt(db, cwd=optout_dir.parent), "prompts_off", db)
    assert not db.exists()


def test_an_invalid_optout_value_refuses_before_prompts_off(db: Path, tmp_path: Path) -> None:
    write_config(db, {"capture_optout_paths": ["optout"], "capture_prompts": False})
    code, env = capture_json(db, cwd=tmp_path)
    assert code == 3 and env["error"]["code"] == "capture_config"


def test_an_unreadable_config_refuses_a_prompt_that_is_not_a_frame(db: Path) -> None:
    write_config(db, "[]")
    code, env = capture_json(db, PREFIX)
    assert code == 3 and env["error"]["code"] == "capture_config"
    assert not db.exists()


def test_picker_capture_ignores_both_keys(db: Path, optout_dir: Path) -> None:
    write_config(db, {"capture_optout_paths": "not an array", "capture_skip_prefixes": 1})
    hook = {
        "session_id": "sess-optout", "cwd": str(optout_dir), "hook_event_name": "PostToolUse",
        "tool_name": "AskUserQuestion", "tool_use_id": "toolu_optout",
        "tool_input": {"questions": [{"question": "Go?", "options": [{"label": "yes"}]}]},
        "tool_response": {"answers": {"Go?": "yes"}},
    }
    done = run(db, "capture", "--kind", "picker", stdin=json.dumps(hook))
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("captured\thook:picker\t")
