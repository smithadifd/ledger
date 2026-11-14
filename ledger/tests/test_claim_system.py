"""``ledger claim-system NAME``: claim a database for its system.

The library (``Ledger.claim_system``) decides; the verb resolves the path the
way every write verb does and reports. Each test reads the database file
directly afterwards, so "writes nothing" is an event count, not a message.
"""
from __future__ import annotations

import contextlib
import inspect
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import ledger.dbfile as dbfile
from ledger import claim_database, open_ledger, store
from ledger.backup import backup_db
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


# --- the outcome a claim's transaction reached --------------------------------
#
# Two same-name callers on a fresh database each checkpoint after their
# commit, and one checkpoint can find the other's running (busy 1). The verb
# then settles the claim (Ledger.claim_outcome) on the open connection: the
# name's claim is on record, a bounded checkpoint retry clears, and the open
# proof's binding check (Ledger._settle_bound) passes. Anything short of that
# refuses.

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
    """Hold a read transaction on ``db`` in another process; yield its release.

    ``release()`` ends the read transaction and waits for the reader to exit;
    it runs again (as a no-op) when the block ends.
    """
    with running(
        [sys.executable, "-c", _READER, db],
        cwd=_LIB,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        read_ready_line(proc, "held")
        errors: list[str] = []

        def release() -> None:
            if proc.returncode is not None:
                return
            assert proc.stdin is not None
            proc.stdin.write("\n")
            proc.stdin.flush()
            _out, err = proc.communicate(timeout=30)
            errors.append(err)

        try:
            yield release
        finally:
            release()
            assert proc.returncode == 0, errors


@pytest.fixture
def short_busy(monkeypatch) -> None:
    """Shorten the connection busy timeout, so a checkpoint the reader holds
    reports busy in a fraction of a second rather than five."""
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 300)


def outcome_reads(monkeypatch) -> list[str]:
    """Record each refusal the verb settled, so a test knows the settle path ran."""
    seen: list[str] = []
    original = Ledger.claim_outcome

    def spy(self, system, refusal):
        seen.append(str(refusal))
        return original(self, system, refusal)

    monkeypatch.setattr(Ledger, "claim_outcome", spy)
    return seen


def settling_checkpoints(monkeypatch, release=None, before=None) -> list[str]:
    """Record each checkpoint the settlement starts, as SQLite starts it.

    With ``release`` (a reader's release), call it just before the
    settlement's second checkpoint starts: the first finds the reader, the
    second does not. With ``before``, call ``before(lg)`` as the settlement
    starts.
    """
    started: list[str] = []
    original = Ledger.claim_outcome

    def settle(self, system, refusal):
        def trace(sql: str) -> None:
            if "wal_checkpoint" in sql:
                started.append(sql)
                if release is not None and len(started) == 2:
                    release()

        if before is not None:
            before(self)
        self._conn.set_trace_callback(trace)
        try:
            return original(self, system, refusal)
        finally:
            self._conn.set_trace_callback(None)

    monkeypatch.setattr(Ledger, "claim_outcome", settle)
    return started


def proof_checks(monkeypatch, db: str, act=None) -> list[str]:
    """Record each binding check on ``db`` (``verify_bound``'s first seam).

    With ``act``, run it once at the first check made while the settlement
    (``Ledger.claim_outcome``) runs.
    """
    seen: list[str] = []
    settling: list[bool] = []
    fired: list[bool] = []
    original = Ledger.claim_outcome

    def settle(self, system, refusal):
        settling.append(True)
        return original(self, system, refusal)

    def seam(stage: str, path: Path) -> None:
        if stage != "before_settle" or path.resolve() != Path(db).resolve():
            return
        seen.append("settling" if settling else "commit")
        if act is not None and settling and not fired:
            fired.append(True)
            act()

    monkeypatch.setattr(Ledger, "claim_outcome", settle)
    monkeypatch.setattr(dbfile, "_TEST_SEAM", seam)
    return seen


def other_ledger(path: Path) -> Path:
    """A closed, unclaimed ledger at ``path`` holding one item."""
    with open_ledger(path) as lg:
        lg.append(
            kind="item.created",
            payload={"type": "ticket", "title": "swapped-in"},
            item_id="OTHER-1",
            idempotency_key="seed-OTHER-1",
        )
    return path


def test_a_claim_whose_checkpoint_clears_on_a_retry_reports_claimed(
    db, monkeypatch, short_busy
) -> None:
    # The claim commits, then the checkpoint after it cannot finish while the
    # reader holds the file. The settlement's first checkpoint still finds the
    # reader; the reader lets go before the second, which finishes. The claim
    # is on record and in the main file, the open proof still binds, and this
    # call wrote it: exit 0, claimed.
    with open_ledger(db):
        pass
    seen = outcome_reads(monkeypatch)
    with a_reader_holding(db) as release:
        started = settling_checkpoints(monkeypatch, release)
        checks = proof_checks(monkeypatch, db)
        code, payload, err = claim(monkeypatch, db, "work")
    assert code == 0, (payload, err)
    assert payload["data"]["claimed"] is True
    assert [e["system"] for e in claim_events(db)] == ["work"]
    assert len(seen) == 1 and "checkpoint of" in seen[0], seen
    assert len(started) == 2, started
    # The settlement makes one binding check, after its clearing checkpoint.
    assert checks.count("settling") == 1 and checks[-1] == "settling", checks


def test_a_repeat_claim_whose_checkpoint_clears_on_a_retry_reports_already_claimed(
    db, monkeypatch, short_busy
) -> None:
    # The same name is on record before the call; an append after the reader
    # took its snapshot leaves WAL frames the checkpoint cannot write back
    # until the reader lets go, before the settlement's second checkpoint.
    claim_database(db, "work")
    seen = outcome_reads(monkeypatch)
    with a_reader_holding(db) as release:
        started = settling_checkpoints(monkeypatch, release)
        seed(monkeypatch, db, "it-1")
        before = event_count(db)
        code, payload, err = claim(monkeypatch, db, "work")
    assert code == 0, (payload, err)
    assert payload["data"]["claimed"] is False
    assert event_count(db) == before
    assert [e["system"] for e in claim_events(db)] == ["work"]
    assert len(seen) == 1 and "checkpoint of" in seen[0], seen
    assert len(started) == 2, started


def test_a_claim_whose_checkpoint_stays_busy_past_the_retry_budget_is_refused(
    db, monkeypatch, short_busy
) -> None:
    # The claim commits, and the reader holds the file through every
    # settlement checkpoint. The claim is on record but never reaches the
    # main file, so it is not proven: exit 3, shared_database.
    with open_ledger(db):
        pass
    seen = outcome_reads(monkeypatch)
    started = settling_checkpoints(monkeypatch)
    with a_reader_holding(db):
        code, payload, err = claim(monkeypatch, db, "work")
        # Read while the reader still holds: the claim is on record.
        assert [e["system"] for e in claim_events(db)] == ["work"]
    assert code == EXIT_REFUSED, (payload, err)
    assert payload["error"]["code"] == "shared_database"
    assert "checkpoint of" in payload["error"]["message"]
    assert len(seen) == 1 and "checkpoint of" in seen[0], seen
    assert 2 <= len(started) <= store.CLAIM_SETTLE_ATTEMPTS, started


def _deny_checkpoints(lg) -> None:
    def authorizer(action, arg1, *_rest):
        if action == sqlite3.SQLITE_PRAGMA and arg1 == "wal_checkpoint":
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    lg._conn.set_authorizer(authorizer)


def test_a_retry_checkpoint_that_raises_after_a_busy_one_is_refused(
    db, monkeypatch, short_busy
) -> None:
    # The claim commits and its checkpoint finds the reader (busy 1). The
    # settlement's checkpoint then raises sqlite3.Error. The claim is on
    # record, yet unproven: exit 3, shared_database.
    with open_ledger(db):
        pass
    seen = outcome_reads(monkeypatch)
    with a_reader_holding(db):
        settling_checkpoints(monkeypatch, before=_deny_checkpoints)
        code, payload, err = claim(monkeypatch, db, "work")
    assert code == EXIT_REFUSED, (payload, err)
    assert payload["error"]["code"] == "shared_database"
    assert "checkpoint of" in payload["error"]["message"]
    assert [e["system"] for e in claim_events(db)] == ["work"]
    assert len(seen) == 1, seen


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
            env=_child_env(), capture_output=True, text=True, timeout=60,
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
    # Commit, then an I/O error, then a settlement checkpoint that clears:
    # exit 0, claimed, and exactly one claim event.
    with open_ledger(db):
        pass
    seen = outcome_reads(monkeypatch)
    _commit_then_fail(monkeypatch)
    checks = proof_checks(monkeypatch, db)
    code, payload, err = claim(monkeypatch, db, "work")
    assert code == 0, (payload, err)
    assert payload["data"]["claimed"] is True
    assert [e["system"] for e in claim_events(db)] == ["work"]
    assert len(seen) == 1 and "could not be read" in seen[0], seen
    # The replaced transaction makes no check after its commit: the
    # settlement's binding check is the one before the report.
    assert checks.count("settling") == 1 and checks[-1] == "settling", checks
    assert len(claim_events(db)) == 1


def test_a_settling_checkpoint_that_raises_after_an_io_error_is_refused(
    db, monkeypatch
) -> None:
    # The claim commits and an I/O error follows; the read-back finds the
    # claim, but the settlement's checkpoint raises (as it can on a connection
    # an I/O error left unusable). The claim is on record, yet unproven: exit
    # 3, shared_database.
    with open_ledger(db):
        pass
    seen = outcome_reads(monkeypatch)
    _commit_then_fail(monkeypatch, _deny_checkpoints)
    code, payload, err = claim(monkeypatch, db, "work")
    assert code == EXIT_REFUSED, (payload, err)
    assert payload["error"]["code"] == "shared_database"
    assert "could not be read" in payload["error"]["message"]
    assert [e["system"] for e in claim_events(db)] == ["work"]
    assert len(seen) == 1, seen


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


def test_a_settlement_inside_an_open_transaction_is_refused(db, monkeypatch) -> None:
    # A read inside an open transaction sees rows that were never committed,
    # so the settlement refuses before it reads. The checkpoint is stubbed to
    # clear, so only that refusal stands between the uncommitted claim and a
    # report.
    monkeypatch.setattr(Ledger, "_checkpoint_within_budget", lambda self: True)
    with open_ledger(db) as lg:
        with pytest.raises(store.UnsettledClaimError) as info:
            with lg.transaction():
                written = lg._append(
                    kind="ledger.system",
                    payload={"system": "work"},
                    idempotency_key="ledger.system:work",
                    internal=True,
                    ignore_retry_attribution=False,
                )
                lg.claim_outcome(
                    "work",
                    store.UnsettledClaimError(
                        "unsettled", event=written, committed=False, data_version=None
                    ),
                )
        assert str(info.value) == "unsettled"
    assert claim_events(db) == []


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
    db, monkeypatch, short_busy
) -> None:
    # The claim commits and its checkpoint cannot finish while the reader
    # holds the file. Before the read-back another caller commits an
    # unrelated event, so the connection's data version moves on; the
    # commit is known to have finished, so the claim is still this call's.
    # The reader lets go before the settlement's second checkpoint.
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
    with a_reader_holding(db) as release:
        started = settling_checkpoints(monkeypatch, release)
        code, payload, err = claim(monkeypatch, db, "work")
    assert code == 0, (payload, err)
    assert payload["data"]["claimed"] is True
    assert [e["system"] for e in claim_events(db)] == ["work"]
    assert len(seen) == 1 and "checkpoint of" in seen[0], seen
    assert len(started) == 2, started


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


def test_a_busy_checkpoint_on_a_database_claimed_for_another_system_is_refused(
    db, monkeypatch, short_busy
) -> None:
    # Another system's claim is refused inside the transaction, before any
    # checkpoint: the settlement never runs and nothing is written.
    claim_database(db, "work")
    before = event_count(db)
    seen = outcome_reads(monkeypatch)
    with a_reader_holding(db):
        code, payload, err = claim(monkeypatch, db, "home")
    assert code == EXIT_REFUSED, (payload, err)
    assert payload["error"]["code"] == "shared_database"
    assert "'work'" in payload["error"]["message"]
    assert event_count(db) == before
    assert [e["system"] for e in claim_events(db)] == ["work"]
    assert seen == []


# --- the settlement's one binding check is the open proof ---------------------

# Tokens of a path or file-identity check. The claim path checks the path only
# through the open proof (Ledger._settle_bound, which runs verify_bound).
_OWN_BINDING_TOKENS = (
    "os.stat",
    "os.lstat",
    "os.fstat",
    "os.path.samefile",
    "samefile",
    "st_ino",
    "st_dev",
    "st_nlink",
    "open_ledger(",
    "connect(",
    "checked_connect",
)


def test_the_claim_path_adds_no_binding_check_of_its_own() -> None:
    import ledger.verbs.claim_system as verb

    sources = {
        "Ledger.claim_system": inspect.getsource(Ledger.claim_system),
        "Ledger.claim_outcome": inspect.getsource(Ledger.claim_outcome),
        "Ledger._checkpoint_within_budget": inspect.getsource(
            Ledger._checkpoint_within_budget
        ),
        "ledger.verbs.claim_system": inspect.getsource(verb),
    }
    found = {
        name: [token for token in _OWN_BINDING_TOKENS if token in source]
        for name, source in sources.items()
    }
    assert found == {name: [] for name in sources}, found
    # Both reports settle through the open proof, after their last checkpoint.
    direct = sources["Ledger.claim_system"]
    assert direct.count("self._settle_bound()") == 1
    assert direct.index("self._settle_bound()") > direct.rindex("wal_checkpoint")
    settled = sources["Ledger.claim_outcome"]
    assert settled.count("self._settle_bound()") == 1
    assert settled.index("self._settle_bound()") > settled.rindex(
        "self._checkpoint_within_budget()"
    )
    assert inspect.getsource(Ledger._settle_bound).count("verify_bound(self._proof)") == 1


def test_a_path_swapped_after_the_clearing_checkpoint_is_refused_through_the_proof(
    db, tmp_path: Path, monkeypatch, short_busy
) -> None:
    # The claim commits, its checkpoint finds the reader, the settlement's
    # second checkpoint clears; then, before the report, another database is
    # renamed onto the path. The open proof's binding check refuses
    # (db_swapped), and the database now at the path is left unclaimed.
    with open_ledger(db):
        pass
    other = other_ledger(tmp_path / "other.db")
    seen = outcome_reads(monkeypatch)
    with a_reader_holding(db) as release:
        started = settling_checkpoints(monkeypatch, release)
        checks = proof_checks(monkeypatch, db, lambda: os.replace(other, db))
        code, payload, err = claim(monkeypatch, db, "work")
    assert code == EXIT_REFUSED, (payload, err)
    assert payload["error"]["code"] == "db_swapped"
    assert len(seen) == 1 and "checkpoint of" in seen[0], seen
    assert len(started) == 2, started
    assert checks.count("settling") == 1 and checks[-1] == "settling", checks
    assert claim_events(db) == []
    assert event_count(db) == 1


def test_a_backup_onto_the_path_after_the_busy_checkpoint_is_refused(
    db, tmp_path: Path, monkeypatch, short_busy
) -> None:
    # A concurrent backup publishes another database at the path after the
    # claim's first checkpoint reported busy. The settlement's checkpoint then
    # clears on the opened file, which the path no longer names: the claim
    # is refused (db_swapped), never reported for the unclaimed database now
    # at the path.
    with open_ledger(db):
        pass
    source = other_ledger(tmp_path / "other.db")
    seen = outcome_reads(monkeypatch)
    with a_reader_holding(db) as release:
        started = settling_checkpoints(
            monkeypatch, release, before=lambda lg: backup_db(source, db)
        )
        code, payload, err = claim(monkeypatch, db, "work")
    assert code == EXIT_REFUSED, (payload, err)
    assert payload["error"]["code"] == "db_swapped"
    assert len(seen) == 1 and "checkpoint of" in seen[0], seen
    assert len(started) == 2, started
    assert claim_events(db) == []
    assert event_count(db) == 1


def test_a_path_swapped_between_the_open_and_the_claim_is_refused(
    db, tmp_path: Path, monkeypatch
) -> None:
    # The verb's open proved one file; another database is renamed onto the
    # path before the claim's transaction. The claim commits into the opened
    # file, and the check after its commit refuses (db_swapped): the
    # swapped-in database file is left as it was. (Its frames went to the
    # opened connection's -wal, which still carries the -wal name; that name
    # is the open proof's to keep, so this test reads the main file only.)
    with open_ledger(db):
        pass
    other = other_ledger(tmp_path / "other.db")
    other_bytes = other.read_bytes()
    original = Ledger.claim_system

    def claim_system(self, system, **kwargs):
        os.replace(other, db)
        return original(self, system, **kwargs)

    seen = outcome_reads(monkeypatch)
    monkeypatch.setattr(Ledger, "claim_system", claim_system)
    code, payload, err = claim(monkeypatch, db, "work")
    assert code == EXIT_REFUSED, (payload, err)
    assert payload["error"]["code"] == "db_swapped"
    assert seen == []
    assert Path(db).read_bytes() == other_bytes


def test_a_hard_link_added_after_the_commit_is_refused_through_the_proof(
    db, monkeypatch, short_busy
) -> None:
    # The claim commits; then, before the checkpoint, a second name is linked
    # to the database after the open-time check. The reader holds the file
    # through the claim's checkpoint and lets go before the settlement's
    # second one, which clears. The open proof's binding check refuses the
    # file the path names now that it has two links (db_swapped).
    with open_ledger(db):
        pass
    original = Ledger.transaction
    alias = Path(db).with_name("alias.db")

    @contextlib.contextmanager
    def transaction(self):
        with original(self):
            yield
        os.link(db, alias)

    seen = outcome_reads(monkeypatch)
    monkeypatch.setattr(Ledger, "transaction", transaction)
    with a_reader_holding(db) as release:
        started = settling_checkpoints(monkeypatch, release)
        code, payload, err = claim(monkeypatch, db, "work")
    assert alias.exists()
    assert code == EXIT_REFUSED, (payload, err)
    assert payload["error"]["code"] == "db_swapped"
    assert len(seen) == 1 and "checkpoint of" in seen[0], seen
    assert len(started) == 2, started


def test_a_hard_link_held_through_every_checkpoint_is_refused(
    db, monkeypatch, short_busy
) -> None:
    # As above, but the reader holds the file through every settlement
    # checkpoint: the claim never reaches the main file and is not settled,
    # whatever the path now names (exit 3, shared_database).
    with open_ledger(db):
        pass
    original = Ledger.transaction
    alias = Path(db).with_name("alias.db")

    @contextlib.contextmanager
    def transaction(self):
        with original(self):
            yield
        os.link(db, alias)

    seen = outcome_reads(monkeypatch)
    started = settling_checkpoints(monkeypatch)
    monkeypatch.setattr(Ledger, "transaction", transaction)
    with a_reader_holding(db):
        code, payload, err = claim(monkeypatch, db, "work")
    assert alias.exists()
    assert code == EXIT_REFUSED, (payload, err)
    assert payload["error"]["code"] == "shared_database"
    assert "checkpoint of" in payload["error"]["message"]
    assert len(seen) == 1 and "checkpoint of" in seen[0], seen
    assert len(started) >= 2, started


def test_verify_system_reads_the_claim_the_verb_wrote(db, monkeypatch) -> None:
    assert claim(monkeypatch, db, "work")[0] == 0
    code, out, err = invoke(monkeypatch, ["--db", db, "verify", "--system", "work"])
    assert code == 0, err
    code, out, err = invoke(monkeypatch, ["--db", db, "verify", "--system", "home"])
    assert code == EXIT_REFUSED

