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
import subprocess
import sys
from pathlib import Path

from ledger.store import Ledger, open_ledger
from ledger.verbs import EXIT_NOT_FOUND
from ledger.verbs.resume import SHORT_FORM

from test_cli import invoke, seed

REPO = Path(__file__).resolve().parents[2]
CONTRACT = REPO / "docs" / "agent-contract.md"


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
        cwd=REPO, capture_output=True, text=True, env=env, check=False,
    )


def _triggers() -> list[str]:
    text = CONTRACT.read_text(encoding="utf-8")
    section = text.split("## Write triggers", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"^- \*\*(.+?)\*\*", section, flags=re.MULTILINE)


def test_two_stopped_items_print_two_runnable_resume_lines(tmp_path, monkeypatch) -> None:
    db = _db(tmp_path)
    for item in ("it-a", "it-b", "it-nostop", "it-closed"):
        seed(monkeypatch, db, item, f"title {item}")
    _stop(monkeypatch, db, "it-a", "old stop")
    _stop(monkeypatch, db, "it-a", "newest stop", skills=["review"], refs=["docs/plan.md"])
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
    assert "skills: review" in out and "refs: docs/plan.md" in out
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
    assert code == 0, err
    assert fired
    assert "stop a" in out, out


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


def test_brief_short_form_is_short_and_names_every_trigger_on_the_page(
    tmp_path, monkeypatch
) -> None:
    triggers = _triggers()
    assert len(triggers) >= 7, triggers
    assert len(SHORT_FORM.splitlines()) < 40
    lowered = SHORT_FORM.lower()
    for trigger in triggers:
        assert trigger.lower() in lowered, f"short form lacks write trigger: {trigger}"
    db = _db(tmp_path)
    seed(monkeypatch, db, "it-a")
    _stop(monkeypatch, db, "it-a", "stop a")
    code, out, err = invoke(monkeypatch, ["--db", db, "resume", "--brief"])
    assert code == 0, err
    assert out.startswith(SHORT_FORM)
    assert "stop a" in out[len(SHORT_FORM):]


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


def test_readme_and_contract_page_never_say_pick_up() -> None:
    for path in (REPO / "README.md", CONTRACT):
        assert "pick up" not in path.read_text(encoding="utf-8").lower(), path
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "docs/agent-contract.md" in readme and "ledger resume --brief" in readme
