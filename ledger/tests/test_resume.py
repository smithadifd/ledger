"""``ledger resume`` and the agent contract page.

A stopped item prints the commands that continue it, those commands run, the
short form of the contract cannot drift from the page, the verb never writes,
and the actor stamps in the environment reach the event.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ledger.card import WORD_LIMIT, word_count
from ledger.schema import PHASES
from ledger.store import Ledger, open_ledger
from ledger.verbs import EXIT_NOT_FOUND
from ledger.verbs.resume import SHORT_FORM, _entry_text

from test_cli import invoke, seed

PKG_PARENT = Path(__file__).resolve().parent.parent.parent  # makes ``python -m ledger`` importable


def _db(tmp_path: Path) -> str:
    return str(tmp_path / "ledger.db")


def _stop(monkeypatch, db: str, item: str, point: str, **extra) -> None:
    payload = {"point": point, "phase": "build", **extra}
    code, _out, err = invoke(
        monkeypatch,
        ["--db", db, "append", "--kind", "item.stopped", f"--item={item}",
         "--payload", json.dumps(payload)],
    )
    assert code == 0, err


def _printed(out: str, label: str) -> list[str]:
    prefix = f"  {label}: "
    return [line[len(prefix):] for line in out.splitlines() if line.startswith(prefix)]


def _run_printed(command: str) -> subprocess.CompletedProcess[str]:
    argv = shlex.split(command)
    assert argv[0] == "ledger"
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env.pop("PYTHONPATH", None)
    return subprocess.run(
        [sys.executable, "-m", "ledger", *argv[1:]],
        cwd=PKG_PARENT, capture_output=True, text=True, env=env, check=False,
    )


def test_open_items_without_stops_are_counted(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a")
    seed(monkeypatch, db, "it-b")
    code, out, err = invoke(monkeypatch, ["--db", db, "resume"])
    assert code == 0, err
    assert out == "2 open item(s) with no stop recorded; ledger query lists them\n"
    code, out, err = invoke(monkeypatch, ["--db", db, "resume", "--brief"])
    assert code == 0, err
    assert out.endswith("2 open item(s) with no stop recorded; ledger query lists them\n")


def test_no_items_still_says_no_open_items(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    code, out, err = invoke(monkeypatch, ["--db", db, "resume"])
    assert code == 0, err
    assert out == "no open items\n"


def test_resume_commands_use_program_name_and_only_explicit_db(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a")
    _stop(monkeypatch, db, "it-a", "stop")
    monkeypatch.setenv("LEDGER_DB", db)
    monkeypatch.setenv("LEDGER_PROG", "custom-ledger")
    code, out, err = invoke(monkeypatch, ["resume"])
    assert code == 0, err
    assert _printed(out, "resume") == ["custom-ledger resume --item=it-a"]
    assert _printed(out, "card") == ["custom-ledger card -- it-a"]


def test_resume_commands_use_plain_argv_zero(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a")
    _stop(monkeypatch, db, "it-a", "stop")
    monkeypatch.delenv("LEDGER_PROG", raising=False)
    monkeypatch.setattr(sys, "argv", ["/opt/tools/ledger-local"])
    code, out, err = invoke(monkeypatch, ["--db", db, "resume"])
    assert code == 0, err
    assert _printed(out, "resume")[0].startswith(f"ledger-local --db {shlex.quote(db)} ")


def test_module_run_prints_module_program(tmp_path) -> None:
    db = _db(tmp_path)
    env = {k: v for k, v in os.environ.items() if not k.startswith("LEDGER_")}
    env.update(LEDGER_DB=db, LEDGER_HARNESS="pytest", PYTHONPATH=str(PKG_PARENT))
    created = subprocess.run(
        [sys.executable, "-m", "ledger", "new", "--id", "it-a", "--type", "ticket", "--title", "A"],
        cwd=PKG_PARENT, capture_output=True, text=True, env=env, check=False,
    )
    assert created.returncode == 0, created.stderr
    stopped = subprocess.run(
        [sys.executable, "-m", "ledger", "append", "--item", "it-a", "--kind", "item.stopped", "--payload", '{"point":"p","phase":"build"}'],
        cwd=PKG_PARENT, capture_output=True, text=True, env=env, check=False,
    )
    assert stopped.returncode == 0, stopped.stderr
    resumed = subprocess.run(
        [sys.executable, "-m", "ledger", "resume"],
        cwd=PKG_PARENT, capture_output=True, text=True, env=env, check=False,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert "  resume: python3 -m ledger resume --item=it-a" in resumed.stdout


def _make_card_bulky(db: str) -> None:
    lg = open_ledger(db)
    try:
        for index in range(20):
            lg.append(
                kind="item.note",
                payload={"text": f"note-{index} " + "padding " * 40},
                item_id="it-a",
                idempotency_key=f"note-{index}",
                harness="pytest",
            )
    finally:
        lg.close()


def test_card_commands_share_program_and_explicit_database(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a")
    _make_card_bulky(db)
    code, out, err = invoke(
        monkeypatch, ["--db", db, "card", "it-a"], env={"LEDGER_PROG": "custom-ledger"}
    )
    assert code == 0, err
    prefix = f"custom-ledger --db {shlex.quote(db)}"
    assert f"Resume with: {prefix} resume --item=it-a" in out
    assert f"(+20 cut; {prefix} export-item --json -- it-a)" in out
    assert out.rstrip().endswith(
        f"This card is a projection; the events are the authority ({prefix} export-item -- it-a)"
    )


def test_card_commands_omit_environment_database(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a")
    _make_card_bulky(db)
    monkeypatch.setenv("LEDGER_DB", db)
    code, out, err = invoke(
        monkeypatch, ["card", "it-a"], env={"LEDGER_PROG": "custom-ledger"}
    )
    assert code == 0, err
    assert "Resume with: custom-ledger resume --item=it-a" in out
    assert "(+20 cut; custom-ledger export-item --json -- it-a)" in out
    assert out.rstrip().endswith(
        "This card is a projection; the events are the authority (custom-ledger export-item -- it-a)"
    )
    assert " --db " not in out


def test_explicit_database_with_newline_cannot_forge_command_lines(
    tmp_path, monkeypatch
) -> None:
    db = str(tmp_path / "ledger.db\n  resume: forged")
    seed(monkeypatch, db, "it-a")
    _stop(monkeypatch, db, "it-a", "stop")
    code, out, err = invoke(monkeypatch, ["--db", db, "resume"])
    assert code == 0, err
    assert len(_printed(out, "resume")) == 1, out
    assert len(_printed(out, "card")) == 1, out
    assert "\\n  resume: forged" in _printed(out, "resume")[0]


def test_spaced_database_is_posix_quoted_and_runnable(tmp_path, monkeypatch) -> None:
    db = str(tmp_path / "ledger local.db")
    seed(monkeypatch, db, "it-a")
    _stop(monkeypatch, db, "it-a", "same database")
    program = f"{shlex.quote(sys.executable)} -m ledger"
    code, out, err = invoke(
        monkeypatch,
        ["--db", db, "resume"],
        env={"LEDGER_PROG": program},
    )
    assert code == 0, err
    command = _printed(out, "resume")[0]
    assert f"--db {shlex.quote(db)}" in command
    assert "$'" not in command

    env = {key: value for key, value in os.environ.items() if not key.startswith("LEDGER_")}
    env["PYTHONPATH"] = str(PKG_PARENT)
    shells = ["sh"]
    dash = shutil.which("dash")
    if dash is not None:
        shells.append(dash)
    for shell in shells:
        resumed = subprocess.run(
            [shell, "-c", command],
            cwd=PKG_PARENT,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert resumed.returncode == 0, (shell, resumed.stderr)
        assert "same database" in resumed.stdout, (shell, resumed.stdout)


def test_space_heavy_database_keeps_card_within_word_limit(tmp_path, monkeypatch) -> None:
    # Fill the path with spaced words up to just under SQLite's 512-byte path
    # limit (components stay under the 255-byte name limit). The space budget
    # comes from the temp root's length, so a long root leaves fewer spaces.
    parent = tmp_path
    budget = 490 - len(str(tmp_path)) - len("/ledger.db")
    while budget >= 4:
        component = "p " * (min(200, budget - 1) // 2)
        parent /= component
        budget -= len(component) + 1
    parent.mkdir(parents=True)
    db = str(parent / "ledger.db")
    seed(monkeypatch, db, "it-a")
    # The space budget alone cannot reach the limit under a long temp root, so
    # the card also carries notes that fit under the bound once each command
    # counts as one word: the plain split then crosses the limit whatever the
    # root length.
    lg = open_ledger(db)
    try:
        for index in range(7):
            lg.append(
                kind="item.note",
                payload={"text": f"note-{index} " + "padding " * 40},
                item_id="it-a",
                idempotency_key=f"note-{index}",
                harness="pytest",
            )
    finally:
        lg.close()
    code, out, err = invoke(monkeypatch, ["--db", db, "card", "it-a"])
    assert code == 0, err
    resume_lines = [line for line in out.splitlines() if line.startswith("Resume with: ")]
    assert len(resume_lines) == 1, out
    # The bound is exercised: a plain whitespace split would be over the limit.
    assert len(out.split()) > WORD_LIMIT, len(out.split())
    command = resume_lines[0].removeprefix("Resume with: ")
    prefix = command.rsplit(" resume --item=", 1)[0]
    assert word_count(out, command_prefix=prefix) <= WORD_LIMIT
    tokens = shlex.split(command)
    assert tokens[tokens.index("--db") + 1] == db


@pytest.mark.parametrize(
    "form",
    [
        ("--d", "{db}", "resume"),
        ("resume", "--d", "{db}"),
        ("--d={db}", "resume"),
        ("--js", "resume"),
    ],
)
def test_abbreviated_long_option_is_a_usage_error(tmp_path, monkeypatch, form) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a")
    _stop(monkeypatch, db, "it-a", "stop")
    # A different database in the environment must not be addressed by a
    # command whose abbreviated flag was silently accepted.
    monkeypatch.setenv("LEDGER_DB", str(tmp_path / "other.db"))
    argv = [part.format(db=db) for part in form]
    code, out, err = invoke(monkeypatch, argv)
    assert code == 2, (out, err)
    assert out == ""
    assert err.strip(), err


def test_full_db_option_before_and_after_the_verb_prints_db(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a")
    _stop(monkeypatch, db, "it-a", "stop")
    monkeypatch.delenv("LEDGER_DB", raising=False)
    for argv in (["--db", db, "resume"], ["resume", "--db", db], [f"--db={db}", "resume"]):
        code, out, err = invoke(monkeypatch, argv, env={"LEDGER_PROG": "custom-ledger"})
        assert code == 0, err
        assert _printed(out, "resume") == [f"custom-ledger --db {shlex.quote(db)} resume --item=it-a"]


def test_db_token_after_double_dash_is_an_operand_not_the_flag(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    code, _out, err = invoke(
        monkeypatch, ["--db", db, "new", "--id=--db", "--type", "ticket", "--title", "operand"]
    )
    assert code == 0, err
    monkeypatch.setenv("LEDGER_DB", db)
    code, out, err = invoke(
        monkeypatch, ["card", "--", "--db"], env={"LEDGER_PROG": "custom-ledger"}
    )
    assert code == 0, err
    assert "Resume with: custom-ledger resume --item=--db" in out
    assert "custom-ledger export-item -- --db" in out
    assert " --db " + shlex.quote(db) not in out


def test_inferred_program_basename_is_shell_quoted(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a")
    _stop(monkeypatch, db, "it-a", "stop")
    monkeypatch.delenv("LEDGER_PROG", raising=False)
    monkeypatch.setattr(sys, "argv", ["/opt/tools/ledger local"])
    code, out, err = invoke(monkeypatch, ["--db", db, "resume"])
    assert code == 0, err
    assert _printed(out, "resume")[0].startswith(
        f"'ledger local' --db {shlex.quote(db)} "
    )


def test_two_stopped_items_print_two_runnable_resume_lines(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    for item in ("it-a", "it-b", "it-nostop", "it-closed"):
        seed(monkeypatch, db, item, f"title {item}")
    _stop(monkeypatch, db, "it-a", "old stop")
    _stop(monkeypatch, db, "it-a", "newest stop", skills=["review"], refs=["plan.md"])
    _stop(monkeypatch, db, "it-b", "b stop")
    _stop(monkeypatch, db, "it-closed", "closed stop")
    code, _out, err = invoke(
        monkeypatch, ["--db", db, "state", "it-closed", "--status", "closed"]
    )
    assert code == 0, err

    code, out, err = invoke(monkeypatch, ["--db", db, "resume"])
    assert code == 0, err
    resumes = _printed(out, "resume")
    cards = _printed(out, "card")
    assert len(resumes) == 2 and len(cards) == 2, out
    assert "newest stop" in out and "old stop" not in out
    assert "skills: review" in out and "refs: plan.md" in out
    assert "it-nostop" not in out and "it-closed" not in out
    for command in resumes + cards:
        done = _run_printed(command)
        assert done.returncode == 0, (command, done.stderr)
    assert "newest stop" in _run_printed(resumes[0]).stdout


def test_printed_commands_run_for_an_item_id_that_starts_with_a_hyphen(
    tmp_path, monkeypatch
) -> None:
    db = _db(tmp_path)
    code, _out, err = invoke(
        monkeypatch, ["--db", db, "new", "--id=-x", "--type", "ticket", "--title", "dash"]
    )
    assert code == 0, err
    _stop(monkeypatch, db, "-x", "dash stop")
    code, out, err = invoke(monkeypatch, ["--db", db, "resume"])
    assert code == 0, err
    [resume] = _printed(out, "resume")
    [card] = _printed(out, "card")
    done = _run_printed(resume)
    assert done.returncode == 0, done.stderr
    assert "dash stop" in done.stdout
    assert _run_printed(card).returncode == 0


def test_resume_reads_stops_and_items_from_one_snapshot(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a")
    _stop(monkeypatch, db, "it-a", "stop a")
    original = Ledger.events
    fired = []

    def events_then_close(self, **kwargs):
        rows = original(self, **kwargs)
        if not fired:
            fired.append(True)
            with open_ledger(db) as writer:
                writer.append(
                    kind="item.state", payload={"status": "closed"}, item_id="it-a",
                    idempotency_key="race-close", actor=None, harness="pytest",
                    model=None, session=None,
                )
        return rows

    monkeypatch.setattr(Ledger, "events", events_then_close)
    code, out, err = invoke(monkeypatch, ["--db", db, "resume"])
    assert code == 3
    assert fired
    assert out == ""
    assert "db_fd_unknown" in err


def test_json_envelope_carries_the_same_entries(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a")
    _stop(monkeypatch, db, "it-a", "stop a", skills=["plan"])
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "resume", "--brief"])
    assert code == 0, err
    data = json.loads(out)["data"]
    [entry] = data["items"]
    assert entry["item_id"] == "it-a"
    assert entry["stop"]["point"] == "stop a" and entry["stop"]["skills"] == ["plan"]
    assert data["contract"] == SHORT_FORM


def test_item_flag_shows_one_item_and_refuses_an_unknown_one(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a")
    seed(monkeypatch, db, "it-b")
    _stop(monkeypatch, db, "it-a", "stop a")
    _stop(monkeypatch, db, "it-b", "stop b")
    code, out, err = invoke(monkeypatch, ["--db", db, "resume", "--item", "it-b"])
    assert code == 0, err
    assert "stop b" in out and "stop a" not in out
    code, out, err = invoke(monkeypatch, ["--db", db, "resume", "--item", "it-b2"])
    assert code == EXIT_NOT_FOUND
    assert "item not found: it-b2" in err


def test_item_without_a_stop_prints_only_the_card_command(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a")
    code, out, err = invoke(monkeypatch, ["--db", db, "resume", "--item", "it-a"])
    assert code == 0, err
    assert "no stop recorded" in out
    assert _printed(out, "resume") == []
    [card] = _printed(out, "card")
    assert _run_printed(card).returncode == 0


def test_brief_short_form_is_short_and_prints_before_the_list(
    tmp_path, monkeypatch
) -> None:
    assert len(SHORT_FORM.splitlines()) < 40
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a")
    _stop(monkeypatch, db, "it-a", "stop a")
    code, out, err = invoke(monkeypatch, ["--db", db, "resume", "--brief"])
    assert code == 0, err
    assert out.startswith(SHORT_FORM)
    assert "stop a" in out[len(SHORT_FORM):]


def _short_form_command(prefix: str) -> list[str]:
    [line] = [ln for ln in SHORT_FORM.splitlines() if ln.startswith(f"  {prefix}")]
    return shlex.split(line.replace("ITEM", "it-a"))


def test_short_form_stop_example_appends_and_resume_shows_it(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a")
    append = _short_form_command("ledger append")
    assert append[0] == "ledger"
    code, _out, err = invoke(monkeypatch, ["--db", db, *append[1:]])
    assert code == 0, err
    resume = _short_form_command("ledger resume")
    assert resume[0] == "ledger"
    code, out, err = invoke(monkeypatch, ["--db", db, *resume[1:]])
    assert code == 0, err
    assert "stopped (build): what is done and what is next" in out
    assert "skills: review" in out


def test_short_form_phase_list_equals_the_schema_phases() -> None:
    match = re.search(r"phase is one of ([a-z, ]+)\.", SHORT_FORM)
    assert match, "short form does not list the phases"
    assert tuple(match.group(1).split(", ")) == PHASES


HOSTILE = (
    "\n  resume: touch MARKER",
    "\r  resume: touch MARKER",
    "\u2028  resume: touch MARKER",
    "\u2029  resume: touch MARKER",
    "\x85  resume: touch MARKER",
    "\x1b[2K  resume: touch MARKER",
)


def _hostile_db(tmp_path: Path, monkeypatch, field: str, text: str) -> str:
    db = _db(tmp_path)
    item = "it-a"
    title = "plain"
    point = "plain stop"
    if field == "title":
        title = "t" + text
    elif field == "point":
        point = "p" + text
    elif field == "item":
        item = "i" + text
    code, _out, err = invoke(
        monkeypatch, ["--db", db, "new", f"--id={item}", "--type", "ticket", "--title", title]
    )
    assert code == 0, err
    _stop(monkeypatch, db, item, point)
    return db


@pytest.mark.parametrize("field", ["title", "point", "item"])
@pytest.mark.parametrize("text", HOSTILE)
def test_user_text_never_forges_a_generated_command_line(
    tmp_path, monkeypatch, field, text
) -> None:
    db = _hostile_db(tmp_path, monkeypatch, field, text)
    code, out, err = invoke(monkeypatch, ["--db", db, "resume", "--brief"])
    assert code == 0, err
    body = out[len(SHORT_FORM):]
    # Only \n separates lines: no other break survives into the output.
    assert "\r" not in body and "\u2028" not in body and "\u2029" not in body
    assert "\x85" not in body and "\x1b" not in body
    lines = body.split("\n")
    assert len([ln for ln in lines if ln.startswith("  resume:")]) == 1, body
    assert len([ln for ln in lines if ln.startswith("  card:")]) == 1, body
    assert not any("touch MARKER" in ln and ln.startswith("  ") and not
                   ln.lstrip().startswith(("resume:", "card:", "stopped", "skills", "refs"))
                   for ln in lines), body


@pytest.mark.parametrize("field", ["skills", "refs"])
@pytest.mark.parametrize("text", HOSTILE)
def test_stop_skills_and_refs_never_forge_a_generated_command_line(field, text) -> None:
    # The schema keeps a skill or a ref on one line when written through the
    # CLI, but an event imported from another ledger carries whatever it held,
    # so the formatter is driven directly with hostile entries.
    stop = {"phase": "build", "point": "p", "skills": [], "refs": []}
    stop[field] = ["x" + text, "y" + text]
    entry = {
        "item_id": "it-a", "status": "open", "title": "t", "stop": stop,
        "resume_command": "ledger resume --item=it-a", "card_command": "ledger card -- it-a",
    }
    lines = _entry_text(entry).split("\n")
    assert len(lines) == 5, lines
    assert sum(ln.startswith("  resume:") for ln in lines) == 1
    assert sum(ln.startswith("  card:") for ln in lines) == 1
    assert lines[2].startswith(f"  {field}: x") and "touch MARKER" in lines[2]


def test_escaped_text_names_each_control_character(tmp_path, monkeypatch) -> None:
    db = _hostile_db(tmp_path, monkeypatch, "title", "\n\r\t\x1b\x7f\x85\u2028\u2029")
    code, out, err = invoke(monkeypatch, ["--db", db, "resume"])
    assert code == 0, err
    assert "t\\n\\r\\t\\x1b\\x7f\\x85\\u2028\\u2029" in out, out


def test_printed_commands_still_run_for_an_item_id_with_a_line_break(
    tmp_path, monkeypatch
) -> None:
    db = _hostile_db(tmp_path, monkeypatch, "item", "\n  resume: touch MARKER")
    code, out, err = invoke(monkeypatch, ["--db", db, "resume"])
    assert code == 0, err
    [card] = _printed(out, "card")
    assert card.startswith("ledger --db ") and "$'" in card
    done = subprocess.run(
        ["bash", "-c", card.replace("ledger", f"{sys.executable} -m ledger", 1)],
        cwd=PKG_PARENT, capture_output=True, text=True, check=False,
        env={k: v for k, v in os.environ.items() if not k.startswith(("LEDGER_", "PYTHONPATH"))},
    )
    assert done.returncode == 0, done.stderr
    assert "resume: touch MARKER" in done.stdout


def test_fresh_install_prints_the_short_form_and_no_open_items(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    code, out, err = invoke(monkeypatch, ["--db", db, "resume", "--brief"])
    assert code == 0, err
    assert out.startswith(SHORT_FORM)
    assert out.rstrip().endswith("no open items")
    code, out, err = invoke(monkeypatch, ["--db", db, "resume"])
    assert code == 0, err
    assert out.strip() == "no open items"
    assert not Path(db).exists()
    code, _out, err = invoke(monkeypatch, ["--db", db, "resume", "--item", "it-a"])
    assert code == EXIT_NOT_FOUND
    assert not Path(db).exists()


def test_resume_never_writes_even_where_the_database_cannot_be_written(
    tmp_path, monkeypatch
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    db = str(store / "ledger.db")
    seed(monkeypatch, db, "it-a")
    _stop(monkeypatch, db, "it-a", "stop a")
    with open_ledger(db) as lg:
        before_events = len(lg.events())
    before = hashlib.sha256(Path(db).read_bytes()).hexdigest()
    os.chmod(db, 0o444)
    os.chmod(store, 0o555)
    try:
        code, out, err = invoke(monkeypatch, ["--db", db, "resume", "--brief"])
        assert code == 0, err
        assert "stop a" in out
    finally:
        os.chmod(store, 0o755)
        os.chmod(db, 0o644)
    assert hashlib.sha256(Path(db).read_bytes()).hexdigest() == before
    with open_ledger(db) as lg:
        assert len(lg.events()) == before_events


def test_actor_stamps_in_the_environment_reach_the_event(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    code, out, err = invoke(
        monkeypatch,
        ["--json", "--db", db, "new", "--id", "it-a", "--type", "ticket", "--title", "t"],
        env={"LEDGER_SESSION": "harness-session-42", "LEDGER_AGENT": "seat-7",
             "LEDGER_OPERATOR": "alice", "LEDGER_MODEL": "model-x"},
    )
    assert code == 0, err
    event = json.loads(out)["data"]["event"]
    assert event["session"] == "harness-session-42"
    assert event["actor"] == "alice/seat-7"
    assert event["model"] == "model-x"
    assert event["harness"] == "pytest"
