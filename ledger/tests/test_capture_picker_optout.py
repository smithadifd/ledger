"""``ledger capture --kind picker`` honours the cwd opt-out that prompt capture does.

A picker answer whose hook ``cwd`` is at or under a path in
``capture_optout_paths`` (the ``config.json`` beside the database) writes
nothing and prints ``skipped<TAB>cwd_optout``, by the rule prompt capture
uses: links and ``..`` resolve, a match ends at a path separator, and an
existing opted-out folder is also matched by file identity, so another
spelling of it opts out where the filesystem treats that spelling as the same
folder. When the identity cannot be read, the answer is skipped as
``cwd_unverified``. An invalid ``capture_optout_paths``, or a config that
cannot be read, refuses ``capture_config`` (exit 3) and writes nothing.
``capture_prompts`` and ``capture_skip_prefixes`` never apply to a picker
answer. The picker input is checked first, so every picker refusal still
stands, and every skip is decided before the database is opened, so none
creates it. Each case runs the CLI as a hook does: a subprocess with the hook
input on stdin.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ledger.store import open_ledger

PKG_PARENT = Path(__file__).resolve().parents[2]  # makes ``python -m ledger`` importable
QUESTION = "Which folder holds the private notes?"
ANSWER = "the one nobody else should read"
NFC_NAME = "café"
NFD_NAME = "café"


def run(db: Path, *argv: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env.update(LEDGER_HARNESS="pytest", LEDGER_DB=str(db), PYTHONDONTWRITEBYTECODE="1")
    done = subprocess.run([sys.executable, "-m", "ledger", *argv], input=stdin.encode("utf-8"),
                          env=env, capture_output=True, timeout=60, cwd=PKG_PARENT)
    return subprocess.CompletedProcess(done.args, done.returncode,
                                       done.stdout.decode("utf-8"), done.stderr.decode("utf-8"))


def picker_hook(cwd=None, tool_use_id: str = "toolu_picker_optout", **extra) -> str:
    hook = {
        "session_id": "sess-picker-optout",
        "hook_event_name": "PostToolUse",
        "tool_name": "AskUserQuestion",
        "tool_use_id": tool_use_id,
        "tool_input": {"questions": [{"question": QUESTION,
                                      "options": [{"label": ANSWER}, {"label": "elsewhere"}]}]},
        "tool_response": {"answers": {QUESTION: ANSWER}},
    }
    if cwd is not None:
        hook["cwd"] = cwd if not isinstance(cwd, Path) else str(cwd)
    hook.update(extra)
    return json.dumps(hook)


def capture_picker(db: Path, cwd=None, **extra) -> subprocess.CompletedProcess[str]:
    return run(db, "capture", "--kind", "picker", stdin=picker_hook(cwd, **extra))


def capture_picker_json(db: Path, cwd=None, **extra) -> tuple[int, dict]:
    done = run(db, "--json", "capture", "--kind", "picker", stdin=picker_hook(cwd, **extra))
    return done.returncode, json.loads(done.stdout)


def picker_events(db: Path) -> list:
    if not db.exists():
        return []
    with open_ledger(db, create=False) as lg:
        return [e for e in lg.events(kind="utterance.captured") if e.channel == "hook:picker"]


def write_config(db: Path, config) -> Path:
    db.parent.mkdir(parents=True, exist_ok=True)
    path = db.parent / "config.json"
    path.write_text(config if isinstance(config, str) else json.dumps(config), encoding="utf-8")
    return path


def opt_out(db: Path, *paths: Path | str, **more) -> Path:
    return write_config(db, {"capture_optout_paths": [str(p) for p in paths], **more})


def assert_skipped(done: subprocess.CompletedProcess[str], reason: str, db: Path) -> None:
    assert (done.returncode, done.stdout, done.stderr) == (0, f"skipped\t{reason}\n", "")
    assert picker_events(db) == []


def assert_captured(done: subprocess.CompletedProcess[str], db: Path) -> None:
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("captured\thook:picker\t"), done.stdout
    [event] = picker_events(db)
    assert event.payload["picker"]["chosen_label"] == ANSWER


def assert_refused(done: subprocess.CompletedProcess[str], db: Path) -> None:
    assert (done.returncode, done.stdout) == (3, ""), done.stderr
    assert ANSWER not in done.stderr
    assert not db.exists()


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "data" / "ledger.db"


@pytest.fixture
def optout_dir(tmp_path: Path) -> Path:
    path = tmp_path / "optout"
    (path / "a-project").mkdir(parents=True)
    return path


# --- what opts out ----------------------------------------------------------------


@pytest.mark.parametrize("where", ["equal", "under", "deep", "not-yet-made", "trailing-sep"])
def test_a_picker_answer_at_or_under_an_optout_path_writes_nothing(db: Path, optout_dir: Path,
                                                                   where: str) -> None:
    opt_out(db, optout_dir)
    cwd = {
        "equal": str(optout_dir),
        "under": str(optout_dir / "a-project"),
        "deep": str(optout_dir / "a-project" / "src" / "pkg"),
        "not-yet-made": str(optout_dir / "no-such-project" / "x"),
        "trailing-sep": str(optout_dir) + os.sep,
    }[where]
    assert_skipped(capture_picker(db, cwd=cwd), "cwd_optout", db)
    assert not db.exists()


def test_a_picker_cwd_optout_skip_in_json(db: Path, optout_dir: Path) -> None:
    opt_out(db, optout_dir)
    code, env = capture_picker_json(db, cwd=optout_dir / "a-project")
    assert code == 0
    assert env == {"data": {"event": None, "skipped": "cwd_optout"}, "error": None, "ok": True}
    assert not db.exists()


def test_an_existing_ledger_gains_no_picker_answer_from_an_opted_out_cwd(db: Path,
                                                                        optout_dir: Path,
                                                                        tmp_path: Path) -> None:
    assert_captured(capture_picker(db, cwd=tmp_path, tool_use_id="toolu_before"), db)
    opt_out(db, optout_dir)
    done = capture_picker(db, cwd=optout_dir / "a-project", tool_use_id="toolu_after")
    assert (done.returncode, done.stdout) == (0, "skipped\tcwd_optout\n")
    assert [e.payload["picker"]["tool_use_id"] for e in picker_events(db)] == ["toolu_before"]


def test_a_symbolic_link_into_an_optout_path_opts_a_picker_answer_out(db: Path, optout_dir: Path,
                                                                     tmp_path: Path) -> None:
    link = tmp_path / "shortcut"
    link.symlink_to(optout_dir / "a-project", target_is_directory=True)
    opt_out(db, optout_dir)
    assert_skipped(capture_picker(db, cwd=link), "cwd_optout", db)
    assert_skipped(capture_picker(db, cwd=link / "sub"), "cwd_optout", db)
    assert not db.exists()


def test_a_dotdot_path_into_an_optout_path_opts_a_picker_answer_out(db: Path, optout_dir: Path,
                                                                   tmp_path: Path) -> None:
    (tmp_path / "elsewhere").mkdir()
    opt_out(db, optout_dir)
    cwd = f"{tmp_path}{os.sep}elsewhere{os.sep}..{os.sep}optout{os.sep}a-project"
    assert_skipped(capture_picker(db, cwd=cwd), "cwd_optout", db)
    assert not db.exists()


# --- the same folder under another spelling ---------------------------------------
#
# As in the prompt tests: where the filesystem under tmp_path treats two
# spellings as one name (the macOS default), the opted-out folder is typed in
# one spelling and the cwd uses the other; where it keeps them apart (Linux),
# the same identity rule is driven through a hard-linked file.


def same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def assert_spellings_opt_out(db: Path, typed: Path, cwds: list[Path]) -> None:
    opt_out(db, typed)
    for cwd in cwds:
        assert_skipped(capture_picker(db, cwd=cwd), "cwd_optout", db)
    assert not db.exists()


def assert_identity_opts_out(db: Path, tmp_path: Path) -> None:
    root = tmp_path / "optout-file"
    root.write_text("x", encoding="utf-8")
    alias = tmp_path / "another-name"
    os.link(root, alias)
    assert os.path.realpath(alias) != os.path.realpath(root)
    assert same_file(alias, root)
    assert_spellings_opt_out(db, root, [alias, alias / "not-yet"])


def assert_variant_opts_out(db: Path, tmp_path: Path, on_disk_name: str, typed_name: str) -> None:
    on_disk = tmp_path / on_disk_name
    (on_disk / "a-project").mkdir(parents=True)
    typed = tmp_path / typed_name
    if same_file(typed, on_disk):
        # The text rule alone would capture: realpath keeps both spellings.
        assert os.path.realpath(typed) != os.path.realpath(on_disk)
        assert_spellings_opt_out(db, typed, [on_disk, on_disk / "a-project",
                                             on_disk / "a-project" / "not-yet"])
    else:
        assert_identity_opts_out(db, tmp_path)


def test_a_letter_case_variant_opts_a_picker_answer_out(db: Path, tmp_path: Path) -> None:
    assert_variant_opts_out(db, tmp_path, "Private", "private")


def test_a_unicode_form_variant_opts_a_picker_answer_out(db: Path, tmp_path: Path) -> None:
    assert_variant_opts_out(db, tmp_path, NFD_NAME, NFC_NAME)


def test_the_same_file_under_another_name_opts_a_picker_answer_out(db: Path,
                                                                   tmp_path: Path) -> None:
    assert_identity_opts_out(db, tmp_path)


def test_a_picker_cwd_that_cannot_be_stated_fails_closed(db: Path, optout_dir: Path,
                                                         tmp_path: Path) -> None:
    # The cwd reaches the opted-out folder through a link under a folder that
    # loses search permission, so its identity cannot be read.
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "into-optout").symlink_to(optout_dir, target_is_directory=True)
    opt_out(db, optout_dir)
    os.chmod(locked, 0o600)
    try:
        with pytest.raises(PermissionError):
            os.stat(locked / "probe")
        done = capture_picker(db, cwd=locked / "into-optout" / "a-project")
    finally:
        os.chmod(locked, 0o700)
    assert_skipped(done, "cwd_unverified", db)
    assert not db.exists()


# --- what is still captured -------------------------------------------------------


@pytest.mark.parametrize("where", ["sibling", "sibling-under", "parent"])
def test_a_picker_answer_outside_the_optout_path_is_captured(db: Path, optout_dir: Path,
                                                            tmp_path: Path, where: str) -> None:
    (tmp_path / "optoutfoo" / "a-project").mkdir(parents=True)
    opt_out(db, optout_dir)
    cwd = {
        "sibling": str(tmp_path / "optoutfoo"),
        "sibling-under": str(tmp_path / "optoutfoo" / "a-project"),
        "parent": str(tmp_path),
    }[where]
    assert str(optout_dir) + "foo" == str(tmp_path / "optoutfoo")
    assert_captured(capture_picker(db, cwd=cwd), db)


@pytest.mark.parametrize("cwd", [None, ""], ids=["missing", "empty"])
def test_a_missing_or_empty_picker_cwd_opts_nothing_out(db: Path, optout_dir: Path, cwd) -> None:
    opt_out(db, optout_dir)
    assert_captured(capture_picker(db, cwd=cwd), db)


@pytest.mark.parametrize("config", [None, {}, {"capture_optout_paths": []}],
                         ids=["no-file", "no-key", "empty-array"])
def test_no_optout_path_captures_the_picker_answer(db: Path, optout_dir: Path, config) -> None:
    if config is not None:
        write_config(db, config)
    assert_captured(capture_picker(db, cwd=optout_dir / "a-project"), db)


# --- capture_prompts and capture_skip_prefixes never apply to a picker answer -----


def test_prompts_off_still_captures_a_picker_answer(db: Path, tmp_path: Path) -> None:
    write_config(db, {"capture_prompts": False})
    assert_captured(capture_picker(db, cwd=tmp_path), db)


@pytest.mark.parametrize("value", ['"false"', "0", "null", "[]"],
                         ids=["string-false", "zero", "null", "array"])
def test_an_invalid_capture_prompts_never_stops_a_picker_answer(db: Path, tmp_path: Path,
                                                               value: str) -> None:
    write_config(db, '{"capture_prompts": ' + value + "}")
    assert_captured(capture_picker(db, cwd=tmp_path), db)


def test_an_invalid_capture_prompts_beside_a_valid_optout_still_opts_out(db: Path,
                                                                        optout_dir: Path,
                                                                        tmp_path: Path) -> None:
    opt_out(db, optout_dir, capture_prompts="false")
    assert_skipped(capture_picker(db, cwd=optout_dir), "cwd_optout", db)
    assert_captured(capture_picker(db, cwd=tmp_path), db)


def test_the_prefix_list_never_applies_to_a_picker_answer(db: Path, tmp_path: Path) -> None:
    write_config(db, {"capture_skip_prefixes": [QUESTION[:5], ANSWER[:5]]})
    assert_captured(capture_picker(db, cwd=tmp_path), db)


# --- refusals ---------------------------------------------------------------------


INVALID_OPTOUTS = {
    "string": '"/abs/path"',
    "object": '{"/abs/path": true}',
    "null": "null",
    "non-string-entry": '["/abs/path", 7]',
    "empty-entry": '[""]',
    "relative-entry": '["optout"]',
    "tilde-entry": '["~/optout_dir"]',
}


@pytest.mark.parametrize("name", sorted(INVALID_OPTOUTS))
def test_an_invalid_optout_value_refuses_a_picker_answer(db: Path, tmp_path: Path,
                                                         name: str) -> None:
    path = write_config(db, '{"capture_optout_paths": ' + INVALID_OPTOUTS[name] + "}")
    done = capture_picker(db, cwd=tmp_path)
    assert_refused(done, db)
    assert "capture_optout_paths" in done.stderr and str(path) in done.stderr
    code, env = capture_picker_json(db, cwd=tmp_path)
    assert code == 3 and env["error"]["code"] == "capture_config"
    assert not db.exists()


def test_an_invalid_optout_value_refuses_a_picker_answer_on_an_existing_ledger(
        db: Path, tmp_path: Path) -> None:
    assert_captured(capture_picker(db, cwd=tmp_path, tool_use_id="toolu_before"), db)
    write_config(db, '{"capture_optout_paths": "/abs/path"}')
    assert capture_picker(db, cwd=tmp_path, tool_use_id="toolu_after").returncode == 3
    assert [e.payload["picker"]["tool_use_id"] for e in picker_events(db)] == ["toolu_before"]


@pytest.mark.parametrize("content", ["{capture_optout_paths: []}", "[]", '"x"'],
                         ids=["bad-json", "array", "string"])
def test_a_config_that_cannot_be_read_refuses_a_picker_answer(db: Path, tmp_path: Path,
                                                              content: str) -> None:
    # The opt-out cannot be decided, so the answer is not stored.
    write_config(db, content)
    assert_refused(capture_picker(db, cwd=tmp_path), db)
    code, env = capture_picker_json(db, cwd=tmp_path)
    assert code == 3 and env["error"]["code"] == "capture_config"
    assert not db.exists()


@pytest.mark.parametrize("cwd", ["relative/dir", 7, "~/x"], ids=["relative", "number", "tilde"])
def test_a_picker_cwd_that_is_not_an_absolute_path_refuses_when_paths_are_set(
        db: Path, optout_dir: Path, cwd) -> None:
    opt_out(db, optout_dir)
    done = capture_picker(db, cwd=cwd)
    assert_refused(done, db)
    assert done.stderr == "cwd must be an absolute path\n"


@pytest.mark.parametrize("bad", ["not-a-picker", "no-tool-use-id", "no-questions"])
def test_an_invalid_picker_input_refuses_before_the_optout(db: Path, optout_dir: Path,
                                                           bad: str) -> None:
    # The cwd is opted out, yet the input is checked first: it refuses as it
    # did before the opt-out, and writes nothing.
    opt_out(db, optout_dir)
    extra, code = {
        "not-a-picker": ({"tool_name": "Bash"}, "capture_not_picker"),
        "no-tool-use-id": ({"tool_use_id": ""}, "capture_bad_input"),
        "no-questions": ({"tool_input": {"questions": []}}, "capture_bad_input"),
    }[bad]
    done = capture_picker(db, cwd=optout_dir, **extra)
    assert_refused(done, db)
    exit_code, env = capture_picker_json(db, cwd=optout_dir, **extra)
    assert (exit_code, env["error"]["code"]) == (3, code)
    assert not db.exists()
