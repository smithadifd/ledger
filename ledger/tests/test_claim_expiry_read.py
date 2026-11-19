"""An expired claim reads as open; old lease-less claims are flagged; reads write nothing.

Read side only: ``lease.read_status`` projects the status that query, ready
and card show. Guards and write verbs keep the recorded status, so a later
claim still records the holder it took over. Doctor lists stale claims (no
lease stamp, claim event older than 7 days) with a release command it never
runs, and refreshes the search index before it reports it.

The clock seam is ``ledger.lease.now`` (and ``store._now`` for recorded_at),
as in ``test_claim_lease.py``. Every database is a temporary ledger.
"""
from __future__ import annotations

import json
import os
import shlex
import sqlite3
import stat
from datetime import timedelta
from pathlib import Path

import pytest

from ledger import lease, search, store
from ledger.paths import resolve_db_path
from ledger.store import open_ledger

from test_cli import invoke, seed

HOUR = 3600
DAY = 24 * HOUR


@pytest.fixture
def clock(monkeypatch):
    real = lease.now
    real_stamp = store._now

    class Clock:
        def at(self, seconds_from_now: int) -> None:
            moved = lambda: real() + timedelta(seconds=seconds_from_now)  # noqa: E731
            monkeypatch.setattr(lease, "now", moved)
            monkeypatch.setattr(store, "_now", lambda: lease.stamp(moved()))

        def real(self) -> None:
            monkeypatch.setattr(lease, "now", real)
            monkeypatch.setattr(store, "_now", real_stamp)

    return Clock()


@pytest.fixture
def db(tmp_path: Path, monkeypatch) -> str:
    path = str(tmp_path / "ledger.db")
    for item_id in ("it-1", "it-2", "it-3"):
        seed(monkeypatch, path, item_id)
    return path


def run(monkeypatch, db, agent, *argv):
    code, out, err = invoke(
        monkeypatch, ["--json", "--db", db, *argv], env={"LEDGER_AGENT": agent}
    )
    assert "Traceback" not in err
    return code, (json.loads(out) if out.strip() else None), err


def text(monkeypatch, db, *argv) -> str:
    code, out, err = invoke(monkeypatch, ["--db", db, *argv])
    assert code == 0, err
    return out


def by_id(items: list[dict]) -> dict[str, dict]:
    return {item["item_id"]: item for item in items}


def query_item(monkeypatch, db, item_id) -> dict:
    code, body, err = run(monkeypatch, db, "ops", "query", "--item", item_id)
    assert code == 0, err
    [item] = body["data"]["items"]
    return item


def lease_row(db, item_id):
    with open_ledger(db, create=False) as lg:
        return lg.item(item_id), lease.current_lease(lg._conn, item_id)


def chain_tail(db) -> tuple[int, str]:
    with open_ledger(db, create=False) as lg:
        events = lg.events()
    return len(events), events[-1].hash


def claim_expired(monkeypatch, db, clock, item_id="it-1", agent="alice") -> None:
    clock.at(-5 * HOUR)  # the default lease is 4 hours
    code, _body, err = run(monkeypatch, db, agent, "claim", item_id)
    assert code == 0, err
    clock.real()


def claim_without_lease(monkeypatch, db, clock, item_id, holder, seconds_ago) -> None:
    """A claim with no ``expires_at``, recorded ``seconds_ago``."""
    clock.at(-seconds_ago)
    code, _body, err = run(
        monkeypatch, db, "ops", "state", item_id, "--status", "claimed", "--owner", holder
    )
    assert code == 0, err
    clock.real()


# --- 1. guard parity ---------------------------------------------------------


def test_expired_claim_reads_open_in_query_ready_and_card(db, monkeypatch, clock) -> None:
    claim_expired(monkeypatch, db, clock)
    row, held = lease_row(db, "it-1")
    assert row["status"] == "claimed"  # the recorded status is untouched
    assert lease.is_expired(held["expires_at"])

    item = query_item(monkeypatch, db, "it-1")
    assert item["status"] == "open"
    assert item["recorded_status"] == "claimed"
    assert item["expired_holder"] == "alice"
    assert item["expires_at"] == held["expires_at"]

    code, body, err = run(monkeypatch, db, "ops", "ready")
    assert code == 0, err
    ready = by_id(body["data"]["items"])["it-1"]
    assert (ready["status"], ready["recorded_status"], ready["expired_holder"]) == (
        "open", "claimed", "alice")
    assert ready["expires_at"] == held["expires_at"]

    code, body, err = run(monkeypatch, db, "ops", "card", "it-1")
    assert code == 0, err
    card = body["data"]
    assert (card["status"], card["recorded_status"], card["expired_holder"]) == (
        "open", "claimed", "alice")
    assert card["expires_at"] == held["expires_at"]

    card_text = text(monkeypatch, db, "card", "it-1")
    assert "\nstatus: open\n" in card_text
    assert (
        f"expired claim: holder alice, lease expired {held['expires_at']}, "
        "recorded status claimed" in card_text
    )
    assert "it-1\tticket\topen\t" in text(monkeypatch, db, "query", "--item", "it-1")
    assert "it-1\tticket\topen\t" in text(monkeypatch, db, "ready")
    # --status filters on the status a reader sees.
    code, body, _ = run(monkeypatch, db, "ops", "query", "--status", "claimed")
    assert code == 1
    code, body, _ = run(monkeypatch, db, "ops", "query", "--status", "open")
    assert "it-1" in by_id(body["data"]["items"])


def test_projection_matches_the_takeover_guard_and_a_later_claim_names_the_prior_holder(
    db, monkeypatch, clock
) -> None:
    claim_expired(monkeypatch, db, clock)
    row, held = lease_row(db, "it-1")
    moment = lease.now()
    assert lease.read_status(row, held, at=moment) == "open"
    assert lease.takeover_holder(row, held, "bob", moment) == "alice"

    code, body, err = run(monkeypatch, db, "bob", "claim", "it-1")
    assert code == 0, err
    assert body["data"]["event"]["payload"]["prior_holder"] == "alice"

    row, held = lease_row(db, "it-1")
    assert lease.read_status(row, held) == "claimed"
    assert lease.takeover_holder(row, held, "carol", lease.now()) is None
    item = query_item(monkeypatch, db, "it-1")
    assert (item["status"], item["recorded_status"]) == ("claimed", "claimed")
    assert item["expired_holder"] is None and item["expires_at"] is None


def test_a_live_claim_and_a_lease_less_claim_keep_claimed(db, monkeypatch, clock) -> None:
    code, _body, err = run(monkeypatch, db, "alice", "claim", "it-2")
    assert code == 0, err
    claim_without_lease(monkeypatch, db, clock, "it-3", "carol", 30 * DAY)
    for item_id, holder in (("it-2", "alice"), ("it-3", "carol")):
        row, held = lease_row(db, item_id)
        moment = lease.now()
        assert lease.takeover_holder(row, held, "bob", moment) is None
        assert lease.read_status(row, held, at=moment) == "claimed"
        item = query_item(monkeypatch, db, item_id)
        assert (item["status"], item["recorded_status"]) == ("claimed", "claimed")
        assert item["expired_holder"] is None and item["expires_at"] is None
        code, body, err = run(monkeypatch, db, "ops", "card", item_id)
        assert code == 0, err
        assert body["data"]["status"] == "claimed"
        assert body["data"]["recorded_status"] == "claimed"
    code, body, _ = run(monkeypatch, db, "ops", "ready")
    assert "it-2" not in by_id(body["data"]["items"])
    assert "it-3" not in by_id(body["data"]["items"])


@pytest.mark.parametrize(
    "stamp",
    ["2000-01-01T00:00:00.000Z", "2999-01-01T00:00:00.000Z", None],
)
def test_read_status_is_open_exactly_when_the_lease_is_expired(stamp) -> None:
    row = {"item_id": "it-1", "status": "claimed", "owner": "alice", "claim": None}
    held = {"item_id": "it-1", "holder": "alice", "expires_at": stamp}
    expected = "open" if lease.is_expired(stamp) else "claimed"
    assert lease.read_status(row, held) == expected
    assert (expected == "open") == (lease.takeover_holder(row, held, "bob", lease.now()) == "alice")


def test_an_unreadable_stamp_never_reads_open() -> None:
    # Text that sorts before now but is no stamp: fail closed, keep claimed.
    row = {"item_id": "it-1", "status": "claimed", "owner": "alice", "claim": None}
    for stamp in ("0000-00-00T00:00:00.000Z", "1999-13-45T99:99:99.000Z", "", "-"):
        held = {"item_id": "it-1", "holder": "alice", "expires_at": stamp}
        assert lease.read_status(row, held) == "claimed", stamp


def test_read_status_keeps_every_other_recorded_status() -> None:
    held = {"item_id": "it-1", "holder": "alice", "expires_at": "2000-01-01T00:00:00.000Z"}
    for status in ("open", "blocked", "closed"):
        row = {"item_id": "it-1", "status": status, "owner": None, "claim": None}
        assert lease.read_status(row, held) == status
    claimed = {"item_id": "it-1", "status": "claimed", "owner": "alice", "claim": None}
    assert lease.read_status(claimed, None) == "claimed"
    other = dict(held, item_id="it-9")
    assert lease.read_status(claimed, other) == "claimed"


# --- 3. reads write nothing --------------------------------------------------


def test_query_card_ready_and_doctor_write_no_events(db, monkeypatch, clock) -> None:
    claim_expired(monkeypatch, db, clock)
    claim_without_lease(monkeypatch, db, clock, "it-3", "carol", 10 * DAY)
    before = chain_tail(db)
    for argv in (
        ["query"],
        ["query", "--item", "it-1"],
        ["ready"],
        ["card", "it-1"],
        ["card", "it-3"],
        ["doctor"],
    ):
        for json_mode in (True, False):
            prefix = ["--json"] if json_mode else []
            code, _out, err = invoke(monkeypatch, [*prefix, "--db", db, *argv])
            assert code == 0, (argv, err)
            assert chain_tail(db) == before, argv
    row, _held = lease_row(db, "it-1")
    assert row["status"] == "claimed"


# --- 4. stale claims ---------------------------------------------------------


def test_doctor_lists_only_lease_less_claims_older_than_seven_days(
    tmp_path, db, monkeypatch, clock
) -> None:
    seed(monkeypatch, db, "it-4")
    claim_without_lease(monkeypatch, db, clock, "it-1", "old-seat", int(7.5 * DAY))
    claim_without_lease(monkeypatch, db, clock, "it-2", "young-seat", int(6.5 * DAY))
    clock.at(-10 * DAY)
    assert run(monkeypatch, db, "lease-seat", "claim", "it-3")[0] == 0  # stamped, expired
    clock.real()
    code, _b, err = run(monkeypatch, db, "live-seat", "claim", "it-4")  # stamped, live
    assert code == 0, err

    before = chain_tail(db)
    code, body, err = run(monkeypatch, db, "ops", "doctor")
    assert code == 0, err
    data = body["data"]
    stale = data["stale_claims"]
    expired = data["expired_claims"]
    assert stale["status"] == "ok"
    assert [c["item_id"] for c in stale["items"]] == ["it-1"]
    assert [c["item_id"] for c in expired["items"]] == ["it-3"]
    [entry] = stale["items"]
    assert entry["holder"] == "old-seat"
    assert entry["expires_at"] is None
    assert 7 * DAY < entry["age_seconds"] < 8 * DAY
    command = entry["release_command"]
    assert isinstance(command, str)
    with open_ledger(db, create=False) as lg:
        revision = max(e.id for e in lg.events() if e.item_id == "it-1")
    assert shlex.split(command) == [
        "ledger", "--db", str(resolve_db_path(db)), "state", "--status", "open",
        "--expect-revision", str(revision), "--", "it-1",
    ]

    out = text(monkeypatch, db, "doctor")
    [line] = [ln for ln in out.splitlines() if ln.startswith("stale claim: ")]
    head, age, release = line.rsplit("\t", 2)
    assert head == "stale claim: it-1\tholder=old-seat"
    assert age.startswith("age=") and age.endswith("s")
    assert abs(int(age[4:-1]) - entry["age_seconds"]) <= 5
    assert release == f"release: {command}"
    assert "stale claim: it-2" not in out and "stale claim: it-3" not in out

    # Printed, never run: nothing was written and it-1 is still claimed.
    assert chain_tail(db) == before
    assert query_item(monkeypatch, db, "it-1")["status"] == "claimed"

    # query flags the stale claim (and only it).
    items = by_id(run(monkeypatch, db, "ops", "query")[1]["data"]["items"])
    assert items["it-1"]["stale_claim"] is True
    assert items["it-1"]["claim_age_seconds"] == pytest.approx(entry["age_seconds"], abs=5)
    for other in ("it-2", "it-3", "it-4"):
        assert items[other]["stale_claim"] is False
        assert items[other]["claim_age_seconds"] is None

    # The printed command is a real release: run by hand, it opens the item.
    argv = shlex.split(command)
    assert argv[0] == "ledger"
    code, _out, err = invoke(monkeypatch, argv[1:])
    assert code == 0, err
    assert query_item(monkeypatch, db, "it-1")["recorded_status"] == "open"


def test_a_release_command_refuses_after_a_newer_write(db, monkeypatch, clock) -> None:
    claim_without_lease(monkeypatch, db, clock, "it-1", "old-seat", 9 * DAY)
    [entry] = run(monkeypatch, db, "ops", "doctor")[1]["data"]["stale_claims"]["items"]
    code, _b, err = run(monkeypatch, db, "ops", "note", "it-1", "--text", "still here")
    assert code == 0, err
    argv = shlex.split(entry["release_command"])
    code, _out, _err = invoke(monkeypatch, argv[1:])
    assert code == 5  # --expect-revision: the claim moved on since doctor read it
    assert query_item(monkeypatch, db, "it-1")["recorded_status"] == "claimed"


def test_the_release_command_names_the_database_doctor_opened(
    tmp_path, monkeypatch, clock
) -> None:
    # A path with a space and a single quote, read through LEDGER_DB (no --db):
    # the command must still name it, quoted, and never fall back to whatever
    # LEDGER_DB holds when someone runs it.
    home = tmp_path / "it's a ledger"
    home.mkdir()
    db = str(home / "ledger.db")
    other = str(tmp_path / "other.db")
    for path in (db, other):
        seed(monkeypatch, path, "it-1")
    claim_without_lease(monkeypatch, db, clock, "it-1", "old-seat", 9 * DAY)
    claim_without_lease(monkeypatch, other, clock, "it-1", "other-seat", 9 * DAY)
    code, out, err = invoke(
        monkeypatch, ["--json", "doctor"], env={"LEDGER_DB": db}
    )
    assert code == 0, err
    [entry] = json.loads(out)["data"]["stale_claims"]["items"]
    command = entry["release_command"]
    argv = shlex.split(command)
    assert argv[:3] == ["ledger", "--db", str(resolve_db_path(db))]
    assert argv[-2:] == ["--", "it-1"]
    assert "'" in str(resolve_db_path(db)) and " " in str(resolve_db_path(db))

    # Run with LEDGER_DB pointing at another database: only the named one changes.
    code, _out, err = invoke(monkeypatch, argv[1:], env={"LEDGER_DB": other})
    assert code == 0, err
    assert query_item(monkeypatch, db, "it-1")["recorded_status"] == "open"
    assert query_item(monkeypatch, other, "it-1")["recorded_status"] == "claimed"


# --- 5. doctor refreshes the search index ------------------------------------


def _doctor(monkeypatch, db):
    code, body, err = run(monkeypatch, db, "ops", "doctor")
    return code, body, err


def _shape(value):
    if isinstance(value, dict):
        return {key: _shape(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return type(value).__name__


def test_doctor_after_a_write_reports_search_current(db, monkeypatch) -> None:
    code, body, err = _doctor(monkeypatch, db)
    assert code == 0, err
    assert body["data"]["search"]["status"] == "current"
    assert (Path(db).parent / "search.db").is_file()
    code, _b, err = run(monkeypatch, db, "ops", "note", "it-1", "--text", "a later write")
    assert code == 0, err
    code, body, err = _doctor(monkeypatch, db)
    assert code == 0, err
    assert body["data"]["search"]["status"] == "current"
    assert body["data"]["search"]["severity"] == "ok"
    assert "\nsearch: current\n" in text(monkeypatch, db, "doctor")


def test_doctor_in_an_unwritable_directory_keeps_exit_code_and_json_shape(
    db, monkeypatch
) -> None:
    code, writable, err = _doctor(monkeypatch, db)
    assert code == 0, err
    code, _b, err = run(monkeypatch, db, "ops", "note", "it-1", "--text", "a later write")
    assert code == 0, err
    directory = Path(db).parent
    original = stat.S_IMODE(directory.stat().st_mode)
    os.chmod(directory, 0o555)
    try:
        assert not os.access(directory, os.W_OK), "the test needs an unwritable directory"
        code, body, err = _doctor(monkeypatch, db)
        text_code, text_out, text_err = invoke(monkeypatch, ["--db", db, "doctor"])
        readable = search.index_status(db)
    finally:
        os.chmod(directory, original)
    assert code == 0, err
    assert text_code == 0, text_err
    # The same parts as a doctor that could refresh; the search part is the
    # state index_status reads, with its own keys.
    found = body["data"].pop("search")
    writable["data"].pop("search")
    assert _shape(body["data"]) == _shape(writable["data"])
    assert found == readable
    assert found["status"] == "behind"  # the state doctor can read
    assert found["severity"] == "info"
    assert "1 new event will be indexed on the next search" in text_out


# --- 6. doctor's refresh never replaces the database or a non-regular path ---


def _event_hashes(path) -> list[tuple[int, str]]:
    """Every event's id and hash, read without changing the database."""
    try:
        with store.open_ledger_readonly(path) as lg:
            return [(event.id, event.hash) for event in lg.events()]
    except (store.LedgerError, sqlite3.Error) as exc:
        pytest.fail(f"the ledger at {path} no longer reads: {type(exc).__name__}: {exc}")


def test_doctor_on_a_ledger_named_search_db_keeps_its_events(
    tmp_path, db, monkeypatch
) -> None:
    # The index path of a ledger named search.db is the ledger itself.
    code, normal, err = _doctor(monkeypatch, db)
    assert code == 0, err
    named = tmp_path / "named" / search.SEARCH_DB_NAME
    named.parent.mkdir()
    seed(monkeypatch, str(named), "it-1")
    before = _event_hashes(named)
    assert len(before) == 1
    assert search.search_db_path(named) == resolve_db_path(named)
    code, body, err = _doctor(monkeypatch, str(named))
    assert code == 0, err
    assert _event_hashes(named) == before, "doctor changed the ledger's events"
    assert sorted(p.name for p in named.parent.iterdir() if p.name.endswith(".partial")) == []
    # The same JSON parts as a doctor on a ledger with its own index path; the
    # search part is the state index_status reads.
    found = body["data"].pop("search")
    normal["data"].pop("search")
    assert _shape(body["data"]) == _shape(normal["data"])
    assert found == search.index_status(named)


def test_doctor_on_a_ledger_named_search_db_in_another_case_keeps_its_events(
    tmp_path, monkeypatch
) -> None:
    # On a case-insensitive file system (the macOS default) the index path
    # names this ledger under another spelling: the real paths differ and the
    # inode is the same. On a case-sensitive one the index is another file,
    # and the events survive either way.
    named = tmp_path / search.SEARCH_DB_NAME.upper()
    seed(monkeypatch, str(named), "it-1")
    before = _event_hashes(named)
    assert len(before) == 1
    code, body, err = _doctor(monkeypatch, str(named))
    assert code == 0, err
    assert _event_hashes(named) == before, "doctor changed the ledger's events"
    assert body["data"]["search"] == search.index_status(named)


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_doctor_leaves_a_non_regular_index_path_alone(
    tmp_path, db, monkeypatch, kind
) -> None:
    index = Path(db).parent / search.SEARCH_DB_NAME
    target = tmp_path / "elsewhere.txt"
    if kind == "symlink":
        target.write_text("not an index\n")
        index.symlink_to(target)
    else:
        os.mkfifo(index)
    before = os.lstat(index)
    code, body, err = _doctor(monkeypatch, db)
    assert code == 0, err
    after = os.lstat(index)
    assert (after.st_ino, stat.S_IFMT(after.st_mode)) == (
        before.st_ino,
        stat.S_IFMT(before.st_mode),
    ), f"doctor replaced the {kind} at the index path"
    if kind == "symlink":
        assert os.readlink(index) == str(target)
        assert target.read_text() == "not an index\n"
    found = body["data"]["search"]
    assert found == search.index_status(db)  # the state doctor reads
    assert found["status"] == "missing"


def test_doctor_on_a_database_that_needs_migrations_reports_search_missing(
    tmp_path, monkeypatch
) -> None:
    # The first doctor after an upgrade: the newest migration is pending and
    # there is no index yet, so a refresh cannot read the ledger.
    path = tmp_path / "older.db"
    migrations = store.load_migrations()
    conn, _proof = store.connect(path)
    try:
        store.migrate(conn, migrations[:-1])
    finally:
        conn.close()
    code, body, err = _doctor(monkeypatch, str(path))
    assert code == 0, (body, err)
    assert body["data"]["schema_version"] == migrations[-2].version
    assert body["data"]["search"]["status"] == "missing"
    assert not (tmp_path / search.SEARCH_DB_NAME).exists()
    assert "\nsearch: missing" in text(monkeypatch, str(path), "doctor")
