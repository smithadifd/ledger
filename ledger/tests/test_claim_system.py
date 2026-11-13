"""``ledger claim-system NAME``: claim a database for its system.

The library (``Ledger.claim_system``) decides; the verb resolves the path the
way every write verb does and reports. Each test reads the database file
directly afterwards, so "writes nothing" is an event count, not a message.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from ledger import claim_database, open_ledger, store
from ledger.store import Ledger
from ledger.tests._child import read_ready_line, running
from ledger.verbs import EXIT_REFUSED, EXIT_USAGE, discover_verbs

from test_cli import invoke, seed

_LIB = Path(__file__).resolve().parents[2]


def claim_events(db: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, payload, hash FROM events WHERE kind = 'ledger.system' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [{"id": r[0], "system": json.loads(r[1])["system"], "hash": r[2]} for r in rows]


def event_count(db: str) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()


def claim(monkeypatch, db, name, *, env=None):
    code, out, err = invoke(monkeypatch, ["--json", "--db", db, "claim-system", "--", name], env=env)
    return code, (json.loads(out) if out.strip() else None), err


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "ledger.db")


def test_verb_is_registered_and_writes() -> None:
    verb = discover_verbs()["claim-system"]
    assert verb.writes is True


def test_claims_an_unclaimed_database(db, monkeypatch) -> None:
    code, payload, err = claim(monkeypatch, db, "work")
    assert code == 0, err
    assert payload["ok"] is True
    assert payload["data"]["system"] == "work"
    assert payload["data"]["claimed"] is True
    assert payload["data"]["path"] == str(Path(db).resolve())
    assert [e["system"] for e in claim_events(db)] == ["work"]
    with open_ledger(db) as lg:
        assert lg.system() == "work"
        lg.verify(system="work")


def test_claims_a_database_that_already_holds_events(db, monkeypatch) -> None:
    seed(monkeypatch, db, "it-1")
    before = event_count(db)
    code, payload, err = claim(monkeypatch, db, "home")
    assert code == 0, err
    assert payload["data"]["claimed"] is True
    assert event_count(db) == before + 1
    assert [e["system"] for e in claim_events(db)] == ["home"]


def test_text_output_names_the_system_and_path(db, monkeypatch) -> None:
    code, out, err = invoke(monkeypatch, ["--db", db, "claim-system", "work"])
    assert code == 0, err
    assert out.strip() == f"claimed for work: {Path(db).resolve()}"
    code, out, err = invoke(monkeypatch, ["--db", db, "claim-system", "work"])
    assert code == 0, err
    assert out.strip() == f"already claimed for work: {Path(db).resolve()}"


def test_same_name_is_a_no_op(db, monkeypatch) -> None:
    assert claim(monkeypatch, db, "work")[0] == 0
    before_events = event_count(db)
    before_claims = claim_events(db)
    code, payload, err = claim(monkeypatch, db, "work")
    assert code == 0, err
    assert payload["ok"] is True
    assert payload["data"]["claimed"] is False
    assert payload["data"]["system"] == "work"
    assert event_count(db) == before_events
    assert claim_events(db) == before_claims


def test_another_name_is_refused_and_writes_nothing(db, monkeypatch) -> None:
    assert claim(monkeypatch, db, "work")[0] == 0
    before_events = event_count(db)
    before_claims = claim_events(db)
    code, payload, err = claim(monkeypatch, db, "home")
    assert code == EXIT_REFUSED
    assert payload["ok"] is False
    assert payload["error"]["code"] == "shared_database"
    assert "'work'" in payload["error"]["message"] and "'home'" in payload["error"]["message"]
    assert event_count(db) == before_events
    assert claim_events(db) == before_claims
    with open_ledger(db) as lg:
        assert lg.system() == "work"


def test_synced_path_is_refused_and_creates_nothing(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "Dropbox" / "ledger.db"
    code, payload, err = claim(monkeypatch, str(db), "work")
    assert code == EXIT_REFUSED
    assert payload["ok"] is False
    assert payload["error"]["code"] == "synced_path"
    assert not db.exists()
    assert not db.parent.exists()


def test_linked_path_is_refused_and_leaves_the_target_alone(tmp_path: Path, monkeypatch) -> None:
    real = str(tmp_path / "real.db")
    assert claim(monkeypatch, real, "work")[0] == 0
    before = event_count(real)
    link = tmp_path / "alias.db"
    link.symlink_to(real)
    code, payload, err = claim(monkeypatch, str(link), "home")
    assert code == EXIT_REFUSED
    assert payload["error"]["code"] == "db_symlink"
    assert event_count(real) == before
    assert [e["system"] for e in claim_events(real)] == ["work"]


@pytest.mark.parametrize(
    "name",
    [
        "",
        " ",
        "   ",
        "\t",
        "Work",
        "work ",
        " work",
        "wo rk",
        "work\n",
        "9work",
        "-work",
        "_work",
        "work.db",
        "w\u00f6rk",
        "x" * 65,
    ],
)
def test_an_invalid_name_is_a_usage_error_and_creates_nothing(
    tmp_path: Path, monkeypatch, name: str
) -> None:
    db = tmp_path / "sub" / "ledger.db"
    code, payload, err = claim(monkeypatch, str(db), name)
    assert code == EXIT_USAGE
    assert payload["ok"] is False
    assert payload["error"]["code"] == "usage"
    assert "lowercase letter" in payload["error"]["message"]
    assert not db.exists()
    assert not db.parent.exists()


@pytest.mark.parametrize("name", ["w", "work", "home-2", "a_b-c9", "x" * 64])
def test_a_valid_name_is_accepted(db, monkeypatch, name: str) -> None:
    code, payload, err = claim(monkeypatch, db, name)
    assert code == 0, err
    assert claim_events(db)[0]["system"] == name


def test_an_invalid_name_on_a_claimed_database_writes_nothing(db, monkeypatch) -> None:
    assert claim(monkeypatch, db, "work")[0] == 0
    before = event_count(db)
    code, _payload, _err = claim(monkeypatch, db, "Work")
    assert code == EXIT_USAGE
    assert event_count(db) == before


def test_a_write_needs_the_harness_and_creates_nothing_without_it(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    code, payload, err = claim(monkeypatch, str(db), "work", env={"LEDGER_HARNESS": None})
    assert code == EXIT_REFUSED
    assert payload["error"]["code"] == "missing_harness"
    assert not db.exists()


def test_a_missing_name_is_a_usage_error(db, monkeypatch) -> None:
    code, _out, _err = invoke(monkeypatch, ["--db", db, "claim-system"])
    assert code == EXIT_USAGE
    assert not Path(db).exists()


def test_two_systems_claiming_at_once_leave_one_claim(tmp_path: Path) -> None:
    db = str(tmp_path / "ledger.db")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    env["LEDGER_HARNESS"] = "pytest"
    procs = {
        name: subprocess.Popen(
            [sys.executable, "-m", "ledger", "--json", "--db", db, "claim-system", name],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for name in ("work", "home")
    }
    codes = {}
    for name, proc in procs.items():
        proc.communicate(timeout=60)
        codes[name] = proc.returncode
    assert sorted(codes.values()) == [0, EXIT_REFUSED], codes
    winner = next(name for name, code in codes.items() if code == 0)
    assert [e["system"] for e in claim_events(db)] == [winner]


def claim_row(db: str) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT actor, harness, model, session FROM events WHERE kind = 'ledger.system'"
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    return dict(rows[0])


def test_the_claim_event_carries_the_callers_attribution(db, monkeypatch) -> None:
    env = {
        "LEDGER_HARNESS": "claude-code",
        "LEDGER_OPERATOR": "alice",
        "LEDGER_AGENT": "builder",
        "LEDGER_MODEL": "model-x",
        "LEDGER_SESSION": "sess-42",
    }
    code, payload, err = claim(monkeypatch, db, "work", env=env)
    assert code == 0, err
    assert claim_row(db) == {
        "actor": "alice/builder",
        "harness": "claude-code",
        "model": "model-x",
        "session": "sess-42",
    }


def test_the_library_claim_returns_whether_it_wrote(db) -> None:
    with open_ledger(db) as lg:
        assert lg.claim_system("work") is True
        assert lg.claim_system("work") is False
    assert [e["system"] for e in claim_events(db)] == ["work"]


def test_the_library_claim_stamps_only_what_it_is_given(db) -> None:
    with open_ledger(db) as lg:
        lg.claim_system("work")
    assert claim_row(db) == {"actor": None, "harness": None, "model": None, "session": None}


def test_claim_database_records_the_callers_attribution(db) -> None:
    claim_database(
        db,
        "work",
        actor="alice/builder",
        harness="claude-code",
        model="model-x",
        session="sess-42",
    )
    assert claim_row(db) == {
        "actor": "alice/builder",
        "harness": "claude-code",
        "model": "model-x",
        "session": "sess-42",
    }


def test_claim_database_with_no_attribution_writes_no_stamps(db) -> None:
    claim_database(db, "work")
    assert claim_row(db) == {
        "actor": None,
        "harness": None,
        "model": None,
        "session": None,
    }


def test_the_report_comes_from_the_claim_not_from_a_read_before_it(
    db, monkeypatch
) -> None:
    # A caller whose read of the claim ran before another caller's commit sees
    # no claim. Play that interleaving: the database is claimed, and
    # Ledger.system() still says it is not. The verb must report the library's
    # answer ("already claimed"), not a stale read ("claimed").
    from ledger.store import Ledger

    with open_ledger(db) as lg:
        lg.claim_system("work")
    monkeypatch.setattr(Ledger, "system", lambda self: None)
    code, payload, err = claim(monkeypatch, db, "work")
    assert code == 0, err
    assert payload["data"]["claimed"] is False
    assert [e["system"] for e in claim_events(db)] == ["work"]


def test_two_callers_claiming_the_same_name_at_once_report_one_claim(tmp_path: Path) -> None:
    db = str(tmp_path / "ledger.db")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    env["LEDGER_HARNESS"] = "pytest"
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "ledger", "--json", "--db", db, "claim-system", "work"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    reports = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err
        reports.append(json.loads(out)["data"]["claimed"])
    assert sorted(reports) == [False, True], reports
    assert [e["system"] for e in claim_events(db)] == ["work"]


# A reader in another process holding a read transaction keeps a checkpoint
# from finishing, without sharing SQLite descriptors with the test.
_READER = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("BEGIN")
conn.execute("SELECT count(*) FROM events").fetchone()
print("held", flush=True)
sys.stdin.readline()
conn.close()
"""


@contextlib.contextmanager
def a_reader_holding(db: str):
    with running(
        [sys.executable, "-c", _READER, db],
        cwd=_LIB,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        read_ready_line(proc, "held")
        try:
            yield
        finally:
            assert proc.stdin is not None
            proc.stdin.write("\n")
            proc.stdin.flush()
            _out, err = proc.communicate(timeout=30)
            assert proc.returncode == 0, err


def outcome_reads(monkeypatch) -> list[str]:
    """Record each refusal the verb re-read, so a test knows the error path ran."""
    seen: list[str] = []
    original = Ledger.claim_outcome

    def spy(self, system, refusal):
        seen.append(str(refusal))
        return original(self, system, refusal)

    monkeypatch.setattr(Ledger, "claim_outcome", spy)
    return seen


def test_a_claim_that_committed_before_its_checkpoint_failed_reports_claimed(
    db, monkeypatch
) -> None:
    # The claim commits, then the checkpoint after it cannot finish while the
    # reader holds the file. The library refuses; the transaction wrote the
    # claim, so the verb reports claimed and exits 0.
    with open_ledger(db):
        pass
    seen = outcome_reads(monkeypatch)
    with a_reader_holding(db):
        code, payload, err = claim(monkeypatch, db, "work")
    assert code == 0, (payload, err)
    assert payload["data"]["claimed"] is True
    assert [e["system"] for e in claim_events(db)] == ["work"]
    assert len(seen) == 1 and "checkpoint of" in seen[0], seen


def test_a_repeat_claim_whose_checkpoint_failed_reports_already_claimed(
    db, monkeypatch
) -> None:
    # The same name is on record before the call; an append after the reader
    # took its snapshot leaves WAL frames the checkpoint cannot write back.
    claim_database(db, "work")
    seen = outcome_reads(monkeypatch)
    with a_reader_holding(db):
        seed(monkeypatch, db, "it-1")
        before = event_count(db)
        code, payload, err = claim(monkeypatch, db, "work")
    assert code == 0, (payload, err)
    assert payload["data"]["claimed"] is False
    assert event_count(db) == before
    assert [e["system"] for e in claim_events(db)] == ["work"]
    assert len(seen) == 1 and "checkpoint of" in seen[0], seen


def _io_error() -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError("disk I/O error")
    exc.sqlite_errorcode = sqlite3.SQLITE_IOERR
    return exc


def _fail_before_the_write(monkeypatch) -> None:
    def append(self, **kwargs):
        raise _io_error()

    monkeypatch.setattr(Ledger, "_append", append)


def _fail_at_the_commit(monkeypatch) -> None:
    # The claim is written inside the transaction, then the commit fails with
    # an I/O error and the write rolls back.
    @contextlib.contextmanager
    def transaction(self):
        self._conn.execute("BEGIN IMMEDIATE")
        self._in_batch = True
        try:
            yield
        finally:
            self._in_batch = False
            self._conn.execute("ROLLBACK")
        raise _io_error()

    monkeypatch.setattr(Ledger, "transaction", transaction)


def _fail_and_leave_the_transaction_open(monkeypatch) -> None:
    # The claim is written and the error leaves the transaction open: a read
    # on that connection would see the uncommitted claim. Closing the
    # connection rolls it back.
    @contextlib.contextmanager
    def transaction(self):
        self._conn.execute("BEGIN IMMEDIATE")
        self._in_batch = True
        try:
            yield
        finally:
            self._in_batch = False
        raise _io_error()

    monkeypatch.setattr(Ledger, "transaction", transaction)


@pytest.mark.parametrize(
    "inject",
    [_fail_before_the_write, _fail_at_the_commit, _fail_and_leave_the_transaction_open],
)
def test_an_error_after_which_the_claim_is_not_on_record_is_refused(
    db, monkeypatch, inject
) -> None:
    with open_ledger(db):
        pass
    seen = outcome_reads(monkeypatch)
    inject(monkeypatch)
    code, payload, err = claim(monkeypatch, db, "work")
    assert code == EXIT_REFUSED, (payload, err)
    assert payload["error"]["code"] == "shared_database"
    assert "could not be read" in payload["error"]["message"]
    assert event_count(db) == 0
    assert claim_events(db) == []
    assert len(seen) == 1, seen


def test_a_failed_commit_while_another_callers_claim_landed_reports_already_claimed(
    db, monkeypatch
) -> None:
    # This call's claim rolls back on an I/O error at its commit, and another
    # caller's claim of the same name commits before the read-back. The name
    # is on record, but not by this call's write.
    with open_ledger(db):
        pass
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    env["LEDGER_HARNESS"] = "pytest"

    @contextlib.contextmanager
    def transaction(self):
        self._conn.execute("BEGIN IMMEDIATE")
        self._in_batch = True
        try:
            yield
        finally:
            self._in_batch = False
            self._conn.execute("ROLLBACK")
        other = subprocess.run(
            [sys.executable, "-m", "ledger", "--json", "--db", db, "claim-system", "work"],
            env=env, capture_output=True, text=True, timeout=60,
        )
        assert other.returncode == 0, other.stderr
        assert json.loads(other.stdout)["data"]["claimed"] is True
        raise _io_error()

    seen = outcome_reads(monkeypatch)
    monkeypatch.setattr(Ledger, "transaction", transaction)
    code, payload, err = claim(monkeypatch, db, "work")
    assert code == 0, (payload, err)
    assert payload["data"]["claimed"] is False
    assert [e["system"] for e in claim_events(db)] == ["work"]
    assert len(seen) == 1 and "could not be read" in seen[0], seen


def _child_env() -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    env["LEDGER_HARNESS"] = "pytest"
    return env


def _commit_then_fail(monkeypatch, after_commit=None) -> None:
    # The claim commits, then an I/O error is raised as an error at the
    # commit would be: the claim is on record, and this call wrote it.
    @contextlib.contextmanager
    def transaction(self):
        self._conn.execute("BEGIN IMMEDIATE")
        self._in_batch = True
        try:
            yield
        finally:
            self._in_batch = False
        self._conn.execute("COMMIT")
        if after_commit is not None:
            after_commit(self)
        raise _io_error()

    monkeypatch.setattr(Ledger, "transaction", transaction)


def test_an_io_error_after_the_claim_committed_reports_claimed(db, monkeypatch) -> None:
    with open_ledger(db):
        pass
    seen = outcome_reads(monkeypatch)
    _commit_then_fail(monkeypatch)
    code, payload, err = claim(monkeypatch, db, "work")
    assert code == 0, (payload, err)
    assert payload["data"]["claimed"] is True
    assert [e["system"] for e in claim_events(db)] == ["work"]
    assert len(seen) == 1 and "could not be read" in seen[0], seen


def test_a_read_back_that_fails_is_refused(db, monkeypatch) -> None:
    # The claim commits and an I/O error follows; then the read-back itself
    # fails, so the outcome cannot be decided and the verb refuses.
    with open_ledger(db):
        pass

    def deny_event_reads(lg) -> None:
        def authorizer(action, arg1, *_rest):
            if action == sqlite3.SQLITE_READ and arg1 == "events":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        lg._conn.set_authorizer(authorizer)

    seen = outcome_reads(monkeypatch)
    _commit_then_fail(monkeypatch, deny_event_reads)
    code, payload, err = claim(monkeypatch, db, "work")
    assert code == EXIT_REFUSED, (payload, err)
    assert payload["error"]["code"] == "shared_database"
    assert "could not be read" in payload["error"]["message"]
    assert [e["system"] for e in claim_events(db)] == ["work"]
    assert len(seen) == 1, seen


def test_an_identical_claim_another_caller_committed_is_not_this_calls(
    db, monkeypatch
) -> None:
    # This call writes its claim and rolls it back on an I/O error at its
    # commit. Another caller with the same attribution then commits its claim
    # in the same millisecond: an event byte for byte identical to the one
    # this call wrote. The claim on record is not this call's write.
    with open_ledger(db):
        pass
    monkeypatch.setattr(store, "_now", lambda: "2026-01-01T00:00:00.000Z")
    original_transaction = Ledger.transaction
    original_claim = Ledger.claim_system
    original_outcome = Ledger.claim_outcome
    attributions: list[dict] = []
    refusals: list = []

    def claim_system(self, system, **kwargs):
        attributions.append(kwargs)
        return original_claim(self, system, **kwargs)

    def claim_outcome(self, system, refusal):
        refusals.append(refusal)
        return original_outcome(self, system, refusal)

    @contextlib.contextmanager
    def transaction(self):
        self._conn.execute("BEGIN IMMEDIATE")
        self._in_batch = True
        try:
            yield
        finally:
            self._in_batch = False
            self._conn.execute("ROLLBACK")
        monkeypatch.setattr(Ledger, "transaction", original_transaction)
        with open_ledger(db) as other:
            assert original_claim(other, "work", **attributions[0]) is True
        raise _io_error()

    monkeypatch.setattr(Ledger, "claim_system", claim_system)
    monkeypatch.setattr(Ledger, "claim_outcome", claim_outcome)
    monkeypatch.setattr(Ledger, "transaction", transaction)
    code, payload, err = claim(monkeypatch, db, "work")
    assert code == 0, (payload, err)
    assert payload["data"]["claimed"] is False
    [on_record] = claim_events(db)
    assert len(refusals) == 1
    written = refusals[0].event
    assert written is not None
    assert (written.id, written.hash) == (on_record["id"], on_record["hash"])


def test_a_finished_commit_stays_claimed_when_another_write_lands_before_the_read_back(
    db, monkeypatch
) -> None:
    # The claim commits and its checkpoint cannot finish while the reader
    # holds the file. Before the read-back another caller commits an
    # unrelated event, so the connection's data version moves on; the
    # commit is known to have finished, so the claim is still this call's.
    with open_ledger(db):
        pass
    original = Ledger.transaction

    @contextlib.contextmanager
    def transaction(self):
        with original(self):
            yield
        other = subprocess.run(
            [sys.executable, "-m", "ledger", "--db", db, "new", "--id", "it-2",
             "--type", "ticket", "--title", "another-write"],
            env=_child_env(), capture_output=True, text=True, timeout=60,
        )
        assert other.returncode == 0, other.stderr

    seen = outcome_reads(monkeypatch)
    monkeypatch.setattr(Ledger, "transaction", transaction)
    with a_reader_holding(db):
        code, payload, err = claim(monkeypatch, db, "work")
    assert code == 0, (payload, err)
    assert payload["data"]["claimed"] is True
    assert [e["system"] for e in claim_events(db)] == ["work"]
    assert len(seen) == 1 and "checkpoint of" in seen[0], seen


def _add_a_hard_link(db: Path) -> None:
    os.link(db, db.with_name("alias.db"))


def _replace_with_a_copy(db: Path) -> None:
    moved = db.with_name("moved.db")
    os.rename(db, moved)
    shutil.copyfile(moved, db)


def _replace_with_a_symbolic_link(db: Path) -> None:
    moved = db.with_name("moved.db")
    os.rename(db, moved)
    db.symlink_to(moved)


@pytest.mark.parametrize(
    "alter", [_add_a_hard_link, _replace_with_a_copy, _replace_with_a_symbolic_link]
)
def test_a_claim_whose_file_changed_before_settlement_is_refused(
    db, monkeypatch, alter
) -> None:
    # The claim commits; then, before the checkpoint, the database path stops
    # naming the opened file as its only link. The checkpoint cannot finish
    # while the reader holds the file, and the claim must not be settled: a
    # connection through another path may hold it and not see the claim.
    with open_ledger(db):
        pass
    original = Ledger.transaction

    @contextlib.contextmanager
    def transaction(self):
        with original(self):
            yield
        alter(Path(db))

    seen = outcome_reads(monkeypatch)
    monkeypatch.setattr(Ledger, "transaction", transaction)
    with a_reader_holding(db):
        code, payload, err = claim(monkeypatch, db, "work")
    assert code == EXIT_REFUSED, (payload, err)
    assert payload["error"]["code"] == "shared_database"
    assert "checkpoint of" in payload["error"]["message"]
    assert len(seen) == 1 and "checkpoint of" in seen[0], seen


def test_an_error_on_a_database_claimed_for_another_system_is_refused(
    db, monkeypatch
) -> None:
    claim_database(db, "work")
    before = claim_events(db)
    seen = outcome_reads(monkeypatch)
    _fail_before_the_write(monkeypatch)
    code, payload, err = claim(monkeypatch, db, "home")
    assert code == EXIT_REFUSED, (payload, err)
    assert payload["error"]["code"] == "shared_database"
    assert claim_events(db) == before
    assert len(seen) == 1, seen


def test_verify_system_reads_the_claim_the_verb_wrote(db, monkeypatch) -> None:
    assert claim(monkeypatch, db, "work")[0] == 0
    code, out, err = invoke(monkeypatch, ["--db", db, "verify", "--system", "work"])
    assert code == 0, err
    code, out, err = invoke(monkeypatch, ["--db", db, "verify", "--system", "home"])
    assert code == EXIT_REFUSED

