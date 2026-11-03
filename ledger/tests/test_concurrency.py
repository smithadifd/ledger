"""Two to four writer processes append with no lost or duplicate event."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from ledger import open_ledger

_LIB = Path(__file__).resolve().parents[2]
WRITER = r"""
import sys
from ledger import open_ledger

db, wid, n = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
lg = open_ledger(db)
for i in range(n):
    lg.append(
        kind="item.created",
        payload={"type": "ticket", "title": "synthetic-w%s-%s" % (wid, i)},
        item_id="item-w%s-%s" % (wid, i),
        idempotency_key="key-w%s-%s" % (wid, i),
        harness="pytest",
        actor="writer-%s" % wid,
    )
lg.close()
"""


def test_concurrent_writer_processes(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    open_ledger(db).close()
    script = tmp_path / "writer.py"
    script.write_text(WRITER, encoding="utf-8")
    workers = 3
    each = 12
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(db), str(wid), str(each)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for wid in range(workers)
    ]
    failures = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=60)
        if proc.returncode != 0:
            failures.append(f"exit {proc.returncode}\n{stdout}\n{stderr}")
    assert failures == []
    lg = open_ledger(db)
    events = lg.events()
    assert len(events) == workers * each
    keys = [event.idempotency_key for event in events]
    assert len(keys) == len(set(keys))
    assert {event.id for event in events} == set(range(1, workers * each + 1))
    lg.verify()
    lg.close()


OPENER = r"""
import sys
import time
from pathlib import Path
from ledger import open_ledger

db, ready, go, wid = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
ready.write_text("1", encoding="utf-8")
for _ in range(10000):
    if go.exists():
        break
    time.sleep(0.001)
else:
    raise SystemExit("timeout waiting for go")
lg = open_ledger(db)
lg.append(
    kind="item.created",
    payload={"type": "ticket", "title": "fresh-%s" % wid},
    item_id="item-fresh-%s" % wid,
    idempotency_key="key-fresh-%s" % wid,
    harness="pytest",
    actor="opener-%s" % wid,
)
lg.close()
"""


def test_concurrent_first_open_fresh_db(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    script = tmp_path / "opener.py"
    script.write_text(OPENER, encoding="utf-8")
    go = tmp_path / "go"
    workers = 5
    readies = [tmp_path / f"ready-{wid}" for wid in range(workers)]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(db), str(readies[wid]), str(go), str(wid)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for wid in range(workers)
    ]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not all(p.exists() for p in readies):
        time.sleep(0.01)
    assert all(p.exists() for p in readies)
    assert not db.exists()
    go.write_text("1", encoding="utf-8")
    failures = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=60)
        if proc.returncode != 0:
            failures.append(f"exit {proc.returncode}\n{stdout}\n{stderr}")
    assert failures == []
    lg = open_ledger(db)
    events = lg.events()
    assert len(events) == workers
    keys = [event.idempotency_key for event in events]
    assert len(keys) == len(set(keys))
    assert {event.id for event in events} == set(range(1, workers + 1))
    lg.verify()
    lg.close()


CLAIM_RACER = r"""
import os
import sys
import time
from pathlib import Path
from ledger.cli import main

db, item, capacity, ready, go, out = sys.argv[1:7]
Path(ready).write_text("1", encoding="utf-8")
for _ in range(10000):
    if Path(go).exists():
        break
    time.sleep(0.001)
else:
    raise SystemExit("timeout waiting for go")
os.environ["LEDGER_HARNESS"] = "pytest"
os.environ["LEDGER_AGENT"] = "racer-%s" % item
code = main(["--db", db, "claim", item, "--capacity", capacity])
Path(out).write_text(str(code), encoding="utf-8")
raise SystemExit(code)
"""

NEW_RACER = r"""
import os
import sys
import time
from pathlib import Path
from ledger.cli import main

db, item, title, ready, go, out = sys.argv[1:7]
Path(ready).write_text("1", encoding="utf-8")
for _ in range(10000):
    if Path(go).exists():
        break
    time.sleep(0.001)
else:
    raise SystemExit("timeout waiting for go")
os.environ["LEDGER_HARNESS"] = "pytest"
os.environ["LEDGER_AGENT"] = "racer-%s" % title
code = main(["--db", db, "new", "--id", item, "--type", "ticket", "--title", title])
Path(out).write_text(str(code), encoding="utf-8")
raise SystemExit(code)
"""


def _run_barrier_pair(tmp_path: Path, script_text: str, argv_a: list[str], argv_b: list[str]) -> tuple[int, int]:
    script = tmp_path / "racer.py"
    script.write_text(script_text, encoding="utf-8")
    go = tmp_path / "go"
    if go.exists():
        go.unlink()
    ready_a = tmp_path / "ready-a"
    ready_b = tmp_path / "ready-b"
    out_a = tmp_path / "out-a"
    out_b = tmp_path / "out-b"
    for path in (ready_a, ready_b, out_a, out_b):
        if path.exists():
            path.unlink()
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), *argv_a, str(ready_a), str(go), str(out_a)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ),
        subprocess.Popen(
            [sys.executable, str(script), *argv_b, str(ready_b), str(go), str(out_b)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ),
    ]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (ready_a.exists() and ready_b.exists()):
        time.sleep(0.001)
    assert ready_a.exists() and ready_b.exists()
    go.write_text("1", encoding="utf-8")
    for proc in procs:
        proc.communicate(timeout=60)
    return int(out_a.read_text(encoding="utf-8")), int(out_b.read_text(encoding="utf-8"))


def test_claim_capacity_never_exceeded_across_processes(tmp_path: Path) -> None:
    trials = 20
    exceeded = 0
    for trial in range(trials):
        db = tmp_path / f"cap-{trial}.db"
        lg = open_ledger(db)
        for item_id in ("it-a", "it-b"):
            lg.append(
                kind="item.created",
                payload={"type": "ticket", "title": item_id},
                item_id=item_id,
                idempotency_key="seed-%s" % item_id,
                harness="pytest",
                actor="seed",
            )
        lg.close()
        trial_dir = tmp_path / f"cap-run-{trial}"
        trial_dir.mkdir()
        code_a, code_b = _run_barrier_pair(
            trial_dir,
            CLAIM_RACER,
            [str(db), "it-a", "1"],
            [str(db), "it-b", "1"],
        )
        lg = open_ledger(db)
        claimed = [row for row in lg.items() if row["status"] == "claimed"]
        lg.verify()
        lg.close()
        successes = int(code_a == 0) + int(code_b == 0)
        if len(claimed) > 1 or successes > 1:
            exceeded += 1
        assert len(claimed) <= 1
        assert successes == 1
        assert successes == len(claimed)
    assert exceeded == 0
    assert trials == 20


def test_new_duplicate_id_race(tmp_path: Path) -> None:
    trials = 20
    duplicates = 0
    for trial in range(trials):
        db = tmp_path / f"dup-{trial}.db"
        open_ledger(db).close()
        trial_dir = tmp_path / f"dup-run-{trial}"
        trial_dir.mkdir()
        code_a, code_b = _run_barrier_pair(
            trial_dir,
            NEW_RACER,
            [str(db), "it-dup", "title-a"],
            [str(db), "it-dup", "title-b"],
        )
        lg = open_ledger(db)
        created = [
            event
            for event in lg.events()
            if event.kind == "item.created" and event.item_id == "it-dup"
        ]
        lg.verify()
        lg.close()
        successes = int(code_a == 0) + int(code_b == 0)
        if len(created) > 1 or successes > 1:
            duplicates += 1
        assert len(created) == 1
        assert successes == 1
        refused = int(code_a == 3) + int(code_b == 3)
        assert refused == 1
    assert duplicates == 0
    assert trials == 20




PAIR_WRITER = r"""
import sys
import time
from pathlib import Path
from ledger import open_ledger

db, go, wid, n = sys.argv[1], Path(sys.argv[2]), sys.argv[3], int(sys.argv[4])
lg = open_ledger(db)
for _ in range(10000):
    if go.exists():
        break
    time.sleep(0.001)
for i in range(n):
    item = "item-w%s-%s" % (wid, i)
    lg.append_many([
        {"kind": "item.created", "item_id": item, "idempotency_key": item + ":created",
         "payload": {"type": "ticket", "title": "synthetic"}, "harness": "pytest"},
        {"kind": "item.state", "item_id": item, "idempotency_key": item + ":state",
         "payload": {"status": "closed"}, "harness": "pytest"},
    ])
lg.close()
"""


def test_concurrent_append_many_writers_never_lose_a_pair(tmp_path: Path) -> None:
    """append_many takes the write lock up front (BEGIN IMMEDIATE).

    A deferred BEGIN reads the tail first and loses the upgrade race to a
    concurrent writer, which shows up here as a failed writer process.
    """
    db = tmp_path / "ledger.db"
    open_ledger(db).close()
    script = tmp_path / "pair_writer.py"
    script.write_text(PAIR_WRITER, encoding="utf-8")
    go = tmp_path / "go"
    workers, each = 3, 40
    env = os.environ.copy()
    env["PYTHONPATH"] = str(_LIB) + os.pathsep + env.get("PYTHONPATH", "")
    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(db), str(go), str(wid), str(each)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for wid in range(workers)
    ]
    time.sleep(0.3)
    go.write_text("1", encoding="utf-8")
    failures = []
    for proc in procs:
        stdout, stderr = proc.communicate(timeout=120)
        if proc.returncode != 0:
            failures.append(f"exit {proc.returncode}\n{stderr.strip().splitlines()[-1:]}")
    assert failures == []
    lg = open_ledger(db)
    events = lg.events()
    assert len(events) == workers * each * 2
    assert {event.id for event in events} == set(range(1, workers * each * 2 + 1))
    for created, state in zip(events[::2], events[1::2]):
        assert (created.kind, state.kind) == ("item.created", "item.state")
        assert created.item_id == state.item_id
    lg.verify()
    lg.close()
