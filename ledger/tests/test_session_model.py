"""The model stamp follows a mid-session model switch.

The plugin's SessionStart and PostModelSwitch hooks run ``ledger resume
--hook-model``, which records the session's model in one file per session
beside the database (``<db name>.session-models/<sha256(session)>.json``).
Every CLI write resolves its model once, from its own session's record, else
``LEDGER_MODEL``, else none. A record for another session or another database
is never read, an unusable record falls back to ``LEDGER_MODEL``, and an
``unknown`` record stamps no model.

Hook and write cases run the CLI as a subprocess, as the hook and an agent do;
cases that need to force the ordering clock, patch a system call or count
resolver calls run ``ledger.cli.main`` in-process.
"""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import threading
import time
import warnings
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from ledger import session_model
from ledger.cli import main
from ledger.export import export_events
from ledger.session_model import resolve_model
from ledger.store import open_ledger

PKG_PARENT = Path(__file__).resolve().parents[2]  # makes ``python -m ledger`` importable
PACKAGE = PKG_PARENT / "ledger"
S1, S2 = "sess-one", "sess-two"
M0, M1, M2 = "model-from-env", "model-start", "model-switched"
MISSING = object()


# --- fixtures and helpers -----------------------------------------------------


def H(session: str) -> str:
    return hashlib.sha256(session.encode("utf-8")).hexdigest()


def SM(db: Path) -> Path:
    return db.parent / (db.name + ".session-models")


def F(db: Path, session: str) -> Path:
    return SM(db) / (H(session) + ".json")


def rec(session: str, model, seq: int = 1, **changes) -> dict:
    record = {"session": session, "state": "model", "model": model,
              "event": "PostModelSwitch", "seq": seq}
    if seq is MISSING:
        record.pop("seq")
    for key, value in changes.items():
        if value is MISSING:
            record.pop(key, None)
        else:
            record[key] = value
    return record


def without(record: dict, key: str) -> dict:
    return {k: v for k, v in record.items() if k != key}


def unk(session: str, seq: int) -> dict:
    return rec(session, None, seq, state="unknown")


def env_rec(session: str, seq: int) -> dict:
    return rec(session, None, seq, state="env", event="SessionStart")


def put(db: Path, session: str, record: dict | bytes) -> Path:
    SM(db).mkdir(parents=True, exist_ok=True)
    path = F(db, session)
    data = record if isinstance(record, bytes) else json.dumps(record).encode("utf-8")
    path.write_bytes(data)
    return path


def stored(db: Path, session: str) -> dict:
    return json.loads(F(db, session).read_bytes())


def hook_input(event: str, session, field: str, value) -> dict:
    data = {
        "session_id": session,
        "transcript_path": "/tmp/fixture/transcript.jsonl",
        "cwd": "/tmp/fixture",
        "hook_event_name": event,
    }
    if event == "PostModelSwitch":
        data.update(from_model="model-before", requested_model="model-requested",
                    source="user", context_tokens=1200, prompt_cache_warm=True)
    else:
        data["source"] = "startup"
    if session is MISSING:
        data.pop("session_id")
    if value is not MISSING:
        data[field] = value
    return data


def start(session=S1, model=MISSING) -> dict:
    return hook_input("SessionStart", session, "model", model)


def switch(session=S1, to_model=MISSING) -> dict:
    return hook_input("PostModelSwitch", session, "to_model", to_model)


def base_env(**changes) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for key, value in changes.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def hookmodel(db: Path, hook: dict | bytes, *extra: str,
              env: dict | None = None) -> subprocess.CompletedProcess[str]:
    data = hook if isinstance(hook, bytes) else json.dumps(hook).encode("utf-8")
    done = subprocess.run(
        [sys.executable, "-m", "ledger", "resume", "--hook-model", *extra],
        input=data, env=base_env(LEDGER_DB=str(db), **(env or {})),
        capture_output=True, timeout=60, cwd=PKG_PARENT,
    )
    return subprocess.CompletedProcess(done.args, done.returncode,
                                       done.stdout.decode("utf-8"), done.stderr.decode("utf-8"))


def write(db: Path, **env) -> dict:
    """``ledger new`` as an agent runs it; the new event."""
    done = subprocess.run(
        [sys.executable, "-m", "ledger", "--json", "new", "--type", "gate", "--title", "t"],
        env=base_env(LEDGER_DB=str(db), LEDGER_HARNESS="claude-code", **env),
        capture_output=True, text=True, timeout=60, cwd=PKG_PARENT,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)["data"]["event"]


def cli(monkeypatch, argv: list[str], *, stdin: str | bytes = b"",
        env: dict | None = None) -> tuple[int, str, str]:
    """``ledger.cli.main`` in this process, with only ``env``'s LEDGER_ variables."""
    for key in [k for k in os.environ if k.startswith("LEDGER_")]:
        monkeypatch.delenv(key)
    for key, value in (env or {}).items():
        if value is not None:
            monkeypatch.setenv(key, value)
    data = stdin.encode("utf-8") if isinstance(stdin, str) else stdin
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data), encoding="utf-8"))
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def hookmodel_inproc(monkeypatch, db: Path, hook: dict, *, stamp: int | None = None,
                     json_mode: bool = True) -> tuple[int, str, str]:
    if stamp is not None:
        monkeypatch.setattr(session_model, "stamp", lambda: stamp)
    argv = ["--db", str(db), "resume", "--hook-model"] + (["--json"] if json_mode else [])
    return cli(monkeypatch, argv, stdin=json.dumps(hook))


def tmp_files(db: Path) -> list[str]:
    return sorted(p.name for p in SM(db).iterdir() if p.name.endswith(".tmp"))


def snapshot(folder: Path) -> dict:
    if not folder.exists():
        return {}
    return {p.name: p.read_bytes() for p in folder.iterdir()}


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "work.db"


@pytest.fixture
def db2(tmp_path: Path) -> Path:
    return tmp_path / "home.db"


# --- A: the ticket's test -----------------------------------------------------


def test_switch_mid_session_stamps_old_then_new(db: Path) -> None:
    assert hookmodel(db, start(S1, model=M1)).returncode == 0
    before = write(db, LEDGER_SESSION=S1)
    done = hookmodel(db, switch(S1, to_model=M2))
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    after = write(db, LEDGER_SESSION=S1)
    assert (before["model"], after["model"]) == (M1, M2)
    assert before["session"] == after["session"] == S1


def test_other_sessions_file_is_ignored(db: Path) -> None:
    assert hookmodel(db, switch(S2, to_model=M2)).returncode == 0
    assert write(db, LEDGER_SESSION=S1, LEDGER_MODEL=M0)["model"] == M0


def test_no_file_falls_back_to_env(db: Path) -> None:
    assert write(db, LEDGER_SESSION=S1, LEDGER_MODEL=M0)["model"] == M0
    assert not SM(db).exists()


def test_no_file_no_env_is_null(db: Path) -> None:
    assert write(db, LEDGER_SESSION=S1)["model"] is None
    assert not SM(db).exists()


def test_file_wins_over_env(db: Path) -> None:
    assert hookmodel(db, switch(S1, to_model=M2)).returncode == 0
    assert write(db, LEDGER_SESSION=S1, LEDGER_MODEL=M0)["model"] == M2


@pytest.mark.parametrize("session", [None, ""], ids=["unset", "empty"])
def test_session_unset_reads_no_file(db: Path, monkeypatch, session) -> None:
    put(db, S1, rec(S1, M2))
    put(db, "", rec("", M2))  # what a missing session hashed as "" would find
    opened: list[str] = []
    real_open = os.open

    def spy(path, *args, **kwargs):
        name = os.fspath(path)
        if ".session-models" in name or name.endswith((".json", ".lock")):
            opened.append(os.fspath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    code, out, err = cli(monkeypatch, ["--json", "--db", str(db), "new", "--type", "gate",
                                       "--title", "t"],
                         env={"LEDGER_HARNESS": "pytest", "LEDGER_MODEL": M0,
                              "LEDGER_SESSION": session})
    assert code == 0, err
    event = json.loads(out)["data"]["event"]
    assert (event["model"], event["session"]) == (M0, None)
    assert opened == []


# --- B: the reader's fallbacks ------------------------------------------------


def _fallback_cases() -> dict[str, bytes]:
    def enc(record) -> bytes:
        return json.dumps(record).encode("utf-8")

    return {
        "not-json": b'{"session":',
        "not-utf8": b"\xff\xfe",
        "not-object": enc(["M2"]),
        "nan": b'{"session": "sess-one", "state": "model", "model": "m", '
               b'"event": "PostModelSwitch", "seq": NaN}',
        "model-missing": enc(without(rec(S1, M2), "model")),
        "model-int": enc(rec(S1, 7)),
        "model-empty": enc(rec(S1, "")),
        "model-newline": enc(rec(S1, "a\nb")),
        "model-surrogate": b'{"session": "sess-one", "state": "model", "model": "\\ud800", '
                           b'"event": "PostModelSwitch", "seq": 1}',
        "model-257-bytes": enc(rec(S1, "m" * 257)),
        "state-model-with-null": enc(rec(S1, None)),
        "state-unknown-with-string": enc(rec(S1, M2, state="unknown")),
        "state-env-with-string": enc(rec(S1, M2, state="env")),
        "state-bad": enc(rec(S1, M2, state="Model")),
        "state-missing": enc(rec(S1, M2, state=MISSING)),
        "event-bad": enc(rec(S1, M2, event="UserPromptSubmit")),
        "event-missing": enc(rec(S1, M2, event=MISSING)),
        "session-missing": enc(without(rec(S1, M2), "session")),
        "extra-key": enc(dict(rec(S1, M2), extra=1)),
    }


@pytest.mark.parametrize("case", sorted(_fallback_cases()))
def test_reader_fallback(db: Path, case: str, capfd) -> None:
    put(db, S1, _fallback_cases()[case])
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert resolve_model(str(db), S1, M0) == M0
        assert resolve_model(str(db), S1, None) is None
    assert caught == []
    assert capfd.readouterr() == ("", "")


def test_reader_model_257_bytes_is_too_long_but_256_is_not(db: Path) -> None:
    put(db, S1, rec(S1, "é" * 128))  # 256 UTF-8 bytes
    assert resolve_model(str(db), S1, M0) == "é" * 128
    put(db, S1, rec(S1, "é" * 128 + "m"))  # 257
    assert resolve_model(str(db), S1, M0) == M0


def test_path_is_keyed_by_session(db: Path) -> None:
    db.parent.mkdir(exist_ok=True)
    assert hookmodel(db, switch(S2, to_model=M2)).returncode == 0
    assert hookmodel(db, switch(S1, to_model=M1)).returncode == 0
    assert {p.name for p in SM(db).glob("*.json")} == {H(S1) + ".json", H(S2) + ".json"}
    assert resolve_model(str(db), S1, M0) == M1
    assert resolve_model(str(db), S2, M0) == M2


def test_reader_content_session_must_match(db: Path) -> None:
    # Another session's record copied to this session's name; every other field valid.
    put(db, S1, rec(S2, M2))
    assert resolve_model(str(db), S1, M0) == M0


def test_reader_refuses_symlinked_file(db: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps(rec(S1, M2)))
    SM(db).mkdir()
    F(db, S1).symlink_to(elsewhere)
    assert resolve_model(str(db), S1, M0) == M0


def test_reader_refuses_symlinked_folder(db: Path, tmp_path: Path) -> None:
    real = tmp_path / "real-folder"
    real.mkdir()
    (real / (H(S1) + ".json")).write_text(json.dumps(rec(S1, M2)))
    SM(db).symlink_to(real)
    assert resolve_model(str(db), S1, M0) == M0


def test_reader_refuses_non_regular_file(db: Path) -> None:
    SM(db).mkdir()
    os.mkfifo(F(db, S1))
    code = ("import sys; from ledger.session_model import resolve_model; "
            "print(resolve_model(sys.argv[1], sys.argv[2], sys.argv[3]))")
    done = subprocess.run([sys.executable, "-c", code, str(db), S1, M0], env=base_env(),
                          capture_output=True, text=True, timeout=5, cwd=PKG_PARENT)
    assert (done.returncode, done.stdout, done.stderr) == (0, M0 + "\n", "")


def test_reader_refuses_oversize_file(db: Path) -> None:
    text = json.dumps(rec(S1, M2))
    put(db, S1, (text + " " * (4097 - len(text))).encode("utf-8"))
    assert F(db, S1).stat().st_size == 4097
    assert resolve_model(str(db), S1, M0) == M0
    put(db, S1, (text + " " * (4096 - len(text))).encode("utf-8"))
    assert resolve_model(str(db), S1, M0) == M2


@pytest.mark.parametrize("failing", ["open", "read"])
def test_reader_unreadable_file(db: Path, monkeypatch, failing: str) -> None:
    # Mode 000 does not stop root, so the refusal comes from the system call:
    # opening the record fails (EACCES) or reading it does (EIO).
    put(db, S1, rec(S1, M2))
    real = getattr(os, failing)

    def fail(*args, **kwargs):
        if failing == "open" and str(args[0]).endswith(".json"):
            raise PermissionError(13, "Permission denied")
        if failing == "read":
            raise OSError(5, "Input/output error")
        return real(*args, **kwargs)

    monkeypatch.setattr(os, failing, fail)
    assert resolve_model(str(db), S1, M0) == M0


def test_reader_unresolvable_db_path(db: Path, tmp_path: Path) -> None:
    real = tmp_path / "real.db"
    real.write_bytes(b"")
    db.symlink_to(real)
    put(db, S1, rec(S1, M2))
    assert resolve_model(str(db), S1, M0) == M0
    args = [sys.executable, "-m", "ledger", "new", "--type", "gate", "--title", "t"]
    plain = subprocess.run(args, env=base_env(LEDGER_DB=str(db), LEDGER_HARNESS="pytest"),
                           capture_output=True, text=True, timeout=60, cwd=PKG_PARENT)
    with_session = subprocess.run(
        args, env=base_env(LEDGER_DB=str(db), LEDGER_HARNESS="pytest", LEDGER_SESSION=S1),
        capture_output=True, text=True, timeout=60, cwd=PKG_PARENT)
    assert plain.returncode == with_session.returncode == 3
    assert "symbolic link" in plain.stderr and with_session.stderr == plain.stderr
    # A verb that never resolves the database is not refused by the resolver.
    env_lines = subprocess.run(
        [sys.executable, "-m", "ledger", "resume", "--hook-env"],
        input=json.dumps(start(S1, model=M1)),
        env=base_env(LEDGER_DB=str(db), LEDGER_SESSION=S1),
        capture_output=True, text=True, timeout=60, cwd=PKG_PARENT)
    assert (env_lines.returncode, env_lines.stderr) == (0, "")


def test_reader_valid_file(db: Path) -> None:
    put(db, S1, rec(S1, M2))
    assert resolve_model(str(db), S1, M0) == M2


def test_reader_ignores_tmp_files(db: Path) -> None:
    SM(db).mkdir()
    (SM(db) / f".{H(S1)}.abc.tmp").write_text(json.dumps(rec(S1, M2)))
    assert resolve_model(str(db), S1, M0) == M0


def test_two_databases_one_folder_keep_separate_state(db: Path, db2: Path) -> None:
    assert hookmodel(db, switch(S1, to_model=M2)).returncode == 0
    assert resolve_model(str(db2), S1, M0) == M0
    assert write(db2, LEDGER_SESSION=S1, LEDGER_MODEL=M0)["model"] == M0
    assert not SM(db2).exists()
    assert F(db, S1).is_file()
    assert resolve_model(str(db), S1, M0) == M2


def test_tombstone_shadows_env(db: Path) -> None:
    put(db, S1, unk(S1, 5))
    assert resolve_model(str(db), S1, M0) is None
    assert write(db, LEDGER_SESSION=S1, LEDGER_MODEL=M0)["model"] is None


@pytest.mark.parametrize("seq", [MISSING, -1, "5", 1.5, True, None],
                         ids=["missing", "negative", "string", "float", "true", "null"])
def test_reader_rejects_bad_seq(db: Path, seq) -> None:
    put(db, S1, rec(S1, M2, seq=seq))
    assert resolve_model(str(db), S1, M0) == M0


def test_reset_record_falls_back_to_env(db: Path) -> None:
    put(db, S1, env_rec(S1, 5))
    assert resolve_model(str(db), S1, M0) == M0
    assert resolve_model(str(db), S1, None) is None


# --- C: the hook's grammar ----------------------------------------------------


def test_hook_records_switch(db: Path) -> None:
    put(db, S1, rec(S1, M1, seq=1))
    done = hookmodel(db, switch(S1, to_model=M2))
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    record = stored(db, S1)
    assert type(record["seq"]) is int and record["seq"] > 1
    assert record == rec(S1, M2, seq=record["seq"])


def test_hook_records_start(db: Path) -> None:
    done = hookmodel(db, start(S1, model=M1), "--json")
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["data"] == {"recorded": M1, "state": "model", "stale": False}
    record = stored(db, S1)
    assert record == {"session": S1, "state": "model", "model": M1, "event": "SessionStart",
                      "seq": record["seq"]}
    assert stat.S_IMODE(SM(db).stat().st_mode) == 0o700
    assert stat.S_IMODE(F(db, S1).stat().st_mode) == 0o600


@pytest.mark.parametrize("model", [MISSING, None], ids=["absent", "null"])
@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_hook_start_without_model_writes_reset_record(db: Path, model, json_mode) -> None:
    put(db, S1, rec(S1, M2, seq=1))
    done = hookmodel(db, start(S1, model=model), *(["--json"] if json_mode else []))
    assert (done.returncode, done.stderr) == (0, "")
    if json_mode:
        assert json.loads(done.stdout) == {
            "data": {"recorded": None, "state": "env", "stale": False}, "error": None,
            "ok": True}
    else:
        assert done.stdout == ""
    record = stored(db, S1)
    assert record == {"session": S1, "state": "env", "model": None, "event": "SessionStart",
                      "seq": record["seq"]}
    assert write(db, LEDGER_SESSION=S1, LEDGER_MODEL=M0)["model"] == M0


BAD_MODELS = {
    "missing": MISSING, "null": None, "int": 7, "object": {"id": M2}, "empty": "",
    "newline": "a\nb", "surrogate": "\ud800", "257-bytes": "m" * 257,
}


@pytest.mark.parametrize("case", sorted(BAD_MODELS))
def test_hook_bad_to_model_clears_then_refuses(db: Path, case: str) -> None:
    put(db, S1, rec(S1, M1, seq=1))
    value = BAD_MODELS[case]
    done = hookmodel(db, switch(S1, to_model=value))
    assert (done.returncode, done.stdout) == (3, "")
    assert "to_model" in done.stderr
    assert M2 not in done.stderr and "m" * 20 not in done.stderr
    record = stored(db, S1)
    assert record["seq"] > 1 and record == unk(S1, record["seq"])
    assert write(db, LEDGER_SESSION=S1, LEDGER_MODEL=M0)["model"] is None
    done = hookmodel(db, switch(S1, to_model=value), "--json")
    assert done.returncode == 3
    assert json.loads(done.stdout)["error"]["code"] == "hook_model_bad_model"


@pytest.mark.parametrize("model", [{"id": M1}, 7, "", "a\nb"],
                         ids=["object", "number", "empty", "newline"])
def test_hook_bad_start_model_refuses(db: Path, model) -> None:
    put(db, S1, rec(S1, M2, seq=1))
    done = hookmodel(db, start(S1, model=model), "--json")
    assert done.returncode == 3
    error = json.loads(done.stdout)["error"]
    assert error["code"] == "hook_model_bad_model"
    assert error["message"].startswith("the input's model is not")
    assert M1 not in error["message"]
    record = stored(db, S1)
    assert record == unk(S1, record["seq"]) | {"event": "SessionStart"}
    assert write(db, LEDGER_SESSION=S1, LEDGER_MODEL=M0)["model"] is None


@pytest.mark.parametrize("session", [MISSING, None, 7, "", "a\nb", "a b", "\ud800",
                                     "s" * 5000],
                         ids=["missing", "null", "int", "empty", "newline", "line-separator",
                              "lone-surrogate", "too-long"])
def test_hook_bad_session_id(db: Path, session) -> None:
    put(db, S1, rec(S1, M1))
    before = snapshot(SM(db))
    done = hookmodel(db, switch(session, to_model=M2), "--json")
    assert done.returncode == 3
    assert json.loads(done.stdout)["error"]["code"] == "hook_model_bad_input"
    text = hookmodel(db, switch(session, to_model=M2))
    assert (text.returncode, text.stdout) == (3, "")
    assert snapshot(SM(db)) == before


@pytest.mark.parametrize("event", ["UserPromptSubmit", MISSING, "postmodelswitch", 7, None],
                         ids=["other-event", "missing", "lowercase", "int", "null"])
def test_hook_wrong_event(db: Path, event) -> None:
    put(db, S1, rec(S1, M1))
    before = snapshot(SM(db))
    hook = switch(S1, to_model=M2)
    if event is MISSING:
        hook.pop("hook_event_name")
    else:
        hook["hook_event_name"] = event
    done = hookmodel(db, hook, "--json")
    assert done.returncode == 3
    assert json.loads(done.stdout)["error"]["code"] == "hook_model_wrong_event"
    text = hookmodel(db, hook)
    assert (text.returncode, text.stdout) == (3, "")
    assert snapshot(SM(db)) == before


@pytest.mark.parametrize("stdin", [b"not json", b"[]", b'"x"', b"\xff", b"", b'{"a": NaN}'],
                         ids=["not-json", "array", "string", "not-utf8", "empty", "nan"])
def test_hook_bad_stdin(db: Path, stdin: bytes) -> None:
    done = hookmodel(db, stdin, "--json")
    assert done.returncode == 3, done.stderr
    assert json.loads(done.stdout)["error"]["code"] == "hook_model_bad_input"
    assert "Traceback" not in done.stderr
    text = hookmodel(db, stdin)
    assert (text.returncode, text.stdout) == (3, "")
    assert "Traceback" not in text.stderr
    assert not SM(db).exists()


@pytest.mark.parametrize("ledger_session", [None, S2, S1], ids=["unset", "other", "same"])
def test_hook_ignores_ledger_session_env(db: Path, ledger_session) -> None:
    done = hookmodel(db, switch(S1, to_model=M2), env={"LEDGER_SESSION": ledger_session})
    assert done.returncode == 0, done.stderr
    assert stored(db, S1)["model"] == M2
    assert not F(db, S2).exists()


def test_hook_never_opens_database(db: Path) -> None:
    done = hookmodel(db, switch(S1, to_model=M2))
    assert done.returncode == 0, done.stderr
    for name in (db.name, db.name + "-wal", db.name + "-shm", db.name + "-journal"):
        assert not (db.parent / name).exists(), name
    assert stored(db, S1)["model"] == M2


def test_hook_missing_db_folder(tmp_path: Path) -> None:
    db = tmp_path / "absent" / "ledger.db"
    done = hookmodel(db, switch(S1, to_model=M2), "--json")
    assert done.returncode == 3
    assert json.loads(done.stdout)["error"]["code"] == "hook_model_no_folder"
    assert not (tmp_path / "absent").exists()


@pytest.mark.parametrize("kind", ["symlink", "file", "dangling-symlink"])
def test_hook_unsafe_folder(db: Path, tmp_path: Path, kind: str) -> None:
    target = tmp_path / "target-folder"
    if kind == "symlink":
        target.mkdir()
        SM(db).symlink_to(target)
    elif kind == "dangling-symlink":
        SM(db).symlink_to(target)
    else:
        SM(db).write_bytes(b"a file")
    done = hookmodel(db, switch(S1, to_model=M2), "--json")
    assert done.returncode == 3
    assert json.loads(done.stdout)["error"]["code"] == "hook_model_unsafe_folder"
    if kind == "symlink":
        assert list(target.iterdir()) == []
    else:
        assert not target.exists()


@pytest.mark.parametrize("extra", [("--hook-env",), ("--brief",), ("--item", "X"), ("--item=",)],
                         ids=["hook-env", "brief", "item", "item-empty"])
def test_hook_flag_exclusive(db: Path, extra) -> None:
    done = hookmodel(db, switch(S1, to_model=M2), *extra)
    assert (done.returncode, done.stdout) == (2, "")
    assert not SM(db).exists()


@pytest.mark.parametrize("json_mode", [False, True], ids=["text", "json"])
def test_hook_bad_model_value_never_echoed(db: Path, json_mode: bool) -> None:
    done = hookmodel(db, switch(S1, to_model="secret\u0007value"),
                     *(["--json"] if json_mode else []))
    assert done.returncode == 3
    assert "secret" not in done.stdout and "secret" not in done.stderr


# --- D: atomicity and ordering ------------------------------------------------


def test_write_is_replace_not_truncate(db: Path, monkeypatch) -> None:
    put(db, S1, rec(S1, M1))
    replaced: list[tuple[str, str]] = []
    written_targets: list[str] = []
    real_replace, real_open = os.replace, os.open

    def spy_replace(src, dst, *args, **kwargs):
        replaced.append((os.fspath(src), os.fspath(dst)))
        return real_replace(src, dst, *args, **kwargs)

    def spy_open(path, flags, *args, **kwargs):
        if os.fspath(path).endswith(H(S1) + ".json") and flags & (os.O_WRONLY | os.O_RDWR):
            written_targets.append(os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "replace", spy_replace)
    monkeypatch.setattr(os, "open", spy_open)
    code, _out, err = hookmodel_inproc(monkeypatch, db, switch(S1, to_model=M2))
    assert code == 0, err
    [(src, dst)] = replaced
    assert re.fullmatch(rf"\.{H(S1)}\.[0-9a-f]+\.tmp", src)
    assert dst == H(S1) + ".json"
    assert written_targets == []
    assert stored(db, S1)["model"] == M2


def test_reader_never_sees_torn_value(db: Path) -> None:
    db.parent.mkdir(exist_ok=True)
    long_model = "M2-long-" + "x" * 192
    session_model.record_hook_model(str(db), S1, "model", M1, "SessionStart",
                                    session_model.stamp())
    stop = threading.Event()
    seen: dict[str, int] = {}
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            flip = False
            while not stop.is_set():
                model = long_model if flip else M1
                flip = not flip
                session_model.record_hook_model(str(db), S1, "model", model, "SessionStart",
                                                session_model.stamp())
        except BaseException as exc:  # surfaced below
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    deadline = time.monotonic() + 3
    try:
        while time.monotonic() < deadline:
            value = resolve_model(str(db), S1, M0)
            seen[value] = seen.get(value, 0) + 1
    finally:
        stop.set()
        thread.join()
    assert errors == []
    # The record exists throughout and is replaced whole, so every read is one
    # of the two models: never the fallback, never a mix.
    assert set(seen) == {M1, long_model}, seen


def test_failed_write_leaves_no_tmp(db: Path, monkeypatch) -> None:
    put(db, S1, rec(S1, M1))
    before = F(db, S1).read_bytes()

    def fail(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", fail)
    code, out, _err = hookmodel_inproc(monkeypatch, db, switch(S1, to_model=M2))
    assert code == 3
    assert json.loads(out)["error"]["code"] == "hook_model_write_failed"
    assert F(db, S1).read_bytes() == before
    assert tmp_files(db) == []


def test_stale_switch_completion_is_dropped(db: Path, monkeypatch) -> None:
    put(db, S1, rec(S1, M2, seq=200))
    before = F(db, S1).read_bytes()
    code, out, err = hookmodel_inproc(monkeypatch, db, switch(S1, to_model=M1), stamp=100)
    assert (code, err) == (0, "")
    assert json.loads(out)["data"] == {"recorded": None, "state": None, "stale": True}
    assert F(db, S1).read_bytes() == before


def test_stale_tombstone_is_dropped(db: Path, monkeypatch) -> None:
    put(db, S1, rec(S1, M2, seq=200))
    before = F(db, S1).read_bytes()
    code, out, err = hookmodel_inproc(monkeypatch, db, switch(S1, to_model=7), stamp=100)
    assert (code, err) == (0, "")
    assert json.loads(out)["data"] == {"recorded": None, "state": None, "stale": True}
    assert F(db, S1).read_bytes() == before


def test_reset_record_blocks_delayed_pre_reset_switch(db: Path, monkeypatch) -> None:
    put(db, S1, rec(S1, M1, seq=100))
    code, _out, err = hookmodel_inproc(monkeypatch, db, start(S1), stamp=300)
    assert code == 0, err
    assert stored(db, S1) == env_rec(S1, 300)
    code, out, err = hookmodel_inproc(monkeypatch, db, switch(S1, to_model=M2), stamp=200)
    assert code == 0, err
    assert json.loads(out)["data"]["stale"] is True
    assert stored(db, S1) == env_rec(S1, 300)
    assert resolve_model(str(db), S1, M0) == M0


def test_session_start_always_wins(db: Path, monkeypatch) -> None:
    put(db, S1, rec(S1, M2, seq=10**18))  # from before a reboot
    code, _out, err = hookmodel_inproc(monkeypatch, db, start(S1, model=M1), stamp=5)
    assert code == 0, err
    assert stored(db, S1) == rec(S1, M1, seq=5, event="SessionStart")
    code, out, err = hookmodel_inproc(monkeypatch, db, switch(S1, to_model="model-b"), stamp=6)
    assert code == 0, err
    assert json.loads(out)["data"] == {"recorded": "model-b", "state": "model", "stale": False}
    assert stored(db, S1) == rec(S1, "model-b", seq=6)


def test_compare_and_swap_holds_the_lock(db: Path) -> None:
    # The test plays writer A (stamp 100): it holds the lock, reads, and only
    # then publishes. Writer B, a real hook process started meanwhile, must
    # wait for the lock, then see A's older record and replace it.
    put(db, S1, rec(S1, M1, seq=50))
    lock = os.open(SM(db) / (H(S1) + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        b = subprocess.Popen(
            [sys.executable, "-m", "ledger", "resume", "--hook-model"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=base_env(LEDGER_DB=str(db)), cwd=PKG_PARENT)
        b.stdin.write(json.dumps(switch(S1, to_model=M2)).encode("utf-8"))
        b.stdin.close()
        time.sleep(1.2)
        assert stored(db, S1) == rec(S1, M1, seq=50), "B wrote while A held the lock"
        tmp = SM(db) / "a-writer.tmp"
        tmp.write_text(json.dumps(rec(S1, "model-a", seq=100)))
        os.replace(tmp, F(db, S1))
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        os.close(lock)
    out, err = b.communicate(timeout=30)
    assert b.returncode == 0, err
    record = stored(db, S1)
    assert record["model"] == M2 and record["seq"] > 100


def _hold_lock(db: Path, seconds: float) -> threading.Thread:
    SM(db).mkdir(exist_ok=True)
    held = threading.Event()

    def hold() -> None:
        fd = os.open(SM(db) / (H(S1) + ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            held.set()
            time.sleep(seconds)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert held.wait(10)
    return thread


def test_hook_refuses_past_its_budget(db: Path, monkeypatch) -> None:
    put(db, S1, rec(S1, M1))
    before = F(db, S1).read_bytes()
    holder = _hold_lock(db, 4)
    try:
        began = time.monotonic()
        code, out, _err = hookmodel_inproc(monkeypatch, db, switch(S1, to_model=M2))
        elapsed = time.monotonic() - began
    finally:
        holder.join()
    assert code == 3
    assert json.loads(out)["error"]["code"] == "hook_model_timeout"
    assert elapsed < 2.5, elapsed
    assert F(db, S1).read_bytes() == before
    assert tmp_files(db) == []


def test_hook_refuses_a_publish_past_its_budget(db: Path, monkeypatch) -> None:
    # No lock contention: closing the temporary file is what runs past the
    # budget, so only the check before the rename can refuse.
    put(db, S1, rec(S1, M1))
    before = F(db, S1).read_bytes()
    temp_fds: set[int] = set()
    real_open, real_close = os.open, os.close

    def spy_open(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if os.fspath(path).endswith(".tmp"):
            temp_fds.add(fd)
        return fd

    def slow_close(fd):
        if fd in temp_fds:
            temp_fds.discard(fd)
            time.sleep(session_model.HOOK_BUDGET_NS / 1e9 + 0.1)
        return real_close(fd)

    monkeypatch.setattr(os, "open", spy_open)
    monkeypatch.setattr(os, "close", slow_close)
    code, out, _err = hookmodel_inproc(monkeypatch, db, switch(S1, to_model=M2))
    monkeypatch.undo()
    assert code == 3
    assert json.loads(out)["error"]["code"] == "hook_model_timeout"
    assert F(db, S1).read_bytes() == before
    assert tmp_files(db) == []


def test_one_invocation_one_model(db: Path, monkeypatch) -> None:
    code, _out, err = cli(monkeypatch, ["--db", str(db), "new", "--id", "it-1", "--type",
                                        "ticket", "--title", "t"],
                          env={"LEDGER_HARNESS": "pytest"})
    assert code == 0, err
    put(db, S1, rec(S1, M1, seq=1))
    calls: list[str | None] = []
    import ledger.cli as cli_module

    def wrapped(*args):
        result = resolve_model(*args)
        calls.append(result)
        put(db, S1, rec(S1, M2, seq=2))  # a switch lands mid-invocation
        return result

    monkeypatch.setattr(cli_module, "resolve_model", wrapped)
    batch = [{"kind": "item.note", "payload": {"text": f"n{i}"}} for i in range(3)]
    code, out, err = cli(monkeypatch, ["--json", "--db", str(db), "append-many", "it-1"],
                         stdin=json.dumps(batch),
                         env={"LEDGER_HARNESS": "pytest", "LEDGER_SESSION": S1})
    assert code == 0, err
    assert calls == [M1]
    with open_ledger(db, create=False) as lg:
        notes = [e for e in lg.events(item_id="it-1") if e.kind == "item.note"]
    assert [e.model for e in notes] == [M1, M1, M1]


# --- E: every writer, and restore ---------------------------------------------


ITEM = "it-writer"
SETUP_ENV = {"LEDGER_HARNESS": "pytest", "LEDGER_MODEL": "setup-model"}
TEST_ENV = {"LEDGER_HARNESS": "pytest", "LEDGER_MODEL": M0, "LEDGER_SESSION": S1}


def _ok(monkeypatch, argv, *, stdin: str = "", env=SETUP_ENV) -> dict:
    code, out, err = cli(monkeypatch, ["--json", *argv], stdin=stdin, env=env)
    assert code == 0, (argv, out, err)
    return json.loads(out)["data"]


def _ask(monkeypatch, db: str) -> int:
    payload = {"question": "Ship it?", "scope": "this item",
               "options": [{"label": "ship", "description": "ship it"}, {"label": "hold"}]}
    asked = _ok(monkeypatch, ["--db", db, "append", "--item", ITEM, "--kind", "decision.asked",
                              "--payload", json.dumps(payload)])["event"]["id"]
    questions = _ok(monkeypatch, ["--db", db, "ask-display", str(asked)])["input"]["questions"]
    hook = {"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion",
            "tool_use_id": "toolu-1", "tool_input": {"questions": questions},
            "tool_response": {"answers": {questions[0]["question"]: "ship"}}}
    _ok(monkeypatch, ["--db", db, "capture", "--kind", "picker"], stdin=json.dumps(hook))
    return asked


def _writer_case(verb: str, monkeypatch, db: str, tmp_path: Path) -> tuple[list[str], str]:
    """Set up ``verb``'s preconditions; the argv and stdin of one write by it."""
    if verb == "new":
        return ["new", "--type", "ticket", "--title", "written"], ""
    if verb == "append":
        return ["append", "--item", ITEM, "--kind", "utterance.captured",
                "--payload", json.dumps({"text": "words"})], ""
    if verb == "append-many":
        batch = [{"kind": "item.note", "payload": {"text": t}} for t in ("a", "b")]
        return ["append-many", ITEM], json.dumps(batch)
    if verb == "note":
        return ["note", ITEM, "--text", "a note"], ""
    if verb == "link":
        return ["link", ITEM, "--rel", "url", "--target", "https://example.com/x"], ""
    if verb == "state":
        return ["state", ITEM, "--status", "blocked"], ""
    if verb == "spec":
        return ["spec", "set", ITEM, "--title", "Spec", "--body", "body"], ""
    if verb == "doc":
        path = tmp_path / "attached.md"
        path.write_text("a document\n", encoding="utf-8")
        return ["doc", "add", ITEM, str(path), "--role", "brief", "--title", "d"], ""
    if verb == "elect":
        quote_ref = _ok(monkeypatch, ["--db", db, "append", "--item", ITEM, "--kind",
                                      "utterance.captured", "--payload",
                                      json.dumps({"text": "use backoff"})])["event"]["id"]
        return ["elect", ITEM, "--question", "Which?", "--chosen", "backoff",
                "--chosen-description", "backoff", "--rejected", "fixed", "--scope", "here",
                "--quote-ref", str(quote_ref), "--quote", "use backoff"], ""
    if verb == "pick":
        return ["pick", ITEM, "--ask", str(_ask(monkeypatch, db))], ""
    if verb == "entity":
        return ["entity", "register", "automation", "job-1", "--title", "Job"], ""
    if verb == "claim":
        return ["claim", ITEM], ""
    if verb == "claim-system":
        return ["claim-system", "home"], ""
    if verb == "guidance":
        return ["guidance", "set", "--pool", "pool-a", "--direction", "burn", "--reason", "r",
                "--starts", "2026-10-19", "--expires", "2026-10-25"], ""
    if verb == "capture":
        hook = {"hook_event_name": "UserPromptSubmit", "prompt": "hello there",
                "session_id": S1, "prompt_id": "p-1"}
        return ["capture", "--kind", "prompt"], json.dumps(hook)
    raise AssertionError(verb)


WRITER_VERBS = ["new", "append", "append-many", "note", "link", "state", "spec", "doc",
                "elect", "pick", "entity", "claim", "claim-system", "guidance", "capture"]


@pytest.mark.parametrize("verb", WRITER_VERBS)
def test_every_writer_stamps_resolved_model(verb: str, tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    _ok(monkeypatch, ["--db", str(db), "new", "--id", ITEM, "--type", "ticket", "--title", "x"])
    argv, stdin = _writer_case(verb, monkeypatch, str(db), tmp_path)
    with open_ledger(db, create=False) as lg:
        last = max(e.id for e in lg.events())
    put(db, S1, rec(S1, M2))
    _ok(monkeypatch, ["--db", str(db), *argv], stdin=stdin, env=TEST_ENV)
    with open_ledger(db, create=False) as lg:
        written = [e for e in lg.events() if e.id > last]
    assert written, verb
    assert [(e.kind, e.model) for e in written] == [(e.kind, M2) for e in written]


def test_every_writer_verb_is_covered() -> None:
    from ledger.verbs import discover_verbs

    writers = {name for name, verb in discover_verbs().items() if verb.writes}
    # doc writes only through ``doc add``; restore has its own case below.
    assert writers | {"doc", "restore"} == set(WRITER_VERBS) | {"restore"}


ENV_READ = re.compile(r"""(_env|environ\.get|environ|getenv)\s*[(\[]\s*["']LEDGER_MODEL["']""")


def test_actor_kwargs_has_one_source() -> None:
    reads = []
    for path in sorted(PACKAGE.rglob("*.py")):
        if "tests" in path.relative_to(PACKAGE).parts:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ENV_READ.search(line):
                reads.append((str(path.relative_to(PACKAGE)), line.strip()))
    assert reads == [("cli.py", 'model=resolve_model(db, session, _env("LEDGER_MODEL")),')]


def test_restore_keeps_recorded_models(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.db"
    with open_ledger(source) as lg:
        for i in range(2):
            lg.append(kind="item.created", payload={"type": "ticket", "title": f"t{i}"},
                      item_id=f"item-{i}", idempotency_key=f"key-{i}", harness="pytest",
                      actor="tester", model=M1)
        export_events(lg, tmp_path / "export")
    dest = tmp_path / "restored.db"
    put(dest, S1, rec(S1, M2))
    _ok(monkeypatch, ["--db", str(dest), "restore", "--dir", str(tmp_path / "export"),
                      "--system", "home"], env=TEST_ENV)
    with open_ledger(dest, create=False) as lg:
        events = lg.events()
    replayed = [e for e in events if e.kind == "item.created"]
    own = [e for e in events if e.kind != "item.created"]
    assert [e.model for e in replayed] == [M1, M1]
    assert own and all(e.model == M2 for e in own)
