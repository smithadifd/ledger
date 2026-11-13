"""``ledger resume --hook-env``: the session environment lines for a SessionStart hook.

The flag reads one SessionStart hook input on stdin and prints up to four
``export NAME=<value>`` lines: ``LEDGER_SESSION`` always (when the input has a
usable ``session_id``), then ``LEDGER_AGENT``, ``LEDGER_HARNESS`` and
``LEDGER_MODEL`` only when each is absent from the environment. It never
resolves, opens or creates the database. Each case runs the CLI as a hook
does: a subprocess with the input on stdin.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PKG_PARENT = Path(__file__).resolve().parents[2]  # makes ``python -m ledger`` importable
SESSION = "3f2b8c1e-5d4a-4e7b-9a10-2c6d8e4f1a2b"
MODEL = "claude-fixture-model"


def hook(**changes) -> dict:
    data = {
        "session_id": SESSION,
        "transcript_path": "/tmp/fixture/transcript.jsonl",
        "cwd": "/tmp/fixture",
        "hook_event_name": "SessionStart",
        "source": "startup",
        "model": MODEL,
    }
    for key, value in changes.items():
        if value is ...:
            data.pop(key, None)
        else:
            data[key] = value
    return data


def hook_env(db: Path, stdin: str | bytes, *extra: str,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    run_env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    run_env["PYTHONDONTWRITEBYTECODE"] = "1"
    run_env.update(env or {})
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    done = subprocess.run(
        [sys.executable, "-m", "ledger", "--db", str(db), "resume", "--hook-env", *extra],
        input=data, env=run_env, capture_output=True, timeout=60, cwd=PKG_PARENT,
    )
    return subprocess.CompletedProcess(done.args, done.returncode,
                                       done.stdout.decode("utf-8"), done.stderr.decode("utf-8"))


def sourced(text: str, tmp_path: Path, name: str) -> str:
    """The value of ``name`` after a POSIX shell sources ``text``."""
    path = tmp_path / "env-file"
    path.write_text(text, encoding="utf-8")
    done = subprocess.run(["/bin/sh", "-c", f'. "$1" && printf "%s" "${name}"', "sh", str(path)],
                          env={"PATH": "/usr/bin:/bin"}, capture_output=True, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout.decode("utf-8")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "data" / "ledger.db"


# --- T21: the full output -----------------------------------------------------


def test_t21_four_lines_in_order_and_no_database(db: Path) -> None:
    done = hook_env(db, json.dumps(hook()))
    assert (done.returncode, done.stderr) == (0, "")
    assert done.stdout == (
        f"export LEDGER_SESSION={SESSION}\n"
        "export LEDGER_AGENT=claude-code\n"
        "export LEDGER_HARNESS=claude-code\n"
        f"export LEDGER_MODEL={MODEL}\n"
    )
    assert not db.exists() and not db.parent.exists()


def test_t21_json_carries_the_same_map(db: Path) -> None:
    done = hook_env(db, json.dumps(hook()), "--json")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {
        "data": {"env": {
            "LEDGER_SESSION": SESSION,
            "LEDGER_AGENT": "claude-code",
            "LEDGER_HARNESS": "claude-code",
            "LEDGER_MODEL": MODEL,
        }},
        "error": None,
        "ok": True,
    }
    assert not db.exists()


# --- T22: values already set --------------------------------------------------


@pytest.mark.parametrize("value", ["already", ""], ids=["set", "set-empty"])
@pytest.mark.parametrize("name", ["LEDGER_AGENT", "LEDGER_HARNESS", "LEDGER_MODEL"])
def test_t22_a_variable_already_set_gets_no_line(db: Path, name: str, value: str) -> None:
    done = hook_env(db, json.dumps(hook()), env={name: value})
    assert done.returncode == 0, done.stderr
    names = [line.split("=", 1)[0] for line in done.stdout.splitlines()]
    assert f"export {name}" not in names
    assert len(names) == 3


def test_t22_all_three_set_leaves_only_the_session(db: Path) -> None:
    env = {"LEDGER_AGENT": "a", "LEDGER_HARNESS": "h", "LEDGER_MODEL": "m"}
    done = hook_env(db, json.dumps(hook()), env=env)
    assert done.stdout == f"export LEDGER_SESSION={SESSION}\n"


def test_t22_the_session_line_is_printed_even_when_the_session_is_set(db: Path) -> None:
    done = hook_env(db, json.dumps(hook()), env={"LEDGER_SESSION": "an-older-session"})
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == f"export LEDGER_SESSION={SESSION}"


# --- T23: values --------------------------------------------------------------


def test_t23_a_model_object_is_stamped_by_its_id(db: Path) -> None:
    done = hook_env(db, json.dumps(hook(model={"id": "model-from-object",
                                                "display_name": "x"})))
    assert done.stdout.splitlines()[-1] == "export LEDGER_MODEL=model-from-object"


@pytest.mark.parametrize("model", [..., None, 7, "", "a\nb", {"id": 7}, {"name": "x"}, ["m"]],
                         ids=["missing", "null", "int", "empty", "newline", "id-int",
                              "no-id", "list"])
def test_t23_an_unusable_model_gets_no_line(db: Path, model) -> None:
    done = hook_env(db, json.dumps(hook(model=model)))
    assert (done.returncode, done.stderr) == (0, "")
    assert "LEDGER_MODEL" not in done.stdout
    assert len(done.stdout.splitlines()) == 3


@pytest.mark.parametrize("session", [..., None, 7, "", "a\nb", "a b", "\ud800"],
                         ids=["missing", "null", "int", "empty", "newline", "line-separator",
                              "lone-surrogate"])
def test_t23_an_unusable_session_id_gets_no_line(db: Path, session) -> None:
    done = hook_env(db, json.dumps(hook(session_id=session)))
    assert (done.returncode, done.stderr) == (0, "")
    assert "LEDGER_SESSION" not in done.stdout
    assert done.stdout.startswith("export LEDGER_AGENT=claude-code\n")


@pytest.mark.parametrize("session", ["it's", "$HOME", "back\\slash", "two words",
                                     "`date`", "a;b", "$(echo x)", "été"])
def test_t23_a_session_id_survives_the_shell_exactly(db: Path, tmp_path: Path,
                                                      session: str) -> None:
    done = hook_env(db, json.dumps(hook(session_id=session)))
    assert done.returncode == 0, done.stderr
    first = done.stdout.splitlines()[0]
    assert first.startswith("export LEDGER_SESSION=") and "$'" not in first
    assert sourced(done.stdout, tmp_path, "LEDGER_SESSION") == session


def test_t23_a_model_survives_the_shell_exactly(db: Path, tmp_path: Path) -> None:
    model = "model 'with' $odd \\chars"
    done = hook_env(db, json.dumps(hook(model=model)))
    assert sourced(done.stdout, tmp_path, "LEDGER_MODEL") == model


# --- T24: refusals ------------------------------------------------------------


@pytest.mark.parametrize("stdin", [
    b"not json", b"[]", b"\xff\xfe", b"", b'{"a": NaN}',
    b'{"hook_event_name": "SessionStart", "session_id": "s-\xff"}',
], ids=["not-json", "array", "not-utf8", "empty", "nan", "not-utf8-inside-an-object"])
def test_t24_malformed_stdin_is_refused_as_bad_input(db: Path, stdin: bytes) -> None:
    done = hook_env(db, stdin, "--json")
    assert done.returncode == 3
    envelope = json.loads(done.stdout)
    assert envelope["ok"] is False and envelope["error"]["code"] == "hook_env_bad_input"
    text = hook_env(db, stdin)
    assert (text.returncode, text.stdout) == (3, "")
    assert text.stderr.strip()
    assert not db.exists()


@pytest.mark.parametrize("event", ["UserPromptSubmit", "sessionstart", None, 7])
def test_t24_another_event_is_refused(db: Path, event) -> None:
    done = hook_env(db, json.dumps(hook(hook_event_name=event)), "--json")
    assert done.returncode == 3
    assert json.loads(done.stdout)["error"]["code"] == "hook_env_not_session_start"
    text = hook_env(db, json.dumps(hook(hook_event_name=event)))
    assert (text.returncode, text.stdout) == (3, "")
    assert text.stderr == "resume --hook-env takes a SessionStart hook input\n"


def test_t24_a_missing_event_name_is_refused(db: Path) -> None:
    done = hook_env(db, json.dumps(hook(hook_event_name=...)))
    assert (done.returncode, done.stdout) == (3, "")


# --- T25: usage and the database ----------------------------------------------


@pytest.mark.parametrize("extra", [("--brief",), ("--item", "X"), ("--item=",)],
                         ids=["brief", "item", "item-empty"])
def test_t25_hook_env_with_another_mode_is_a_usage_error(db: Path, extra) -> None:
    done = hook_env(db, json.dumps(hook()), *extra)
    assert (done.returncode, done.stdout) == (2, "")


def test_t25_a_database_in_a_missing_folder_is_never_created(tmp_path: Path) -> None:
    db = tmp_path / "no" / "such" / "folder" / "ledger.db"
    done = subprocess.run(
        [sys.executable, "-m", "ledger", "resume", "--hook-env"],
        input=json.dumps(hook()).encode("utf-8"),
        env={**{k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")},
             "LEDGER_DB": str(db), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, timeout=60, cwd=PKG_PARENT,
    )
    assert done.returncode == 0, done.stderr
    assert not (tmp_path / "no").exists()


def test_t25_hook_env_ignores_an_existing_database(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    db.write_bytes(b"not a sqlite file")
    done = hook_env(db, json.dumps(hook()))
    assert done.returncode == 0, done.stderr
    assert db.read_bytes() == b"not a sqlite file"
